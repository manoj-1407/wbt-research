> [!WARNING]
> **This document is superseded. Its root-cause conclusion is incorrect.**
>
> The delayed post-reader writeback burst hypothesis presented here was subsequently
> tested and empirically falsified in commit `5506376`. The P99 regression was
> measured during active reader execution (t ≤ 20s), not in a post-reader burst
> (t ≈ 23.5s). This document is preserved as historical reasoning — a record of
> the analysis before the falsification — not as an established finding.
>
> The authoritative interpretation is in `docs/p99-root-cause-evidence-review.md`
> and `docs/p99-causation-analysis.md`. The root cause of the P99 regression
> remains unresolved.

---

# Root-Cause Analysis: Why v5 Worsens P99 Latency

**Date:** 2026-11-01
**Method:** Trace timeline analysis of 10 controlled benchmark logs (5 baseline + 5 v5)
**Script:** `scripts/trace_timeline_analysis.py`
**Raw data:** `results/controlled_benchmark/*.log`

---

## 1. The finding

v5 worsens P99 read latency consistently (5/5 repetitions, mean +1763.5 µs).
The trace evidence identifies the mechanism: **delayed write-depth expansion
causes a concentrated writeback burst after readers stop.**

---

## 2. The mechanism (confirmed by trace data)

### Baseline behavior

The baseline kernel classifies nearly all mixed windows as
`LAT_UNKNOWN_WRITES` (status=3, mean 192 per run), which unconditionally
calls `scale_up()`. This causes rapid escalation:

| Rep | First step < 0 | Step -3 reached | Time at step -3 | Time at step 0 |
|---|---|---|---|---|
| 1 | 3.848s | 3.954s | 20.192s | 0.104s |
| 2 | 3.803s | 3.907s | 20.188s | 0.104s |
| 3 | 3.990s | 4.094s | 20.195s | 0.104s |
| 4 | 3.852s | 3.956s | 19.844s | 0.208s |
| 5 | 4.315s | 4.419s | 20.200s | 0.104s |
| **Mean** | **3.962s** | **4.066s** | **20.124s** | **0.125s** |

The baseline reaches `scale_step = -3` (`wb_normal = 48`, `max_depth = 96`)
within ~4 seconds and stays there for ~20 seconds — **96% of the 20s
reader workload runs at maximum write depth**. Dirty pages drain gradually
throughout the run because write queue depth is 6× the default.

### v5 behavior

v5 reclassifies mixed windows as `LAT_UNKNOWN` (status=2, mean ~126 per run),
which calls `noop` (or the BUMP return-to-center mechanism). Write depth stays
at step 0:

| Rep | First step < 0 | Step -3 reached | Time at step -3 | Time at step 0 |
|---|---|---|---|---|
| 1 | 23.530s | 23.635s | 0.729s | 19.578s |
| 2 | 23.604s | 23.708s | 0.728s | 19.636s |
| 3 | 23.421s | (never, min=-2) | 0.000s | 19.880s |
| 4 | 23.552s | 23.656s | 0.832s | 19.491s |
| 5 | 23.410s | 23.514s | 0.520s | 19.295s |
| **Mean** | **23.503s** | — | **0.562s** | **19.576s** |

v5 holds at step 0 (`wb_normal = 8`, `max_depth = 16`) for ~19.6 seconds —
**98% of the reader workload runs at default write depth**. Write depth never
expands during active reader activity.

### The burst

When readers stop (at t ≈ 20s, workload duration ends), the writer continues
alone. Without concurrent reads, the next WBT windows have `r_samples = 0`,
which triggers `LAT_UNKNOWN_WRITES` even under v5's gate. The controller then
rapidly escalates:

- t ≈ 23.3s: step 0 → -1 (normal=16)
- t ≈ 23.4s: step -1 → -2 (normal=31)
- t ≈ 23.5s: step -2 → -3 (normal=48)

This escalation happens in ~0.2 seconds — the same ~0.1s per-step cadence as
the baseline, but compressed because it starts from accumulated state.

The problem: during the ~19.6s at step 0, the buffered writer has been
generating dirty pages at 4 KB per 80 ms (the workload's write rate), but
write depth was only 8 (default). When depth suddenly expands to 48 at t ≈ 23.5s,
the accumulated dirty pages flush in a concentrated burst.

The readers, though their main workload has ended, still have outstanding
I/O from the tail of their 20s window. The writeback burst competes with
these final reads for block-layer resources, producing the P99 spike.

### Evidence summary

| Metric | Baseline (mean of 5) | v5 (mean of 5) | Delta |
|---|---:|---:|---:|
| Time at step 0 (s) | 0.125 | 19.576 | +19.451 |
| Time at step -3 (s) | 20.124 | 0.562 | -19.562 |
| First step < 0 (s) | 3.962 | 23.503 | +19.541 |
| Step -3 reached (s) | 4.066 | ~23.6 | +19.5 |
| Mean P99 (µs) | 8004.6 | 9768.0 | +1763.5 |
| Max MAX (µs) | 107616 | 99662 | -7954 |

The max-MAX values are actually slightly lower in v5 on average (99,662 vs
107,616), but the P99 is consistently higher. This means v5 does not produce
worse single worst-case stalls; it produces a *higher density* of tail-latency
events around the burst at t ≈ 23.5s.

---

## 3. Why the baseline's approach works better here

The baseline's `LAT_UNKNOWN_WRITES → scale_up` behavior, while discarding read
latency evidence, has an unintended benefit: it expands write depth early and
keeps it expanded. This means dirty pages drain continuously at a higher rate
(normal=48 vs 8), preventing accumulation.

When the writer eventually stops or readers finish, the baseline has already
drained most dirty pages. There is no burst because the depth was already
expanded throughout the run.

v5's reclassification prevents this early expansion, which is the intended
behavioral change. But the consequence is that dirty pages accumulate at
step 0 for the entire reader-active period, then flush in a burst when depth
finally expands.

---

## 4. Implication for future work

This finding has a clear implication: **merely preventing write-depth expansion
is not the right intervention.** The controller needs to either:

1. **Expand write depth early but cap it** — allow some expansion (e.g. to step
   -1, normal=16) without going all the way to -3. This would drain dirty pages
   at a moderate rate without the full 6× expansion.

2. **Actively throttle the writer** (scale_down) rather than merely holding depth
   — the mean-based predicate proposal would return `LAT_EXCEEDED` under sparse
   writes, calling `scale_down()` which reduces depth and slows write submission.
   This would drain dirty pages slowly rather than accumulating them.

3. **Drain dirty pages before readers stop** — a flush or sync mechanism that
   prevents accumulation during the LAT_UNKNOWN period.

The mean-based predicate (option 2) remains the most promising direction
because it provides an active response rather than a passive hold. It would
reduce write depth (slowing the writer) when read latency is elevated under
sparse writes, rather than merely preventing expansion and accumulating dirty
pages.

---

## 5. Limitations of this analysis

- The burst hypothesis is supported by step-timeline correlation, not direct
  dirty-page measurement. The kernel does not expose dirty-page counts per
  device in the tracepoint stream. The correlation is strong (time at step 0
  correlates perfectly with delayed escalation) but not a direct measurement.

- The P99 spike occurs at the end of the reader workload, not during it.
  This means v5's behavior during active readers (the ~19.6s at step 0) is
  actually fine for P50 — the regression is specifically in the tail,
  specifically at the end of the run, specifically when the burst happens.

- The workload uses a single writer with an 80ms sleep. A higher write rate
  or multiple writers would accumulate more dirty pages and potentially worsen
  the burst. A lower write rate might not accumulate enough to matter.

- KVM with virtio-scsi, not bare metal. The absolute burst size and its
  impact on P99 may differ on real hardware with different I/O scheduling.
