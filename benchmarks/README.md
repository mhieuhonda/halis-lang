# benchmarks/

Everything that measures Halis, in one place: the criterion-style
microbenchmark suites, the workload programs, the compile-time /
binary-size / RSS watch suites, and the record of one full measurement
campaign — date, versions, commands and every number — so a future run
can be compared against it, and a regression can be named.

---

## What lives here

| Path | Kind | What it measures | How to run it |
| --- | --- | --- | --- |
| `regression_watch.hls` | hls-bench suite | Five hot shapes: the arithmetic loop, string growth, list churn, the FNV-1a byte loop, enum match dispatch. The suite the perf ledger tracks by default. | `bin/hls-bench --native benchmarks/regression_watch.hls` |
| `e2e_watch.hls` | hls-bench suite | Six algorithm benches: the sieve to 10k, JSON parse of a 221-byte document, base64 both ways, CRC-32, an LCG-fed comparison sort, CSV split/join. | `bin/hls-bench --native benchmarks/e2e_watch.hls` |
| `sieve.hls` | program | Sieve of Eratosthenes to 100,000, self-times the count. | `bin/hlc run benchmarks/sieve.hls` |
| `json_bench.hls` | program | 100 parses of a fixed 221-byte JSON payload. | `bin/hlc run benchmarks/json_bench.hls` |
| `crypto_bench.hls` | program | FNV-1a 32/64 and CRC-32 over a 77-byte string, 100 rounds each. | `bin/hlc run benchmarks/crypto_bench.hls` |
| `conc_bench.hls` | program | Channel concurrency scaling: 8 requests through 1/2/4 workers, wall clock per width. | `bin/hlc run benchmarks/conc_bench.hls` |
| `simd_bench.hls` | program | 8-tap FIR correlation, scalar vs the SIMD surface, 1M-element correctness phase plus a cache-resident timing phase. | `bin/hlc run benchmarks/simd_bench.hls` |
| `ctime_watch/` | watch suite | Five compile-shaped workloads (lexer, parser, generics, effects, codegen) timed through both compilers. | `make ctime` / `bin/hls-ctime record --instrument native` |
| `size_watch/` | watch suite | Five size-shaped workloads plus `hlc_self` (the compiler's own girth), measured byte-exact. | `make size` |
| `rss_watch/` | watch suite | Four allocation canaries (startup floor, string growth, boxed nodes, list churn), resident set at cycle markers. | `make mem` |
| `run_bench.sh` | script | The differential check: every program benchmark run three ways — Stage-0 interpreter, native via Stage-0-compiled hlc, native via the self-hosted hlc — all three outputs must agree. | `bash benchmarks/run_bench.sh` |

The two `*_watch.hls` files at the top level are **suites**, not
programs: they define no `main`, only `fn bench_<name>()` bodies, and
`bin/hls-bench` is their runner (30-sample adaptive plan, medians with
seeded-bootstrap confidence intervals — the criterion law). Everything
else is a standalone program you can compile and run directly.

---

## The campaign record

One full campaign was run and every number below comes from it.

| Field | Value |
| --- | --- |
| Measurement date | 2026-11-16 (UTC) |
| Halis version | `v1.0.2-6-g7641796` |
| Commit measured | `764179625b032537264d031b3f62616d0497be66` |
| Bootstrap | verified at this commit — the native hlc re-derives `snapshot/hlc.c` byte for byte (`make bootstrap` exits OK) |
| Compiler under test | `bin/hlc`, built from the committed C snapshot with `cc -O1` (the Makefile's `BOOT_CFLAGS` default), then proven at the fixed point |
| Native benchmark recipe | `bin/hlc` (default flags — no `--fast`, no `--lto`, no `--target-feature`) → C, then `gcc -O2 <file>.c -lm -pthread` |
| C toolchain | GCC 14.2.0 (`gcc (Debian 14.2.0-19) 14.2.0`) |
| Operating system | Linux (64-bit) |
| Instruments | `hls-bench` (report schema 0.142.0-alpha), `hls-perf`, `hls-ctime`, `hls-size`, `hls-mem`, `hls-rss`, `hls-bench-stdlib` — all built from the same tree at the commit above |

**Disclosure policy.** The hardware the campaign ran on is not
published; all that is said about the measurement machine is that it
runs 64-bit Linux. Treat the absolute numbers as one machine's
testimony, useful for relative comparison and regression watching on
this platform class — never as a cross-machine ranking. Nothing on
this page depends on the machine's identity for its validity: every
axis below also carries the exact command that re-measures it.

### Method

* **Suites (`hls-bench`)** — default adaptive plan: 30 samples per
  bench, 30 ms warmup, 15 ms per sample, iteration counts auto-scaled
  between 1 and 2^30. Reported: the median of per-sample per-iteration
  estimates with a 2000-resample bootstrap CI (seeded from the bench
  name via CRC-32, so the same data always yields the same report),
  plus mean, standard deviation, median absolute deviation, throughput
  in declared units, outlier census, and the harness overhead per
  iteration (subtracted context, printed separately). Both backends
  were run: `--native` (hlc + gcc -O2) and the default Stage-0
  interpreter leg.
* **Ledger records (`hls-perf`, `hls-ctime`)** — the fixed plan the
  dashboards pin: 10 samples × 400 fixed iterations per bench (perf),
  6 samples per workload (ctime), each record pinned to the clean
  commit, the suite digest and the instrument identity. The numbers
  quoted below for interpreter/native ratios come from these records.
* **Standalone programs** — each compiled once with the recipe above,
  then run 9 times on a quiet machine; the self-reported millisecond
  readings and the process wall clock were collected per run and
  summarised (min / median / mean / max / stdev / CV). The programs
  self-time in whole milliseconds — honest at that resolution, coarse
  below it; the ns-resolution view of the same workloads is the
  `e2e_watch` suite.
* **Binary size** — no statistics by design: the emitted C is
  byte-exact (the bootstrap invariant) and the link recipe is fixed,
  so one measurement is the measurement.
* **RSS** — 5 samples of the resident set at cycle markers, run under
  `setarch -R` with the layout pinned; each canary must certify a flat
  slope (0.0 pages per kilocycle) before it can be filed.
* **Differential check** — `run_bench.sh` ran every program benchmark
  through all three legs; outputs (counts, checksums, hashes) were
  identical leg to leg, the property the bootstrap chain promises.

---

## Results

All times are per bench iteration as defined by each suite (one call
of `bench_<name>` unless the suite says otherwise); CI is the
bootstrap 95% interval of the median.

### Microbenchmarks — `regression_watch.hls`, native backend

| Bench | Median | CI | Mean | Std dev | MAD | Throughput |
| --- | --- | --- | --- | --- | --- | --- |
| `bench_int_loop` | 519.9 ns | [519.5, 520.3] ns | 519.9 ns | 0.7 ns | 0.5 ns | 246 M loop-iterations/s |
| `bench_str_build` | 113.1 ns | [113.0, 113.4] ns | 113.5 ns | 1.2 ns | 0.2 ns | 846 M chars/s grown |
| `bench_list_ops` | 202.5 ns | [202.4, 202.6] ns | 202.4 ns | 2.7 ns | 0.1 ns | 316 M elements/s moved |
| `bench_fnv_hash` | 1.081 µs | [1.081, 1.082] µs | 1.094 µs | 35.2 ns | 0.9 ns | 351 MB/s hashed |
| `bench_match_enum` | 280.0 ns | [279.8, 280.3] ns | 280.3 ns | 1.4 ns | 0.3 ns | 171 M dispatches/s |

30 samples per bench. The outlier census flagged 0–6 samples per bench
(up to 20% on `bench_fnv_hash`, mostly high-severe, scheduler-shaped) —
the medians and MADs barely move, which is the point of reporting
both.

### Workload benches — `e2e_watch.hls`, native backend

| Bench | Median | CI | Mean | Throughput |
| --- | --- | --- | --- | --- |
| `bench_sieve_10k` | 22.932 µs | [22.919, 22.983] µs | 22.957 µs | 436 M slots/s sieved |
| `bench_json_parse` | 131.716 µs | [131.558, 131.941] µs | 135.449 µs | 16.3 MB/s parsed |
| `bench_base64_round` | 502.172 µs | [501.735, 502.460] µs | 502.779 µs | 15.9 MB/s coded |
| `bench_crc32_100` | 76.065 µs | [76.044, 76.101] µs | 76.153 µs | 101 MB/s hashed |
| `bench_sort_lcg_512` | 156.729 µs | [156.618, 156.857] µs | 160.304 µs | 3.19 M elements/s sorted |
| `bench_split_join` | 40.160 µs | [40.129, 40.229] µs | 40.204 µs | 89.5 MB/s scanned |

Each iteration carries the work stated in the suite header: ten JSON
parses (2,210 bytes), fifty base64 round-trips (8,000 bytes coded),
one hundred CRC-32 rounds (7,700 bytes), one full sieve pass, one
512-element sort, one hundred split/join rounds (3,600 bytes).

### Interpreter vs native — the self-hosting gap

Median per-iteration times through the Stage-0 interpreter versus the
native backend, from the ledger records (fixed 10 × 400 plan):

| Bench | Interpreter | Native | Ratio |
| --- | --- | --- | --- |
| `bench_int_loop` | 381.7 µs | 515 ns | 741× |
| `bench_str_build` | 25.67 µs | 112.1 ns | 229× |
| `bench_list_ops` | 231.2 µs | 200.9 ns | 1151× |
| `bench_fnv_hash` | 1.405 ms | 1.091 µs | 1288× |
| `bench_match_enum` | 234.1 µs | 278.4 ns | 841× |
| **geometric mean** | | | **733×** |

The same comparison through `e2e_watch` (adaptive plan): sieve 2888×,
CRC-32 3146×, sort 1357×, base64 173×, JSON 126×, split/join 14× —
geometric mean 394×. The gap is shape-dependent, as it must be: a
bench whose work rides bulk runtime calls (string split/join, JSON
walks) narrows the gap; a bench that is pure interpreter dispatch
(sieve, byte loops) widens it.

### Standalone programs (9 runs each, native)

| Program | Self-reported median | Range | Process wall (median) | Binary size |
| --- | --- | --- | --- | --- |
| `sieve` (100k, 9,592 primes) | < 1 ms | 0–1 ms | 1.005 ms | 62,856 B |
| `json_bench` (100 parses) | 2 ms | 1–2 ms | 2.108 ms | 99,672 B |
| `crypto_bench` (3 × 100 rounds) | 0 ms each | 0–1 ms | 0.604 ms | 94,880 B |
| `conc_bench` | per-width table below | — | 1,374.9 ms | 28,360 B |
| `simd_bench` | per-build table below | — | 808.3 ms | 87,376 B |

The self-reported readings are whole milliseconds; `crypto_bench`'s
workloads are genuinely sub-millisecond and read as 0 ms — the
ns-resolution twins of those workloads live in `e2e_watch`
(`bench_crc32_100`: 76 µs per 100 rounds). The differential check
confirmed identical outputs across all three legs for every program:
9592 primes, checksum 4799999856 for every worker width, FIR checksum
185322304113 scalar = vector, FNV/CRC values byte-identical.

### Concurrency scaling — `conc_bench`

8 requests, each a CPU-bound serve loop, through a shared jobs channel:

| Workers | Wall clock (median) | Range | Speedup vs 1 worker |
| --- | --- | --- | --- |
| 1 | 613 ms | 540–641 ms | 100% |
| 2 | 386 ms | 330–402 ms | 159% |
| 4 | 370 ms | 362–377 ms | 165% |

Scaling from one worker to two pays clearly; four workers plateaus on
this machine rather than doubling again. The per-request checksum was
4799999856 in every run of every width — the answer never changes,
only the clock does.

### SIMD — default build vs `--target-feature avx2`

The FIR correlation kernel, cache-resident phase, best-of-5
interleaved rounds (medians of 5 program runs each):

| Build | Scalar | Vector | Scalar/vector | Checksums |
| --- | --- | --- | --- | --- |
| default (portable) | 7 ms | 129 ms | 0.05× | match |
| `--target-feature avx2` | 7–8 ms | 1 ms | 7–8× | match |

Two honest findings. First: the default build compiles the SIMD
surface to its portable path, and for this kernel that path is ~18×
slower than the scalar loop — `has_feature(avx2)` reports false while
`simd_cpu_supports(avx2)` reports true, i.e. the runtime detects the
capability but the compiler was not told to use it. Second: with the
feature flag on, the vector path wins 7–8× and the checksums still
match the scalar side. The lesson for readers: pass
`--target-feature` when the deployment machine supports it; the
default is the correctness-first, portability-first build.

### Compile time — `ctime_watch`, both instruments

Median of 6 samples per workload; each sample is one full HLS-to-C
compile:

| Workload | Stage-0 chain | Native hlc | Ratio |
| --- | --- | --- | --- |
| `lex_dense` | 768.0 ms | 6.418 ms | 120× |
| `parse_deep` | 548.7 ms | 4.143 ms | 132× |
| `check_generic` | 953.0 ms | 7.454 ms | 128× |
| `check_effects` | 507.7 ms | 3.522 ms | 144× |
| `gen_data` | 593.0 ms | 4.238 ms | 140× |
| **geometric mean** | | | **133×** |

The two instruments are formally never compared (the gate refuses to
translate between them); the ratio column is this campaign's
observation of what self-hosting bought, not a gate. The native
compiler chews through 120–144× more front-end per second than the
interpreter chain that booted it.

### Binary size — `size_watch`, native instrument

One measurement, no noise — the emitted C is byte-exact by the
bootstrap invariant and the link recipe is fixed:

| Workload | Emitted C | Binary | text | data | bss |
| --- | --- | --- | --- | --- | --- |
| `startup_floor` | 120,218 B | 17,496 B | 4,338 B | 688 B | 120 B |
| `str_table` | 122,801 B | 22,112 B | 8,195 B | 800 B | 120 B |
| `fn_grid` | 124,236 B | 22,224 B | 8,086 B | 704 B | 120 B |
| `struct_grid` | 124,139 B | 21,952 B | 8,238 B | 704 B | 120 B |
| `match_matrix` | 128,126 B | 21,936 B | 8,090 B | 704 B | 120 B |
| `hlc_self` | 3,108,017 B | 1,527,464 B | 1,106,964 B | 180,344 B | 1,792 B |

A Halis program that does nothing but start and stop costs ~17.5 KB
end to end (4.3 KB of actual text); the compiler itself is a 1.5 MB
native binary. The machine column the tool pins is
`gcc (Debian 14.2.0-19) 14.2.0` with this repo's fixed link recipe —
sizes from another toolchain are another language.

### Resident memory — `rss_watch`

Marker-median resident set over 5 samples, layout pinned, slope
certified flat before filing (all four canaries measured 0.0 pages per
kilocycle — no leak, no creep):

| Canary | Marker RSS | Interpretation |
| --- | --- | --- |
| `startup_floor` | 269 pages | the resident cost of a bare program (~1.0 MiB at 4 KiB pages) |
| `node_box` | 824 pages | boxed-node heap traffic settles flat |
| `list_churn` | 425 pages | heavy list push/pop recycles instead of growing |
| `str_grow` | 2,123 pages | sustained string growth reaches its plateau and holds it |

### The stdlib zero-cost audit — `hls-bench-stdlib`

The Stage-32 law: every public stdlib function benchmarks under
1.0 µs per call. The audit catalogues all 1,799 public functions,
compiles a driver per function, runs it with representative inputs
(200,000 iterations, lower of two wall clocks), and reports the median
table slowest-first. Result of this campaign:

| Outcome | Count |
| --- | --- |
| measured | 1,003 (median 0.010 µs, 90th percentile 0.43 µs) |
| skipped — no safe default inputs | 793 |
| over the 1.0 µs threshold | 7 |
| failed to run | 3 |

The seven over threshold, slowest first: `sha256.sha256_hex` 9.85 µs,
`sha256.sha256_hash` 5.34 µs, `sha256.sha256_pad` 2.29 µs,
`uuid.ulid_from` 1.41 µs, `hex.hex_encode` 1.18 µs,
`log.log_epoch_ms_to_rfc3164` 1.11 µs, `sse.sse_build_heartbeat_block`
1.01 µs — dominated by work-doing cores (hash rounds, a 64-byte hex
encode) where a per-call µs threshold measures the algorithm more than
any abstraction overhead; the slowest *wrapper-shaped* functions
(`base64_encode` 0.80 µs, `crypto_fnv1a_64_hex` 0.85 µs) all clear the
bar. The three failures — `sha256_block_word`, `sha256_message_schedule`,
`sha256_compress_block` — panicked with "string access out of bounds"
under the audit's representative input, an input-shape mismatch this
campaign reports as found, not excused.

---

## How to reproduce

```console
$ make bootstrap                                  # the compiler, proven at the fixed point
$ make bin/hls-bench bin/hls-perf bin/hls-ctime bin/hls-size bin/hls-rss bin/hls-mem bin/hls-bench-stdlib

$ bin/hls-bench --native benchmarks/regression_watch.hls     # tables above, native leg
$ bin/hls-bench          benchmarks/e2e_watch.hls            # interpreter leg
$ bin/hls-perf record --native                               # ledger record, native backend
$ bin/hls-perf record                                       # ledger record, interpreter backend
$ bin/hls-ctime record                                      # compile time, Stage-0 chain
$ bin/hls-ctime record --instrument native                  # compile time, native hlc
$ make size                                                 # binary size (also: make size-dashboard)
$ make mem                                                  # RSS canaries
$ bash benchmarks/run_bench.sh                              # the three-leg differential check
$ bin/hls-bench-stdlib --json stdlib-report.json            # the zero-cost audit
```

For the SIMD comparison, build `simd_bench` twice — once with the
default flags, once with `bin/hlc --target-feature avx2` — and diff
the two reports.

Expect your absolute numbers to differ from the tables above —
different machine, same laws. The medians' *shape* (which bench
dominates which, the CI widths, the ratios) is the part that travels.

## Watching for regressions

The suites are not just for one-off reading; the repo's gates ride
them per commit:

* `make perf` / `make perf-gate` / `make perf-dashboard` — the
  tracked suite (`regression_watch.hls`) recorded into the append-only
  `.hlperf/` ledger, gated Welch + seeded-bootstrap at a 5% noise
  band, rendered to `PERFORMANCE.md`.
* `make ctime` / `make ctime-gate` / `make ctime-dashboard` — the
  same law for compile time, two instruments, never compared.
* `make size` / `make size-gate` / `make size-dashboard` — one byte
  of growth is a named regression; no noise band to hide behind.
* `make mem` / `make mem-gate` / `make mem-dashboard` — RSS medians
  under the page law (a canary that leaks is a bug, not a record).

All four ledgers live under gitignored dot-directories; each record is
pinned to one clean commit, and the gates refuse dirty trees.

## Notes from this campaign

* The campaign itself found and fixed a tool bug: `hls-perf record`
  starved on the report field the native `hls-bench` had renamed
  (`samples` → `raw_samples`) when the runner dropped Python — every
  record died on "fewer than 2 samples" validation. The record path
  now eats both spellings (commit `e308d85`).
* `run_bench.sh` now recognises suite files (`regression_watch.hls`,
  `e2e_watch.hls`) and points them at `hls-bench` instead of failing
  to compile a program that deliberately has no `main`.
* `e2e_watch.hls` is new with this campaign — six algorithm benches
  under the criterion runner, the workload-level companion to
  `regression_watch.hls`.
