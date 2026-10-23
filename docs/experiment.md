# Experiment: can a guard on `LAT_UNKNOWN_WRITES` reduce read tail latency?

## 1. Problem

`block/blk-wbt.c` classifies each 100 ms measurement window in
`latency_exceeded()` and acts on the classification in `wb_timer_fn()`.

`stat_sample_valid()` requires **both**:

* at least one completed read (`stat[READ].nr_samples >= 1`), and
* at least `RWB_MIN_WRITE_SAMPLES` (= 3) completed writes.

When a window contains completed reads but fewer than 3 write completions,
`stat_sample_valid()` is false. If writes are also in flight or a wait
occurred recently, `latency_exceeded()` returns **`LAT_UNKNOWN_WRITES`**
(status 3) *before* it ever compares `stat[READ].min` against
`rwb->min_lat_nsec`.

`wb_timer_fn()` then executes `scale_up(rwb)` unconditionally for that
status. `scale_up()` drives `scale_step` negative, and
`rq_depth_calc_max_depth()` turns a negative step into a **larger** allowed
write depth:

```c
else if (rqd->scale_step < 0) {
        unsigned int maxd = 3 * rqd->queue_depth / 4;
        depth = 1 + ((depth - 1) << -rqd->scale_step);
        if (depth > maxd) { depth = maxd; ret = true; }
}
```

So the window in which reads are already slow is precisely the window in
which WBT widens the write queue, adding write-side interference.

## 2. Hypothesis

> In the `LAT_UNKNOWN_WRITES` branch, if completed reads were sampled in the
> same window and their **minimum** observed latency already exceeds the
> configured target, holding the current depth instead of scaling up will
> reduce read tail latency without harming write throughput.

## 3. Implemented guard

The guard is inserted at the top of the `LAT_UNKNOWN_WRITES` case
(`block/blk-wbt.c`), matching
`0001-wbt-hold-depth-on-LAT_UNKNOWN_WRITES-when-reads-are-elevated.patch`:

```c
case LAT_UNKNOWN_WRITES:
        /*
         * Guard: if concurrent reads are sampled in this window
         * and their minimum observed latency already exceeds the
         * configured target (rwb->min_lat_nsec), do not scale up
         * write depth. Hold depth conservatively.
         */
        if (cb->stat[READ].nr_samples &&
            cb->stat[READ].min > rwb->min_lat_nsec) {
                trace_wbt_guard(rwb->rqos.disk->bdi, rqd->scale_step,
                                cb->stat[READ].nr_samples, cb->stat[READ].min,
                                rwb->min_lat_nsec, 1, "hold");
                break;              /* hold depth */
        }

        trace_wbt_guard(rwb->rqos.disk->bdi, rqd->scale_step,
                        cb->stat[READ].nr_samples, cb->stat[READ].min,
                        rwb->min_lat_nsec, 0, "scale_up");
        scale_up(rwb);
        break;
```

A dedicated tracepoint `TRACE_EVENT(wbt_guard)` was added in
`include/trace/events/wbt.h` so that every evaluation is observable
in-kernel, including the case where the guard does **not** fire. Its
`TP_printk` format is:

```text
%s: step=%d, r_samples=%llu, r_min=%lluus, target=%lluus, guard_fired=%d, action=%s
```

`r_min` and `target` are printed via `div_u64(..., 1000)`, i.e. in
microseconds.

### Why the guard can only ever fire in one branch

* `LAT_OK` (1) / `LAT_EXCEEDED` (4) require `stat_sample_valid() == true`,
  so the `LAT_UNKNOWN_WRITES` case is not reached.
* `LAT_UNKNOWN` (2) is the `!stat[READ].nr_samples` case and is untouched.
* Therefore the *only* reachable evaluation point is
  `LAT_UNKNOWN_WRITES`, and the patch modifies exactly that.

### Observability subtlety: `stat[READ].min` when there are no samples

`blk-stat` leaves `stat[READ].min` at `U64_MAX` until a read is recorded.
When `r_samples == 0`, the tracepoint consequently prints
`div_u64(18446744073709551615, 1000) = 18446744073709551` µs. This is
expected output, not corruption: the guard condition short-circuits on
`nr_samples` first, so the comparison is never evaluated. Two of the eight
configurations show this value (see `results.md`).

## 4. What was falsifiable

1. The guard's tracepoint must appear **only** on the patched kernel.
2. `wbt_timer` must be non-zero in every configuration — otherwise the
   workload never armed the WBT window timer and nothing was measured.
3. The guard must fire **only** where a read sample exists with
   `min > target`, and must not fire elsewhere.
4. Where the guard fires, the depth escalation timeline must be delayed
   relative to the matched baseline run.
5. Reader latency must improve, and writer throughput must not collapse.

Point 5 is the hypothesis under test; points 1–4 are mechanism checks and
are reported separately so that a mechanism pass can never be mistaken for
a performance win.
