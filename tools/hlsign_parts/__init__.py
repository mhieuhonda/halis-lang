"""hlsign_parts — internals of tools/hls-sign.py (Stage 106).

Split for maintainability exactly like hpkg_parts / hlserve_parts /
wopt_parts before it: the CLI stays readable, the crypto and the
format rules live one directory over, and the acceptance gate can
import the primitives directly to test them against the RFC vectors.
"""
