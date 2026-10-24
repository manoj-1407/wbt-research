# Linux Block Layer WBT Research: Sparse-Write Classification Investigation

**Subsystem:** Linux Block Layer / Writeback Throttling (`block/blk-wbt.c`)  
**Target Mailing List:** `linux-block@vger.kernel.org`  
**Subsystem Maintainer:** Jens Axboe  
**Reference Sources:**
* Microsoft WSL2 Linux 6.6.y (Pinned Commit: `a07f9ea8a99139913acbcc1c160b132cb2a49c81`)
* Linux Mainline (`git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git`, verified identical in `block/blk-wbt.c:304–471`)

---

## 1. Investigation Overview & Narrow Hypothesis

In Linux's block-layer writeback throttling (WBT), request completion statistics are accumulated over discrete time windows (default: 100 ms) via `blk-stat`. At window expiration, `wb_timer_fn()` calls `latency_exceeded()` to evaluate queue state and scale write depth up or down.

### The Specific Classifier Observation
1. **Sample Validity Requirement (`stat_sample_valid`)**:
   Requires `stat[READ].nr_samples >= 1` and `stat[WRITE].nr_samples >= RWB_MIN_WRITE_SAMPLES` (3 write completions).
2. **Watchdog Check**:
   If an in-flight synchronous read exceeds target latency and zero reads completed (`!stat[READ].nr_samples`), the watchdog returns `LAT_EXCEEDED`. However, if any reads completed (`stat[READ].nr_samples >= 1`), the watchdog yields to completion statistics.
3. **Sparse-Write Branch (`LAT_UNKNOWN_WRITES`)**:
   When completed reads exist but write completions are sparse (1 or 2 completions, `< 3`), `stat_sample_valid()` evaluates to `false`. If writes are in-flight or recently waited, the classifier assumes *"we're only doing writes"* and returns `LAT_UNKNOWN_WRITES`.
4. **Disputed Decision**:
   The ordinary comparison (`stat[READ].min > rwb->min_lat_nsec`) is bypassed. In `wb_timer_fn()`, `LAT_UNKNOWN_WRITES` executes `scale_up(rwb)`, widening write queue depth even though completed reads in that same window may have experienced latency far exceeding the configured target.

---

## 2. Empirical Investigation & Final Research Status

> [!IMPORTANT]
> **Research Decision: REVISE (Not GO / Not STOP)**
> * **Empirical Kernel Validation Completed**: 3 matched repetitions executed in QEMU across an 8-configuration matrix (virtio-scsi deep queue vs IDE QD=1, heavy mixed vs read-dominated sparse).
> * **In-Kernel Observability Verified**: The `wbt_guard` tracepoint confirms the guard executes in-kernel and actively holds depth (276 holds in Run 1, 176 holds in Reps 2 & 3).
> * **Finding**: While the mechanism works as coded, **the performance hypothesis is not supported**. Read latency delta flips sign across runs (+5.13% P50 in Run 1, -0.93% in Rep 2, -1.60% in Rep 3), inert controls flip sign identically, and emulation host noise floor (+7–8% writer OPS) exceeds the observed latency delta.
> * Full reports: [Final Decision](docs/final-decision.md), [Results & Three-Run Audit](docs/results.md), [Limitations](docs/limitations.md), [Three-Run Reconciliation](docs/three-run-reconciliation.md).

---

## 3. Repository Contents

| File / Directory | Type | Purpose |
| :--- | :--- | :--- |
| `0001-wbt-hold-depth-on-LAT_UNKNOWN_WRITES-when-reads-are-elevated.patch` | Kernel Patch | 13-line kernel patch adding guard and `wbt_guard` tracepoint to `block/blk-wbt.c` |
| `test_wbt_state_transition.py` | Unit Tests | Specification test suite verifying 11 state machine invariants and regression edge cases |
| `test_tracer_guard.py` | Unit Tests | Ftrace format regex validation against kernel tracepoint output |
| `scripts/summarize_bench.py` | Analysis | Matrix consolidator generating byte-reproducible comparison matrices from raw logs |
| `scripts/recalculate_all_runs.py` | Audit Script | Independent multi-run recalculation, validity gate checker, and cross-run reconciliation |
| `results/` | Evidence | Committed raw QEMU benchmark logs (`*_qemu.log`), analyses, and matrices for all 3 runs |
| `results/reconciliation_manifest.json` | Manifest | Machine-readable three-run audit manifest with verified GitHub Actions provenance |
| `docs/` | Research Docs | Full documentation: `final-decision.md`, `results.md`, `limitations.md`, `three-run-reconciliation.md` |
| `.github/workflows/` | CI Workflows | `build-wbt-kernels.yml`, `qemu-wbt-smoke-test.yml`, and `verify-evidence.yml` |

---

## 4. Source-Derived Model Trace (5 Cases)

Running `python3 wbt_transition_tracer.py` models the classifier across 5 canonical scenarios:

* **Case 1 (Sparse Writes + Elevated Reads)**: 50 reads (min: 8 ms), 1 write completion. Watchdog=False, SampleValid=False. Classifier returns `LAT_UNKNOWN_WRITES`.
  * *Baseline*: `scale_up()` (`scale_step = -1`, queue widened).
  * *Patched*: `break` / hold depth (`scale_step` unchanged, guard tracepoint `action="hold"`).
* **Case 2 (Sufficient Writes + Elevated Reads)**: 50 reads (min: 8 ms), 5 write completions. Watchdog=False, SampleValid=True. Classifier returns `LAT_EXCEEDED`. Both execute `scale_down(hard=True)`.
* **Case 3 (In-Flight Sync Read Stall)**: 0 completed reads, 1 pending sync read at 15 ms. Watchdog fires. Classifier returns `LAT_EXCEEDED`. Both execute `scale_down(hard=True)`.
* **Case 4 (Pure Streaming Writes)**: 0 reads, 10 write completions. Classifier returns `LAT_UNKNOWN_WRITES`. Both execute `scale_up()`. (Pure write performance preserved).
* **Case 5 (Device Idle / Solitary Read)**: 10 reads (1 ms), 0 writes. Classifier returns `LAT_UNKNOWN`. Both execute `noop`. (Device power-state wakeup safety preserved).

---

## 5. Remote Build & In-Kernel Trace Plan

### Remote Build via GitHub Actions
To protect local laptop resources and disk space, compilation is handled remotely:
1. Workflow: `.github/workflows/build-wbt-kernels.yml`
2. Run via GitHub web UI or CLI:
   ```bash
   gh workflow run build-wbt-kernels.yml --ref main
   ```
3. Artifacts generated:
   * `wbt-baseline-<sha>`: Baseline `bzImage` with `CONFIG_BLK_WBT=y`.
   * `wbt-patched-<sha>`: Patched `bzImage` with guard applied.

### In-Kernel Ftrace Capture Plan
Once the test kernels are built, we observe the actual C controller via native tracepoints:
```bash
# Enable WBT tracepoints
echo 1 > /sys/kernel/tracing/events/wbt/enable

# Capture timer decisions during mixed read/write workload
cat /sys/kernel/tracing/trace_pipe | grep -E 'wbt_timer|wbt_step'
```
* Trace log logs `status` (`1=OK, 2=UNKNOWN, 3=UNKNOWN_WRITES, 4=EXCEEDED`) and `scale_step`.
* Metrics compared:
  * Frequency of `status == 3` events.
  * Read P99 and P99.9 latencies (`fio` direct sync reader).
  * Total write throughput (MB/s).

---

## 6. Upstream Inquiry Draft (`linux-block@vger.kernel.org`)

*Note: To be submitted only after empirical kernel tracing confirms that real mixed workloads trigger this state.*

```text
To: linux-block@vger.kernel.org
Cc: Jens Axboe <axboe@kernel.dk>
Subject: [RFC/Inquiry] blk-wbt: LAT_UNKNOWN_WRITES handling when completed reads are elevated

Hello,

I have been investigating the writeback throttling (WBT) classifier in 
block/blk-wbt.c (verified against both Linux 6.6.y and current mainline master).

In latency_exceeded(), stat_sample_valid() requires both completed reads and 
at least RWB_MIN_WRITE_SAMPLES (3). When a monitoring window contains completed 
reads whose observed minimum latency exceeds rwb->min_lat_nsec, but write 
completions are sparse (1 or 2 completions), stat_sample_valid() evaluates to 
false. 

If writes are in-flight or a wait occurred, latency_exceeded() returns 
LAT_UNKNOWN_WRITES before reaching the stat[READ].min comparison. In wb_timer_fn(), 
LAT_UNKNOWN_WRITES unconditionally calls scale_up(), widening write queue depth 
despite the presence of elevated read completion latency in that window.

I have modeled this state transition and evaluated whether this 
behavior manifests under real workloads (e.g. concurrent sync reads with bursty 
large writes) and whether conservatively holding the current write depth when 
stat[READ].min > rwb->min_lat_nsec improves read tail latency without 
destabilizing write throughput.

Is the unconditional scale_up() in LAT_UNKNOWN_WRITES intentional for sparse 
completions, or are there known constraints around relying on stat[READ].min 
in that branch that I should consider?

Thank you,
G Manoj Kumar
```
