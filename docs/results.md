# Results

Primary experiment: GitHub Actions run **`40002470957`**,
`head_sha = c53cd2a`, `conclusion = success`, all 8 configurations.
Raw traces in `results/baseline`, `results/patched`; machine-extracted
matrix in `results/comparison/bench_matrix.txt`.

Two further repetitions of the same commit are now **content-available**:
raw traces in `results/reps/rep2` and `results/reps/rep3`, matrices
`results/comparison/bench_matrix_rep2.txt` and `_rep3.txt`, all generated
from the traces by `scripts/summarize_bench.py`. Sections 1–6 describe
run 1 only; Section 7 reconciles the three runs and Section 8 is the
cross-run result. The cross-run finding **revises** the run-1 reading in
Section 5 — see §8.

## 1. Validity gates — all passed

| Check | Result |
| :--- | :--- |
| `CONFIG_BLK_WBT=y`, `CONFIG_BLK_WBT_MQ=y` in guest | yes (both profiles) |
| target device resolved | `/dev/sda`, `rotational=1` |
| `queue/nr_requests` | 256 (virtio-scsi) / 2 (ide) |
| `Active wbt_lat_usec` | 150 (sparse) / 1000 (heavy) |
| `/sys/kernel/tracing/events/wbt` present | yes |
| writers report `mode=BUFFERED` | yes, all |
| readers report `open_mode=O_DIRECT` | yes, all |
| non-zero read **and** write sectors at the block layer | yes, all 8 |
| `wbt_timer` non-zero | yes, all 8 (293–304) |

No configuration produced the all-zero signature that invalidated the
earlier run `39996857389`.

## 2. Tracepoint counters

| config | `wbt_timer` | `wbt_step` | `wbt_stat` | `wbt_lat` | `wbt_guard` | blk read / write sectors |
| :--- | ---: | ---: | ---: | ---: | ---: | :--- |
| baseline_ide_h | 294 | 2 | 272 | 0 | 0 | 188 928 / 4 246 272 |
| patched_ide_h | 294 | 2 | 280 | 0 | **8** | 200 680 / 4 590 208 |
| baseline_vio_h | 304 | 6 | 287 | 0 | 0 | 199 560 / 7 303 040 |
| patched_vio_h | 304 | 6 | 288 | 0 | 0 | 199 544 / 7 220 992 |
| baseline_ide_s | 293 | 2 | 0 | 0 | 0 | 758 456 / 2 912 |
| patched_ide_s | 293 | 2 | 0 | 0 | **288** | 759 640 / 2 912 |
| baseline_vio_s | 303 | 6 | 0 | 0 | 0 | 787 488 / 2 936 |
| patched_vio_s | 298 | 6 | 0 | 0 | **283** | 769 664 / 2 936 |

`wbt_guard` is 0 on every baseline kernel because the tracepoint does not
exist there (symbol gate: 0 vs 47 occurrences). Non-zero values on the
patched kernel are therefore unambiguous.

`wbt_stat = 0` for both sparse configurations is expected: the `wbt_stat`
tracepoint only executes on `LAT_OK` / `LAT_EXCEEDED`, and sparse windows
(< 3 write completions) never satisfy `stat_sample_valid()`, so they stay
in `LAT_UNKNOWN_WRITES`.

## 3. Guard census — where it fires and, equally important, where it does not

| patched config | guard events | held (`fired=1`) | `scale_up` (`fired=0`) | events with `r_samples=0` | `r_min` µs (min/mean/max) | target µs | comes from |
| :--- | ---: | ---: | ---: | ---: | :--- | ---: | :--- |
| patched_ide_h | 8 | 0 | 8 | 8 | n/a | 1000 | `LAT_UNKNOWN_WRITES` |
| patched_vio_h | 0 | – | – | – | – | 1000 | not reached |
| patched_ide_s | 288 | 0 | 288 | 7 | 62 / 68.2 / 77 | 150 | `LAT_UNKNOWN_WRITES` |
| patched_vio_s | 283 | **276** | 7 | 0 | 146 / 160.9 / 175 | 150 | `LAT_UNKNOWN_WRITES` |

Status distribution for both sparse configurations over the whole run:
`LAT_UNKNOWN_WRITES 95.0 %`, `LAT_UNKNOWN 5.0 %`.

Reading:

* **`patched_vio_s` is the only configuration where the guard holds.**
  276 of 283 evaluations returned `hold`; the 7 `scale_up` events all
  occurred when `r_min` dipped to 146–149 µs, just under the 150 µs target.
  The condition behaves exactly as written, including the boundary.
* **`patched_ide_s` evaluates 288 times and never holds**, because
  `r_min` was 62–77 µs, far below the 150 µs target. This is a property of
  the *loaded* condition, not of the device: the guard compares the window's
  **minimum** read latency, and on the IDE path the minimum stays low even
  while P50 is ~540 µs.
* **`patched_ide_h` holds nothing**: all 8 evaluations had `r_samples == 0`,
  so `nr_samples && ...` short-circuits and the huge
  `r_min = 18446744073709551` µs (`U64_MAX / 1000`, `blk-stat`'s "no
  sample" sentinel) is printed but never compared.
* **`patched_vio_h` never reaches the branch at all** — the heavy profile
  produces ≥ 3 write completions per window, so `stat_sample_valid()` is
  true, WBT classifies `LAT_OK`, and `LAT_UNKNOWN_WRITES` is not entered.

Caveat introduced by the repetitions (§7): the census above is run-1
specific. The *decision rule* reproduces exactly, but the *regime* does
not. On reps 2/3 the slower host stretches both the read-latency floor and
the 100 ms window, so `patched_vio_s` produces 184 evaluations instead of
283, every live evaluation holds, and `patched_ide_s` — which never held in
run 1 (`r_min` 62–77 µs) — holds 3–4 times because its `r_min` floor rises
to 100–166 µs and now straddles the 150 µs target. The guard is therefore
not measuring the workload so much as the host's speed.

## 4. Mechanism evidence: the depth lever moved, and only where predicted

`wbt_step` transitions (scale step; `normal` is the tracepoint's reported
`wb_normal`):

| config | escalation | de-escalation |
| :--- | :--- | :--- |
| baseline_ide_h | 2.05 s → −1 (n=2) | 32.52 s → 0 |
| patched_ide_h | 1.95 s → −1 (n=2) | 32.42 s → 0 |
| baseline_vio_h | 2.16 → −1 (16), 2.26 → −2 (31), 2.36 → −3 (48) | 32.63 → −2, 33.15 → −1, 33.67 → 0 |
| patched_vio_h | 2.08 → −1 (16), 2.19 → −2 (31), 2.29 → −3 (48) | 32.56 → −2, 33.08 → −1, 33.60 → 0 |
| baseline_ide_s | 2.07 s → −1 (n=2) | 32.44 s → 0 |
| patched_ide_s | 1.95 s → −1 (n=2) | 32.32 s → 0 |
| baseline_vio_s | 2.15 → −1, 2.26 → −2, 2.36 → −3 | 32.52 → −2, 33.04 → −1, 33.56 → 0 |
| **patched_vio_s** | **3.77 → −1, 7.41 → −2, 8.56 → −3** | 32.58 → −2, 33.10 → −1, 33.62 → 0 |

Negative `scale_step` **increases** allowed write depth
(`depth = 1 + ((depth-1) << -scale_step)`, capped at `3 * queue_depth / 4`;
step −3 reports `normal=48, max=96`). Therefore:

* In `patched_vio_s` the write-depth escalation was delayed from
  **2.36 s to 8.56 s** — the depth lever was restrained for the first
  ~6.2 s (~21 % of the 30 s window) and then converged to the same cap.
* In the other seven configurations the patched timeline is within 0.12 s of
  its matched baseline, i.e. **the patch changed nothing**. `ide` is inert
  because `queue_depth == 1` degenerates the lever (both step 0 and step −1
  report `normal=2`); `vio_h` is inert because the branch is never entered.

This is a clean mechanism result: the guard modifies the controller **only**
in the branch it patches, and **only** where read samples exist with
`min > target`.

## 5. Outcome evidence: reader latency did not improve

Per-config reader latency, µs, averaged over reader processes
(`sd` = within-run spread across the concurrent reader processes):

| config | readers | SAMPLES | MIN | P50 | P90 | P99 | MEAN |
| :--- | ---: | ---: | :--- | :--- | :--- | :--- | :--- |
| baseline_vio_s | 6 | 16 406 | 241 (sd 3.0) | 526 (sd 1.4) | 802 (sd 2.2) | 1137 (sd 12.4) | 557 |
| patched_vio_s | 6 | 16 035 | 251 (sd 2.1) | **553** (sd 2.4) | **833** (sd 3.3) | **1158** (sd 6.8) | **580** |
| baseline_ide_s | 6 | 15 801 | 139 (sd 4.7) | 540 (sd 22.6) | 957 | 1295 | 568 |
| patched_ide_s | 6 | 15 826 | 140 (sd 4.6) | 544 (sd 19.2) | 953 | 1310 | 569 |
| baseline_vio_h | 2 | 12 472 | 242 | 862 | 1437 | 2138 | 914 |
| patched_vio_h | 2 | 12 472 | 250 | 876 | 1457 | 2004 | 919 |
| baseline_ide_h | 2 | 11 808 | 170 | 762 | 1527 | 2427 | 1002 |
| patched_ide_h | 2 | 12 542 | 161 | 736 | 1532 | 2418 | 857 |

Delta, patched − baseline:

| config | P50 | P90 | P99 | MEAN | read samples | writer OPS |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| **vio_s** (guard held) | **+27 µs (+5.1 %)** | **+31 µs (+3.9 %)** | **+21 µs (+1.8 %)** | **+23 µs (+4.1 %)** | **−2.3 %** | 367 → 367 (0.0 %) |
| ide_s (guard evaluated, never held) | +4 µs (+0.7 %) | −4 µs (−0.4 %) | +15 µs (+1.2 %) | +1 µs | +0.2 % | 364 → 364 |
| vio_h (branch not entered) | +14 µs (+1.6 %) | +20 µs (+1.4 %) | −134 µs (−6.3 %) | +5 µs | 0.0 % | −1.1 % |
| ide_h (guard never held) | −26 µs (−3.4 %) | +5 µs | −9 µs (−0.4 %) | −145 µs | +6.2 % | +8.1 % |

### How to read this

* **In the only configuration where the guard actually held depth
  (`vio_s`), read latency got slightly worse, not better.** The regression
  is small (+1.8 % at P99, +5.1 % at P50) but it is larger than the
  within-run spread of the concurrent reader processes: the P50 gap of
  27 µs is ~11× the pooled per-config standard deviation (~1.9 µs), so it is
  not probe noise.
* Read sample throughput fell ~2.3 % in the same configuration, consistent
  with readers being served slightly more slowly.
* **Writer throughput was unchanged** (367 → 367 OPS, 1 503 232 bytes in
  both). The guard did not cost write performance in the sparse profile.
* `ide_s` — where the guard was evaluated but never held — shows no
  change beyond noise. That is the correct control result.
* `vio_h` and `ide_h` differences are **not** attributable to the patch:
  in `vio_h` the branch is never entered, and in `ide_h` all 8 evaluations
  returned `scale_up`, so the patched kernel made the same decision as the
  baseline. The eye-catching `ide_h` MAX drop (644 724 → 25 790 µs) comes
  from two ~645 ms stalls in the baseline's 2 probes; with n=2 per config
  this is a single-sample artifact and must not be presented as a win.

## 6. Answering the six questions

1. **Did the guard fire?** Yes — 276/283 evaluations in `patched_vio_s`.
   It did not fire anywhere else, and in two configurations it was never
   even reached. All of this matches the predicate exactly.
2. **Did it hold depth?** Yes — the `wbt_step` escalation in `patched_vio_s`
   is delayed from 2.36 s to 8.56 s and step 0 / −1 / −2 intervals are
   correspondingly lengthened.
3. **Did it change queue-depth behaviour?** Yes, but only transiently and
   only in one configuration; the run still converges to the same step −3
   cap, and 7/283 windows still scaled up.
4. **Did it change latency?** Yes, in the wrong direction: +1.8 % (P99) to
   +5.1 % (P50) in the one configuration where it acted.
5. **Did it change throughput?** No measurable change for writers
   (367 OPS both); readers completed ~2.3 % fewer samples.
6. **Is the effect explainable?** Partly. Holding depth at step 0 keeps more
   dirty data in the page cache and delays the write-depth relief that the
   baseline grants within ~200 ms; the guard removes write-side headroom
   exactly during the read-latency-sensitive phase. The observed sign is
   consistent with that story, but a single run with content cannot exclude
   run-to-run variation — which is what Section 7 addresses.

## 7. Repeatability

Three matched runs of the same commit (`c53cd2a`) and the same harness all
succeeded. All three are now **content-available** in-repo:

| rep | run id | `head_sha` | artifact size | artifact sha256 | traces | matrix |
| :--- | :--- | :--- | ---: | :--- | :--- | :--- |
| run 1 | `40002470957` | `c53cd2a` | 90 338 B | not published here | `results/baseline`, `results/patched` | `bench_matrix.txt` |
| rep 2 | `40004718895` | `c53cd2a` | 90 956 B | `d27aaed1…a407587` | `results/reps/rep2` | `bench_matrix_rep2.txt` |
| rep 3 | `40004771234` | `c53cd2a` | 90 255 B | `575bbfab…9d25dd0e` | `results/reps/rep3` | `bench_matrix_rep3.txt` |

### 7.1 Provenance

The two repetition payloads were supplied as ZIPs and their digests
recomputed **from the ZIP bytes**:

| file | sha256 (over the ZIP bytes) | → run |
| :--- | :--- | :--- |
| `qemu-matched-benchmark-logs.zip` | `D27AAED10C9D0CEF512C67FADC9F06D7FEDD804B1209809862E9E0115A407587` | `40004718895` (rep 2) |
| `qemu-matched-benchmark-logs (1).zip` | `575BBFAB4CF3AEA16681ABB5BD88317B3E80543B21603BCC696BD0109D25DD0E` | `40004771234` (rep 3) |

Both digests equal the artifact digests the GitHub run pages report for
those runs — the same values already recorded here before the payloads
existed — which is what ties each ZIP, and therefore each extracted
`*_qemu.log` set, to its run. The mapping is fixed by the digest, not by
file order or ZIP name.

Each ZIP has the same 16-file structure as run 1: eight `*_qemu.log`
traces (`{baseline,patched}_{ide,vio}_{h,s}`) plus eight `*_analysis.txt`
summaries. Both sets are committed under `results/reps/`. The traces carry
the run's own header, and all three runs report the same
`PROFILE: sparse writers=1 readprocs=6` / `heavy writers=4 readprocs=2`,
`wchunk=4KB wwindow=4MB wstride=4MB wsleep=80ms rpace=1000us target=150us`,
`Linux 6.6.123.2-microsoft-standard-WSL2`, and `CONFIG_BLK_WBT=y` +
`CONFIG_BLK_WBT_MQ=y`.

All matrices are regenerated from the committed traces by

```text
scripts/summarize_bench.py                                                              # run 1
scripts/summarize_bench.py results/reps/rep2 "run 40004718895 (rep 2)" bench_matrix_rep2.txt
scripts/summarize_bench.py results/reps/rep3 "run 40004771234 (rep 3)" bench_matrix_rep3.txt
```

with no value transcribed by hand. The run-1 invocation is unchanged and
still produces a byte-identical `bench_matrix.txt`, which CI asserts.

The unauthenticated retrieval routes remain closed for anyone reproducing
this from scratch — `GET /actions/artifacts/<id>/zip` returns `401`,
`GET /actions/runs/<id>/logs` returns `403`, and neither the run page nor
the job page HTML is served anonymously — so the payloads had to be
supplied out-of-band and are now archived in-repo rather than re-fetched.
`.gitignore` re-includes `results/**/*.log`, so these traces are tracked
exactly like run 1's.

### 7.2 The three runs are not three comparable repetitions

Same commit, same harness, same declared parameters — yet runs 2 and 3 sit
at a different operating point from run 1. On the sparse profiles:

| quantity | run 1 | rep 2 | rep 3 | ratio (rep 2 / run 1) |
| :--- | ---: | ---: | ---: | ---: |
| `baseline_vio_s` P50 (µs) | 526.0 | 1166.0 | 1142.3 | ≈ 2.2× |
| `baseline_ide_s` P50 (µs) | 540.2 | 1394.5 | 1361.2 | ≈ 2.6× |
| read samples per probe (`baseline_vio_s`) | ≈ 16 400 | ≈ 10 360 | ≈ 10 200 | ≈ 1.6× |
| `wbt_timer`, `patched_vio_s` | 298 | 194 | 195 | ≈ 1.5× |

A ~2.2× step in absolute latency with byte-identical workloads, kernel and
parameters is a **host-speed artifact**, not a configuration change:
GitHub's `ubuntu-22.04` pool has no usable KVM for this workflow, so the
guest runs under QEMU **TCG** and the emulation rate dominates everything.
Every dependent quantity scales coherently with it — the read-latency floor,
the samples each probe can collect, and the number of 100 ms windows the
run yields.

Consequence: these are **not three interchangeable replicates of one
measurement**. They are effectively `n = 1` fast-host run plus `n = 2`
slow-host runs. Any pooled median therefore mixes two regimes; it is
arithmetically valid but not an estimate of a common effect.

### 7.3 The guard's *regime* moves with the host too

Same nominal configuration, same predicate, very different behaviour:

| run | `patched_vio_s` evaluations | live `hold` | live `scale_up` | teardown `scale_up` (`r_samples=0`) | `r_min` sane (µs) | `patched_ide_s` evaluations | `hold` | `r_min` sane (µs) |
| :--- | ---: | ---: | ---: | ---: | :--- | ---: | ---: | :--- |
| run 1 | 283 | 276 | 7 | 0 | 146–175 | 288 | 0 | 62–77 |
| rep 2 | 184 | 176 | 0 | 8 | 209–276 | 289 | 4 | 100–166 |
| rep 3 | 184 | 176 | 0 | 8 | 189–268 | 288 | 3 | 106–166 |

Two distinctions matter and were previously conflated:

* **Live vs teardown decisions.** A `hold`/`scale_up` pair is a *live*
  decision only when `r_samples > 0`. When `r_samples == 0` the predicate
  short-circuits, `r_min` prints the `U64_MAX/1000` sentinel
  (`18446744073709551` µs) and the code necessarily takes `scale_up`. In
  reps 2/3 **all eight** `patched_vio_s` `scale_up` events are of this kind:
  they occur at t ≈ 32.2–32.9 s, i.e. after the 30 s reader window has
  closed, and are teardown relaxations, not in-run decisions. Run 1 has
  none of these (`insane_events = 0`).
* **The 7 live `scale_up` in run 1 are a boundary effect.** They occur at
  t = 3.77–31.85 s with `r_min` 146–149 µs, i.e. just *under* the 150 µs
  target, always with 311–343 read samples. They are genuine in-run
  decisions at the predicate's edge, and they are the reason the run-1
  escalation timeline still reaches step −3 (§4).

Because the host is slower in reps 2/3, the `r_min` floor for `vio_s` rises
to 189–276 µs — comfortably above target — so **every** live evaluation
holds and there is no in-run `scale_up` at all. The same shift makes
`patched_ide_s`, inert in run 1 (`r_min` 62–77 µs), hold 3–4 times
(`r_min` 100–166 µs straddling 150 µs). The predicate is reacting to the
emulator's speed floor, not to the read-quality situation it was written to
detect.

## 8. Cross-run result

Averaged over the six reader probes **within** each run (the six probes are
concurrent observations of one execution, not six repetitions), then
compared across the three runs. All figures are means in µs; `Δ` is
patched − baseline.

Treatment, sparse profile (`vio_s`) — the only configuration where the
guard holds depth:

| run | base P50 | pat P50 | ΔP50 | Δ% | base P99 | pat P99 | ΔP99 | Δ% | writer OPS |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| run 1 | 526.00 | 553.00 | **+27.00** | **+5.13 %** | 1137.00 | 1157.83 | **+20.83** | **+1.83 %** | 367 → 367 |
| rep 2 | 1166.00 | 1155.17 | −10.83 | −0.93 % | 2955.17 | 2901.50 | −53.67 | −1.82 % | 362 → 362 |
| rep 3 | 1142.33 | 1124.00 | −18.33 | −1.60 % | 2522.00 | 2481.17 | −40.83 | −1.62 % | 363 → 362 |
| **median (n=3)** | | | **−10.83** | | | | **−40.83** | | |

Control, sparse profile (`ide_s`) — guard evaluated, depth lever degenerate:

| run | ΔP50 | Δ% | ΔP99 | Δ% | writer OPS |
| :--- | ---: | ---: | ---: | ---: | ---: |
| run 1 | +3.67 | +0.68 % | +15.00 | +1.16 % | 364 → 364 |
| rep 2 | −27.00 | −1.94 % | −94.67 | −3.49 % | 355 → 356 |
| rep 3 | +11.17 | +0.82 % | +5.00 | +0.19 % | 358 → 358 |

All four workloads, per run (heavy has n=2 readers and is reported for
completeness only — see `limitations.md` §5):

| config | run | ΔP50 µs (%) | ΔP99 µs (%) | writer OPS | guard behaviour |
| :--- | :--- | ---: | ---: | ---: | :--- |
| `vio_s` | 1 | +27.00 (+5.13 %) | +20.83 (+1.83 %) | 367 → 367 | 276 hold / 7 live `scale_up` |
| `vio_s` | 2 | −10.83 (−0.93 %) | −53.67 (−1.82 %) | 362 → 362 | 176 hold / 0 live `scale_up` |
| `vio_s` | 3 | −18.33 (−1.60 %) | −40.83 (−1.62 %) | 363 → 362 | 176 hold / 0 live `scale_up` |
| `ide_s` | 1 | +3.67 (+0.68 %) | +15.00 (+1.16 %) | 364 → 364 | 0 holds |
| `ide_s` | 2 | −27.00 (−1.94 %) | −94.67 (−3.49 %) | 355 → 356 | 4 holds |
| `ide_s` | 3 | +11.17 (+0.82 %) | +5.00 (+0.19 %) | 358 → 358 | 3 holds |
| `vio_h` | 1 | +14.50 (+1.68 %) | −135.00 (−6.31 %) | 57 055 → 56 414 | branch never entered |
| `vio_h` | 2 | −51.50 (−2.77 %) | −198.50 (−4.83 %) | 26 821 → 27 226 | 9 `scale_up`, all `r_samples=0` |
| `vio_h` | 3 | +29.00 (+1.69 %) | +28.50 (+0.78 %) | 28 310 → 27 725 | 7 `scale_up`, all `r_samples=0` |
| `ide_h` | 1 | −26.00 (−3.41 %) | −9.50 (−0.39 %) | 33 174 → 35 861 | 8 `scale_up`, inert |
| `ide_h` | 2 | −58.00 (−3.51 %) | −307.50 (−6.11 %) | 18 548 → 19 888 | 7 `scale_up`, inert |
| `ide_h` | 3 | −2.50 (−0.16 %) | −405.00 (−8.43 %) | 18 936 → 20 306 | 6 `scale_up`, inert |

### How to read §8

* **The treatment sign flips across runs.** `vio_s` P50 goes
  +5.13 % → −0.93 % → −1.60 %. The run-1 regression in §5 is not
  reproduced; it is one point in a spread that changes sign.
* **The control varies just as much, and also flips sign.** `ide_s` P50
  goes +0.68 % → −1.94 % → +0.82 %, on a configuration whose depth lever is
  degenerate (`queue_depth == 1` → `max_depth == 2` for all
  `scale_step ≤ 0`), so nothing the patch does can produce it.
* **The noise floor is measured directly, and it is bigger than the
  effect.** `ide_h` is provably inert — all 6–8 evaluations return
  `scale_up`, the same decision the baseline kernel makes, and the lever is
  degenerate — yet its writer throughput differs by **+8.1 % / +7.2 % /
  +7.2 %** across the three runs. That is a *systematic* bias of the same
  magnitude as the largest reported latency effect, present in all three
  runs, on a configuration where the patch cannot act. The likely cause is
  the harness ordering defect already recorded in `limitations.md` §6
  (baseline and patched are always executed in the same order, never
  alternated), which a monotonically drifting runner would bias
  identically every time.
* **The one thing that is stable is writer throughput in `vio_s`**:
  367 → 367, 362 → 362, 363 → 362. Holding depth did not cost write
  performance in the sparse profile in any run.
* **The median is not a defensible effect estimate.** −10.83 µs (P50) and
  −40.83 µs (P99) are correct as medians of three numbers, but they pool a
  fast-host run with two slow-host ones (§7.2), they mix a live-hold regime
  with a teardown-only regime (§7.3), and their own control flips sign. The
  honest statement is that **the effect is not resolvable above the
  observed cross-run variation** — which is exactly the condition the
  repeatability plan defines for a REVISE decision.

Consequently §5's conclusion stands only as a *run-1* observation, not as
the study result. The cross-run result is: mechanism reproduced, direction
not reproduced, effect not established.
