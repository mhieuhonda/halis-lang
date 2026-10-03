"""hltlog_parts — Stage 107 machinery behind the hls-tlog CLI.

Split for maintainability like hpkg_parts and hlsign_parts before it:
common (shapes + views), chain (verify, gossip, proofs), sources
(file/directory/HTTP), witness (signed views over Stage 106's keys).
"""
