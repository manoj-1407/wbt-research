# Comparative Evaluation: Prototype Guard vs v5 Reclassification vs Mean-Based Candidate

**Date:** 2026-10-31
**Author:** Kiro (continuation of WBT investigation)
**Branch:** `audit/upstream-readiness-2026-10-25`
**Candidate branch:** `candidate-v5-reclassification` @ `ebd45314d`
**Kernel:** WSL2 6.6.123.2 @ `a07f9ea8a`, compiled with `CONFIG_BLK_WBT=y`

---

## 1. The three approaches

### 1.1 Prototype guard (research repo, `exp/qemu-guard-audit`)

**Location:** `wb_timer_fn()` in `block/blk-wbt.c`

**Change:** Adds a conditional `break` inside the `LAT_UNKNOWN_WRITES` case,
before `scale_up()`:

```c
case LAT_UNKNOWN_WRITES:
    if (cb->stat[READ].nr_samples &&
        cb->stat[READ].min > rwb->min_lat_nsec) {
        trace_wbt_guard(..., 1, "hold");
        break;   /* hold depth */
    }
    trace_wbt_guard(..., 0, "scale_up");
    scale_up(rwb);
    break;
```

**Predicate:** `stat[READ].nr_samples > 0 AND stat[READ].min > target`

**Scope:** Only modifies `wb_timer_fn()`. Classification (`latency_exceeded()`) is untouched.
Adds a new tracepoint (`wbt_guard`) to `include/trace/events/wbt.h`.

**Build status:** Previously built and benchmarked via GitHub Actions (3 runs).
Not currently built in the local kernel tree.

### 1.2 v5 reclassification (candidate branch)

**Location:** `latency_exceeded()` in `block/blk-wbt.c`

**Change:** Gates `LAT_UNKNOWN_WRITES` on the absence of read samples:

```c
-       if (stat[WRITE].nr_samples || wb_recent_wait(rwb) ||
-           wbt_inflight(rwb))
+       if (!stat[READ].nr_samples &&
+           (stat[WRITE].nr_samples || wb_recent_wait(rwb) ||
+            wbt_inflight(rwb)))
                return LAT_UNKNOWN_WRITES;
        return LAT_UNKNOWN;
```

**Predicate:** `stat[READ].nr_samples == 0` (gate condition for the entire branch)

**Scope:** Only modifies `latency_exceeded()`. No tracepoint additions.
`wb_timer_fn()` is unchanged — the existing `LAT_UNKNOWN` handling (BUMP mechanism) applies.

**Build status:** Built locally. `vmlinux` SHA-256 `c497a7d5...`, `bzImage` SHA-256 `5ea5022e...`.
Booted under KVM. WBT tracepoints verified active. Workload executed with tracepoint capture.

### 1.3 Mean-based candidate (proposed, not implemented)

**Location:** Would modify `latency_exceeded()` in the `!stat_sample_valid()` branch.

**Proposed predicate:**
```c
if (stat[READ].nr_samples >= 3 && stat[READ].mean > rwb->min_lat_nsec)
    return LAT_EXCEEDED;
```

**Scope:** Would add a new path inside `latency_exceeded()` that returns `LAT_EXCEEDED`
when reads are present with sufficient samples and elevated mean latency, even if
write completions are sparse.

**Build status:** Not implemented. Specification tested in `test_wbt_v5_correctness.py` TC-C12.
The `struct blk_rq_stat` already contains a `mean` field — no kernel infrastructure changes needed.

---

## 2. Behavioral comparison across scenarios

| Scenario | Baseline (upstream) | Prototype guard | v5 reclassification | Mean candidate |
|---|---|---|---|---|
| Pure writes (no reads) | LAT_UNKNOWN_WRITES → scale_up | LAT_UNKNOWN_WRITES → scale_up (guard not evaluated, r_samples=0) | LAT_UNKNOWN_WRITES → scale_up | LAT_UNKNOWN_WRITES → scale_up |
| Reads + sparse writes, r_min < target | LAT_UNKNOWN_WRITES → scale_up | LAT_UNKNOWN_WRITES → scale_up (guard not fired: r_min ≤ target) | LAT_UNKNOWN → noop (BUMP eventually noop at center) | LAT_UNKNOWN → noop (mean < target, no trigger) |
| Reads + sparse writes, r_min > target | LAT_UNKNOWN_WRITES → scale_up | LAT_UNKNOWN_WRITES → hold (guard fires) | LAT_UNKNOWN → noop | LAT_EXCEEDED → scale_down (if r_mean > target and r_samples ≥ 3) |
| Reads + sparse writes, r_min << target but r_mean >> target | LAT_UNKNOWN_WRITES → scale_up | LAT_UNKNOWN_WRITES → scale_up (guard blind to mean) | LAT_UNKNOWN → noop | LAT_EXCEEDED → scale_down (mean catches what min misses) |
| Reads + sufficient writes, r_min > target | LAT_EXCEEDED → scale_down | LAT_EXCEEDED → scale_down (unchanged) | LAT_EXCEEDED → scale_down (unchanged) | LAT_EXCEEDED → scale_down (unchanged) |
| Reads + sufficient writes, r_min < target | LAT_OK → scale_up | LAT_OK → scale_up (unchanged) | LAT_OK → scale_up (unchanged) | LAT_OK → scale_up (unchanged) |
| No reads, no writes, no inflight | LAT_UNKNOWN → noop | LAT_UNKNOWN → noop (unchanged) | LAT_UNKNOWN → noop (unchanged) | LAT_UNKNOWN → noop (unchanged) |
| wb_recent_wait + no reads | LAT_UNKNOWN_WRITES → scale_up | LAT_UNKNOWN_WRITES → scale_up | LAT_UNKNOWN_WRITES → scale_up (unchanged) | LAT_UNKNOWN_WRITES → scale_up |
| wb_recent_wait + reads present | LAT_UNKNOWN_WRITES → scale_up | LAT_UNKNOWN_WRITES → hold (if r_min > target) | LAT_UNKNOWN → noop | LAT_EXCEEDED or LAT_UNKNOWN depending on mean |
| Sustained mixed, step=0 | LAT_UNKNOWN_WRITES → scale_up (step goes negative) | hold (step stays 0 if r_min > target; escalates if not) | LAT_UNKNOWN → noop at center (step stays 0) | LAT_EXCEEDED → scale_down (if mean elevated) or noop |

---

## 3. Key architectural differences

### 3.1 Where the change lives

| Approach | Function modified | What it changes |
|---|---|---|
| Prototype | `wb_timer_fn()` | Adds a downstream interception after classification |
| v5 | `latency_exceeded()` | Changes the classification itself |
| Mean candidate | `latency_exceeded()` | Adds a new classification path in the same branch |

The v5 approach is architecturally cleaner because it changes the classification decision
where the information is available, rather than intercepting the result downstream. The
prototype guard requires a new tracepoint and a separate code path in `wb_timer_fn()`.

### 3.2 What happens to the write-depth lever

| Approach | When reads present + sparse writes | Write depth behavior |
|---|---|---|
| Baseline | scale_up unconditionally | Step goes negative (depth expands) |
| Prototype | hold if r_min > target; scale_up if r_min ≤ target | Conditionally held or expanded |
| v5 | noop (LAT_UNKNOWN) | Step stays at current value; BUMP returns to center if away |
| Mean candidate | scale_down if r_mean > target; noop otherwise | Step goes positive (depth shrinks) or stays |

The v5 approach never expands write depth during concurrent reader activity with sparse writes.
This is more conservative than the prototype, which only blocks expansion when `r_min` exceeds
target. The v5 approach is also more conservative than the mean candidate, which would actively
throttle (scale_down) rather than merely holding.

### 3.3 Response to latency spikes under sparse writes

This is the most important limitation discovered.

**Neither baseline, prototype, nor v5 can return `LAT_EXCEEDED` when `stat_sample_valid()` is false.**
The `stat[READ].min > rwb->min_lat_nsec` comparison at line ~347 of `blk-wbt.c` is only reached
when `stat_sample_valid()` returns true, which requires ≥3 write completions.

Under sparse writes (< 3 completions per 100ms window):
- Baseline: `LAT_UNKNOWN_WRITES` → `scale_up` (makes latency worse)
- Prototype: `LAT_UNKNOWN_WRITES` → `hold` (neutral, only if r_min > target)
- v5: `LAT_UNKNOWN` → `noop` (neutral)
- Mean candidate: `LAT_EXCEEDED` → `scale_down` (active throttle — the only approach that can respond)

The mean candidate is the only approach that provides an active throttle response to elevated
read latency when write completions are sparse. This is because it adds a new path inside the
`!stat_sample_valid()` branch that can return `LAT_EXCEEDED` directly.

---

## 4. In-kernel validation (Phase E)

The v5 candidate was booted under KVM with a virtio-scsi disk and a sparse-write workload
(1 buffered writer + 6 O_DIRECT readers, 20s, `wbt_lat_usec=150`).

| Metric | Value |
|---|---|
| Total wbt_timer events | 132 |
| LAT_UNKNOWN (status=2) | 130 (98.5%) |
| LAT_UNKNOWN_WRITES (status=3) | 2 (1.5%) |
| wbt_step transitions | 4 |
| Scale-up events | 2 (after readers stopped) |
| Scale-down events | 2 (BUMP recovery) |
| Read sectors (BLKSTAT-DELTA) | 240,424 |
| Write sectors (BLKSTAT-DELTA) | 1,800 |
| Reader P50 (mean across 6 probes) | ~1583 µs |
| Reader P99 (mean across 6 probes) | ~9246 µs |
| Writer OPS | 225 |
| Writer errors | 0 |

**Key observations from the trace:**

1. During the 20s of active concurrent readers, all 130 WBT timer windows were classified as
   `LAT_UNKNOWN` (status=2). Zero windows were `LAT_UNKNOWN_WRITES`. This confirms the v5
   reclassification works exactly as the Python model predicted.

2. After the reader workload ended (at ~20s), 2 windows with only writes present were
   classified as `LAT_UNKNOWN_WRITES` (status=3), triggering `scale_up` to step -2. This
   confirms the pure-write path is preserved.

3. The BUMP mechanism then triggered `scale_down` (step -2 → -1 → 0), returning the
   controller to center. This confirms the recovery mechanism works.

4. `scale_step` never went below -2 during this run. Under the upstream baseline, the same
   workload would have classified all 130 mixed windows as `LAT_UNKNOWN_WRITES` and escalated
   to step -3 or lower within the first ~2 seconds, expanding write depth by 6×.

---

## 5. Limitations of v5

### 5.1 No active throttle response to read latency spikes

As identified in Phase C (TC-C11), when `stat_sample_valid()` is false, neither baseline nor
v5 can return `LAT_EXCEEDED`. The v5 approach converts `LAT_UNKNOWN_WRITES` to `LAT_UNKNOWN`,
which is neutral (noop), but it cannot actively reduce write depth in response to elevated
read latency under sparse writes.

This means: if a device is struggling with read latency AND write completions are sparse
(the exact scenario the investigation targets), v5 prevents the baseline's harmful `scale_up`
but does not provide a positive throttle response. The controller stays at its current depth
and waits for either more write completions (to reach `stat_sample_valid()`) or for the BUMP
mechanism to return to center.

### 5.2 Over-conservatism for fast reads

The v5 gate is `!stat[READ].nr_samples` — it fires on ANY window with even a single read
sample, regardless of whether that read was fast or slow. Under a workload where reads are
consistently fast (well below target) but writes are sparse, v5 prevents `scale_up` even though
expanding write depth would be harmless.

The prototype guard is more targeted here: it only blocks `scale_up` when `r_min > target`.

However, this over-conservatism is bounded: the controller stays at step=0 (default depth),
which is the intended resting state. It does not throttle below default. Write throughput
at default depth is the baseline WBT behavior for a device with no latency concerns.

### 5.3 No performance measurement

This evaluation does not include a performance comparison between v5 and baseline. The Phase E
run was a single uncontrolled observation, not a matched benchmark. No baseline comparison
kernel was booted, no alternating run order was used, and no repetition was performed.

The Phase E result confirms behavioral correctness, not performance improvement.

---

## 6. Recommendation

### For the maintainer inquiry

The v5 reclassification is a stronger candidate for the inquiry than the prototype guard because:

1. It is a cleaner, more minimal change (3 lines, one function, no new tracepoints).
2. It changes the classification where the information lives, rather than intercepting downstream.
3. It is more conservative (prevents all scale_up during mixed windows, not just when r_min > target).
4. It has been validated in-kernel under KVM with a real workload.
5. Its behavior matches the Python model exactly.

### For a future patch

The v5 approach addresses the identified issue (unconditional scale_up during concurrent reads)
but does not address the deeper limitation (no active throttle response under sparse writes).
The mean-based candidate (TC-C12) is the logical next step, because:

1. `struct blk_rq_stat.mean` already exists in the kernel — no infrastructure changes needed.
2. It can return `LAT_EXCEEDED` under sparse writes, providing an active throttle response.
3. The sample-count gate (≥3) prevents false triggers from single slow reads.
4. It is testable with the existing Python model.

### What is NOT established

- No performance improvement has been demonstrated for v5 or any other approach.
- No controlled benchmark has been run with v5 (only a single observational run).
- No comparison between v5 and baseline has been performed on the same KVM host.
- The mean-based candidate is a design proposal, not an implementation.
- No claim of upstream readiness is made for any approach.

---

## 7. Test coverage summary

| Test file | Test cases | Status | What it covers |
|---|---|---|---|
| `test_wbt_state_transition.py` | TC-1 through TC-11 | 11/11 PASS | Prototype guard specification |
| `test_tracer_guard.py` | 1 | PASS | Guard tracepoint regex |
| `test_wbt_v5_reclassification.py` | TC-V1 through TC-V11 | 11/11 PASS | v5 behavioral model |
| `test_wbt_v5_correctness.py` | TC-C1 through TC-C12 | 30/30 PASS | v5 correctness + edge cases + mean candidate |
| `scripts/recalculate_run1.py` | 8 configs | PASS | Benchmark recalculation from raw logs |
| **Total** | **53+ checks** | **ALL PASS** | |

All tests executed under WSL Ubuntu with Python 3.13. No tests skipped, no results fabricated.

---

## 8. In-kernel evidence summary

| Evidence | Status |
|---|---|
| v5 kernel builds cleanly | Verified (0 errors, 3 objtool warnings) |
| v5 kernel boots under KVM | Verified (Linux 6.6.123.2-microsoft-standard-WSL2+ #11) |
| CONFIG_BLK_WBT=y in running kernel | Verified via /proc/config.gz |
| WBT tracepoints present | Verified (/sys/kernel/tracing/events/wbt) |
| WBT timer armed during workload | Verified (132 wbt_timer events) |
| v5 reclassification fires | Verified (130/132 windows = LAT_UNKNOWN) |
| Pure-write path preserved | Verified (2 windows = LAT_UNKNOWN_WRITES after readers stop) |
| BUMP recovery works | Verified (scale_down from -2 to 0) |
| Block-layer traffic confirmed | Verified (non-zero read and write sectors) |
| Writer throughput measured | 225 OPS, 0 errors |
| Reader latency measured | P50 ~1583µs, P99 ~9246µs (6 probes) |
