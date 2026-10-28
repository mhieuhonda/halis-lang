# Release candidate — Halis (HLS)

Generated from the live state of the tree by `tools/hls-rc.hls` — this document is rendered, not maintained. Do not hand-edit; regenerate it with `bin/hls-rc report --out RC.md`. `make rc-report-check` re-derives the committed copy against the same render and refuses drift.

## The candidate

- the tag: **v1.0.0-rc.2** — the release candidate of v1.0.0, cut at roadmap Stage 147 (v1.0 Release Candidate 2, bug-fix only)
- the anchor: commit `bb2ae69b7bf6bf55e7177c88ed5ef0d60338973f`, cut on 2026-10-28 (the committer day), recorded in `.hls-rc.json`
- the freeze: the API surface travels frozen at digest `84d1068f80d62f3f...` (Stage 141, frozen at 9dbed2e1138d since release 0.159.0-alpha); from this anchor to v1.0.0 the surface cannot move except through the amendment chain (`hls-freeze amend --reason ... --by ...`)
- the successor: v1.0.0-rc.3 — the next step of the road below, cut the same way this one was: the roadmap row ticked, the markers agreeing, the gate green

## The readiness the tag stands on

| law | verdict |
|-----|---------|
| the repo law | held — the root is a git repository |
| the roadmap law | held — stages 1..147 closed, the open set is the tail from Stage 148 |
| the freeze law | held — the Stage 141 freeze, anchored at 9dbed2e1138d |
| the marker law | held — changelog, snapshot manifest and editor manifest all speak 1.0.0-rc.2 |
| the snapshot law | held — snapshot/hlc.c is present — the bootstrap seed travels with the candidate |
| the lts law | held — LTS.md published, no line declared — the policy is the promise until v1.0.0 makes it the clock |

## The freeze law — what lands between the candidate and v1.0.0

| class | may land | the rule |
|-------|----------|----------|
| BUGFIX | yes | fix / bugfix / revert / hotfix / regression |
| CHORE | yes | chore / release / docs / test / ci / meta |
| FEATURE | no | feat / feature / add / stage / spec / stdlib / perf / opt / refactor |
| UNKNOWN | no | no recognized prefix — fail-closed |

A BUGFIX that adds a `std/` or `core/` file, introduces a new public function, or edits SPEC.md is re-classed into the FEATURE it really is (the diff gate). `bin/hls-rc gate` walks the range since the anchor and names every violation sha by sha; `make rc-gate` is the release gate.

## The road to v1.0.0

- Stage 147 — v1.0 Release Candidate 2 (bug-fix only) [✅]
- Stage 148 — v1.0 Release Candidate 3 (final dry-run) [⬜]
- Stage 149 — v1.0 Release Candidate 4 (sign-off) [⬜]
- Stage 150 — **HLS v1.0 — LTS release** [⬜]

The LTS policy ([LTS.md](LTS.md)) begins at v1.0.0: the day `hls-lts init --tag v1.0.0` runs, the support windows become the clock. This candidate opens no LTS line — a pre-1.0 LTS is a contradiction. Until then the freeze law above is the only thing standing between the tree and the release, and `make rc-readiness` / `make rc-gate` / `make rc-report-check` keep every fact on this page re-derived.
