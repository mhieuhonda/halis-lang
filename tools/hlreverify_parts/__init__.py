"""hls-reverify parts — Stage 108 (v0.127.0-alpha).

The memory-safety re-verification under `-O fast` (proof replay):
    hlrev_common   — shapes, constants, the report schema
    hlrev_collect  — the baseline (AST) and IR site harvests
    hlrev_ircheck  — the IR-level abstract interpreter (the replay)
    hlrev_replay   — the replay law + the report
    hlrev_selftest — deterministic unit vectors
"""
