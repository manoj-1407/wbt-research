# Linux Kernel WBT Investigation — Final Research Summary & Closure Record

**Date of Closure:** 2026-11-02
**Status:** Concluded — Preserved Negative Result Study (Upstream Patch Submission Rejected)
**Target Subsystem:** Linux Block Layer Writeback Throttling (`block/blk-wbt.c`)
**Repository:** `D:\io_investigation\wbt_patch` (`/mnt/d/io_investigation/wbt_patch`)
**Active Audit Branch:** `audit/upstream-readiness-2026-10-25`
**Base Kernel:** Linux 6.6.123.2 (`a07f9ea8a99139913acbcc1c160b132cb2a49c81`)
**Patch Commit:** `ebd45314d7e26ddb71110bb4702c8d82925de9b8` (`candidate-v5-reclassification`)

---

## 1. Executive Summary

This project investigated the latency classification behavior of the Linux kernel Writeback Throttling (WBT) controller under mixed read and sparse write workloads.

1. **Original Research Concern:** When write completion samples are insufficient ($< 3$ in a sample window), WBT’s `latency_exceeded()` function classifies the window as `LAT_UNKNOWN_WRITES`, prompting `wb_timer_fn()` to invoke `scale_up()`. This expands write queue depth even when concurrent read completions are present in the same window.
2. **The v5 Candidate Patch:** A three-line modification in `block/blk-wbt.c` reclassified windows with active reads and sparse writes as `LAT_UNKNOWN`, preventing automatic scale-up.
3. **Controlled Experimental Benchmark:** A 10-run controlled benchmark (5 alternating baseline/v5 pairs in identical QEMU/KVM virtio-scsi environments) demonstrated that while v5 altered controller state transitions as designed, **it degraded read P99 tail latency in all five matched pairs** (mean increase $+1763.5\ \mu\text{s}$, $+24.0\%$). P50 latency and writer throughput remained essentially flat.
4. **Falsification of Burst Collision:** The initial hypothesis that delayed post-reader writeback bursts caused the regression was empirically disproved (commit `5506376`). Readers finished their workload 3.5 seconds before the delayed escalation began, with zero requests in flight. The regression occurred during the concurrent reading phase.
5. **Final Decision:** **The v5 patch is definitively rejected as an upstream performance optimization.** The investigation is closed without submitting a kernel patch or contacting maintainers, and is preserved as a fully reproducible, methodologically rigorous systems engineering negative result.

---

## 2. Technical Investigation Progression

```
+--------------------------------------------------------------------------------------------------+
| INVESTIGATION STAGE                                            | STATUS   | EVIDENCE / OUTCOME   |
+----------------------------------------------------------------+----------+----------------------+
| 1. Sparse-write classification behavior identified             | Complete | Source audit in      |
|    (stat_sample_valid requires >= 3 writes)                    |          | block/blk-wbt.c      |
+----------------------------------------------------------------+----------+----------------------+
| 2. Development of v5 reclassification candidate                | Complete | 3-line patch in      |
|    (returns LAT_UNKNOWN when reads are active)                 |          | commit ebd45314d     |
+----------------------------------------------------------------+----------+----------------------+
| 3. Kernel build, boot, and tracepoint validation               | Complete | QEMU/KVM 8.2.2 boot; |
|    (verified 130 s2 events vs 0 s3 during reading)             |          | bzImage validated    |
+----------------------------------------------------------------+----------+----------------------+
| 4. Controlled baseline vs. v5 benchmark (5 alternating pairs)  | Complete | P50 flat; wOPS flat; |
|    (10 runs total, fresh disk images, identical initramfs)     |          | P99 worse in 5/5     |
+----------------------------------------------------------------+----------+----------------------+
| 5. Independent clean-room benchmark audit                      | Complete | All 10 raw logs pass;|
|    (SHA-256 verified; clean-room recalculation)                |          | 0 discrepancies      |
+----------------------------------------------------------------+----------+----------------------+
| 6. Temporal causation analysis & burst hypothesis audit        | Complete | Burst hypothesis     |
|    (reader end at t<=20s; scale-up at t~23.4s)                 |          | formally falsified   |
+----------------------------------------------------------------+----------+----------------------+
| 7. Maintainer inquiry reassessment                             | Complete | Inquiry drafted;     |
|    (architectural question evaluated vs. empirical results)    |          | hold recommended     |
+----------------------------------------------------------------+----------+----------------------+
| 8. Final research closure & repository archival                | Complete | Clean documentation; |
|    (negative result preserved; decoupled from RTT research)    |          | investigation closed |
+----------------------------------------------------------------+----------+----------------------+
```

---

## 3. Controlled Benchmark Results & Independent Audit

All ten raw benchmark logs (`results/controlled_benchmark/`) were independently verified by [`scripts/independent_benchmark_audit.py`](file:///d:/io_investigation/wbt_patch/scripts/independent_benchmark_audit.py).

### Summary Metrics Across Five Matched Pairs ($\Delta = \text{v5} - \text{baseline}$)

| Repetition | P50 Delta ($\mu$s) | P50 % | P90 Delta ($\mu$s) | P90 % | P99 Delta ($\mu$s) | P99 % | Writer OPS $\Delta$ | `status=2` $\Delta$ | `status=3` $\Delta$ |
|:---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **Pair 1** | +2.5 | +0.16% | +131.3 | +4.09% | **+1446.0** | +21.99% | +2 | +112 | −187 |
| **Pair 2** | −106.5 | −6.56% | −280.7 | −7.73% | **+90.8** | +0.90% | −2 | +118 | −187 |
| **Pair 3** | +37.5 | +2.40% | +332.0 | +10.42% | **+2165.2** | +27.19% | −4 | +119 | −190 |
| **Pair 4** | −70.7 | −4.38% | −48.2 | −1.45% | **+630.5** | +7.65% | 0 | +115 | −184 |
| **Pair 5** | +129.0 | +8.38% | +669.7 | +21.64% | **+4484.8** | +62.34% | −9 | +120 | −189 |

### Statistical Characterization & Limitations
- **P99 Read Tail Latency:** Mean delta $+1763.5\ \mu\text{s}$ (median $+1446.0\ \mu\text{s}$, $s = 1713.9\ \mu\text{s}$). P99 was higher under v5 in all five paired runs (100% directional concordance).
- **Sign Test Significance:** Because the sample size is small ($n=5$), a two-sided sign test yields $p = 2/32 = 0.0625$, which does not cross the conventional 5% significance threshold, while a one-sided sign test yields $p = 1/32 = 0.03125$.
- **Parametric 95% Confidence Interval:** $[-364.6\ \mu\text{s}, +3891.5\ \mu\text{s}]$ ($t_4 = 2.776$). The interval straddles zero due to sample variance between Rep 2 ($+90.8\ \mu\text{s}$) and Rep 5 ($+4484.8\ \mu\text{s}$).
- **P50 Read Latency:** Mean delta $-1.6\ \mu\text{s}$ (median $+2.5\ \mu\text{s}$, mixed direction: 3 worse, 2 better).
- **Writer Throughput:** Mean delta $-2.6\text{ ops}$ across 20 seconds (flat; $\approx -0.13\text{ ops/s}$).

---

## 4. Root-Cause Analysis Summary

Detailed in [`docs/p99-root-cause-evidence-review.md`](file:///d:/io_investigation/wbt_patch/docs/p99-root-cause-evidence-review.md):

1. **Operating Difference During Reader Activity ($t \in [3.7\text{s}, 20.0\text{s}]$):**
   - **Baseline:** Scaled to `step = -3` within 4 seconds (`wb_normal = 48`, `wb_background = 24`, `max_depth = 96`). Ran at maximum write depth for 96% of the reader duration.
   - **v5:** Held at `step = 0` (`wb_normal = 8`, `wb_background = 4`, `max_depth = 16`). Ran at default write depth for 98% of the reader duration.
2. **Evaluation of Competing Explanations:**
   - *Dirty-Page Accumulation:* Unlikely as a primary cause; only $\approx 912\text{ KB}$ was written total ($< 0.1\%$ of 1 GB RAM). However, transient writeback and synchronization effects cannot be completely ruled out.
   - *Synchronization Stalls:* Plausible observational lead. The writer called `fsync()` after every 4 KB write. Under restricted depth (`wb_background = 4`), synchronous flushes may experience queuing delay. In `v5_rep5`, a $106.8\text{ ms}$ latency stall occurred at $t = 17.68\text{s}$, though direct causal link to queue depth remains unproven without request-correlated block traces.
   - *Intentional Kernel Mechanism:* The source code comment (`/* Allow step to go negative, to increase write perf. */`) confirms that negative steps are an intentional architectural design. However, whether negative scaling is the intended or optimal policy for mixed read/sparse-write workloads remains an open question.
   - *Virtualization Noise:* Host hypervisor scheduling in QEMU/KVM introduces tail latency variance, though directional concordance across all 5 pairs indicates an underlying behavioral difference.

---

## 5. Maintainer Inquiry Reassessment

Detailed in [`docs/maintainer-inquiry-reassessment.md`](file:///d:/io_investigation/wbt_patch/docs/maintainer-inquiry-reassessment.md):

- The original draft in `docs/upstream-rfc-inquiry.md` was audited: obsolete prototype code, falsified burst theories, and premature proposals for a v6 patch were excised.
- A concise, technically rigorous 38-line inquiry was drafted addressing Jens Axboe and `linux-block@vger.kernel.org`.
- **Final Strategic Decision:** **HOLD / DO NOT SEND.** Because our benchmark demonstrated that baseline's early scale-up produced lower tail latency than holding write depth, and because the testing was limited to virtualized virtio-scsi microbenchmarks, approaching maintainers without a working performance improvement or bare-metal enterprise validation is not justified.

---

## 6. Project Artifact Directory & Cross-References

All research outputs are preserved in `D:\io_investigation\wbt_patch` and cross-referenced below:

| Artifact Path | Description |
|---|---|
| [`docs/reproducibility-guide.md`](file:///d:/io_investigation/wbt_patch/docs/reproducibility-guide.md) | Verified step-by-step reproduction instructions, build commands, and execution scripts |
| [`docs/independent-benchmark-validation.md`](file:///d:/io_investigation/wbt_patch/docs/independent-benchmark-validation.md) | Clean-room audit report verifying raw benchmark log integrity and recalculations |
| [`docs/p99-root-cause-evidence-review.md`](file:///d:/io_investigation/wbt_patch/docs/p99-root-cause-evidence-review.md) | Deep technical review of competing causal explanations, timeline correlation, and missing signals |
| [`docs/maintainer-inquiry-reassessment.md`](file:///d:/io_investigation/wbt_patch/docs/maintainer-inquiry-reassessment.md) | Assessment of upstream viability, refined inquiry draft, and hold recommendation |
| [`docs/p99-causation-analysis.md`](file:///d:/io_investigation/wbt_patch/docs/p99-causation-analysis.md) | Formal empirical falsification of the post-reader writeback burst hypothesis (commit `5506376`) |
| [`scripts/independent_benchmark_audit.py`](file:///d:/io_investigation/wbt_patch/scripts/independent_benchmark_audit.py) | Standalone Python validation and statistical analysis script |
| [`scripts/causation_analysis.py`](file:///d:/io_investigation/wbt_patch/scripts/causation_analysis.py) | Timeline correlation script extracting reader duration vs. writeback burst onset |
| [`scripts/run_controlled_benchmark.sh`](file:///d:/io_investigation/wbt_patch/scripts/run_controlled_benchmark.sh) | Automated 10-run QEMU/KVM benchmark runner with alternating execution order |
| [`results/controlled_benchmark/`](file:///d:/io_investigation/wbt_patch/results/controlled_benchmark/) | All 10 raw, untouched benchmark logs (`baseline_rep[1-5].log`, `v5_rep[1-5].log`) |
| [`results/independent_audit/`](file:///d:/io_investigation/wbt_patch/results/independent_audit/) | Machine-readable JSON summary (`independent_audit_summary.json`) of the clean-room audit |

*(Note: The untracked distributed-storage research document `docs/experimental-protocol-rtt-characterization.md` has been verified for SHA-256 integrity and migrated to an independent Git repository at `D:\rtt_storage_research`, fully decoupling the two investigations.)*

---

## 7. Final Research Position & Closure

1. **v5 Patch Status:** Rejected. No kernel patch should be submitted upstream.
2. **Root Cause Status:** Unresolved at the micro-architectural level. Holding write depth at `step = 0` coincides with worse read tail latency during active reading, but the exact mechanism (queuing stalls, lock friction, or hypervisor scheduling) requires sub-millisecond block tracepoints that were not captured.
3. **Investigation Status:** **Formally Closed.** The repository represents a complete, transparent, and reproducible systems engineering negative result.
