"""policy — the effect-to-syscall mapping and the profile (Stage 110).

The audit (Stage 103) tells you what a tree DECLARES; this module
decides what the kernel will be TOLD. One policy, three questions:

  WHAT RUNS        the execution surface — the checker's own
                   `computed_effects` fixpoint from `main` (source
                   mode), or the root package's audited surface
                   (package mode). Never a guess: the same checker
                   the compiler runs decides what the filter must
                   cover, one definition end to end.

  HOW PRECISE      the effect vocabulary is coarse (`Fs`), but the
                   builtin census is not: a tree whose reachable
                   code calls only the read side (read_file,
                   file_exists, fs_read_dir, fs_size, fs_is_dir)
                   arms `openat` under a WRITE-INTENT ARGUMENT MASK —
                   the kernel itself refuses to open for writing.
                   One write-side builtin (write_file, fs_set_perms)
                   or one opaque `extern` block declaring `uses Fs`
                   and the split is honestly surrendered: an extern's
                   C side is invisible, so its Fs is read-write.

  WHAT ENFORCES    a seccomp-bpf filter: arch guard (the numbers
                   mean nothing on the wrong ABI), a dispatch over
                   the allowlist, argument-mask checks where the
                   effect is finer than the syscall, and a default
                   action (errno EPERM by default — a denial the
                   language can OBSERVE as a value; kill for the
                   strict posture; trap; log).

The syscall NUMBER tables are explicit per arch — x86_64 and the
asm-generic numbering aarch64 and riscv64 share (the toolchain's
three triples). Every layer entry must resolve on all three arches
or carry an `absent` note naming WHY (plain `stat` does not exist
on asm-generic; `newfstatat` answers the same question there). The
selftest and the acceptance gate machine-check that invariant; the
gate additionally re-checks the x86_64 numbers against the local
kernel headers when they exist.

`execve` is baseline and the reason is structural, not laziness:
the launcher installs the filter and then must EXEC the artifact,
so the filter has to let that crossing through — and because a
filter installed under NO_NEW_PRIVS is INHERITED across execve,
the exec'd image runs under the same program. execve is an
entrance, never an exit. (Denying it is still possible — for
self-armed binaries built with the emitted shim, which install
the filter in a constructor AFTER the exec. The launcher refuses
that profile at run time with the reason; `emit` accepts it.)

`exit` / `exit_group` / `brk` are not deniable at all: a sandbox
a program cannot leave is not a sandbox, it is a hang.
"""
from __future__ import annotations

import os
import struct
import sys

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(TOOL_DIR))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
_SBX = os.path.dirname(TOOL_DIR)
if _SBX not in sys.path:
    sys.path.insert(0, _SBX)

from sandbox_parts import sbx_bpf as bpf                      # noqa: E402

# ---------------------------------------------------------------------------
# The one effect vocabulary (hls-audit's, imported — never copied).
# ---------------------------------------------------------------------------

import importlib.util                                        # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "hls_audit_mod_sbx", os.path.join(TOOL_DIR, "..", "hls-audit.py"))
hls_audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hls_audit)

ALL_EFFECTS = hls_audit.ALL_EFFECTS          # IO Fs Clock Args Exit Net
                                             # Rand Proc Conc — one set

# ---------------------------------------------------------------------------
# Syscall number tables (explicit triples; see the module docstring).
#
# The aarch64 and riscv64 columns share the asm-generic numbering —
# that is an ABI fact, recorded as data rather than assumed: the
# selftest pins aarch64 == riscv64 for EVERY row, so a future edit
# that breaks the equality breaks the test, not a release binary.
# ---------------------------------------------------------------------------

# name: (x86_64, aarch64, riscv64, absent-note or None)
#   a None entry means "does not exist on this ABI" and MUST carry
#   the note that names its asm-generic replacement.
_SYSCALLS = {
    # -- process lifecycle (the undeniables) ------------------------------
    "exit":            (60, 93, 93, None),
    "exit_group":      (231, 94, 94, None),
    "brk":             (12, 214, 214, None),
    "execve":          (59, 221, 221, None),
    # -- memory ------------------------------------------------------------
    "mmap":            (9, 222, 222, None),
    "munmap":          (11, 215, 215, None),
    "mprotect":        (10, 226, 226, None),
    "mremap":          (25, 216, 216, None),
    "madvise":         (28, 233, 233, None),
    # -- descriptor I/O (already-open fds) ----------------------------------
    "read":            (0, 63, 63, None),
    "write":           (1, 64, 64, None),
    "readv":           (19, 65, 65, None),
    "writev":          (20, 66, 66, None),
    "ioctl":           (16, 29, 29, None),
    "pread64":         (17, 67, 67, None),
    "close":           (3, 57, 57, None),
    # -- the open family ----------------------------------------------------
    "open":            (2, None, None,
                        "x86_64 only; asm-generic opens through openat"),
    "openat":          (257, 56, 56, None),
    "stat":            (4, None, None,
                        "x86_64 only; asm-generic answers via newfstatat"),
    "fstat":           (5, 80, 80, None),
    "lseek":           (8, 62, 62, None),
    "newfstatat":      (262, 79, 79, None),
    "statx":           (332, 291, 291, None),
    "access":          (21, None, None,
                        "x86_64 only; asm-generic answers via faccessat"),
    "faccessat":       (269, 48, 48, None),
    "getdents64":      (217, 61, 61, None),
    "readlink":        (89, None, None,
                        "x86_64 only; asm-generic answers via readlinkat"),
    "readlinkat":      (267, 78, 78, None),
    # -- the write-side family (Fs(rw) only) --------------------------------
    "unlink":          (87, None, None,
                        "x86_64 only; asm-generic answers via unlinkat"),
    "unlinkat":        (263, 35, 35, None),
    "rename":          (82, None, None,
                        "x86_64 only; asm-generic answers via renameat"),
    "renameat":        (264, 38, 38, None),
    "renameat2":       (316, 276, 276, None),
    "mkdir":           (83, None, None,
                        "x86_64 only; asm-generic answers via mkdirat"),
    "mkdirat":         (258, 34, 34, None),
    "rmdir":           (84, None, None,
                        "x86_64 only; asm-generic answers via unlinkat"),
    "chmod":           (90, None, None,
                        "x86_64 only; asm-generic answers via fchmodat"),
    "fchmod":          (91, 52, 52, None),
    "fchmodat":        (268, 53, 53, None),
    # -- identity / platform -------------------------------------------------
    "uname":           (63, 160, 160, None),
    "arch_prctl":      (158, None, None,
                        "x86_64 only (TLS base); asm-generic uses nothing"),
    "set_tid_address": (218, 96, 96, None),
    "set_robust_list": (273, 99, 99, None),
    "rseq":            (334, 293, 293, None),
    "prlimit64":       (302, 261, 261, None),
    "getrandom":       (318, 278, 278, None),
    "futex":           (202, 98, 98, None),
    "sched_yield":     (24, 124, 124, None),
    "gettid":          (186, 178, 178, None),
    "tgkill":          (234, 131, 131, None),
    "getpid":          (39, 172, 172, None),
    "rt_sigaction":    (13, 134, 134, None),
    "rt_sigprocmask":  (14, 135, 135, None),
    # -- clocks ---------------------------------------------------------------
    "clock_gettime":   (228, 113, 113, None),
    "clock_getres":    (229, 114, 114, None),
    "clock_nanosleep": (230, 115, 115, None),
    "nanosleep":       (35, 101, 101, None),
    "gettimeofday":    (96, 169, 169, None),
    # -- sockets ---------------------------------------------------------------
    "socket":          (41, 198, 198, None),
    "connect":         (42, 203, 203, None),
    "bind":            (49, 200, 200, None),
    "listen":          (50, 201, 201, None),
    "accept":          (43, 202, 202, None),
    "accept4":         (288, 242, 242, None),
    "shutdown":        (48, 210, 210, None),
    "sendto":          (44, 206, 206, None),
    "recvfrom":        (45, 207, 207, None),
    "sendmsg":         (46, 211, 211, None),
    "recvmsg":         (47, 212, 212, None),
    "getsockname":     (51, 204, 204, None),
    "getpeername":     (52, 205, 205, None),
    "setsockopt":      (54, 208, 208, None),
    "getsockopt":      (55, 209, 209, None),
    # -- process creation / control ---------------------------------------------
    "clone":           (56, 220, 220, None),
    "clone3":          (435, 435, 435, None),
    "fork":            (57, None, None,
                        "x86_64 only; asm-generic forks via clone"),
    "vfork":           (58, None, None,
                        "x86_64 only; asm-generic vforks via clone"),
    "wait4":           (61, 260, 260, None),
    "waitid":          (247, 95, 95, None),
    "pipe":            (22, None, None,
                        "x86_64 only; asm-generic answers via pipe2"),
    "pipe2":           (293, 59, 59, None),
    "dup":             (32, 23, 23, None),
    "dup2":            (33, None, None,
                        "x86_64 only; asm-generic answers via dup3"),
    "dup3":            (292, 24, 24, None),
    "kill":            (62, 129, 129, None),
    "getcwd":          (79, 17, 17, None),
    "chdir":           (80, 49, 49, None),
    "fchdir":          (81, 50, 50, None),
    "execveat":        (322, 281, 281, None),
    # -- the arm itself -------------------------------------------------------
    # prctl and seccomp are how a filter gets installed — by the
    # launcher (before the filter exists) or by the shim's
    # constructor (same). Neither needs ALLOWING in any profile:
    # the arm always precedes the thing it arms. The numbers are
    # recorded anyway — the table is the toolchain's syscall
    # encyclopedia for the three triples, and the selftest pins
    # them.
    "prctl":           (157, 167, 167, None),
    "seccomp":         (317, 277, 277, None),
}

_ARCHES = ("x86_64", "aarch64", "riscv64")
_ARCH_INDEX = {"x86_64": 0, "aarch64": 1, "riscv64": 2}
_AUDIT_ARCH = {
    "x86_64": bpf.AUDIT_ARCH_X86_64,
    "aarch64": bpf.AUDIT_ARCH_AARCH64,
    "riscv64": bpf.AUDIT_ARCH_RISCV64,
}

# The write-intent mask on the open-family flags: O_WRONLY | O_RDWR
# | O_CREAT | O_TRUNC | O_APPEND | O_TMPFILE(= __O_TMPFILE |
# O_DIRECTORY). The O_* constants are IDENTICAL on x86_64 and
# asm-generic (both follow the asm-generic flag numbering) — one
# mask, three arches. Bits:
#   0x000001 O_WRONLY   0x000002 O_RDWR     0x000040 O_CREAT
#   0x000200 O_TRUNC   0x000400 O_APPEND
#   0x410000 O_TMPFILE (__O_TMPFILE 0x400000 | O_DIRECTORY 0x10000)
# The flags ARGUMENT POSITION differs per syscall and that fact is
# pinned here, per name: open(path, flags, ...) puts them in arg1,
# openat(dfd, path, flags, ...) in arg2 — masking the wrong slot
# would test a POINTER against the mask and deny every open.
OPEN_WRITE_MASK = 0x410643
OPEN_FLAGS_ARG = {"open": 1, "openat": 2}


def ro_constraint(name):
    """The read-only justification for an open-family syscall:
    (arg_index, mask) — allowed when (flags & mask) == 0."""
    if name not in OPEN_FLAGS_ARG:
        raise PolicyError("%s is not an open-family syscall with a "
                          "flags-argument contract" % name)
    return (OPEN_FLAGS_ARG[name], OPEN_WRITE_MASK)

# ---------------------------------------------------------------------------
# Baselines and layers.
# ---------------------------------------------------------------------------

# What every artifact needs merely to BE a process: terminate, grow
# the heap (malloc's brk path and its mmap fallback), be exec'd by
# the launcher (the crossing the filter inherits — see the module
# docstring), and run its own PROLOGUE — the static flavor of it:
# TLS setup (arch_prctl on x86_64, set_tid_address), the robust-list
# and rseq registration a modern glibc performs even single-
# threaded, and prlimit64. `--baseline minimal` ships this alone;
# pair it with a STATICALLY linked artifact (the auto baseline
# reads the ELF and chooses) — a dynamically linked one needs the
# hosted set, because its prologue includes a loader.
BASELINE_MINIMAL = ("exit", "exit_group", "brk", "execve",
                    "mmap", "munmap", "mprotect", "arch_prctl",
                    "set_tid_address", "set_robust_list", "rseq",
                    "prlimit64")

# The hosted baseline adds what a DYNAMICALLY linked artifact's own
# prologue needs before main runs: the loader mapping libc (mmap,
# mprotect, close, fstat), glibc's thread bookkeeping (set_tid_address,
# set_robust_list, rseq, prlimit64), the canary (getrandom), and the
# libc startup signal/identity calls. openat is here under the SAME
# read-only argument mask the Fs(ro) layer uses — the loader and libc
# open READ-ONLY, so the baseline must not smuggle write access in.
BASELINE_HOSTED = BASELINE_MINIMAL + (
    "read", "write", "writev", "ioctl", "pread64", "close",
    # BOTH opens, read-only masked: the loader opens through
    # openat, and the libc of some eras through plain open —
    # whichever the prologue leans on, the mask is the same.
    "open", "openat",
    "stat", "fstat", "newfstatat", "statx", "access",
    "faccessat", "lseek", "getdents64", "readlink", "readlinkat",
    "mprotect", "mremap", "madvise", "uname", "arch_prctl",
    "set_tid_address", "set_robust_list", "rseq", "prlimit64",
    "getrandom", "futex", "sched_yield", "gettid", "tgkill",
    "getpid", "rt_sigaction", "rt_sigprocmask",
)

# The effect layers. Values are syscall names; OPENAT_RO is the
# read-only-constrained openat rule (the argument mask above).
#
#   IO    already-open descriptors: print/println/eprint/read_line
#         write to streams someone else opened. Opening is Fs.
#   Fs    the read side is the floor; the write side (plain-file
#         mutations) arrives only with fs=rw.
#   Net   sockets AND the resolver's reads: glibc's getaddrinfo
#         reads /etc/resolv.conf and /etc/hosts and may demand-load
#         NSS modules (read-only opens, mmap) — a Net-only program
#         with no Fs effect can still resolve names, so the layer
#         carries those reads itself instead of stealing Fs.
#   Proc  spawning and process state: clone/execve/wait/pipe/dup,
#         cwd, identity. execve itself is baseline (see above); the
#         layer adds the machinery AROUND it.
#   Conc  threads: clone (CLONE_VM|CLONE_THREAD), futex, stack
#         mapping. clone3 is included with intent: glibc tries
#         clone3 first and treats EPERM as FATAL (only ENOSYS falls
#         back to clone) — omitting it would turn thread creation
#         into a denial the program cannot recover from.
#   Clock / Rand  the obvious two.
#   Args / Exit   empty BY DESIGN: argv is process memory (no
#         syscall exists to grant), and exit_group is baseline
#         (undeniable — see the module docstring). The layers exist
#         so the report can say so instead of saying nothing.

OPENAT_RO = ("openat", "ro")   # flags live in arg2 — see OPEN_FLAGS_ARG

LAYERS = {
    "IO": ("read", "write", "writev", "ioctl"),
    "Fs": (
        # the read floor
        OPENAT_RO, "close", "stat", "fstat", "newfstatat", "statx",
        "access", "faccessat", "lseek", "pread64", "getdents64",
    ),
    "FsWrite": (
        # BOTH opens upgrade: openat AND open — which one glibc's
        # fopen leans on is a per-version detail (modern glibc goes
        # through openat; older through open), and an rw profile
        # that breaks a legitimate write because it upgraded only
        # one of them is a bug, not a policy.
        "openat", "open", "unlink", "unlinkat", "rename", "renameat",
        "renameat2", "mkdir", "mkdirat", "rmdir", "chmod", "fchmod",
        "fchmodat",
    ),
    "Net": (
        "socket", "connect", "bind", "listen", "accept", "accept4",
        "shutdown", "sendto", "recvfrom", "sendmsg", "recvmsg",
        "getsockname", "getpeername", "setsockopt", "getsockopt",
        # the resolver's reads (see the comment above)
        OPENAT_RO, "close", "read", "stat", "fstat", "newfstatat",
        "statx", "access", "faccessat", "mmap", "mprotect", "munmap",
        "madvise", "readlink", "readlinkat", "futex",
    ),
    "Proc": (
        "clone", "clone3", "fork", "vfork", "execve", "execveat",
        "wait4", "waitid", "pipe", "pipe2", "dup", "dup2", "dup3",
        "kill", "getcwd", "chdir", "fchdir", "getpid", "uname",
        "gettid", "tgkill", "rt_sigaction", "rt_sigprocmask",
    ),
    "Conc": (
        "clone", "clone3", "futex", "mmap", "munmap", "mprotect",
        "mremap", "madvise", "rseq", "set_robust_list", "gettid",
        "tgkill", "sched_yield", "rt_sigaction", "rt_sigprocmask",
    ),
    "Clock": ("clock_gettime", "clock_getres", "clock_nanosleep",
              "nanosleep", "gettimeofday"),
    "Rand": ("getrandom",),
    "Args": (),
    "Exit": (),
}

# The Fs read/write census, over the one true builtin→effect table.
# The selftest pins every member against BUILTIN_EFFECTS: a builtin
# that changes effect families breaks the build, not a sandbox.
FS_READ_BUILTINS = ("read_file", "read_file_tainted", "file_exists",
                    "fs_read_dir", "fs_size", "fs_is_dir")
FS_WRITE_BUILTINS = ("write_file", "fs_set_perms")

DEFAULT_ACTIONS = {
    "errno": "the denied syscall returns -1/EPERM — a value the "
             "program can observe and answer",
    "kill":  "SECCOMP_RET_KILL_PROCESS — the kernel ends the process "
             "at the denied call (SIGSYS)",
    "trap":  "SIGSYS delivered to the process — a handler (or the "
             "default corpse) answers the denial",
    "log":   "allowed to run but logged by the kernel — the audit "
             "posture, not the enforcement posture",
}

BASELINES = ("auto", "minimal", "hosted")

# The syscalls a profile may never deny: termination and the heap.
UNDENIABLE = ("exit", "exit_group", "brk")

SCHEMA = "hls-sandbox/v1"


class PolicyError(Exception):
    """The profile cannot be built: an unknown syscall name, a deny
    on an undeniable, an unknown default action or fs mode, an
    unknown effect. All fatal (exit 1) — a sandbox that guesses is
    not a sandbox."""


# ---------------------------------------------------------------------------
# Arch resolution.
# ---------------------------------------------------------------------------

def arch_of_host():
    """The arch we are actually RUNNING on, in the table's names —
    or None when the machine is none of the three triples (the run
    refuses to install numbers it cannot vouch for; emit --arch
    still can)."""
    m = os.uname().machine
    if m in ("x86_64", "amd64"):
        return "x86_64"
    if m in ("aarch64", "arm64"):
        return "aarch64"
    if m == "riscv64":
        return "riscv64"
    return None


def nr_of(name, arch):
    """(nr, absent_note) for a syscall on an arch. nr is None when
    the syscall does not exist there — with the row's note saying
    what answers the same question instead."""
    row = _SYSCALLS.get(name)
    if row is None:
        raise PolicyError("unknown syscall: %s (known: %s)"
                          % (name, ", ".join(sorted(_SYSCALLS))))
    nr = row[_ARCH_INDEX[arch]]
    note = row[3]
    if nr is None and note is None:
        raise PolicyError("syscall %s has no number on %s and no "
                          "recorded reason — the table is incomplete"
                          % (name, arch))
    return nr, note


# ---------------------------------------------------------------------------
# Rules and the profile.
# ---------------------------------------------------------------------------

class Rule(object):
    """One dispatch entry: the syscall, and under which conditions
    it is allowed. `constraints` is a set of (arg_index, mask)
    pairs — the call is allowed when (arg & mask) == 0 for at least
    ONE recorded pair (OR of justifications); `unconditional` is
    the explicitly-unconstrained justification and absorbs every
    constrained one (fs=rw's openat subsumes every read-only
    justification for it). The distinction is load-bearing: a fresh
    rule has NEITHER — 'not yet justified' is not 'justified
    unconditional', and conflating them is how a read-only open
    silently becomes an open."""

    __slots__ = ("name", "nr", "constraints", "origins", "_uncond")

    def __init__(self, name, nr):
        self.name = name
        self.nr = nr
        self.constraints = set()
        self.origins = []
        self._uncond = False

    def merge(self, constraints, origin):
        if not constraints:
            self._uncond = True
            self.constraints = set()
        elif not self._uncond:
            self.constraints |= set(constraints)
        if origin not in self.origins:
            self.origins.append(origin)

    @property
    def unconditional(self):
        return self._uncond


class Profile(object):
    """A derived, override-adjusted policy — everything needed to
    assemble the filter, render the report, or write the release
    statement. Immutable in spirit; `program()` and `rules()` are
    pure functions of the fields."""

    def __init__(self, effects, fs_mode, fs_origin, default_action,
                 baseline, extra_allow=(), deny=(), notes=None):
        self.effects = sorted(set(effects))
        self.fs_mode = fs_mode            # "ro" | "rw" | None
        self.fs_origin = fs_origin
        self.default_action = default_action
        self.baseline = baseline          # "minimal" | "hosted"
        self.extra_allow = tuple(extra_allow)
        self.deny = tuple(deny)
        self.notes = list(notes or [])

        for eff in self.effects:
            if eff not in ALL_EFFECTS:
                raise PolicyError(
                    "unknown effect: %s (the audit's vocabulary: %s)"
                    % (eff, ", ".join(ALL_EFFECTS)))
        if self.default_action not in DEFAULT_ACTIONS:
            raise PolicyError("unknown default action: %s (known: %s)"
                              % (self.default_action,
                                 ", ".join(sorted(DEFAULT_ACTIONS))))
        if self.baseline not in ("minimal", "hosted"):
            raise PolicyError("unknown baseline: %s (known: minimal, "
                              "hosted)" % self.baseline)
        if self.fs_mode not in (None, "ro", "rw"):
            raise PolicyError("unknown fs mode: %s (known: ro, rw)"
                              % self.fs_mode)
        for name in self.deny:
            if name in UNDENIABLE:
                raise PolicyError(
                    "deny on %s refused — a sandbox the program "
                    "cannot leave is not a sandbox, it is a hang"
                    % name)

    # -- composition --

    def rules(self, arch):
        """The merged allowlist for an arch: baseline + layers +
        extras - denies, one Rule per syscall number."""
        out = {}

        def add(name, constraints, origin, strict):
            nr, note = nr_of(name, arch)
            if nr is None:
                if strict:
                    raise PolicyError(
                        "layer syscall %s does not exist on %s — the "
                        "layer invariant is broken (%s)"
                        % (name, arch, note))
                if note and note not in self.notes:
                    self.notes.append(note)
                return
            if name in self.deny:
                return
            r = out.get(nr)
            if r is None:
                r = out[nr] = Rule(name, nr)
            r.merge(constraints, origin)

        base = (BASELINE_MINIMAL if self.baseline == "minimal"
                else BASELINE_HOSTED)
        for name in base:
            # The baseline's opens are read-only masked: the loader
            # and libc open READ-ONLY, and a baseline that allowed
            # write-opens would hollow out every fs=ro profile. The
            # mask applies to BOTH open(2) and openat(2) — on x86_64
            # glibc's fopen goes through plain `open`, the loader
            # through `openat` — each at its OWN flags position
            # (arg1 / arg2, see OPEN_FLAGS_ARG).
            cons = ((ro_constraint(name),)
                     if name in OPEN_FLAGS_ARG else ())
            add(name, cons, "baseline(%s)" % self.baseline, strict=False)

        for eff in self.effects:
            for entry in LAYERS[eff]:
                if isinstance(entry, tuple):        # OPENAT_RO
                    name, _mode = entry
                    cons = (ro_constraint(name),)
                else:
                    name, cons = entry, ()
                add(name, cons, "layer(%s)" % eff, strict=True)
            if eff == "Fs" and self.fs_mode == "rw":
                for name in LAYERS["FsWrite"]:
                    add(name, (), "layer(Fs,rw)", strict=True)

        for name in self.extra_allow:
            add(name, (), "override(allow)", strict=True)

        return dict(sorted(out.items()))

    # -- assembly --

    def default_ret(self):
        if self.default_action == "errno":
            return bpf.ret_errno(bpf.EPERM)
        if self.default_action == "kill":
            return bpf.SECCOMP_RET_KILL_PROCESS
        if self.default_action == "trap":
            return bpf.SECCOMP_RET_TRAP
        return bpf.SECCOMP_RET_LOG

    def program(self, arch):
        """The seccomp-bpf Program for an arch (see sbx_bpf's module
        docstring for the fixed, auditable shape). Classic BPF
        jumps FORWARD only, so each constrained block carries its own
        denial return after its allow return — the program stays a
        straight line, and every path is one of exactly two answers."""
        rules = self.rules(arch)
        dflt = self.default_ret()
        asm = bpf.Asm()
        asm.load(bpf.OFF_ARCH)
        asm.jeq(_AUDIT_ARCH[arch], jt_label="nr")
        asm.ret(dflt)
        asm.label("nr")
        asm.load(bpf.OFF_NR)
        for nr in sorted(rules):
            asm.jeq(nr, jt_label="b%d" % nr)
        asm.label("default")
        asm.ret(dflt)
        for nr in sorted(rules):
            r = rules[nr]
            asm.label("b%d" % nr)
            if r.unconditional:
                asm.ret(bpf.SECCOMP_RET_ALLOW)
            else:
                for arg_index, mask in sorted(r.constraints):
                    asm.load(bpf.off_arg_low(arg_index))
                    asm.jset(mask, jt_label="d%d" % nr)
                asm.ret(bpf.SECCOMP_RET_ALLOW)
                asm.label("d%d" % nr)
                asm.ret(dflt)
        return asm.finish()

    # -- reporting --

    def to_report(self, arch, mode, root, derivation=None, gate=None):
        rules = self.rules(arch)
        names = {}
        for nr in sorted(rules):
            cons = ""
            if not rules[nr].unconditional:
                cons = " [" + ", ".join(
                    "arg%d & 0x%x == 0" % (a, m)
                    for a, m in sorted(rules[nr].constraints)) + "]"
            names[rules[nr].name] = cons
        rep = {
            "schema": SCHEMA,
            "mode": mode,
            "root": root,
            "arch": arch,
            "effects": list(self.effects),
            "fs": {"mode": self.fs_mode, "origin": self.fs_origin},
            "default_action": self.default_action,
            "baseline": self.baseline,
            "layers": {eff: [_layer_name(e) for e in LAYERS[eff]]
                       for eff in self.effects},
            "allowlist": {
                "count": len(rules),
                "syscalls": sorted(names),
                "constrained": {k: v for k, v in names.items() if v},
            },
            "denied": list(self.deny),
            "extra_allow": list(self.extra_allow),
            "absent_notes": sorted(set(self.notes)),
            "program": {"instructions": len(self.program(arch))},
        }
        if derivation is not None:
            rep["derivation"] = derivation
        if gate is not None:
            rep["gate"] = gate
        return rep


def _layer_name(entry):
    if isinstance(entry, tuple):
        return "%s (%s)" % (entry[0], entry[1])
    return entry


# ---------------------------------------------------------------------------
# Source-mode derivation — the checker IS the oracle.
# ---------------------------------------------------------------------------

def derive_source(entry_path, repo_root=REPO_ROOT):
    """Walk an entry file's tree exactly as the audit does (the same
    import resolution, the same merged check) and derive:

      effects      computed_effects of `main` — the checker's own
                   fixpoint, i.e. precisely what executing the entry
                   can perform (a library with no main falls back to
                   the union over every fn — the tightest statement
                   a library admits, documented as such)
      builtins     the reachable builtin census (BFS over the call
                   graph from main) — this is what splits Fs(ro)
                   from Fs(rw)
      extern_fx    every extern block's declared effects — an extern
                   declaring Fs is OPAQUE and forces fs=rw

    Raises AuditError (the audit's own class — one definition) when
    the tree cannot be checked: a tree that cannot be checked cannot
    be sandboxed either."""
    entry_abs = os.path.realpath(os.path.abspath(entry_path))
    if not os.path.isfile(entry_abs):
        raise hls_audit.AuditError("no such file: %s" % entry_abs)
    edges, order = hls_audit._walk_import_graph(entry_abs)
    program, checker = hls_audit._check_merged(entry_abs)
    computed = getattr(checker, "computed_effects", {})

    # Reachable function set + builtin census from the entry.
    if "main" in computed or "main" in getattr(checker, "edges", {}):
        roots = ["main"]
        scope = "main"
    else:
        roots = [k for k in computed if not k.startswith("b:")]
        scope = "union (no main — library entry)"
    reach = set()
    builtins = set()
    stack = list(roots)
    while stack:
        k = stack.pop()
        if k in reach:
            continue
        reach.add(k)
        for callee in checker.edges.get(k, ()):
            if callee.startswith("b:"):
                builtins.add(callee[2:])
            else:
                stack.append(callee)
    effects = set()
    for k in reach:
        effects |= computed.get(k, set())

    # The extern census: opaque Fs anywhere in the tree surrenders
    # the read/write split.
    _, _, module_externs = hls_audit._fn_keys_per_module(order)
    extern_fx = set()
    for mp in order:
        for blk in module_externs.get(mp, []):
            extern_fx |= set(blk["effects"])

    fs_mode, fs_origin = _derive_fs(effects, builtins, extern_fx)
    return {
        "scope": scope,
        "effects": sorted(effects),
        "builtins": sorted(builtins),
        "extern_effects": sorted(extern_fx),
        "fs_mode": fs_mode,
        "fs_origin": fs_origin,
    }


def _derive_fs(effects, builtins, extern_fx):
    """The read/write split, claimed only where the toolchain can
    SEE the calls (see the module docstring)."""
    if "Fs" not in effects:
        return None, "no Fs in the execution surface"
    if "Fs" in extern_fx:
        return "rw", ("derived — an extern block declares Fs and the "
                      "C side is opaque; the split is surrendered")
    if builtins & set(FS_WRITE_BUILTINS):
        return "rw", ("derived — the reachable builtin census contains "
                      "the write side (%s)"
                      % ", ".join(sorted(builtins & set(FS_WRITE_BUILTINS))))
    if builtins & set(FS_READ_BUILTINS):
        return "ro", ("derived — the reachable builtin census is "
                      "read-only (%s)"
                      % ", ".join(sorted(builtins & set(FS_READ_BUILTINS))))
    return "ro", ("derived — Fs is in the surface through no reachable "
                  "fs builtin; the tightest honest floor is read-only")


# ---------------------------------------------------------------------------
# The manifest [sandbox] section.
# ---------------------------------------------------------------------------

def parse_sandbox_section(manifest):
    """Validate and extract the optional [sandbox] section of a
    hls-pkg.toml manifest. Every mistake is fatal with the fix in
    the message — a sandbox configured by guessing is not a sandbox.

      [sandbox]
      default = "errno"            # errno | kill | trap | log
      fs = "ro"                    # ro | rw (absent: derive)
      allow = ["clock_nanosleep"]  # extra syscalls, by name
      deny = ["ptrace"]            # carve-outs, by name
    """
    sec = (manifest or {}).get("sandbox")
    if sec is None:
        return {"default": None, "fs": None, "allow": [], "deny": []}
    if not isinstance(sec, dict):
        raise PolicyError("[sandbox] must be a table (found %s)"
                          % type(sec).__name__)
    out = {"default": None, "fs": None, "allow": [], "deny": []}

    d = sec.get("default")
    if d is not None:
        if not isinstance(d, str) or d not in DEFAULT_ACTIONS:
            raise PolicyError(
                "[sandbox] default must be one of %s (found %r)"
                % (", ".join(sorted(DEFAULT_ACTIONS)), d))
        out["default"] = d

    f = sec.get("fs")
    if f is not None:
        if not isinstance(f, str) or f not in ("ro", "rw"):
            raise PolicyError('[sandbox] fs must be "ro" or "rw" '
                              "(found %r)" % (f,))
        out["fs"] = f

    for key in ("allow", "deny"):
        raw = sec.get(key)
        if raw is None:
            continue
        if not isinstance(raw, list) or \
                any(not isinstance(x, str) for x in raw):
            raise PolicyError("[sandbox] %s must be a list of syscall "
                              "names" % key)
        for name in raw:
            if name not in _SYSCALLS:
                raise PolicyError(
                    "[sandbox] %s: unknown syscall %r (known: %s)"
                    % (key, name, ", ".join(sorted(_SYSCALLS))))
        out[key] = sorted(set(raw))

    for name in out["deny"]:
        if name in UNDENIABLE:
            raise PolicyError(
                "[sandbox] deny on %s refused — a sandbox the program "
                "cannot leave is not a sandbox, it is a hang" % name)
    return out


# ---------------------------------------------------------------------------
# Static-vs-hosted detection for --baseline auto.
# ---------------------------------------------------------------------------

def is_static_elf(path):
    """True when the ELF carries no PT_INTERP (a static artifact —
    no dynamic loader runs before main, so the hosted baseline's
    loader set is dead weight). Anything unparseable is treated as
    hosted: the SAFE default is the wider baseline, never the one
    that would EPERF the loader mid-flight."""
    try:
        with open(path, "rb") as fh:
            eh = fh.read(64)
            if len(eh) < 64 or eh[:4] != b"\x7fELF":
                return False
            (phoff,) = struct.unpack_from("<Q", eh, 32)
            (phentsize, phnum) = struct.unpack_from("<HH", eh, 54)
            fh.seek(phoff)
            ph = fh.read(phentsize * phnum)
            for i in range(phnum):
                (p_type,) = struct.unpack_from("<I", ph, i * phentsize)
                if p_type == 3:            # PT_INTERP
                    return False
            return True
    except (OSError, struct.error):
        return False


__all__ = [
    "ALL_EFFECTS", "SCHEMA", "PolicyError",
    "BASELINE_MINIMAL", "BASELINE_HOSTED", "LAYERS",
    "FS_READ_BUILTINS", "FS_WRITE_BUILTINS", "DEFAULT_ACTIONS",
    "UNDENIABLE", "OPEN_WRITE_MASK", "BASELINES",
    "arch_of_host", "nr_of", "Rule", "Profile",
    "derive_source", "parse_sandbox_section", "is_static_elf",
    "hls_audit",
]
