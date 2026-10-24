"""Interp runtime support (rt_cpu) - verbatim segment of the
original boot/interp.py (lines 592..689), split for maintainability."""
from .rt_num import (
    _TARGET_FEATURES,
)

def _set_target_feature(feat):
    """Set the active --target-feature (normalised, no leading '+')."""
    _TARGET_FEATURES.clear()
    if feat:
        _TARGET_FEATURES.add(feat)


def _cpu_supports(feat):
    """Probe the host CPU for a SIMD feature (interpreter side; the
    native side uses __builtin_cpu_supports). /proc/cpuinfo on Linux
    x86; NEON is baseline on aarch64; rvv checks __riscv_v macro on
    riscv64; False otherwise."""
    try:
        feat = feat.decode("utf-8") if isinstance(feat, bytes) else feat
    except Exception:
        return False
    import platform
    machine = platform.machine().lower()
    if feat == "neon":
        return machine in ("aarch64", "arm64", "armv8l")
    # Stage 26 (v0.49.0-alpha): RISC-V Vector extension. The probe is
    # based on the `Features` line in /proc/cpuinfo (Linux RISC-V
    # exposes the V extension as `v` in the Features list) plus the
    # platform machine check. The __riscv_v macro is defined by the C
    # compiler when -march includes the V extension; the interpreter
    # has no equivalent compile-time probe, so the runtime probe is
    # the only signal (best-effort: returns False if /proc/cpuinfo is
    # unreadable, e.g. on macOS/BSD where the V extension would be
    # queried differently).
    if feat == "rvv":
        if machine not in ("riscv64", "rv64", "riscv"):
            return False
        try:
            with open("/proc/cpuinfo", "rb") as f:
                for line in f:
                    if line.startswith(b"Features") or line.startswith(b"isa"):
                        # Linux RISC-V /proc/cpuinfo exposes the ISA
                        # extensions as `isa: rv64imafdv` (lowercase,
                        # one string). The V extension is present
                        # when 'v' appears in the isa string after
                        # the base extensions. We check for "_v" or
                        # "v" as a standalone token.
                        isa = line.decode("utf-8", "replace").lower()
                        # Strip the "isa:" / "features:" prefix.
                        isa = isa.split(":", 1)[-1].strip()
                        # The V extension is denoted by 'v' in the
                        # isa string (e.g. "rv64imafdcv"). Check that
                        # it appears as a suffix token (not inside
                        # another extension name).
                        # Stage 53 deep-scan-22 fix (HIGH): the previous
                        # `"v" in isa` matched the 'v' inside the BASE
                        # ISA prefix "rv64" (present in EVERY rv64 ISA
                        # string), so simd_cpu_supports("rvv") returned
                        # True on every RISC-V system — including those
                        # WITHOUT the V extension (e.g. rv64imac). The
                        # native runtime uses __riscv_v (a compile-time
                        # macro set by -march=rv64gcv), so this was a
                        # differential mismatch: the interpreter falsely
                        # reported V support. Skip the "rv64i" base
                        # prefix (5 chars) before testing for 'v' so
                        # only EXTENSION letters are considered.
                        ext_part = isa[5:] if isa.startswith("rv64") else isa
                        if "v" in ext_part:
                            return True
                        break
        except OSError:
            return False
        return False
    flags = ""
    try:
        with open("/proc/cpuinfo", "rb") as f:
            for line in f:
                if line.startswith(b"flags") or line.startswith(b"Features"):
                    flags = line.decode("utf-8", "replace").lower()
                    break
    except OSError:
        return False
    # Stage 53 deep-scan-22 fix (HIGH): /proc/cpuinfo uses UNDERSCORES in
    # feature names (sse4_2, sse4_1) while HLS / __builtin_cpu_supports
    # use DOTS (sse4.2, sse4.1). The previous code looked for " sse4.2 "
    # in the flags, which never matched (the flags have "sse4_2"), so
    # simd_cpu_supports("sse4.2") returned False on every x86 CPU — a
    # differential mismatch with the native runtime (which calls
    # __builtin_cpu_supports("sse4.2") and returns True). Map the dot
    # form to the underscore form before the lookup so the probe matches.
    # Deep-scan-24 fix (HIGH): TOKENISE the flags line instead of doing
    # padded substring searches. The old string normalisation left the
    # trailing "\n" (and tabs) in place while the search pattern demands
    # a space on BOTH sides of the token, so a feature that happened to
    # be the LAST flag on the line (e.g. "... avx2\n") never matched —
    # the interpreter then reported "no AVX2" while the native runtime
    # (via __builtin_cpu_supports) reported support, the exact
    # differential mismatch this probe exists to prevent. split()
    # handles spaces, tabs, commas and the newline uniformly.
    probe = feat.replace(".", "_")
    return probe in flags.replace(",", " ").split()




__all__ = [
    "_cpu_supports",
    "_set_target_feature",
]
