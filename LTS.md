# Long-term support policy — Halis (HLS)

Generated from the live constants of `tools/hls-lts.py` —
this document is rendered, not maintained. Do not hand-edit;
regenerate it with `python3 tools/hls-lts.py policy --out
LTS.md`. `make lts-policy-verify` re-derives the committed copy
against the same render and refuses drift — the numbers below
are the numbers the gates enforce, or the tree says no.

## The promise

Every LTS line this project opens gets **3 years of bug-fix
support** and **5 years of security support**, counted from
the day its base release was cut. The last 2 years of a 5-year
line are security-only: after the third anniversary a plain
bug fix is refused on the line even when it is a perfectly good
fix, and after the fifth the line is dead and takes nothing at
all. A published promise a tool cannot read is prose; this one
is arithmetic (`hls-lts window`), it is enforced where a change
would enter the branch (`hls-lts backport`, `hls-lts
tag-patch`), and it audits history after the fact (`hls-lts
audit`).

## The line, and the day it is measured from

An LTS line is a major.minor — `1.0` is the line of `v1.0.0`,
`v1.0.1`, `v1.0.2` and every patch after. The window belongs to
the LINE, not to each patch: it is measured from the committer
day of the base release (`vMAJOR.MINOR.0`'s commit), and every
patch on the line lives inside the same window. A line born
late does not get a late window; a patch cut late does not
extend one. `hls-lts init --tag vX.Y.0` declares the line;
`hls-lts window` reads it, with `--at` to pin the day the law
is judged on (default: today) — the same law, on any day.

## The three phases

| phase | days | what may land on the line |
|-------|------|----------------------------|
| `active` | base → base +3 years | any BUGFIX backport and chores |
| `security` | base +3 → +5 years | SECURITY-marked BUGFIX backports and chores |
| `eol` | past base +5 years | nothing — a dead line takes nothing |

The horizon day itself belongs to the phase it closes: support
runs THROUGH the anniversary, so the third birthday is the last
full-support day and the day after it is security-only; the
fifth birthday is the last supported day and the day after it
is end of life.

## The calendar law

`base + N years` is the same month and day — an anniversary,
not a fixed count of days. February 29 clamps to the 28th when
the target year is not a leap year (a line born 2024-02-29 is
bug-fixed through 2027-02-28). The policy speaks in days: every
date is a calendar day, read from git's committer timestamp —
the same clock the landing laws already read.

## What makes a fix a security fix

A `Security: <reason>` line in the commit message (a CVE id, a
one-word severity, a sentence — anything non-empty). The name
matches case-insensitively, the first such line decides, and an
empty value is not a marking. Fail-closed, like everything in
this policy: **no trailer, no security claim** — an unmarked
fix is a plain fix, and a plain fix does not land on a
security-only line. The trailer is a line of the message,
wherever it stands; `hls-lts classify` reports the marking
beside the class (a marked FEATURE is still a FEATURE, and
still refused — the marking is a fact about a fix, not a key
past the class law).

## What the tool refuses

- `backport` in the `security` phase: a plain BUGFIX is
  refused with the phase and both horizons named; in `eol`,
  everything is refused — the dead line takes no chores either.
- `tag-patch` past end of life: no release is cut from a dead
  line. In the `security` phase a release must carry at least
  one security backport since the previous tag — a chore-only
  release is how a security line pretends to be alive.
- `audit`: history is judged the day it landed. A backport
  that arrived in the security phase without the mark, any
  commit past end of life, and any tag cut from a commit dated
  past end of life are violations, named sha by sha.

## Relationship to the branch policy

This is the second dimension of the Stage 132 policy, not a new
one. `halis-lts-1` keeps its name and its declaration; the
window law derives from the base tag live on every run and
adds no state. Stage 132 said WHAT may land on an LTS branch
(backport-only, patch-only, fail-closed); this policy says
WHEN — the same line, with a birthday and a death day.

## When it begins

The policy begins at v1.0 — a pre-1.0 LTS is a contradiction.
Until the first LTS line is declared this document is the
promise; the day `hls-lts init --tag v1.0.0` runs, it becomes
the clock. The gates prove the law in the meantime on scratch
lines with fabricated birthdays (`make lts-policy-acceptance`),
so the enforcement is tested before it is ever needed.
