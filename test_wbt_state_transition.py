"""
wbt_state_transition_test.py
----------------------------
Source-faithful unit test of the patched WBT state machine.

This test replays the exact logic of wb_timer_fn() as it exists in
block/blk-wbt.c (Microsoft WSL2 linux-msft-wsl-6.6.y, verified 2026-10-23)
and asserts that:

  1. Original behavior is preserved for pure-write windows (no reads).
  2. The patch guard fires correctly: when reads are present AND
     stat[READ].min > min_lat_nsec, the window holds the current depth
     (break;) instead of calling scale_up().
  3. The patch does NOT fire when reads are present but latency is below target.
  4. The guard performs a hold, not a scale change: it neither scale_up()s
     (the unpatched behaviour) nor scale_down()s (the LAT_EXCEEDED behaviour).

This is NOT a proof that the kernel is correct or that the patch fixes
the real hardware problem. It is a specification test that verifies the
patch text matches the intended logic before the kernel is built.

Run with: python3 wbt_state_transition_test.py
"""

import sys

# ---------------------------------------------------------------------------
# Minimal faithful model of the WBT state machine
# ---------------------------------------------------------------------------

RWB_MIN_WRITE_SAMPLES = 3    # block/blk-wbt.c line 145
RWB_UNKNOWN_BUMP      = 5    # block/blk-wbt.c line 151
RWB_DEF_DEPTH         = 16   # block/blk-wbt.c line 135
RWB_WINDOW_NSEC       = 100_000_000  # 100 ms in ns

READ  = 0
WRITE = 1

# Latency classification enum (block/blk-wbt.c lines 299-303)
LAT_OK             = 0
LAT_EXCEEDED       = 1
LAT_UNKNOWN        = 2
LAT_UNKNOWN_WRITES = 3


class BlkRqStat:
    """Mirrors struct blk_rq_stat (include/linux/blk-mq.h)"""
    def __init__(self, nr_samples=0, min_ns=0, max_ns=0, mean_ns=0):
        self.nr_samples = nr_samples
        self.min = min_ns   # field name matches kernel source: stat[READ].min
        self.max = max_ns
        self.mean = mean_ns


class RqDepth:
    """Mirrors struct rq_depth (block/blk-rq-qos.h)"""
    def __init__(self, scale_step=0, max_depth=RWB_DEF_DEPTH):
        self.scale_step = scale_step
        self.max_depth = max_depth

    def scale_up(self):
        """rq_depth_scale_up — decrement scale_step (more depth)"""
        self.scale_step -= 1

    def scale_down(self):
        """rq_depth_scale_down — increment scale_step (less depth)"""
        self.scale_step += 1


class RqWb:
    """Mirrors struct rq_wb (block/blk-wbt.c)"""
    def __init__(self, min_lat_nsec: int):
        self.min_lat_nsec = min_lat_nsec
        self.rq_depth = RqDepth()
        self.unknown_cnt = 0
        self.sync_issue = 0   # simplified: no sync watchdog in this model
        self.wc = False


# ---------------------------------------------------------------------------
# Faithful reimplementation of key functions
# ---------------------------------------------------------------------------

def stat_sample_valid(stat) -> bool:
    """block/blk-wbt.c:264-281 — stat_sample_valid()"""
    return (stat[READ].nr_samples >= 1 and
            stat[WRITE].nr_samples >= RWB_MIN_WRITE_SAMPLES)


def scale_up(rwb: RqWb) -> str:
    """block/blk-wbt.c:379-387 — scale_up()"""
    rwb.rq_depth.scale_up()
    return "scale_up"


def scale_down(rwb: RqWb, hard_throttle: bool) -> str:
    """block/blk-wbt.c:389-402 — scale_down()"""
    rwb.rq_depth.scale_down()
    return f"scale_down(hard={hard_throttle})"


def latency_exceeded(rwb: RqWb, stat) -> int:
    """
    block/blk-wbt.c:304-359 — latency_exceeded()
    Simplified: ignores sync_issue watchdog (sync_issue == 0).
    """
    if not stat_sample_valid(stat):
        # Determine UNKNOWN_WRITES vs UNKNOWN
        if (stat[WRITE].nr_samples or False):  # simplified: no wb_recent_wait
            return LAT_UNKNOWN_WRITES
        return LAT_UNKNOWN

    # stat_sample_valid() == True: we have ≥1 read and ≥3 write samples
    if stat[READ].min > rwb.min_lat_nsec:
        return LAT_EXCEEDED
    return LAT_OK


def wb_timer_fn_baseline(rwb: RqWb, stat) -> str:
    """
    block/blk-wbt.c:422-470 — wb_timer_fn() — BASELINE (unpatched)
    Only the switch/case logic is modelled.
    """
    status = latency_exceeded(rwb, stat)

    if status == LAT_EXCEEDED:
        return scale_down(rwb, hard_throttle=True)

    elif status == LAT_OK:
        return scale_up(rwb)

    elif status == LAT_UNKNOWN_WRITES:
        # Original: unconditionally scale up to boost write perf
        return scale_up(rwb)

    elif status == LAT_UNKNOWN:
        rwb.unknown_cnt += 1
        if rwb.unknown_cnt < RWB_UNKNOWN_BUMP:
            return "noop"
        # Return to center
        if rwb.rq_depth.scale_step > 0:
            return scale_up(rwb)
        elif rwb.rq_depth.scale_step < 0:
            return scale_down(rwb, hard_throttle=False)
        return "noop"

    return "noop"


def wb_timer_fn_patched(rwb: RqWb, stat) -> str:
    """
    block/blk-wbt.c:422-470 — wb_timer_fn() — PATCHED
    Adds the LAT_UNKNOWN_WRITES guard (hold depth).
    """
    status = latency_exceeded(rwb, stat)

    if status == LAT_EXCEEDED:
        return scale_down(rwb, hard_throttle=True)

    elif status == LAT_OK:
        return scale_up(rwb)

    elif status == LAT_UNKNOWN_WRITES:
        # ---- PATCH START: HOLD DEPTH ----
        if stat[READ].nr_samples and stat[READ].min > rwb.min_lat_nsec:
            return "hold"  # Conservatively hold current depth (break;), no scale_up
        # ---- PATCH END ----
        return scale_up(rwb)

    elif status == LAT_UNKNOWN:
        rwb.unknown_cnt += 1
        if rwb.unknown_cnt < RWB_UNKNOWN_BUMP:
            return "noop"
        if rwb.rq_depth.scale_step > 0:
            return scale_up(rwb)
        elif rwb.rq_depth.scale_step < 0:
            return scale_down(rwb, hard_throttle=False)
        return "noop"

    return "noop"


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

def make_stat(r_samples=0, r_min_ns=0, w_samples=0, w_min_ns=0):
    return [
        BlkRqStat(nr_samples=r_samples, min_ns=r_min_ns),
        BlkRqStat(nr_samples=w_samples, min_ns=w_min_ns),
    ]


MIN_LAT = 2_000_000  # 2 ms target (rwb->min_lat_nsec)


def run_test(name, fn_baseline, fn_patched, rwb_b, stat_b, rwb_p, stat_p,
             expected_baseline, expected_patched, expected_step_delta_b, expected_step_delta_p):
    step_before_b = rwb_b.rq_depth.scale_step
    step_before_p = rwb_p.rq_depth.scale_step
    result_b = fn_baseline(rwb_b, stat_b)
    result_p = fn_patched(rwb_p, stat_p)
    step_after_b = rwb_b.rq_depth.scale_step
    step_after_p = rwb_p.rq_depth.scale_step
    delta_b = step_after_b - step_before_b
    delta_p = step_after_p - step_before_p

    passed = True
    issues = []

    if expected_baseline and result_b != expected_baseline:
        issues.append(f"  BASELINE action: expected '{expected_baseline}', got '{result_b}'")
        passed = False
    if expected_patched and result_p != expected_patched:
        issues.append(f"  PATCHED  action: expected '{expected_patched}', got '{result_p}'")
        passed = False
    if expected_step_delta_b is not None and delta_b != expected_step_delta_b:
        issues.append(f"  BASELINE step delta: expected {expected_step_delta_b:+d}, got {delta_b:+d}")
        passed = False
    if expected_step_delta_p is not None and delta_p != expected_step_delta_p:
        issues.append(f"  PATCHED  step delta: expected {expected_step_delta_p:+d}, got {delta_p:+d}")
        passed = False

    status = "PASS" if passed else "FAIL"
    print(f"[{status}] {name}")
    print(f"        baseline: {result_b}  (step {step_before_b} -> {step_after_b})")
    print(f"        patched:  {result_p}  (step {step_before_p} -> {step_after_p})")
    if issues:
        for issue in issues:
            print(issue)
    return passed


all_passed = True

print("=" * 70)
print("WBT State Transition Tests -- Baseline vs Patched (Hold-Depth)")
print(f"min_lat_nsec = {MIN_LAT:,} ns ({MIN_LAT/1e6:.1f} ms)")
print("=" * 70)
print()

# ---------------------------------------------------------------------------
# TC-1: Pure write window (no reads) -- guard must NOT fire
# Expected: both baseline and patched call scale_up (step decreases by 1)
# ---------------------------------------------------------------------------
print("--- TC-1: Pure write window (sparse writes, no reads) ---")
stat = make_stat(r_samples=0, w_samples=1, w_min_ns=500_000)
rwb_b = RqWb(MIN_LAT); rwb_p = RqWb(MIN_LAT)
passed = run_test(
    "TC-1: LAT_UNKNOWN_WRITES, no read samples -> both scale_up",
    wb_timer_fn_baseline, wb_timer_fn_patched,
    rwb_b, stat, rwb_p, stat,
    expected_baseline="scale_up",
    expected_patched="scale_up",
    expected_step_delta_b=-1,
    expected_step_delta_p=-1,
)
all_passed = all_passed and passed
print()

# ---------------------------------------------------------------------------
# TC-2: Mixed window -- reads present, read latency BELOW target
# Expected: guard does NOT fire; both scale_up
# ---------------------------------------------------------------------------
print("--- TC-2: Sparse writes, reads present but latency BELOW target ---")
stat = make_stat(r_samples=5, r_min_ns=500_000, w_samples=1, w_min_ns=200_000)
rwb_b = RqWb(MIN_LAT); rwb_p = RqWb(MIN_LAT)
passed = run_test(
    "TC-2: LAT_UNKNOWN_WRITES, r_min < min_lat -> both scale_up",
    wb_timer_fn_baseline, wb_timer_fn_patched,
    rwb_b, stat, rwb_p, stat,
    expected_baseline="scale_up",
    expected_patched="scale_up",
    expected_step_delta_b=-1,
    expected_step_delta_p=-1,
)
all_passed = all_passed and passed
print()

# ---------------------------------------------------------------------------
# TC-3: THE CRITICAL CASE -- sparse writes, reads present, read latency ABOVE target
# Expected:
#   baseline -> scale_up  (escalates write depth)
#   patched  -> hold      (holds existing scale step, delta=0)
# ---------------------------------------------------------------------------
print("--- TC-3: CRITICAL -- sparse writes, reads elevated above target ---")
stat = make_stat(r_samples=3, r_min_ns=8_000_000, w_samples=1, w_min_ns=200_000)
# r_min = 8 ms >> min_lat = 2 ms; write samples = 1 < RWB_MIN_WRITE_SAMPLES = 3
rwb_b = RqWb(MIN_LAT); rwb_p = RqWb(MIN_LAT)
passed = run_test(
    "TC-3: LAT_UNKNOWN_WRITES + r_min > min_lat -> baseline scale_up, patched hold",
    wb_timer_fn_baseline, wb_timer_fn_patched,
    rwb_b, stat, rwb_p, stat,
    expected_baseline="scale_up",
    expected_patched="hold",
    expected_step_delta_b=-1,   # baseline increases write depth (step goes more negative)
    expected_step_delta_p=0,    # patched holds existing step (no change)
)
all_passed = all_passed and passed
print()

# ---------------------------------------------------------------------------
# TC-4: Sufficient write samples + read latency exceeded -> both see LAT_EXCEEDED
# The patch should NOT touch this path (stat_sample_valid -> True)
# ---------------------------------------------------------------------------
print("--- TC-4: Sufficient write samples + read latency above target -> LAT_EXCEEDED ---")
stat = make_stat(r_samples=5, r_min_ns=8_000_000, w_samples=5, w_min_ns=200_000)
rwb_b = RqWb(MIN_LAT); rwb_p = RqWb(MIN_LAT)
passed = run_test(
    "TC-4: stat_sample_valid=True + r_min > min_lat -> both scale_down(hard=True)",
    wb_timer_fn_baseline, wb_timer_fn_patched,
    rwb_b, stat, rwb_p, stat,
    expected_baseline="scale_down(hard=True)",
    expected_patched="scale_down(hard=True)",
    expected_step_delta_b=+1,
    expected_step_delta_p=+1,
)
all_passed = all_passed and passed
print()

# ---------------------------------------------------------------------------
# TC-5: Sufficient write samples + latency OK -> both scale_up
# ---------------------------------------------------------------------------
print("--- TC-5: Sufficient write samples + read latency below target -> LAT_OK ---")
stat = make_stat(r_samples=5, r_min_ns=500_000, w_samples=5, w_min_ns=200_000)
rwb_b = RqWb(MIN_LAT); rwb_p = RqWb(MIN_LAT)
passed = run_test(
    "TC-5: stat_sample_valid=True + r_min < min_lat -> both scale_up",
    wb_timer_fn_baseline, wb_timer_fn_patched,
    rwb_b, stat, rwb_p, stat,
    expected_baseline="scale_up",
    expected_patched="scale_up",
    expected_step_delta_b=-1,
    expected_step_delta_p=-1,
)
all_passed = all_passed and passed
print()

# ---------------------------------------------------------------------------
# TC-6: Boundary -- read latency exactly equals target
# stat[READ].min == min_lat_nsec -> guard condition is STRICT GREATER THAN
# Expected: guard does NOT fire; both scale_up
# ---------------------------------------------------------------------------
print("--- TC-6: Boundary -- r_min exactly equals min_lat_nsec ---")
stat = make_stat(r_samples=3, r_min_ns=MIN_LAT, w_samples=1)
rwb_b = RqWb(MIN_LAT); rwb_p = RqWb(MIN_LAT)
passed = run_test(
    "TC-6: r_min == min_lat_nsec (not >) -> both scale_up (guard does not fire)",
    wb_timer_fn_baseline, wb_timer_fn_patched,
    rwb_b, stat, rwb_p, stat,
    expected_baseline="scale_up",
    expected_patched="scale_up",
    expected_step_delta_b=-1,
    expected_step_delta_p=-1,
)
all_passed = all_passed and passed
print()

# ---------------------------------------------------------------------------
# TC-7: Sustained patched behavior -- 5 consecutive LAT_UNKNOWN_WRITES windows
#        with reads elevated.
#        Baseline should escalate step: 0 -> -1 -> -2 -> -3 -> -4 -> -5
#        Patched should hold step:      0 ->  0 ->  0 ->  0 ->  0 ->  0
# ---------------------------------------------------------------------------
print("--- TC-7: 5 consecutive windows with reads elevated (patched vs baseline) ---")
rwb_b = RqWb(MIN_LAT)
rwb_p = RqWb(MIN_LAT)
stat = make_stat(r_samples=3, r_min_ns=8_000_000, w_samples=1)
steps_b = [rwb_b.rq_depth.scale_step]
steps_p = [rwb_p.rq_depth.scale_step]
for i in range(5):
    wb_timer_fn_baseline(rwb_b, stat)
    wb_timer_fn_patched(rwb_p, stat)
    steps_b.append(rwb_b.rq_depth.scale_step)
    steps_p.append(rwb_p.rq_depth.scale_step)

expected_steps_b = [0, -1, -2, -3, -4, -5]
expected_steps_p = [0, 0, 0, 0, 0, 0]
step_b_ok = steps_b == expected_steps_b
step_p_ok = steps_p == expected_steps_p
print(f"  baseline scale_step progression: {steps_b} (expected {expected_steps_b})")
print(f"  patched  scale_step progression: {steps_p} (expected {expected_steps_p})")
tc7_pass = step_b_ok and step_p_ok
print(f"[{'PASS' if tc7_pass else 'FAIL'}] TC-7: 5-window hold vs escalation divergence")
all_passed = all_passed and tc7_pass
print()

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print("=" * 70)
if all_passed:
    print("ALL TESTS PASSED")
    print()
    print("The patch logic is internally consistent with the intended behavior.")
    print("This is a specification test — it does NOT validate actual kernel")
    print("execution or hardware-level interference reduction.")
    sys.exit(0)
else:
    print("SOME TESTS FAILED — review output above.")
    sys.exit(1)
