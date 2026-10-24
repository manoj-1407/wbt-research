# Upstream RFC & Maintainer Inquiry Draft

**Target:** `linux-block@vger.kernel.org`  
**CC:** Jens Axboe `<axboe@kernel.dk>`  
**Topic:** `block/blk-wbt.c: LAT_UNKNOWN_WRITES` controller decision under concurrent elevated read latency  
**Repository & Reproducible Evidence:** `https://github.com/manoj-1407/wbt-research` (branch `research/three-run-reconciliation`)

---

## Cover Note & Rationale

This inquiry is structured as a technical observation and question for maintainers rather than a patch submission. It reflects the completed empirical audit:
1. It explains the exact classifier mechanism in `block/blk-wbt.c`.
2. It documents empirical confirmation using native tracepoints (`wbt_timer`, `wbt_guard`) showing that `LAT_UNKNOWN_WRITES` does occur frequently under concurrent sparse writes + read traffic.
3. It transparently reports our prototype experiment results: holding write depth did not provide a reproducible read-latency improvement in QEMU, and minimum-latency comparison has a known masking flaw.
4. It asks for architectural guidance: is `LAT_UNKNOWN_WRITES` intentionally biased toward loosening writes without full write samples, or is read-aware throttling in this branch a desirable direction?

---

## Email Text

```text
To: linux-block@vger.kernel.org
Cc: Jens Axboe <axboe@kernel.dk>
Subject: [RFC/Inquiry] blk-wbt: LAT_UNKNOWN_WRITES handling when completed reads are elevated

Hello,

I have been investigating the classification logic and state transitions in the
Linux block-layer Writeback Throttling controller (block/blk-wbt.c, verified on
v6.6.y and mainline).

I would appreciate maintainer insight on the intended behavior of the
LAT_UNKNOWN_WRITES branch when concurrent read latency is elevated.

--- Mechanism Observation ---

In latency_exceeded(), stat_sample_valid() requires both completed reads and at
least RWB_MIN_WRITE_SAMPLES (3):

    static inline bool stat_sample_valid(struct blk_rq_stat *stat)
    {
        return (stat[READ].nr_samples >= 1 &&
                stat[WRITE].nr_samples >= RWB_MIN_WRITE_SAMPLES);
    }

When write completions are sparse (1 or 2 completions in a 100 ms window),
stat_sample_valid() evaluates to false. If writeback is in-flight or recently
waited, latency_exceeded() returns LAT_UNKNOWN_WRITES:

    if (!stat_sample_valid(stat)) {
        if (stat[WRITE].nr_samples || wb_recent_wait(rwb))
            return LAT_UNKNOWN_WRITES;
        return LAT_UNKNOWN;
    }

In wb_timer_fn(), LAT_UNKNOWN_WRITES unconditionally calls scale_up(rwb):

    case LAT_UNKNOWN_WRITES:
        scale_up(rwb);
        break;

This branch is reached even when concurrent reads completed in that exact same
window and experienced latency far exceeding rwb->min_lat_nsec. Because
stat_sample_valid() is false, the controller bypasses the read latency comparison
(stat[READ].min > rwb->min_lat_nsec) and widens write depth (allowing scale_step
to go negative), potentially increasing write pressure at the very moment reads
are struggling.

--- Empirical Investigation & Findings ---

To observe whether this state actually occurs under realistic I/O, we set up
a matched QEMU benchmark with instrumented tracepoints (wbt_timer, wbt_step, and
a custom wbt_guard tracepoint) evaluating virtio-scsi and IDE controllers under
mixed I/O (buffered fsync writes + direct random 4KB reads).

Observations:
1. Frequency: Under a sparse buffered writer (4KB chunk, 80 ms sleep) with
   concurrent readers, LAT_UNKNOWN_WRITES was entered in ~95% of 100 ms windows
   (283 out of 298 timer windows in a 30s run).
2. Prototype Guard: We tested a 13-line guard at the top of LAT_UNKNOWN_WRITES:
   if (cb->stat[READ].nr_samples && cb->stat[READ].min > rwb->min_lat_nsec)
       break; /* hold depth conservatively instead of scale_up */
3. Empirical Outcome:
   - The guard reliably held write depth (276 holds out of 283 evaluations in
     Run 1; 176 holds in subsequent repetitions) and delayed scale_step escalation.
   - Sparse writer throughput was unaffected (held depth did not starve writes).
   - However, read tail latency did NOT show a consistent improvement across
     three repetitions: P50 latency delta was +5.1% in Run 1, -0.9% in Rep 2,
     and -1.6% in Rep 3.
4. Structural Limitation: We observed that testing `stat[READ].min > target`
   is sensitive only to the fastest read in the window. If 99 reads suffer severe
   10 ms queuing delays but a single read completes in 100 µs (below a 150 µs target),
   min latency stays low, the guard fails to hold, and scale_up proceeds.

The full benchmark traces, regression tests, and audit manifest are published for
inspection at:
https://github.com/manoj-1407/wbt-research/tree/research/three-run-reconciliation

--- Questions for Maintainers ---

1. Is the unconditional scale_up() in LAT_UNKNOWN_WRITES deliberately designed to
   prioritize write throughput whenever write sample count falls below 3, regardless
   of concurrent read latency?
2. If preventing write queue expansion during sparse writes with elevated read
   latency is considered worthwhile, what would be the preferred direction:
   (a) Gating scale_up on sample-count-aware read percentiles/distributions rather
       than window minimums?
   (b) An active throttling action (scale_down) rather than a depth hold?
   (c) Leaving the controller as-is because 1–2 write completions are considered
       too noisy for any throttling decision?

Thank you for your time and feedback.

Regards,
Manoj Kumar
```
