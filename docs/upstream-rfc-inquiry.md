# Upstream Maintainer Inquiry — Final Draft

> [!IMPORTANT]
> **Pre-send checklist (from final audit, 2026-10-25):**
> 1. Do **not** attach the kernel patch to this inquiry. It carries a placeholder
>    `Signed-off-by: VoxBridge Research <research@voxbridge.local>`. The inquiry
>    can be sent independently; the patch identity must be resolved before any
>    separate patch-submission message.
> 2. Confirm the previously exposed GitHub Actions token has been revoked/rotated
>    (out-of-band action — cannot be verified from repository content).
> 3. Verify `linux-block@vger.kernel.org` and `axboe@kernel.dk` against the
>    current `MAINTAINERS` file before sending.
> 4. The email text below is otherwise verified correct and ready to send.

**Target:** `linux-block@vger.kernel.org`  
**CC:** Jens Axboe `<axboe@kernel.dk>`  
**Subject:** `[RFC/Inquiry] blk-wbt: LAT_UNKNOWN_WRITES handling when completed reads are elevated`  
**Repository & Evidence:** `https://github.com/manoj-1407/wbt-research/tree/research/three-run-reconciliation`

---

## Cover Note

This inquiry is structured as a technical observation and question for maintainers,
not a patch submission. It asks maintainers to clarify the intended control policy,
acceptable evidence requirements, and the appropriate direction for any future
behavioral change — not to evaluate a performance improvement claim or audit the
research repository. The experimental results are reported transparently and do not
establish a consistent read-latency benefit.

---

## Email Text

```text
To: linux-block@vger.kernel.org
Cc: Jens Axboe <axboe@kernel.dk>
Subject: [RFC/Inquiry] blk-wbt: LAT_UNKNOWN_WRITES handling when completed reads are elevated

Hello,

I am investigating the behavior of the Linux block-layer Writeback Throttling
controller (block/blk-wbt.c), specifically the interaction between
LAT_UNKNOWN_WRITES, sparse write completions, and concurrent read latency.

I would appreciate maintainer feedback on whether the behavior described below
is intentional, what assumptions govern this controller state, and whether the
proposed direction for further investigation is appropriate.

This is a research inquiry, not a request to merge the prototype patch. The
experimental results do not establish a consistent read-latency improvement, and
I am not proposing the current implementation as a demonstrated performance fix.


1. Controller behavior under investigation
------------------------------------------

In latency_exceeded(), stat_sample_valid() requires completed read samples and
at least RWB_MIN_WRITE_SAMPLES write completions:

    static inline bool stat_sample_valid(struct blk_rq_stat *stat)
    {
        return (stat[READ].nr_samples >= 1 &&
                stat[WRITE].nr_samples >= RWB_MIN_WRITE_SAMPLES);
    }

When write completions are sparse, this condition is not satisfied.

If write completions exist or the controller recently waited,
latency_exceeded() can return LAT_UNKNOWN_WRITES:

    if (!stat_sample_valid(stat)) {
        if (stat[WRITE].nr_samples || wb_recent_wait(rwb))
            return LAT_UNKNOWN_WRITES;
        return LAT_UNKNOWN;
    }

In wb_timer_fn(), the corresponding state unconditionally calls:

    case LAT_UNKNOWN_WRITES:
        scale_up(rwb);
        break;

Consequently, when the write sample count is insufficient, the controller can
increase write-depth headroom without applying the ordinary read-latency
comparison in that decision path.

The question is whether this behavior is deliberately intended to prioritize
write progress under insufficient write-completion evidence, even when concurrent
reads in the same window exhibit elevated latency.

I am not assuming that the unconditional expansion is a bug. The controller may
intentionally treat sparse write completions as insufficient evidence for
throttling. I would like to understand the intended policy and its constraints.


2. In-kernel observation
-------------------------

I instrumented the relevant state transitions and evaluated mixed I/O consisting of:

  - A sparse buffered writer issuing 4 KiB writes with an 80 ms interval.
  - Concurrent direct-I/O readers issuing random 4 KiB reads.
  - Instrumented WBT timer and scale-step events.
  - A prototype guard tracepoint recording the decision and action.

Across the reported 30-second run, LAT_UNKNOWN_WRITES occurred in 283 of 298
timer windows, approximately 95%.

This establishes that the state was frequently reached under the tested workload.
It does not establish that the workload represents a common production workload or
that the controller's behavior is incorrect.


3. Prototype guard and observed controller behavior
----------------------------------------------------

I implemented a small prototype guard at the beginning of LAT_UNKNOWN_WRITES:

    if (cb->stat[READ].nr_samples &&
        cb->stat[READ].min > rwb->min_lat_nsec)
        break;

The intended behavior was narrowly scoped: when completed reads are present and
the observed minimum read latency exceeds the configured target, hold the current
write-depth setting rather than immediately expanding it.

In the instrumented runs:

  - Run 1 recorded 276 holds out of 283 evaluations.
  - Subsequent repetitions recorded 176 holds per run.
  - The controller's depth escalation was delayed.
  - Sparse-writer throughput was reported as 367 ops/s for both baseline and
    patched execution.

These observations demonstrate that the prototype changes the controller's
expansion decision under the tested condition.

They do not establish that holding depth is the correct policy, that write
starvation cannot occur, or that the guard improves read latency.


4. Experimental outcome
------------------------

The benchmark compared baseline and patched behavior across three repetitions,
including virtio-scsi and IDE configurations.

The reported vio_s (virtio-scsi, sparse-writer) P50 changes were:

  Run 1:  +5.13%
  Run 2:  −0.93%
  Run 3:  −1.60%

The corresponding reported P99 changes were +1.83%, −1.82%, and −1.62%.

The treatment effect changed direction across repetitions. The inert IDE control
also showed run-to-run variation, and the audit identified a substantial
operating-point difference associated with QEMU TCG execution across runs.

The benchmark therefore does not establish a reproducible performance improvement.

The results support reporting the controller behavior separately from the
performance hypothesis: the guard changes expansion behavior, but the current
evidence does not show that this change reliably protects read tail latency.

I do not consider the existing performance results sufficient to justify an
upstream performance claim.


5. Structural limitation of the current predicate
--------------------------------------------------

The prototype uses:

    cb->stat[READ].min > rwb->min_lat_nsec

This measures the minimum completed read latency in the window.

That statistic can be misleading when the latency distribution is degraded but
contains a small number of fast completions.

For example, a window could contain many slow reads and one fast read. The minimum
would still reflect the fast completion, even if the bulk of the read workload
experienced substantial delay.

This limitation was observed in the IDE sparse-workload results: the reported P50
was approximately 540 µs while the observed minimum was approximately 62–77 µs
against a 150 µs target. The guard evaluated 288 times and did not hold.

Thus, the predicate can remain inactive while the broader read-latency distribution
is elevated.

This is not evidence that a mean-based or percentile-based predicate would
necessarily improve the controller. It identifies a limitation of the current
minimum-based prototype.


6. Questions for maintainers
-----------------------------

I would particularly appreciate guidance on the following.


6.1 Intended policy of LAT_UNKNOWN_WRITES

Is the unconditional scale_up() in LAT_UNKNOWN_WRITES deliberately intended to
prioritize write progress whenever write-completion samples are insufficient,
regardless of concurrent read latency?

More specifically, should this state be interpreted as:

  (a) insufficient evidence to make a latency-based throttling decision,
      therefore preserve write progress;
  (b) a distinct controller condition where expansion is intentionally permitted;
  (c) or a condition where concurrent read evidence may still constrain expansion?

I would like to understand the design intent before proposing a behavioral change.


6.2 Appropriate use of concurrent read samples

When completed reads are available in a window but write samples are insufficient
for stat_sample_valid(), is it appropriate for the controller to use those read
observations to constrain write-depth expansion?

If so, are there existing assumptions about sample count, measurement reliability,
or the relationship between read latency and write-depth control that should
govern such a decision?


6.3 Read statistic and predicate design

Would a sample-count-gated read statistic be a reasonable direction to investigate
for this state?

For example, the existing struct blk_rq_stat tracks read sample count and mean
latency. A candidate experiment could evaluate a condition such as:

    stat[READ].nr_samples >= 3 && stat[READ].mean > rwb->min_lat_nsec

This is only a candidate for investigation. I have not established that this
threshold or statistic is appropriate for the controller.

Would maintainers prefer investigation of a mean-based condition, a percentile or
distribution-aware condition, a different statistic, or no additional read-latency
condition in this state?


6.4 Depth hold versus active throttling

If the intended policy is to avoid additional write-depth expansion when read
latency is elevated, would holding the current depth be preferable to actively
reducing depth?

The prototype only prevents expansion. It does not call scale_down() and does not
claim to provide active throttling.

I would appreciate guidance on whether a conservative depth hold is conceptually
appropriate, or whether any intervention should instead use the existing
controller's active scaling policy.


6.5 Required evidence before a behavioral patch

The current experiments are insufficient to support a performance-improvement
claim.

For a follow-up investigation, I am considering:

  - KVM-accelerated or bare-metal execution to avoid the identified TCG
    operating-point confound.
  - Alternating or randomized baseline and treatment order to reduce temporal
    drift.
  - At least five repetitions at a predefined operating point.
  - A predefined treatment acceptance criterion and an inert-control bound.
  - Separate reporting of read latency distributions, writer throughput, fsync
    completion and latency, controller transitions, and guard decisions.
  - A sample-aware predicate experiment that addresses the minimum-latency
    masking limitation.

Would this be an appropriate validation direction for a future behavioral change,
or are there existing WBT testing and evaluation practices that should be
followed instead?


7. Research artifacts
----------------------

The implementation, instrumented traces, three-run reconciliation, regression
checks, and evidence manifest are available here:

  https://github.com/manoj-1407/wbt-research/tree/research/three-run-reconciliation

The repository contains the research results and supporting material, including:

  - The prototype patch and instrumentation.
  - Raw benchmark traces and SHA-256 reconciliation manifest.
  - Three-run result reconciliation.
  - State-transition specification checks.
  - Benchmark recalculation scripts.
  - The documented limitations and final research decision.

The reported audit verified the 24 raw-log hashes against the manifest, reproduced
the published calculations, and passed the 11 state-transition specification checks.

The repository is provided for inspection of the investigation, not as a claim
that the current patch is ready for upstream acceptance.


8. Scope of this inquiry
-------------------------

The current prototype is not being proposed as a performance fix.

The research has established that:

  - LAT_UNKNOWN_WRITES is frequently reached under the tested sparse-write
    workload.
  - The prototype can hold write-depth expansion when its predicate is satisfied.
  - The current minimum-latency predicate can fail to detect a degraded read
    distribution.
  - The measured read-latency changes are inconsistent across repetitions.
  - The available benchmark evidence does not justify claiming improved read tail
    latency.

I have therefore kept the current patch at research status and am seeking
architectural guidance before investing in a predicate redesign or preparing a
behavioral patch for submission.

If there is an existing design rationale, relevant discussion, or preferred
direction for this state that I have overlooked, I would appreciate being pointed
toward it.

Thank you for your time and for maintaining the block layer.

Regards,
Manoj Kumar
```
