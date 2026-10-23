# Results

Primary experiment: GitHub Actions run **`40002470957`**,
`head_sha = c53cd2a`, `conclusion = success`, all 8 configurations.
Raw traces in `results/baseline`, `results/patched`; machine-extracted
matrix in `results/comparison/comparison.md`.

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

| | run | head | conclusion | artifact size |
| :--- | :--- | :--- | :--- | ---: |
| rep 1 | 40002470957 | c53cd2a | success | 90 338 B |
| rep 2 | 40004718895 | c53cd2a | success | 90 956 B |
| rep 3 | 40004771234 | c53cd2a | success | 90 255 B |

All three runs executed the identical commit and harness, and all three
produced an artifact of comparable size (all 8 configurations × 2 files),
i.e. the experiment is **operationally** reproducible: it does not
intermittently collapse into the empty-trace failure mode.

**Content-level comparison across repetitions is not available.** GitHub
requires authentication to download Actions artifacts; this environment has
no GitHub credential, and the token used earlier in the project was exposed
and must be rotated.

The three artifact payloads are provably distinct — the run pages expose
their digests, so the runs are independent executions rather than repeated
uploads of one file:

| run | artifact sha256 |
| :--- | :--- |
| 40004718895 | `d27aaed10c9d0cef512c67fadc9f06d7fedd804b1209809862e9e0115a407587` |
| 40004771234 | `575bbfab4cf3aea16681abb5bd88317b3e80543b21603bcc696bd0109d25dd0e` |

Four retrieval routes were attempted and all are closed to an unauthenticated
reader:

| route | result |
| :--- | :--- |
| `GET /actions/artifacts/<id>/zip` | `401` |
| `GET /actions/runs/<id>/logs` | `403` |
| run page HTML (anonymous) | run metadata only; the `$GITHUB_STEP_SUMMARY` body, which holds the full per-config analysis, is not served |
| job page HTML (anonymous) | step names and conclusions only, no step output |

Reopening this requires either a rotated token carrying `actions:read`, which
would allow the artifacts to be fetched, or re-running the harness on a
KVM-capable host where the repetitions can be produced locally.

The workflow's own `Commit benchmark logs to repository` step does not close
the gap either: `.gitignore` contains `*.log`, so
`git add benchmarks/smoke_test/` stages nothing and the push is a no-op —
`origin/exp/qemu-guard-audit` is still at `c53cd2a`. Repetition 2 and 3
traces exist only inside their (unauthenticated-inaccessible) artifacts.

Consequently the 3-repetition median/variability table required by the
repeatability plan **could not be produced**, and no claim in Section 5 is
backed by cross-run statistics. This is a limitation of the evidence, not a
result.
