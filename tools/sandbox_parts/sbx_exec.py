"""exec — the launcher: arm the kernel, then cross (Stage 110).

`run` forks, arms the CHILD, and execs the artifact:

    prctl(PR_SET_NO_NEW_PRIVS, 1)     -- the contract that makes the
                                        filter survivable: no setuid
                                        climb, no privilege transition
                                        across the coming exec
    seccomp(SECCOMP_SET_MODE_FILTER)  -- install the program (the
                                        prctl(PR_SET_SECCOMP) fallback
                                        for kernels without the
                                        seccomp(2) syscall)
    execvp(cmd)                       -- the crossing; the filter is
                                        INHERITED, so what runs next
                                        runs confined

Everything after the fork happens in the child; the parent only
waits and reports. The exit contract mirrors the shell's, extended
by one code:

    0..255   the artifact's own exit status (a panic is 101)
    101      the artifact panicked (the language's own convention;
             a sandboxed denial OBSERVED as a value usually lands
             here, via errno-driven error paths)
    126      the artifact could not be executed (permission)
    127      the artifact was not found
    125      the sandbox itself could not be armed (the message on
             stderr says why: unsupported kernel, prctl refused, the
             filter rejected) — chosen to sit beside 126/127 so the
             launcher's failures read like the shell's
    128+N    killed by signal N; SIGSYS (31) is the kill-mode denial:
             the kernel ended the process AT the denied syscall

Why fork at all: the filter cannot be uninstalled, and the launcher
is a Python process with work left to do (report the exit). Arming
in a child keeps the parent honest — the only process confined is
the one that volunteered.

`--strict` is default=kill spelled the way people say it. The
manifest section and the CLI agree on the four actions (errno /
kill / trap / log) — see sbx_policy.DEFAULT_ACTIONS.
"""
from __future__ import annotations

import ctypes
import errno
import os
import signal
import sys

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(TOOL_DIR))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
_SBX = os.path.dirname(TOOL_DIR)
if _SBX not in sys.path:
    sys.path.insert(0, _SBX)

from sandbox_parts import sbx_bpf as bpf                      # noqa: E402
from sandbox_parts.sbx_policy import Profile, PolicyError      # noqa: E402

# --- the kernel interface, through ctypes -----------------------------------

PR_SET_NO_NEW_PRIVS = 38
PR_GET_NO_NEW_PRIVS = 39
PR_SET_SECCOMP = 22
SECCOMP_MODE_FILTER = 2
SECCOMP_SET_MODE_FILTER = 1

# seccomp(2) syscall numbers — ABI-specific (22 on 32-bit x86, the
# asm-generic number on aarch64/riscv64). The prctl fallback covers
# kernels without the syscall; both paths install the same program.
_SECCOMP_NR = {"x86_64": 317, "aarch64": 277, "riscv64": 277}


class _SockFilter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_uint16),
                ("jt", ctypes.c_uint8),
                ("jf", ctypes.c_uint8),
                ("k", ctypes.c_uint32)]


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort),
                ("filter", ctypes.POINTER(_SockFilter))]


def _libc():
    return ctypes.CDLL(None, use_errno=True)


def seccomp_supported(arch):
    """Cheap probe: can THIS kernel take a filter from THIS process?
    Installs nothing — checks the two prerequisites (Linux, and a
    prctl(PR_GET_NO_NEW_PRIVS) that answers at all) without side
    effects."""
    if arch is None or os.name != "posix" or not sys.platform.startswith("linux"):
        return False, "seccomp is Linux-only (this host: %s/%s)" % (
            os.name, sys.platform)
    try:
        libc = _libc()
        libc.prctl.restype = ctypes.c_int
        rc = libc.prctl(PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0)
        if rc < 0:
            return False, "prctl(PR_GET_NO_NEW_PRIVS) refused: errno %d" \
                % ctypes.get_errno()
    except Exception as ex:                       # pragma: no cover
        return False, "cannot reach libc prctl: %s" % ex
    return True, "ok"


def _install(program, arch):
    """Arm THIS process. Returns None on success or a string naming
    the refusal. Called only inside the forked child."""
    libc = _libc()
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        return "prctl(PR_SET_NO_NEW_PRIVS) failed: errno %d (%s)" % (
            ctypes.get_errno(), os.strerror(ctypes.get_errno()))
    arr = (_SockFilter * len(program.instrs))(
        *[_SockFilter(i.code, i.jt, i.jf, i.k) for i in program.instrs])
    fprog = _SockFprog(len(program.instrs), arr)
    nr = _SECCOMP_NR[arch]
    rc = libc.syscall(nr, SECCOMP_SET_MODE_FILTER, 0,
                      ctypes.byref(fprog))
    if rc != 0:
        err = ctypes.get_errno()
        # The historical fallback: kernels without the seccomp(2)
        # syscall expose the same mode through prctl.
        ctypes.set_errno(0)
        rc2 = libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER,
                         ctypes.byref(fprog), 0, 0)
        if rc2 == 0:
            return None
        return "seccomp(SECCOMP_SET_MODE_FILTER) failed: errno %d (%s)" \
            % (err, os.strerror(err))
    return None


# --- the launcher ------------------------------------------------------------

ARM_EXIT = 125          # the launcher's own failure code (see docstring)


def run(profile, arch, cmd, passthrough_env=None):
    """Run `cmd` under the profile. Returns (exit_code, killed_by,
    arm_error) — the parent's honest report of what happened."""
    if not cmd:
        raise PolicyError("nothing to run")
    if "execve" in profile.deny:
        raise PolicyError(
            "this profile denies execve — the launcher installs the "
            "filter and then must exec the artifact, so it cannot "
            "cross. Emit the shim instead (hls-sandbox emit) and let "
            "the binary arm itself in its constructor")
    program = profile.program(arch)

    pid = os.fork()
    if pid == 0:
        # -- the child: arm, then cross. Nothing here may raise into
        # a traceback — every failure is an _exit with a reason on
        # stderr, so the artifact never runs unarmed by accident.
        try:
            why = _install(program, arch)
        except BaseException as ex:              # pragma: no cover
            sys.stderr.write("hls-sandbox: arm failed: %s\n" % ex)
            os._exit(ARM_EXIT)
        if why is not None:
            sys.stderr.write("hls-sandbox: %s\n" % why)
            os._exit(ARM_EXIT)
        try:
            os.execvp(cmd[0], list(cmd))
        except FileNotFoundError:
            sys.stderr.write("hls-sandbox: command not found: %s\n"
                             % cmd[0])
            os._exit(127)
        except PermissionError:
            sys.stderr.write("hls-sandbox: cannot execute: %s\n"
                             % cmd[0])
            os._exit(126)
        except OSError as ex:
            sys.stderr.write("hls-sandbox: exec failed: %s\n" % ex)
            os._exit(126)
        os._exit(126)                            # pragma: no cover

    # -- the parent: wait, and say exactly what happened.
    _, status = os.waitpid(pid, 0)
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status), None, None
    if os.WIFSIGNALED(status):
        sig = os.WTERMSIG(status)
        return 128 + sig, sig, None
    return ARM_EXIT, None, "uninterpretable wait status %r" % status  # pragma: no cover


def describe_exit(code, sig):
    """The human line for `run`'s answer — one sentence, no spin."""
    if sig == signal.SIGSYS:
        return "killed by SIGSYS — a syscall the filter denies " \
               "(kill mode ends the process at the call)"
    if sig is not None:
        return "killed by signal %d (%s)" % (sig, signal.Signals(sig).name)
    if code == ARM_EXIT:
        return "the sandbox could not be armed (exit 125)"
    if code == 101:
        return "the artifact panicked (exit 101 — the language's " \
               "convention; under a sandbox this is usually a " \
               "denial observed as a value)"
    return "exit %d" % code


__all__ = ["run", "seccomp_supported", "describe_exit", "ARM_EXIT",
           "PR_SET_NO_NEW_PRIVS", "PR_SET_SECCOMP",
           "SECCOMP_MODE_FILTER", "SECCOMP_SET_MODE_FILTER"]
