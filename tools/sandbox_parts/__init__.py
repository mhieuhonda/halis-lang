"""sandbox_parts — the Stage 110 implementation package.

Split for maintainability like hpkg_parts / hlsign_parts /
hltlog_parts / hlreverify_parts before it; tools/hls-sandbox.py is
the facade that re-exports this API.

  sbx_bpf       the seccomp-bpf assembler / disassembler / C
                serializer — one definition of the bytes
  sbx_policy    the syscall tables (x86_64 + the asm-generic pair),
                the baselines, the effect layers, the Fs read/write
                split, the profile, the [sandbox] manifest section
  sbx_exec      the launcher: fork, NO_NEW_PRIVS, seccomp, exec
  sbx_cemit     the self-arming C shim emitter
  sbx_release   the release statement + the transparency record
"""
from sandbox_parts import sbx_bpf          # noqa: F401
from sandbox_parts import sbx_policy       # noqa: F401
from sandbox_parts import sbx_exec         # noqa: F401
from sandbox_parts import sbx_cemit        # noqa: F401
from sandbox_parts import sbx_release      # noqa: F401

__all__ = ["sbx_bpf", "sbx_policy", "sbx_exec", "sbx_cemit",
           "sbx_release"]
