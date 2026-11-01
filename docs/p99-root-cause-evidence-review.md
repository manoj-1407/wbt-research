# WBT P99 Latency Regression — Root-Cause Evidence Review

**Date:** 2026-11-01
**Status:** Technical Evidence Review & Methodological Analysis
**Repository:** `D:\io_investigation\wbt_patch` (`/mnt/d/io_investigation/wbt_patch`)
**Target Subsystem:** Linux Kernel Block Layer (`block/blk-wbt.c`)
**Base Commit:** `a07f9ea8a99139913acbcc1c160b132cb2a49c81` (WSL2 Kernel 6.6.123.2)
**Patch Commit:** `ebd45314d7e26ddb71110bb4702c8d82925de9b8` (Candidate v5 reclassification)

---

## 1. Executive Summary

In a controlled benchmark of five matched pairs (10 runs total, alternating execution order), the candidate v5 patch succeeded in modifying WBT's classification state machine: it eliminated early write-depth escalation during mixed read/sparse-write windows by returning `LAT_UNKNOWN` rather than `LAT_UNKNOWN_WRITES`.

However, this behavioral change **worsened read P99 tail latency in all five matched pairs** (mean paired delta $+1763.5\ \mu\text{s}$, $+24.0\%$ relative increase).

An initial hypothesis suggested that v5 caused a "delayed writeback burst" that collided with reader tail I/O after the readers finished. **This burst hypothesis was formally falsified in commit `5506376`:** the delayed escalation in v5 does not begin until $t \approx 23.4\text{s}$, whereas readers complete their 20-second workload by $t \le 20.0\text{s}$ (a 3.5-second temporal separation with zero reader requests in flight).

Therefore, **the P99 tail regression occurred during the active reading window ($t \in [3.7\text{s}, 20.0\text{s}]$)**, where:
- **Baseline** operated at `scale_step = -3` (`wb_normal = 48`, `wb_background = 24`, `max_depth = 96`).
- **v5** operated at `scale_step = 0` (`wb_normal = 8`, `wb_background = 4`, `max_depth = 16`).

This review examines why running at lower writeback queue depth (`step = 0`) produced worse read tail latency than running at higher queue depth (`step = -3`), audits the limits of our current logs, and identifies the minimum instrumentation needed to resolve the remaining questions.

---

## 2. Evidence Inventory: Facts, Calculations, Estimates, and Hypotheses

To maintain absolute scientific integrity, all analytical statements are categorized by empirical status:

```
+--------------------------------------------------------------------------------------------------+
| CATEGORY          | DESCRIPTION & ARTIFACT SOURCE                                               |
+-------------------+------------------------------------------------------------------------------+
| Directly Measured | Logged in raw files: Per-reader MIN/P50/P90/P99/MAX/MEAN/SAMPLES;            |
|                   | Writer OPS/BYTES/ITERS/ERR; Blockstat sectors read/written;                  |
|                   | Kernel ftrace timestamps for wbt_timer, wbt_step, wbt_lat.                   |
+-------------------+------------------------------------------------------------------------------+
| Calculated        | Unweighted arithmetic mean of 6 readers' P50/P90/P99 percentiles;            |
|                   | Paired deltas (v5 - baseline); Status code frequency distributions.          |
+-------------------+------------------------------------------------------------------------------+
| Estimated         | Reader lifecycle duration (derived from sample count * (1000us + mean_us)); |
|                   | Reader exit timestamp (~19.6s to 20.0s); Temporal gap to burst (~3.5s to 3.8s)|
+-------------------+------------------------------------------------------------------------------+
| Hypothesized      | Dirty page accumulation; Memory pressure; Virtio queue kick frequency;       |
| (Unproven)        | Block-layer lock contention; Driver thread preemption.                       |
+-------------------+------------------------------------------------------------------------------+
| Falsified         | Post-reader writeback burst collision hypothesis (Commit 5506376).           |
+-------------------+------------------------------------------------------------------------------+
```

---

## 3. Baseline vs. v5 Reconstructed Event Timeline

Below is the time-correlated sequence reconstructed from kernel ftrace timestamps, reader probe logs, and harness shell scripts:

```
Timeline: Baseline vs v5 Lifecycle (t in seconds)

t=0.0s         t~3.7s             t~4.0s                       t~19.8s         t~23.4s        t~26.2s
  |--------------|-------------------|----------------------------|---------------|--------------|
Boot          Harness             Early Scale-up              Readers         Delayed        QEMU
              Launch              (Baseline Only)             Finish          Scale-up       Shutdown
                                                              (Both)          (v5 Only)

[BASELINE]
t = 0.0s  : QEMU boots bzImage, mounts sysfs, tracefs.
t = 3.7s  : Tracing enabled. 1 writer + 6 paced direct readers spawn.
t = 3.74s : First wbt_timer fires. stat[READ] > 0, but stat[WRITE] < 3 (insufficient writes).
            latency_exceeded() returns LAT_UNKNOWN_WRITES (status=3).
t = 3.75s : scale_up() decrements scale_step: 0 -> -1 -> -2 -> -3 by t = 4.07s.
            Write limits expand: normal=48, background=24, max=96.
t = 4.1s-19.8s: Baseline runs 96% of reader duration at step = -3.
            WBT timer fires every 100ms: status=3 (writes sparse), depth maintained at -3.
t = 19.8s : All 6 readers finish their 20s loops, sort local latencies, log PROBE_RESULT.
t = 20.0s : Harness script detects reader exit, signals writer to terminate.
t = 24.1s : Writer terminates, disk unmounted, QEMU powers down cleanly.

[v5 CANDIDATE]
t = 0.0s  : QEMU boots bzImage, mounts sysfs, tracefs.
t = 3.7s  : Tracing enabled. 1 writer + 6 paced direct readers spawn.
t = 3.74s : First wbt_timer fires. stat[READ] > 0, stat[WRITE] < 3.
            v5 guard activates: latency_exceeded() returns LAT_UNKNOWN (status=2).
t = 3.75s-19.8s: scale_up() is INHIBITED.
            wb_timer_fn() receives status=2. Since scale_step == 0, BUMP logic does nothing.
            Write limits held at: normal=8, background=4, max=16 for the entire 16 seconds.
t = 19.8s : All 6 readers finish their 20s loops, sort local latencies, log PROBE_RESULT.
            *** P99 regression (+1763.5 us mean) is ALREADY fully recorded by this point. ***
t = 20.0s : Harness script detects reader exit, signals writer to terminate.
t = 23.3s : Readers have been dead for 3.5 seconds. Read samples drop to 0.
            v5 guard sees !stat[READ].nr_samples -> returns LAT_UNKNOWN_WRITES (status=3).
t = 23.4s : Controller executes delayed scale-up (0 -> -1 -> -2 -> -3).
            During this entire escalation, inflight requests == 0.
t = 24.5s : Writer terminates, disk unmounted, QEMU powers down cleanly.
```

### Critical Timeline Deduction
The P99 regression occurs **strictly during the interval $t \in [3.7\text{s}, 19.8\text{s}]$**. It is not an artifact of post-reader shutdown, late disk flushing, or the delayed scale-up.

---

## 4. Evaluation of Competing Explanations

### Explanation 1: Dirty-Page Accumulation & Memory Pressure
* **Hypothesis:** Under v5, write concurrency was held at `wb_normal = 8` (`wb_background = 4`). Buffered writes dirtying pages in the page cache were throttled, allowing dirty pages to accumulate. This accumulation created page-cache memory pressure or background writeback wakeups that interfered with reader memory allocations.
* **Supporting Evidence:**
  - Write depth was constrained by 6× relative to baseline (8 vs 48).
  - WBT limits background writeback to `wb_background` (4 in v5 vs 24 in baseline) whenever `close_io()` is true (which is continuously true during active reading).
* **Contradicting Evidence & Nuances:**
  - The writer issued only 228 operations of 4 KB across 20 seconds, totaling **912 KB to 933 KB** of data in a **1024 MB RAM** virtual machine (< 0.1% of RAM).
  - Linux default `dirty_background_ratio` is 10% ($\approx 100\text{ MB}$). Background flush daemons (`kworker/flush`) are not typically awakened by memory pressure for $< 1\text{ MB}$ of dirty pages.
  - The writer explicitly invoked `fsync()` after every 4 KB write, forcing direct sync submission (`WB_SYNC_ALL`).
* **Missing Measurements:** `/proc/vmstat` counters (`nr_dirty`, `nr_writeback`, `pgpgin`, `pgpgout`) were not sampled over time.
* **Verdict:** **Unproven / Qualified.** Large-scale dirty-page accumulation is unlikely given the low write volume and synchronous write pattern. However, transient writeback and synchronization effects have not been directly measured.

---

### Explanation 2: Device-Level Writeback Batching & Queue Stalls
* **Hypothesis:** Because the writer called `fsync()` every 80 ms, each write had to be flushed. Under baseline (`wb_normal = 48`, `wb_background = 24`), writeback bios were granted immediate access to the request queue without queuing delay. Under v5 (`wb_background = 4`), if multiple writeback bios were generated during `fsync()`, or if waitqueues were throttled in `wbt_wait()`, the writer experienced wait-queue contention and issued requests in bursty mini-batches, creating transient block-device queuing that delayed concurrent direct reads.
* **Supporting Evidence:**
  - In `v5_rep5`, where P99 was worst ($+4484.8\ \mu\text{s}$), writer throughput dropped to 219 ops (compared to 228 in baseline), suggesting write stalls occurred.
  - In `v5_rep5`, a severe latency stall of **$106,772\ \mu\text{s}$ ($106.8\text{ ms}$)** occurred at $t = 17.681\text{s}$, triggering a `wbt_lat` tracepoint and stepping down the controller.
* **Contradicting Evidence & Nuances:**
  - In other runs (e.g. `v5_rep1`), writer throughput was actually slightly higher (230 vs 228 ops), yet P99 still degraded by $+1446.0\ \mu\text{s}$.
  - Inflight counters recorded at 100 ms timer intervals rarely show queue saturation (mostly inflight=0 or 1).
  - The 106.8 ms stall in `v5_rep5` is a useful observational lead, but without timestamp-correlated block request issue/completion traces, it cannot be definitively attributed to queue-depth restriction.
* **Missing Measurements:** Time spent waiting in `wbt_wait()`, block request queue latency (`block_rq_issue` to `block_rq_complete`).
* **Verdict:** **Plausible Lead (Unconfirmed).** Intermittent synchronization stalls during `fsync` under low queue depth could block device queues temporarily, but causal attribution remains unproven.

---

### Explanation 3: Baseline's Scale-Up Reflects Intentional Kernel Design
* **Hypothesis:** The original behavior (`LAT_UNKNOWN_WRITES -> scale_up`) reflects an intentional controller mechanism designed by block-layer maintainers. When writes are sparse (e.g., 1 write every 80 ms), they may have been assumed to pose no meaningful bandwidth threat to concurrent readers. By allowing write depth to go negative (`scale_step = -3`), the controller increases write throughput.
* **Supporting Evidence:**
  - Source code comment in `block/blk-wbt.c` (lines 450-454):
    ```c
    case LAT_UNKNOWN_WRITES:
        /*
         * We started at the center step, but don't have a valid
         * read/write sample, but we do have writes going on.
         * Allow step to go negative, to increase write perf.
         */
        scale_up(rwb);
        break;
    ```
  - Total `wbt_timer` events were 35% higher in baseline (1,009 vs 656), because baseline processed requests promptly without timer disarming.
* **Contradicting Evidence & Nuances:**
  - While the comment establishes that negative steps are an intentional part of WBT's architectural design, it does **not** establish that every sparse-write workload should scale up, that scaling up is necessarily optimal when reads are active, or that `LAT_UNKNOWN_WRITES` is the intended policy for all mixed workloads.
  - It does not intuitively explain why clearing writes faster would improve *read* tail latency unless the writer's presence at low queue depth creates software lock contention in the block layer.
* **Missing Measurements:** Spinlock contention on `q->queue_lock` or request-queue waitqueues.
* **Verdict:** **Plausible Mechanism, Unproven Rationale.** The code comment confirms negative scaling is intentional, but whether it is the optimal control policy under active reading remains an open question.

---

### Explanation 4: QEMU/KVM Virtualization & Host Threading Noise
* **Hypothesis:** The observed differences are influenced by virtualization scheduling artifacts (virtio-scsi worker threads on the host, vCPU preemption, host page caching, or thread wakeups).
* **Supporting Evidence:**
  - High variance in paired deltas: Rep 2 had an insignificant delta of $+90.8\ \mu\text{s}$ ($+0.9\%$), while Rep 5 had $+4484.8\ \mu\text{s}$ ($+62.3\%$), resulting in $s = 1713.9\ \mu\text{s}$.
  - Both baseline (`baseline_rep4` at $t=5.28\text{s}$, $137.9\text{ ms}$) and v5 (`v5_rep5` at $t=17.68\text{s}$, $106.8\text{ ms}$) experienced 100+ ms outlier stalls, which are typical of hypervisor thread scheduling latency rather than disk mechanics.
* **Contradicting Evidence:**
  - All five pairs consistently degraded in P99 under v5 (5/5 positive deltas). A purely random host noise source would be expected to produce at least one negative delta out of five.
* **Missing Measurements:** Bare-metal testing without KVM; host CPU steal time (`/proc/stat`); host-side `kvm_stat`.
* **Verdict:** **Contributing Factor.** Hypervisor jitter introduces significant noise into the tail distribution, though the directional bias remains consistent.

---

### Explanation 5: Reader Measurement & Aggregation Artifacts
* **Hypothesis:** The P99 metric is computed as the unweighted mean of six independent reader processes' individual 99th percentiles. If one reader thread encounters an outlier stall while five do not, the mean is skewed upward.
* **Supporting Evidence:**
  - The reader probe outputs only summary quantiles, not a raw distribution.
* **Contradicting Evidence:**
  - Detailed inspection of the raw probe data in `independent_audit_summary.json` reveals that **all six reader threads in v5 exhibited elevated P99 values** relative to baseline. In `v5_rep1`, the minimum reader P99 ($7,653\ \mu\text{s}$) was higher than the maximum reader P99 of any reader in `baseline_rep1` ($7,107\ \mu\text{s}$).
* **Verdict:** **Refuted as the primary cause.** The degradation is shared across all concurrent readers, not isolated to a single thread artifact.

---

### Explanation 6: Workload-Specific Non-Saturation
* **Hypothesis:** The regression is specific to this particular artificial workload: sparse buffered writes (80 ms sleep) with concurrent direct readers. Under heavy writeback saturation (e.g. streaming buffered writers with dirty page cache filling), holding queue depth down might successfully prevent writeback from overwhelming direct reads. But under sparse writes, holding queue depth down introduces throttling overhead without any corresponding protection benefit.
* **Supporting Evidence:**
  - The workload was explicitly constructed to test the sparse-write boundary condition (`RWB_MIN_WRITE_SAMPLES = 3`).
* **Contradicting Evidence:**
  - None within the existing dataset, as no heavy writeback tests were executed in the controlled suite.
* **Missing Measurements:** Matched benchmarks under high-throughput writeback (0 ms sleep, multi-threaded buffered writers).
* **Verdict:** **Open Question / High Probability.** WBT was designed to throttle heavy writeback saturation, not sparse intermittent writes.

---

## 5. Summary Evaluation Matrix

| Explanation | Plausibility | Supporting Empirical Data | Key Contradiction or Flaw |
|---|:---:|---|---|
| **1. Dirty-Page Accumulation** | Unproven | Low write queue depth (8 vs 48) | Low write volume (~912 KB); sync write pattern |
| **2. Writeback Batching & Sync Stalls** | Plausible Lead | Intermittent sync stalls & write throughput dips | Causal link unproven without correlated block traces |
| **3. Intentional Controller Mechanism** | **Plausible Mechanism** | Explicit comment confirms negative scaling is intentional | Optimality for mixed read/sparse workloads unproven |
| **4. QEMU / Hypervisor Scheduling Jitter** | Medium | High sample variance ($s = 1713.9\ \mu\text{s}$) | Directional consistency across all 5 pairs |
| **5. Probe Aggregation Artifact** | Low | Mean of percentiles used | All 6 reader threads systematically shifted |
| **6. Sparse-Workload Boundary Sensitivity**| **High** | Workload specifically targets sparse writes | Heavy writeback not yet tested |

---

## 6. Minimum Additional Instrumentation Required

To definitively distinguish between **Explanation 2 (Writeback Batching/Stalls)**, **Explanation 3 (Maintainer Heuristic & Lock Synergy)**, and **Explanation 4 (Hypervisor Jitter)**, the following minimal, tightly scoped instrumentation set is required:

### 1. Per-Request Read Latency Timestamps (Probe-level)
* **Current Limitation:** Current probe outputs one line at the end: `[PROBE_RESULT] ... P99_US=...`. We cannot tell whether the tail latency events occurred in a single cluster (e.g. coinciding with an `fsync`) or were uniformly distributed.
* **Minimal Change:** Modify `read_latency_probe.c` to record any read taking $> 4000\ \mu\text{s}$ (the tail) with an absolute timestamp (`CLOCK_MONOTONIC`).

### 2. Block-Layer Tracepoints (`block:block_rq_issue`, `block:block_rq_complete`)
* **Current Limitation:** Existing trace only records `wbt:wbt_timer` and `wbt:wbt_step`. It does not record individual I/O dispatch or completion.
* **Minimal Change:** Enable `events/block/block_rq_issue/enable` and `events/block/block_rq_complete/enable` in the initramfs tracing configuration. This directly measures queue residency time and whether write requests delay read request completions on the wire.

### 3. Page Cache Dirty Accounting (`/proc/vmstat`)
* **Current Limitation:** Dirty memory volume is inferred from writer bytes, not measured directly.
* **Minimal Change:** A simple background loop logging `grep -E "nr_dirty|nr_writeback" /proc/vmstat` every 500 ms to confirm whether dirty pages ever accumulate.

---

## 7. Root-Cause Confidence Assessment

```
+--------------------------------------------------------------------------------------------------+
| WHAT WE CAN STATE WITH HIGH CONFIDENCE:                                                          |
| 1. v5 successfully alters classification from LAT_UNKNOWN_WRITES to LAT_UNKNOWN.                |
| 2. v5 holds write queue depth at step 0 (normal=8) throughout reader activity.                  |
| 3. Baseline expands write queue depth to step -3 (normal=48) within the first 4 seconds.       |
| 4. v5 worsened read P99 latency in all 5 matched pairs (+1.76 ms mean, +24%).                   |
| 5. The regression occurred DURING the reader workload, NOT in a post-reader burst.              |
+--------------------------------------------------------------------------------------------------+
| WHAT REMAINS UNPROVEN:                                                                           |
| 1. The exact biophysical mechanism linking step 0 to elevated tail latency (e.g. lock           |
|    contention, batching delay, or virtio-scsi ring overhead).                                    |
| 2. Whether this regression manifests on bare-metal enterprise hardware or is magnified by KVM.   |
| 3. Whether v5 behaves beneficially under heavy, saturating writeback workloads.                  |
+--------------------------------------------------------------------------------------------------+
```

---

## 8. Recommended Next Action

1. **Do not create a v6 patch or re-modify kernel source.** Modifying code without knowing why step 0 degraded latency would repeat the mistake of v5.
2. **Preserve this root-cause review alongside the benchmark audit** as a complete, peer-reviewable negative-result study.
3. **Reassess the Maintainer Inquiry (Commit `75bfef8`):** Rather than presenting v5 as a fix, formulate the inquiry as a focused architectural question to Jens Axboe and linux-block:
   > *"Is the scale_up on LAT_UNKNOWN_WRITES during concurrent read activity intentional because sparse writes pose negligible threat to read bandwidth, whereas throttling sparse writes adds unnecessary scheduling friction?"*
   This question directly engages the maintainer's original code comments while backed by our empirical findings.
