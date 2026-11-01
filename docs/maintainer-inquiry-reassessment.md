# WBT Maintainer Inquiry — Post-Benchmark Technical Reassessment

**Date:** 2026-11-02
**Target:** Reassessment of [`docs/upstream-rfc-inquiry.md`](file:///d:/io_investigation/wbt_patch/docs/upstream-rfc-inquiry.md)
**Context:** Completed Independent Benchmark Validation ([`docs/independent-benchmark-validation.md`](file:///d:/io_investigation/wbt_patch/docs/independent-benchmark-validation.md)) and Root-Cause Evidence Review ([`docs/p99-root-cause-evidence-review.md`](file:///d:/io_investigation/wbt_patch/docs/p99-root-cause-evidence-review.md))
**Repository:** `D:\io_investigation\wbt_patch` (`/mnt/d/io_investigation/wbt_patch`)
**Target Subsystem:** Linux Block Layer (`block/blk-wbt.c`)

---

## 1. Executive Summary

The original maintainer inquiry draft in `docs/upstream-rfc-inquiry.md` was drafted before the controlled benchmark audit was completed and before commit `5506376` formally falsified the post-reader burst hypothesis. It mixes discussion of an obsolete downstream prototype guard in `wb_timer_fn()`, speculative claims regarding dirty-page bursts, and premature proposals for a new "mean-based predicate" patch.

This document performs an exhaustive audit of the maintainer inquiry:
1. Strips all invalidated hypotheses (burst collisions, dirty-page accumulation claims, downstream prototype code).
2. Distinguishes confirmed code mechanisms from unproven performance rationale.
3. Incorporates the verified controlled benchmark evidence (consistent P99 regression, flat P50/wOPS).
4. Re-evaluates whether approaching Linux block maintainers (`linux-block@vger.kernel.org`, Jens Axboe) is technically justified.
5. Produces a polished, concise RFC draft (under 45 lines).
6. Delivers a firm recommendation on whether and when to send it.

---

## 2. Audit of Existing Inquiry Claims

| Section in Original Draft | Status | Audit Assessment & Action Required |
|---|:---:|---|
| **Code path description (`stat_sample_valid`, `latency_exceeded`, `wb_timer_fn`)** | **Supported** | **Keep.** The code path analysis is exact: sparse write completions ($< 3$) cause `!stat_sample_valid(stat)`, triggering `LAT_UNKNOWN_WRITES` and `scale_up()`, even when concurrent read samples exist. |
| **Observation of `LAT_UNKNOWN_WRITES` frequency (~95%)** | **Supported** | **Keep.** Verified in ftrace logs: baseline runs ~192 `LAT_UNKNOWN_WRITES` out of ~202 windows. |
| **Discussion of prototype guard (`stat[READ].min > target`)** | **Obsolete** | **Remove completely.** The prototype guard was superseded by the v5 classification approach. Maintainers should not be burdened with retired prototypes. |
| **Post-reader writeback burst explanation** | **Falsified** | **Remove completely.** Commit `5506376` proved that the delayed scale-up occurs 3.5 seconds after readers terminate, with zero in-flight requests. Attributing the regression to a burst is empirically false. |
| **Dirty-page accumulation claims** | **Unsupported** | **Remove.** Total writes across 20 seconds were $\approx 912\text{ KB}$ ($< 0.1\%$ of 1 GB RAM). Large-scale accumulation is physically impossible under this volume and pattern. |
| **BUMP oscillation theory** | **Unsupported** | **Remove.** Trace data shows that at `scale_step = 0`, the BUMP logic is a no-op. Depth remained stable at step 0; it did not oscillate. |
| **Proposal of a "mean-based predicate" (v6 candidate)** | **Premature** | **Remove.** Proposing another patch before understanding why holding depth worsened P99 violates engineering rigor. Maintainers do not want speculative patches. |

---

## 3. Intentional Controller Mechanism vs. Workload-Specific Optimality

A central conceptual error in early analysis was confusing the **existence of a mechanism** with its **performance rationale**:

### A. What is Intentionally Designed
In `block/blk-wbt.c`, the controller explicitly supports negative `scale_step` values (0 down to -3). When `scale_step < 0`:
- `wb_normal` expands from 8 up to 48.
- `wb_background` expands from 4 up to 24.
- `max_depth` expands from 16 up to 96.
- The source comment states: `/* Allow step to go negative, to increase write perf. */`.

This proves that negative scaling is an intentional capability designed to maximize write performance when throttling is deemed unnecessary.

### B. What Remains an Open Architectural Question
When a sampling window contains **both active direct reads and sparse writes**:
- Did maintainers intend for `LAT_UNKNOWN_WRITES` to unconditionally scale up write depth even in the presence of concurrent reads, on the principle that non-saturating writes (e.g. 1 IO per 80ms) cannot meaningfully harm read bandwidth?
- Or was `LAT_UNKNOWN_WRITES` intended primarily for pure-write phases, with mixed sparse-write windows representing an unhandled edge case in sample classification?

Our benchmark demonstrated that forcing the controller to stay at `step = 0` (v5) resulted in higher read P99 latency in all five paired runs than letting it expand to `step = -3` (baseline). While this empirical result does not prove that early scale-up is optimal or that it causes no contention, it demonstrates that simply holding write depth at `step = 0` does not improve read latency under this workload profile.

---

## 4. Honest Summary of the Benchmark Evidence

Any technical communication with maintainers must present our findings with complete transparency:

1. **Experimental Setup:** QEMU/KVM 8.2.2, WSL2 Kernel 6.6.123.2, virtio-scsi (`num_queues=1`), 1024 MB RAM, 128 MB disk image. Workload: 1 buffered writer (4KB writes every 80ms + fsync) and 6 direct-I/O readers (4KB random, 1ms pacing) for 20 seconds.
2. **Behavioral Result:** v5 successfully reclassified mixed windows to `LAT_UNKNOWN` (mean 126.8 per run vs 10.0 in baseline), preventing early scale-up and holding write depth at `step = 0` for 98% of the workload.
3. **Performance Result:**
   - **P50 read latency:** Flat (mean delta $-1.6\ \mu\text{s}$, median $+2.5\ \mu\text{s}$, mixed direction).
   - **Writer throughput:** Flat (mean delta $-2.6\text{ ops}$ over 20s, or $-0.13\text{ ops/s}$).
   - **P99 read latency:** Worse in all 5 matched pairs (mean delta $+1763.5\ \mu\text{s}$, $+24.0\%$ relative increase).
4. **Statistical Bounds:** While directionally consistent across all 5 pairs, a two-tailed sign test yields $p = 2/32 = 0.0625$, which does not meet the conventional 5% significance threshold. Parametric 95% confidence interval is $[-364.6\ \mu\text{s}, +3891.5\ \mu\text{s}]$ due to small sample size and high variance.
5. **Engineering Conclusion:** The v5 reclassification patch fails to demonstrate an I/O performance benefit and is rejected.

---

## 5. Revised Maintainer Inquiry Draft

If an inquiry is sent, it must be concise, technically exact, respectful of maintainer time, and focused solely on architectural intent.

```text
To: linux-block@vger.kernel.org
Cc: Jens Axboe <axboe@kernel.dk>
Subject: [Question] blk-wbt: Design intent of LAT_UNKNOWN_WRITES during concurrent reads

Hello,

I have been studying the latency classification logic in block/blk-wbt.c and would
appreciate clarification on the intended design behavior of LAT_UNKNOWN_WRITES.

In latency_exceeded(), stat_sample_valid() requires at least RWB_MIN_WRITE_SAMPLES (3)
write completions. When writes are sparse but completed reads exist:

    if (!stat_sample_valid(stat)) {
        if (stat[WRITE].nr_samples || wb_recent_wait(rwb) || wbt_inflight(rwb))
            return LAT_UNKNOWN_WRITES;
        return LAT_UNKNOWN;
    }

In wb_timer_fn(), LAT_UNKNOWN_WRITES unconditionally calls scale_up(rwb), decrementing
scale_step down to -3 (expanding write depth from normal=8 to normal=48):

    case LAT_UNKNOWN_WRITES:
        /*
         * We started at the center step, but don't have a valid
         * read/write sample, but we do have writes going on.
         * Allow step to go negative, to increase write perf.
         */
        scale_up(rwb);
        break;

Under a synthetic workload with 6 active direct-I/O readers and 1 sparse buffered writer
(4KB write every 80ms + fsync), this causes the controller to expand to step -3 within
4 seconds and stay there throughout reader activity (~95% of windows).

We tested reclassifying mixed windows with active reads to LAT_UNKNOWN to hold write
depth at step 0 (normal=8). In a controlled 5-pair benchmark, holding write depth did
not improve read latency; P99 read latency was higher in all five paired runs, with a
mean paired increase of approximately 1.76 ms. The sample is small (n=5), and the
two-sided sign test (p=0.0625) does not reach the conventional 5% significance threshold.
P50 read latency and writer OPS were essentially flat.

My question for maintainers:
Is LAT_UNKNOWN_WRITES intentionally designed to expand write depth even when concurrent
reads are present, under the assumption that sparse write completions do not warrant
throttling? Or was this state primarily intended for pure-write workloads?

Thank you for your insights.

Regards,
Manoj Kumar
```

---

## 6. Strategic Evaluation: Should We Send the Inquiry?

### Arguments in Favor of Sending:
1. **Technically Honest:** The draft does not claim a bug, does not ask maintainers to review code, and openly discloses that our attempt to hold queue depth degraded P99 tail latency.
2. **Directly Addresses Maintainer Comments:** It engages directly with the comment in `blk-wbt.c` and offers empirical corroboration that maintainers' heuristic may be performing better than our conservative alternative.
3. **Legitimate Upstream Interaction:** Asking for architectural clarification on subsystem intent is standard practice on `linux-block`.

### Arguments Against Sending (Critical Constraints):
1. **Virtualization-Only Evidence:** All empirical data comes from QEMU/KVM over virtio-scsi on a developer workstation. We have not validated this on bare-metal enterprise hardware (e.g., NVMe or SATA SSD). Maintainers are often skeptical of latency anomalies observed exclusively under virtualized virtio devices.
2. **Artificial Workload:** The workload (1 writer sleeping 80ms vs 6 paced direct readers) is an artificial microbenchmark constructed specifically to hit the boundary condition `stat[WRITE] < 3`. Maintainers may view this as an uninteresting corner case.
3. **No Actionable Patch:** Because our patch was rejected, sending an inquiry provides no immediate code contribution to the kernel. Maintainers have limited time and prioritize fixing active bugs or reviewing working patches.

---

## 7. Final Recommendation

### **RECOMMENDATION: HOLD THE INQUIRY. DO NOT SEND AT THIS TIME.**

**Rationale:**
1. We have successfully determined that WBT's existing behavior is **not demonstrably broken**—in fact, our attempt to "fix" it degraded tail latency.
2. Asking maintainers whether their existing code was intentional when our own tests suggest their code produces better tail latency than the alternative is unlikely to lead to actionable upstream changes.
3. The most valuable outcome of this research is as a **reproducible systems engineering study of writeback throttling dynamics and negative results**, rather than an upstream patch.

### Next Recommended Step:
Proceed to **Task 5: Final Closure & Research Archival**:
- Archive the research repository cleanly with the complete audit trail, reproducible benchmarks, and root-cause analysis.
- Separate the untracked RTT/distributed storage protocol into its own independent workspace.
- Close the WBT investigation with high technical integrity.
