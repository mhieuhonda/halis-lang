"""hlaudit_parts — the Stage 112 audit log, split for maintainability.

The toolchain performs privileged operations every day it runs: it
locks dependency trees, mints signing keys, signs releases, arms
kernel filters around untrusted code, witnesses transparency views.
Until now those operations left only their side effects behind — and
a REFUSED operation, the most interesting audit fact of all, left
nothing at all. This package gives every privileged operation a
hash-chained, signable record of its own:

  hal_common      the shapes and the constants (this module)
  hal_log         the chain writer, reader and verifier
  hal_ops         the privilege inventory and the one `record()` call
                  every tool makes at its privileged operations
  hal_checkpoint  signed checkpoints — an Ed25519 promise about the
                  log's (length, head), the Stage 106 keys
"""
