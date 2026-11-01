# P99 Causation Analysis: Burst Hypothesis Falsified

**Date:** 2026-11-01
**Correction to:** `docs/p99-root-cause-analysis.md`

## The burst hypothesis was wrong

The initial root-cause analysis proposed that v5 worsens P99 because of a
delayed writeback burst after readers stop. The causation analysis disproves
this: **the burst does not overlap with reader measurements.**

## Evidence

For all 5 v5 runs, reader estimated end times and burst start times:

| Rep | Reader end (est) | Burst start | Gap (s) |
|---|---:|---:|---:|
| 1 | 19.68s | 23.43s | 3.74 |
| 2 | 20.01s | 23.50s | 3.49 |
| 3 | 19.83s | 23.32s | 3.48 |
| 4 | 19.92s | 23.45s | 3.53 |
| 5 | 19.52s | 23.31s | 3.78 |

Reader end estimates are computed from: first_wbt_timer + (samples × (pacing + mean_latency) / 1e6). Each reader completes ~5200 samples at ~2000 µs mean with 1000 µs pacing, giving ~16s of measurement time starting at ~3.7s.

The burst (first `LAT_UNKNOWN_WRITES` after the reader-active period) occurs
at t ≈ 23.3-23.5s, which is 3.5-3.8 seconds after all readers have finished.
**No reader I/O is in flight during the burst** (inflight=0 in all v5 runs
throughout the burst period).

## What this means

The P99 regression is NOT caused by a post-reader writeback burst competing
with reader tail I/O. The P99 regression occurs during the reader-active
period itself, when v5 holds write depth at step 0.

## Revised hypothesis

The P99 regression likely stems from the difference in write-depth behavior
during the reader-active period:

- **Baseline:** write depth expands to step -3 (normal=48, max=96) within ~4s
  and stays there. Writeback is aggressive throughout — dirty pages drain
  continuously at 6× the default rate.
- **v5:** write depth stays at step 0 (normal=8, max=16) for ~19.6s.
  Writeback is throttled — dirty pages accumulate in the page cache
  throughout the reader-active period.

The accumulation of dirty pages during reader activity may cause:

1. **Memory pressure:** more dirty pages in the page cache reduce available
   memory for read I/O buffers, potentially increasing read latency.
2. **Block-layer contention:** even at low write depth, the accumulated dirty
   pages may trigger periodic flushes that compete with reader I/O at the
   block layer.
3. **Write-back cache interference:** the virtio-scsi backend may service
   writeback requests from accumulated dirty pages ahead of reader requests.

## What the evidence does NOT yet establish

- Whether dirty-page accumulation is the actual cause (no direct dirty-page
  measurement is available in the trace data).
- Whether the P99 outliers occur at specific timestamps during the
  reader-active period or are uniformly distributed.
- Whether memory pressure or block-layer contention is the mechanism.

## Next steps for investigation

To confirm the revised hypothesis, we would need:

1. **Per-request latency timestamps:** The current read probe reports
   aggregated percentiles, not individual request timestamps. A modified probe
   that logs each request's start/end time would allow correlating P99
   outliers with specific WBT state transitions.

2. **Dirty-page monitoring:** `/proc/vmstat` (nr_dirty, nr_writeback) sampled
   during the workload would show whether dirty pages accumulate differently
   between baseline and v5.

3. **Block-layer I/O profiling:** `blktrace` or `iostat` would show whether
   write I/O competes with read I/O differently at step 0 vs step -3.

## Status

- **v5 behavioral change:** demonstrated (trace evidence)
- **v5 does not improve read latency:** demonstrated (controlled benchmark)
- **v5 P99 regression:** demonstrated (5/5 runs worse)
- **Post-reader burst hypothesis:** falsified (no temporal overlap)
- **Dirty-page accumulation during reader activity:** plausible, not yet confirmed
- **Root cause:** not yet established

The investigation should not claim the burst mechanism is the root cause.
The correct statement is: v5 holds write depth at default during reader
activity, which is associated with worse P99, but the specific mechanism
(memory pressure, block-layer contention, or something else) is not yet
identified.
