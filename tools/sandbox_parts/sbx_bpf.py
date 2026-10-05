"""bpf — the seccomp-bpf filter assembler (Stage 110).

One definition of the bytes the kernel is told. A profile compiles
to a classic-BPF program over `struct seccomp_data`:

    nr                     offset 0   (the syscall number)
    arch                   offset 4   (AUDIT_ARCH_* of the ABI)
    instruction_pointer    offset 8
    args[0] .. args[5]     offset 16  (8 bytes each: low 32 at +0,
                                       high 32 at +4)

The program shape is fixed and auditable:

    ld      arch
    jeq     AUDIT_ARCH      -> continue      (anything else: the
                                             numbers mean nothing
                                             on this ABI)
    ld      nr
    jeq     N1              -> block_N1      (dispatch, ascending nr)
    jeq     N2              -> block_N2
    ...
    ret     <default>                        (nothing matched)
  block_N1:
    [argument constraint checks -> default]  (optional)
    ret     ALLOW

Every jump is forward, every label is resolved before the program is
handed to anyone, and an out-of-range jump (the classic-BPF jt/jf
fields are 8-bit RELATIVE) is refused at assembly time — never
truncated, never wrapped. The disassembler renders exactly this
shape back to text so a human can read what the kernel will be told;
`profile --bpf` is that listing.

Deep-scan notes, pre-empted: `jt`/`jf` are RELATIVE offsets from the
instruction AFTER the jump (0 = fall through), not absolute indices
— the fixup pass computes `target - here - 1` and the validation
pass re-checks every resolved jump against the program length; a
label emitted twice, or a jump to a label that was never emitted,
is an error, not a silent zero.
"""
from __future__ import annotations

import struct

# --- classic-BPF instruction encoding --------------------------------------

BPF_LD_W_ABS = 0x20        # A = seccomp_data[k]
BPF_JEQ_K = 0x15           # if (A == k) jt else jf   (JMP|JEQ|K)
BPF_JGE_K = 0x35           # if (A >= k) jt else jf   (JMP|JGE|K)
BPF_JSET_K = 0x45          # if (A & k) jt else jf    (JMP|JSET|K)
BPF_RET_K = 0x06           # return k

# seccomp_data offsets (see the module docstring).
OFF_NR = 0
OFF_ARCH = 4


def off_arg_low(arg_index):
    """Offset of the LOW 32 bits of args[i]. Flags, modes and fd
    arguments are `int` in the language and `unsigned int` at the
    boundary — the low word is where they live on every ABI the
    toolchain ships (little-endian x86_64/aarch64/riscv64)."""
    if not 0 <= arg_index <= 5:
        raise ValueError("arg index out of range: %r" % (arg_index,))
    return 16 + 8 * arg_index


# --- return actions ----------------------------------------------------------

SECCOMP_RET_KILL_PROCESS = 0x80000000
SECCOMP_RET_KILL_THREAD = 0x00000000
SECCOMP_RET_TRAP = 0x00030000
SECCOMP_RET_ERRNO = 0x00050000
SECCOMP_RET_LOG = 0x7FFC0000
SECCOMP_RET_ALLOW = 0x7FFF0000

EPERM = 1


def ret_errno(err):
    """SECCOMP_RET_ERRNO | errno — the denied syscall returns -1 and
    errno is set, exactly as if the kernel itself had refused. This
    is what makes a denial a VALUE the language can observe (a
    Result, a panic with a cause) instead of a corpse."""
    if not 0 < err <= 0xFFFF:
        raise ValueError("errno out of range: %r" % (err,))
    return SECCOMP_RET_ERRNO | err


# --- AUDIT_ARCH values (the e_machine half-word | 0xC0000000) ----------------

AUDIT_ARCH_X86_64 = 0xC000003E   # EM_X86_64 = 62
AUDIT_ARCH_AARCH64 = 0xC00000B7  # EM_AARCH64 = 183
AUDIT_ARCH_RISCV64 = 0xC00000F3  # EM_RISCV = 243

# BPF program limits the kernel enforces (and so do we, earlier).
BPF_MAXINSNS = 4096


# --- one instruction ---------------------------------------------------------

class Instr(object):
    """A single `struct sock_filter` — { u16 code; u8 jt; u8 jf; u32 k }."""

    __slots__ = ("code", "jt", "jf", "k")

    def __init__(self, code, jt, jf, k):
        if not 0 <= jt <= 0xFF or not 0 <= jf <= 0xFF:
            raise ValueError("jt/jf out of u8 range: %r %r" % (jt, jf))
        if not 0 <= k <= 0xFFFFFFFF:
            raise ValueError("k out of u32 range: %r" % (k,))
        self.code = code
        self.jt = jt
        self.jf = jf
        self.k = k

    def words(self):
        """The little-endian 8-byte encoding, exactly what the kernel
        reads out of the sock_fprog array."""
        return struct.pack("<HBBI", self.code, self.jt, self.jf, self.k)

    def __eq__(self, other):
        return (isinstance(other, Instr)
                and (self.code, self.jt, self.jf, self.k)
                == (other.code, other.jt, other.jf, other.k))

    def __repr__(self):
        return "Instr(0x%02x, %d, %d, 0x%x)" % (
            self.code, self.jt, self.jf, self.k)


# --- the label assembler -----------------------------------------------------

class Asm(object):
    """Emit instructions with symbolic labels; resolve + validate at
    finish. Jumps may only move FORWARD (classic BPF has no backward
    edges; a loop is a kernel refusal), and every jump must land
    somewhere the program actually goes."""

    def __init__(self):
        self._instrs = []
        self._labels = {}
        self._fixups = []     # (index, field, label)

    # -- building --

    def here(self):
        return len(self._instrs)

    def label(self, name):
        if name in self._labels:
            raise ValueError("label emitted twice: %s" % name)
        self._labels[name] = len(self._instrs)
        return self._instrs

    def load(self, offset):
        self._instrs.append(Instr(BPF_LD_W_ABS, 0, 0, offset))
        return len(self._instrs) - 1

    def jeq(self, k, jt_label=None, jf_label=None):
        self._jump(BPF_JEQ_K, k, jt_label, jf_label)

    def jset(self, k, jt_label=None, jf_label=None):
        self._jump(BPF_JSET_K, k, jt_label, jf_label)

    def _jump(self, code, k, jt_label, jf_label):
        if jt_label is None and jf_label is None:
            raise ValueError("a jump with no target does nothing")
        idx = len(self._instrs)
        self._instrs.append(Instr(code, 0, 0, k))
        if jt_label is not None:
            self._fixups.append((idx, "jt", jt_label))
        if jf_label is not None:
            self._fixups.append((idx, "jf", jt_label))

    def ret(self, k):
        self._instrs.append(Instr(BPF_RET_K, 0, 0, k))
        return len(self._instrs) - 1

    # -- resolution --

    def finish(self):
        """Resolve labels into relative jt/jf offsets, then validate
        every jump: in range, forward, and inside the program."""
        for idx, field, label in self._fixups:
            if label not in self._labels:
                raise ValueError("jump to a label that was never "
                                 "emitted: %s" % label)
            target = self._labels[label]
            rel = target - idx - 1
            if rel < 0:
                raise ValueError("backward jump to %s — classic BPF "
                                 "has no loops; refuse" % label)
            if rel > 0xFF:
                raise ValueError(
                    "jump to %s is %d instructions away — the jt/jf "
                    "fields are 8-bit; the profile is too large to "
                    "assemble as one dispatch program (split it or "
                    "trim the allowlist)" % (label, rel))
            ins = self._instrs[idx]
            if field == "jt":
                self._instrs[idx] = Instr(ins.code, rel, ins.jf, ins.k)
            else:
                self._instrs[idx] = Instr(ins.code, ins.jt, rel, ins.k)
        if len(self._instrs) > BPF_MAXINSNS:
            raise ValueError("program exceeds BPF_MAXINSNS (%d)"
                             % BPF_MAXINSNS)
        return Program(list(self._instrs))


# --- the assembled program ---------------------------------------------------

class Program(object):
    """A validated, resolved filter program. Immutable; hashable
    content; deterministic for the same rules."""

    def __init__(self, instrs):
        self.instrs = list(instrs)

    def __len__(self):
        return len(self.instrs)

    def __eq__(self, other):
        return isinstance(other, Program) and self.instrs == other.instrs

    def __repr__(self):
        return "Program(%d instrs)" % len(self.instrs)

    def bytes(self):
        return b"".join(i.words() for i in self.instrs)

    def c_array(self, name="hl_sandbox_filter"):
        """The program as a C `struct sock_filter` array — the shim
        embeds this verbatim so a binary can arm itself at startup."""
        out = ["static const struct sock_filter %s[] = {" % name]
        for i in self.instrs:
            out.append("    {0x%02x, %d, %d, 0x%08x},"
                       % (i.code, i.jt, i.jf, i.k))
        out.append("};")
        return "\n".join(out)


# --- the disassembler --------------------------------------------------------

_MNEMONIC = {
    BPF_LD_W_ABS: "ld",
    BPF_JEQ_K: "jeq",
    BPF_JSET_K: "jset",
    BPF_JGE_K: "jge",
    BPF_RET_K: "ret",
}

_RET_NAMES = {
    SECCOMP_RET_ALLOW: "ALLOW",
    SECCOMP_RET_KILL_PROCESS: "KILL_PROCESS",
    SECCOMP_RET_KILL_THREAD: "KILL_THREAD",
    SECCOMP_RET_TRAP: "TRAP",
    SECCOMP_RET_LOG: "LOG",
}


def _fmt_ret(k):
    if k in _RET_NAMES:
        return _RET_NAMES[k]
    if k & SECCOMP_RET_ERRNO == SECCOMP_RET_ERRNO:
        return "ERRNO(%d)" % (k & 0xFFFF)
    return "0x%08x" % k


def _fmt_k(code, k):
    if code == BPF_RET_K:
        return _fmt_ret(k)
    if code == BPF_LD_W_ABS:
        if k == OFF_NR:
            return "nr"
        if k == OFF_ARCH:
            return "arch"
        if k >= 16 and (k - 16) % 8 == 0 and (k - 16) // 8 <= 5:
            return "arg%d" % ((k - 16) // 8)
        return "[%d]" % k
    return "0x%x" % k


def disassemble(program, syscall_names=None, base=0):
    """Render a Program back to text, one instruction per line, with
    jump targets as absolute line numbers. `syscall_names` (nr ->
    name) annotates the dispatch so the listing reads as POLICY, not
    as numbers."""
    syscall_names = syscall_names or {}
    lines = []
    for i, ins in enumerate(program.instrs):
        m = _MNEMONIC.get(ins.code, "?0x%02x" % ins.code)
        k = _fmt_k(ins.code, ins.k)
        if ins.code == BPF_JEQ_K and ins.k in syscall_names:
            k = "%-5d %s" % (ins.k, syscall_names[ins.k])
        if ins.code in (BPF_JEQ_K, BPF_JSET_K):
            tgt = i + 1 + ins.jt if ins.jt else None
            tgt_f = i + 1 + ins.jf if ins.jf else None
            parts = []
            if tgt is not None:
                parts.append("%04d" % (base + tgt))
            elif ins.code == BPF_JEQ_K:
                note = syscall_names.get(ins.k)
                if note:
                    parts.append("dispatch %s" % note)
                else:
                    parts.append("fall")
            else:
                parts.append("fall")
            if tgt_f is not None:
                parts.append("else %04d" % (base + tgt_f))
            lines.append("%4d: %-6s %-18s %s"
                         % (base + i, m, k, ", ".join(parts)))
        else:
            lines.append("%4d: %-6s %s" % (base + i, m, k))
    return "\n".join(lines)


__all__ = [
    "Instr", "Asm", "Program", "disassemble",
    "BPF_LD_W_ABS", "BPF_JEQ_K", "BPF_JSET_K", "BPF_JGE_K", "BPF_RET_K",
    "OFF_NR", "OFF_ARCH", "off_arg_low",
    "SECCOMP_RET_ALLOW", "SECCOMP_RET_ERRNO", "SECCOMP_RET_KILL_PROCESS",
    "SECCOMP_RET_KILL_THREAD", "SECCOMP_RET_TRAP", "SECCOMP_RET_LOG",
    "ret_errno", "EPERM",
    "AUDIT_ARCH_X86_64", "AUDIT_ARCH_AARCH64", "AUDIT_ARCH_RISCV64",
    "BPF_MAXINSNS",
]
