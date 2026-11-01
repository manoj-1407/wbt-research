# Controlled Benchmark Results — v5 vs Baseline

**Date:** 2026-11-01
**Environment:** QEMU 8.2.2 with KVM acceleration, virtio-scsi (num_queues=1), 1024 MB RAM
**Kernel:** WSL2 6.6.123.2 @ `a07f9ea8a`
**Baseline:** Unpatched upstream, bzImage SHA-256 `7ab7c848...`
**v5:** Candidate `ebd45314d`, bzImage SHA-256 `5ea5022e...`
**Workload:** 1 buffered writer (4KB, 80ms sleep) + 6 O_DIRECT readers (random 4KB, 1000µs pace), 20s, `wbt_lat_usec=150`
**Repetitions:** 5 per kernel, alternating order (v5, baseline, baseline, v5, v5, baseline, baseline, v5, v5, baseline)
**Fresh disk image per run:** 128 MB, upper 64 MB filled with urandom

---

## Per-run results

| Run | P50 (µs) | P90 (µs) | P99 (µs) | Writer OPS | Total Samples | WBT Timer | s2 (UNKNOWN) | s3 (UNKNOWN_WRITES) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| baseline_rep1 | 1569.0 | 3214.0 | 6576.3 | 228 | 32425 | 202 | 10 | 192 |
| baseline_rep2 | 1623.0 | 3628.8 | 10053.3 | 225 | 29935 | 202 | 10 | 192 |
| baseline_rep3 | 1564.5 | 3184.8 | 7962.2 | 228 | 32673 | 202 | 10 | 192 |
| baseline_rep4 | 1614.0 | 3324.8 | 8236.7 | 226 | 31314 | 201 | 10 | 190 |
| baseline_rep5 | 1539.5 | 3094.0 | 7194.3 | 228 | 32368 | 202 | 10 | 192 |
| v5_rep1 | 1571.5 | 3345.3 | 8022.3 | 230 | 31240 | 127 | 122 | 5 |
| v5_rep2 | 1516.5 | 3348.2 | 10144.2 | 223 | 31210 | 133 | 128 | 5 |
| v5_rep3 | 1602.0 | 3516.8 | 10127.3 | 224 | 30767 | 131 | 129 | 2 |
| v5_rep4 | 1543.3 | 3276.7 | 8867.2 | 226 | 31544 | 131 | 125 | 6 |
| v5_rep5 | 1668.5 | 3763.7 | 11679.2 | 219 | 28394 | 134 | 130 | 3 |

## Paired deltas (v5 minus baseline)

| Rep | P50 Δ (µs) | P50 % | P99 Δ (µs) | P99 % | wOPS Δ | s2 Δ | s3 Δ |
|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | +2.5 | +0.16% | +1446.0 | +21.99% | +2 | +112 | -187 |
| 2 | -106.5 | -6.56% | +90.8 | +0.90% | -2 | +118 | -187 |
| 3 | +37.5 | +2.40% | +2165.2 | +27.19% | -4 | +119 | -190 |
| 4 | -70.7 | -4.38% | +630.5 | +7.65% | +0 | +115 | -184 |
| 5 | +129.0 | +8.38% | +4484.8 | +62.34% | -9 | +120 | -189 |

## Summary statistics

| Metric | Mean | Median | Std Dev | Direction |
|---|---:|---:|---:|---|
| P50 delta | -1.6 µs | +2.5 µs | 92.7 µs | 3 worse, 2 better |
| P99 delta | +1763.5 µs | +1446.0 µs | 1713.9 µs | **5 worse, 0 better** |
| wOPS delta | -2.6 | -2.0 | — | 1 increased, 3 decreased, 1 tied |

## WBT classification behavior

**Baseline:** ~192 windows classified as `LAT_UNKNOWN_WRITES` (status=3) per run, ~10 as `LAT_UNKNOWN` (status=2). The controller unconditionally calls `scale_up()` on all ~192 mixed windows, expanding write depth from step 0 to step -3 within the first ~2 seconds.

**v5:** ~125-130 windows classified as `LAT_UNKNOWN` (status=2) per run, ~2-6 as `LAT_UNKNOWN_WRITES` (status=3). The controller does NOT call `scale_up()` on mixed windows with active readers. The ~5 remaining `LAT_UNKNOWN_WRITES` windows occur after readers stop (pure-write phase).

## Assessment

**MIXED RESULTS — no performance improvement established.**

- **P50:** Mixed direction across 5 reps. Mean delta is -1.6 µs (essentially zero). No stable improvement or regression.
- **P99:** v5 is consistently worse — **5 out of 5 repetitions show increased P99 latency** (mean +1763.5 µs, range +90.8 to +4484.8 µs). This is a consistent P99 regression.
- **Writer throughput:** Essentially unchanged (mean -2.6 OPS, within noise).
- **Behavioral correctness:** v5 reclassifies mixed windows as LAT_UNKNOWN as designed (confirmed by tracepoint data: ~125 s2 vs ~192 s3 in baseline).

## Interpretation

The v5 reclassification successfully prevents write-depth expansion during concurrent reader activity. However, this does not translate to a read latency improvement. P50 is unchanged (mixed), and P99 is consistently worse.

Two explanations are consistent with the data:

1. **Holding depth at step 0 keeps more dirty pages in the page cache.** When the writer eventually flushes, the burst is larger, creating longer tail-latency spikes. The baseline's early `scale_up` (within ~2s) allows write depth to expand and drain dirty pages more gradually, smoothing the tail.

2. **The controller's existing LAT_UNKNOWN BUMP mechanism is too coarse.** After 5 consecutive LAT_UNKNOWN windows, the BUMP triggers a return-to-center action. In the baseline, write depth reaches step -3 (normal=48) and stays there, providing consistent write throughput. In v5, the controller oscillates between step 0 and step -1/-2 via the BUMP mechanism, creating more variable write behavior that may worsen tail latency.

## Limitations

- KVM with virtio-scsi, not bare metal. Absolute latencies are not transferable to hardware.
- Single workload profile (sparse writer). Heavy profile not tested.
- 5 repetitions may not be sufficient for robust statistical significance, though the P99 direction is consistent.
- No P95 reported by the read latency probe (it reports P50, P90, P99, MAX).
- Run order alternated but was not fully randomized.

## Decision

**NOT GO for patch submission.** The v5 reclassification changes controller behavior as designed but produces a consistent P99 regression. No performance improvement is established.

The behavioral observation remains valid — `LAT_UNKNOWN_WRITES` does discard read evidence and unconditionally expands write depth. The reclassification approach is architecturally cleaner than the prototype guard. But the performance hypothesis is not supported by this controlled experiment.

The mean-based predicate (proposed in `test_wbt_v5_correctness.py` TC-C12) remains the logical next step for investigation, as it could provide an active throttle response to elevated read latency under sparse writes, rather than merely preventing expansion.
