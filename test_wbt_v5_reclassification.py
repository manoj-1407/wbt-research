"""
test_wbt_v5_reclassification.py
--------------------------------
Source-faithful unit tests for the candidate-v5-reclassification patch.

The v5 patch modifies latency_exceeded() in block/blk-wbt.c:

    BEFORE (upstream):
        if (stat[WRITE].nr_samples || wb_recent_wait(rwb) ||
            wbt_inflight(rwb))
                return LAT_UNKNOWN_WRITES;
        return LAT_UNKNOWN;

    AFTER (v5 candidate, commit ebd45314d):
        if (!stat[READ].nr_samples &&
            (stat[WRITE].nr_samples || wb_recent_wait(rwb) ||
             wbt_inflight(rwb)))
                return LAT_UNKNOWN_WRITES;
        return LAT_UNKNOWN;

The change adds one condition: `LAT_UNKNOWN_WRITES` is only returned when
there are NO completed read samples in the current window.

When read samples ARE present but stat_sample_valid() is false (e.g. fewer
than 3 write completions), the function now falls through to `LAT_UNKNOWN`
rather than `LAT_UNKNOWN_WRITES`.

Downstream effect: a window with active concurrent reads and sparse writes
no longer unconditionally calls scale_up(rwb) via LAT_UNKNOWN_WRITES.
Instead it enters the LAT_UNKNOWN path, which applies the unknown_cnt
bump-and-center logic.

This is architecturally different from the prototype hold-depth guard:
- Prototype: added a break-out guard inside wb_timer_fn() LAT_UNKNOWN_WRITES
- v5:        prevents LAT_UNKNOWN_WRITES classification when reads are present

Key properties to test:
  1. Pure write window (no reads): still returns LAT_UNKNOWN_WRITES → scale_up
  2. Reads present + sparse writes: baseline returns LAT_UNKNOWN_WRITES,
     v5 returns LAT_UNKNOWN
  3. Reads present + sufficient writes: both return via stat_sample_valid path
     (unchanged — v5 only touches the !stat_sample_valid branch)
  4. LAT_UNKNOWN path behavior: unknown_cnt accumulates; returns to center
  5. Watchdog path (sync_issue): unchanged by v5

Run with: python3 test_wbt_v5_reclassification.py
"""

import sys

# ---------------------------------------------------------------------------
# Faithful kernel model (same constants as other test files)
# ---------------------------------------------------------------------------

RWB_MIN_WRITE_SAMPLES = 3     # block/blk-wbt.c
RWB_UNKNOWN_BUMP      = 5     # block/blk-wbt.c
RWB_DEF_DEPTH         = 16    # block/blk-wbt.c

READ  = 0
WRITE = 1

LAT_OK             = 0
LAT_EXCEEDED       = 1
LAT_UNKNOWN        = 2
LAT_UNKNOWN_WRITES = 3

LAT_NAMES = {
    LAT_OK: "LAT_OK",
    LAT_EXCEEDED: "LAT_EXCEEDED",
    LAT_UNKNOWN: "LAT_UNKNOWN",
    LAT_UNKNOWN_WRITES: "LAT_UNKNOWN_WRITES",
}


class BlkRqStat:
    def __init__(self, nr_samples=0, min_ns=0, mean_ns=0, max_ns=0):
        self.nr_samples = nr_samples
        self.min   = min_ns
        self.mean  = mean_ns
        self.max   = max_ns


class RqDepth:
    def __init__(self, scale_step=0):
        self.scale_step = scale_step

    def scale_up(self):
        self.scale_step -= 1

    def scale_down(self):
        self.scale_step += 1


class RqWb:
    def __init__(self, min_lat_nsec: int, wc: bool = False):
        self.min_lat_nsec = min_lat_nsec
        self.rq_depth     = RqDepth()
        self.unknown_cnt  = 0
        self.wc           = wc
        self._sync_issue_lat = 0   # simplified; set per test
        self._recent_wait    = False
        self._inflight       = 0


def stat_sample_valid(stat) -> bool:
    return (stat[READ].nr_samples >= 1 and
            stat[WRITE].nr_samples >= RWB_MIN_WRITE_SAMPLES)


# ---------------------------------------------------------------------------
# latency_exceeded() — UPSTREAM (baseline)
# ---------------------------------------------------------------------------
def latency_exceeded_baseline(rwb: RqWb, stat) -> int:
    """Upstream latency_exceeded(): LAT_UNKNOWN_WRITES whenever writes/inflight,
    regardless of whether reads are present."""
    thislat = rwb._sync_issue_lat
    if thislat > rwb.min_lat_nsec and not stat[READ].nr_samples:
        return LAT_EXCEEDED

    if not stat_sample_valid(stat):
        if (stat[WRITE].nr_samples or rwb._recent_wait or rwb._inflight):
            return LAT_UNKNOWN_WRITES
        return LAT_UNKNOWN

    if stat[READ].min > rwb.min_lat_nsec:
        return LAT_EXCEEDED
    return LAT_OK


# ---------------------------------------------------------------------------
# latency_exceeded() — V5 CANDIDATE (ebd45314d)
# ---------------------------------------------------------------------------
def latency_exceeded_v5(rwb: RqWb, stat) -> int:
    """v5 candidate: LAT_UNKNOWN_WRITES only when NO read samples present."""
    thislat = rwb._sync_issue_lat
    if thislat > rwb.min_lat_nsec and not stat[READ].nr_samples:
        return LAT_EXCEEDED

    if not stat_sample_valid(stat):
        # THE CHANGE: gate LAT_UNKNOWN_WRITES on !stat[READ].nr_samples
        if (not stat[READ].nr_samples and
                (stat[WRITE].nr_samples or rwb._recent_wait or rwb._inflight)):
            return LAT_UNKNOWN_WRITES
        return LAT_UNKNOWN

    if stat[READ].min > rwb.min_lat_nsec:
        return LAT_EXCEEDED
    return LAT_OK


# ---------------------------------------------------------------------------
# wb_timer_fn() action — shared, both baseline and v5 (wb_timer_fn unchanged)
# ---------------------------------------------------------------------------
def wb_timer_action(rwb: RqWb, status: int) -> str:
    if status == LAT_EXCEEDED:
        rwb.rq_depth.scale_down()
        return "scale_down(hard=True)"
    elif status == LAT_OK:
        rwb.rq_depth.scale_up()
        return "scale_up"
    elif status == LAT_UNKNOWN_WRITES:
        rwb.rq_depth.scale_up()
        return "scale_up"
    elif status == LAT_UNKNOWN:
        rwb.unknown_cnt += 1
        if rwb.unknown_cnt < RWB_UNKNOWN_BUMP:
            return "noop"
        # Return toward center
        if rwb.rq_depth.scale_step > 0:
            rwb.rq_depth.scale_up()
            return "scale_up(return_to_center)"
        elif rwb.rq_depth.scale_step < 0:
            rwb.rq_depth.scale_down()
            return "scale_down(return_to_center)"
        return "noop"
    return "noop"


def run_one(rwb_b: RqWb, rwb_v: RqWb, stat,
            expected_status_b: int, expected_status_v: int,
            expected_action_b: str, expected_action_v: str,
            expected_step_b: int, expected_step_v: int) -> bool:
    sb = latency_exceeded_baseline(rwb_b, stat)
    sv = latency_exceeded_v5(rwb_v, stat)
    ab = wb_timer_action(rwb_b, sb)
    av = wb_timer_action(rwb_v, sv)
    ok = True
    issues = []
    if sb != expected_status_b:
        issues.append(f"  baseline status: expected {LAT_NAMES[expected_status_b]}, got {LAT_NAMES[sb]}")
        ok = False
    if sv != expected_status_v:
        issues.append(f"  v5      status: expected {LAT_NAMES[expected_status_v]}, got {LAT_NAMES[sv]}")
        ok = False
    if ab != expected_action_b:
        issues.append(f"  baseline action: expected '{expected_action_b}', got '{ab}'")
        ok = False
    if av != expected_action_v:
        issues.append(f"  v5      action: expected '{expected_action_v}', got '{av}'")
        ok = False
    if rwb_b.rq_depth.scale_step != expected_step_b:
        issues.append(f"  baseline step: expected {expected_step_b}, got {rwb_b.rq_depth.scale_step}")
        ok = False
    if rwb_v.rq_depth.scale_step != expected_step_v:
        issues.append(f"  v5      step: expected {expected_step_v}, got {rwb_v.rq_depth.scale_step}")
        ok = False
    label = "PASS" if ok else "FAIL"
    print(f"[{label}] baseline={ab}(step {rwb_b.rq_depth.scale_step})  v5={av}(step {rwb_v.rq_depth.scale_step})")
    for i in issues:
        print(i)
    return ok


def make_stat(r_samples=0, r_min=0, r_mean=0, w_samples=0, w_min=0) -> list:
    return [
        BlkRqStat(nr_samples=r_samples, min_ns=r_min, mean_ns=r_mean),
        BlkRqStat(nr_samples=w_samples, min_ns=w_min),
    ]


MIN_LAT = 2_000_000  # 2 ms
all_passed = True

print("=" * 72)
print("WBT v5 Reclassification Tests — Baseline vs Candidate v5")
print(f"min_lat_nsec = {MIN_LAT:,} ns ({MIN_LAT/1e6:.1f} ms)")
print("=" * 72)
print()

# ---------------------------------------------------------------------------
# TC-V1: Pure write window — no reads, writes present
# Both: LAT_UNKNOWN_WRITES -> scale_up (behavior unchanged)
# ---------------------------------------------------------------------------
print("--- TC-V1: Pure write window (no read samples) ---")
stat = make_stat(r_samples=0, w_samples=1)
rwb_b = RqWb(MIN_LAT); rwb_b._inflight = 1
rwb_v = RqWb(MIN_LAT); rwb_v._inflight = 1
r = run_one(rwb_b, rwb_v, stat,
            LAT_UNKNOWN_WRITES, LAT_UNKNOWN_WRITES,
            "scale_up", "scale_up", -1, -1)
all_passed = all_passed and r
print("  Rationale: no read samples -> v5 condition !stat[READ].nr_samples=True -> both return LAT_UNKNOWN_WRITES")
print()

# ---------------------------------------------------------------------------
# TC-V2: THE KEY CASE — reads present, sparse writes
# Baseline: LAT_UNKNOWN_WRITES -> scale_up
# v5:       LAT_UNKNOWN -> noop (unknown_cnt not yet at BUMP)
# ---------------------------------------------------------------------------
print("--- TC-V2: Reads present + sparse writes (KEY DIVERGENCE) ---")
stat = make_stat(r_samples=5, r_min=800_000, r_mean=1_500_000, w_samples=1)
# r_samples=5, but w_samples=1 < RWB_MIN_WRITE_SAMPLES=3 -> stat_sample_valid=False
# r_min=0.8ms < min_lat=2ms so stat[READ].min <= target (if sample_valid were true)
rwb_b = RqWb(MIN_LAT); rwb_b._inflight = 1
rwb_v = RqWb(MIN_LAT); rwb_v._inflight = 1
r = run_one(rwb_b, rwb_v, stat,
            LAT_UNKNOWN_WRITES, LAT_UNKNOWN,
            "scale_up", "noop", -1, 0)
all_passed = all_passed and r
print("  Rationale: reads present -> v5 blocks LAT_UNKNOWN_WRITES -> LAT_UNKNOWN -> noop (unknown_cnt=1)")
print()

# ---------------------------------------------------------------------------
# TC-V3: Reads present + sparse writes + elevated read latency
# Baseline: LAT_UNKNOWN_WRITES -> scale_up (ignores elevated reads)
# v5:       LAT_UNKNOWN -> noop (unknown_cnt accumulates; no immediate scale)
# Note: v5 doesn't reach stat[READ].min comparison because stat_sample_valid=False
# ---------------------------------------------------------------------------
print("--- TC-V3: Reads present, r_min > target, sparse writes ---")
stat = make_stat(r_samples=5, r_min=8_000_000, r_mean=12_000_000, w_samples=1)
rwb_b = RqWb(MIN_LAT); rwb_b._inflight = 1
rwb_v = RqWb(MIN_LAT); rwb_v._inflight = 1
r = run_one(rwb_b, rwb_v, stat,
            LAT_UNKNOWN_WRITES, LAT_UNKNOWN,
            "scale_up", "noop", -1, 0)
all_passed = all_passed and r
print("  Rationale: v5 falls through to LAT_UNKNOWN; min comparison never reached (sample_valid=False)")
print()

# ---------------------------------------------------------------------------
# TC-V4: Reads present, sufficient writes, r_min > target
# Both: stat_sample_valid=True -> LAT_EXCEEDED (unchanged by v5)
# ---------------------------------------------------------------------------
print("--- TC-V4: Reads present, sufficient writes, r_min > target -> LAT_EXCEEDED ---")
stat = make_stat(r_samples=5, r_min=8_000_000, r_mean=12_000_000, w_samples=5)
rwb_b = RqWb(MIN_LAT)
rwb_v = RqWb(MIN_LAT)
r = run_one(rwb_b, rwb_v, stat,
            LAT_EXCEEDED, LAT_EXCEEDED,
            "scale_down(hard=True)", "scale_down(hard=True)", 1, 1)
all_passed = all_passed and r
print("  Rationale: stat_sample_valid=True -> v5 change not reached; both use min comparison")
print()

# ---------------------------------------------------------------------------
# TC-V5: Reads present, sufficient writes, r_min < target -> LAT_OK
# Both: scale_up (unchanged by v5)
# ---------------------------------------------------------------------------
print("--- TC-V5: Reads present, sufficient writes, r_min < target -> LAT_OK ---")
stat = make_stat(r_samples=5, r_min=500_000, r_mean=900_000, w_samples=5)
rwb_b = RqWb(MIN_LAT)
rwb_v = RqWb(MIN_LAT)
r = run_one(rwb_b, rwb_v, stat,
            LAT_OK, LAT_OK,
            "scale_up", "scale_up", -1, -1)
all_passed = all_passed and r
print()

# ---------------------------------------------------------------------------
# TC-V6: No reads, no writes, no inflight, no recent_wait
# Both: LAT_UNKNOWN -> noop (unchanged)
# ---------------------------------------------------------------------------
print("--- TC-V6: Empty window (no reads, no writes, no inflight) -> LAT_UNKNOWN ---")
stat = make_stat(r_samples=0, w_samples=0)
rwb_b = RqWb(MIN_LAT)
rwb_v = RqWb(MIN_LAT)
r = run_one(rwb_b, rwb_v, stat,
            LAT_UNKNOWN, LAT_UNKNOWN,
            "noop", "noop", 0, 0)
all_passed = all_passed and r
print()

# ---------------------------------------------------------------------------
# TC-V7: LAT_UNKNOWN unknown_cnt accumulation to BUMP
# v5 sparse-write-with-reads window: after RWB_UNKNOWN_BUMP windows,
# returns to center from a negative scale_step.
# ---------------------------------------------------------------------------
print("--- TC-V7: v5 LAT_UNKNOWN accumulates to BUMP -> return to center ---")
stat = make_stat(r_samples=5, r_min=800_000, w_samples=1)
rwb_v = RqWb(MIN_LAT)
rwb_v._inflight = 1
rwb_v.rq_depth.scale_step = -3   # already at expanded depth

# Apply RWB_UNKNOWN_BUMP - 1 windows (noop each time)
for _ in range(RWB_UNKNOWN_BUMP - 1):
    sv = latency_exceeded_v5(rwb_v, stat)
    wb_timer_action(rwb_v, sv)
assert rwb_v.rq_depth.scale_step == -3, "step should not change before BUMP"
assert rwb_v.unknown_cnt == RWB_UNKNOWN_BUMP - 1

# Window RWB_UNKNOWN_BUMP: triggers return-to-center
sv = latency_exceeded_v5(rwb_v, stat)
av = wb_timer_action(rwb_v, sv)
step_after = rwb_v.rq_depth.scale_step
tc7_pass = (step_after == -2 and "return_to_center" in av)
print(f"  After {RWB_UNKNOWN_BUMP} LAT_UNKNOWN windows: action={av} step={step_after} (expected -2)")
print(f"[{'PASS' if tc7_pass else 'FAIL'}] TC-V7: unknown_cnt BUMP triggers scale_down to center from -3")
all_passed = all_passed and tc7_pass
print()

# ---------------------------------------------------------------------------
# TC-V8: v5 does NOT suppress LAT_UNKNOWN_WRITES for wb_recent_wait
# with no read samples — pure writeback traffic still gets LAT_UNKNOWN_WRITES
# ---------------------------------------------------------------------------
print("--- TC-V8: wb_recent_wait=True, no reads -> both LAT_UNKNOWN_WRITES ---")
stat = make_stat(r_samples=0, w_samples=0)
rwb_b = RqWb(MIN_LAT); rwb_b._recent_wait = True
rwb_v = RqWb(MIN_LAT); rwb_v._recent_wait = True
r = run_one(rwb_b, rwb_v, stat,
            LAT_UNKNOWN_WRITES, LAT_UNKNOWN_WRITES,
            "scale_up", "scale_up", -1, -1)
all_passed = all_passed and r
print("  Rationale: recent_wait=True, r_samples=0 -> both return LAT_UNKNOWN_WRITES; pure write path preserved")
print()

# ---------------------------------------------------------------------------
# TC-V9: Boundary — exactly 1 read sample, exactly below write threshold
# Baseline: LAT_UNKNOWN_WRITES (read present but ignored)
# v5:       LAT_UNKNOWN (read present -> gate prevents LAT_UNKNOWN_WRITES)
# ---------------------------------------------------------------------------
print("--- TC-V9: Boundary: r_samples=1, w_samples=2 (one below RWB_MIN_WRITE_SAMPLES) ---")
stat = make_stat(r_samples=1, r_min=3_000_000, r_mean=4_000_000, w_samples=2)
rwb_b = RqWb(MIN_LAT); rwb_b._inflight = 1
rwb_v = RqWb(MIN_LAT); rwb_v._inflight = 1
r = run_one(rwb_b, rwb_v, stat,
            LAT_UNKNOWN_WRITES, LAT_UNKNOWN,
            "scale_up", "noop", -1, 0)
all_passed = all_passed and r
print("  Rationale: r_samples=1 triggers v5 gate even with elevated r_min; falls to LAT_UNKNOWN")
print()

# ---------------------------------------------------------------------------
# TC-V10: 5 consecutive sparse-write windows with active readers
# Baseline escalates: 0 -> -1 -> -2 -> -3 -> -4 -> -5
# v5 does NOT escalate (LAT_UNKNOWN noop until BUMP, then returns to center)
# ---------------------------------------------------------------------------
print("--- TC-V10: 5 consecutive windows, reads present, sparse writes ---")
stat = make_stat(r_samples=5, r_min=800_000, w_samples=1)
rwb_b = RqWb(MIN_LAT); rwb_b._inflight = 1
rwb_v = RqWb(MIN_LAT); rwb_v._inflight = 1
steps_b = [rwb_b.rq_depth.scale_step]
steps_v = [rwb_v.rq_depth.scale_step]
for _ in range(5):
    sb = latency_exceeded_baseline(rwb_b, stat)
    sv = latency_exceeded_v5(rwb_v, stat)
    wb_timer_action(rwb_b, sb)
    wb_timer_action(rwb_v, sv)
    steps_b.append(rwb_b.rq_depth.scale_step)
    steps_v.append(rwb_v.rq_depth.scale_step)

expected_b = [0, -1, -2, -3, -4, -5]
expected_v = [0, 0, 0, 0, 0, 0]   # noop until unknown_cnt hits BUMP (BUMP=5, so window 5 triggers)
tc10_pass = (steps_b == expected_b and steps_v == expected_v)
print(f"  baseline steps: {steps_b} (expected {expected_b})")
print(f"  v5      steps:  {steps_v} (expected {expected_v})")
print(f"[{'PASS' if tc10_pass else 'FAIL'}] TC-V10: baseline escalates, v5 holds at center for 5 windows")
all_passed = all_passed and tc10_pass
print()

# ---------------------------------------------------------------------------
# TC-V11: v5 with mean-based predicate extension (research proposal)
# This test models the PROPOSED next predicate from the audit:
#     stat[READ].nr_samples >= 3 && stat[READ].mean > rwb->min_lat_nsec
# embedded inside stat_sample_valid path (or as a separate gate).
# Tests the theoretical behavior — not implemented in kernel yet.
# ---------------------------------------------------------------------------
print("--- TC-V11: Proposed mean-based predicate (research design, not yet implemented) ---")

def latency_exceeded_mean_candidate(rwb: RqWb, stat) -> int:
    """Research proposal: in !stat_sample_valid path, if reads are present
    and mean latency exceeds target, return LAT_EXCEEDED directly.
    This would replace the current stat[READ].min comparison."""
    thislat = rwb._sync_issue_lat
    if thislat > rwb.min_lat_nsec and not stat[READ].nr_samples:
        return LAT_EXCEEDED

    if not stat_sample_valid(stat):
        if not stat[READ].nr_samples and (stat[WRITE].nr_samples or rwb._recent_wait or rwb._inflight):
            return LAT_UNKNOWN_WRITES
        # Research extension: if reads present with sufficient samples and elevated mean
        if (stat[READ].nr_samples >= 3 and
                stat[READ].mean > rwb.min_lat_nsec):
            return LAT_EXCEEDED   # active read pressure detected via mean
        return LAT_UNKNOWN

    if stat[READ].min > rwb.min_lat_nsec:
        return LAT_EXCEEDED
    return LAT_OK

# Fast min, but degraded mean and many reads
stat = make_stat(r_samples=10, r_min=50_000, r_mean=8_000_000, w_samples=1)
rwb_b = RqWb(MIN_LAT); rwb_b._inflight = 1
rwb_m = RqWb(MIN_LAT); rwb_m._inflight = 1

sb = latency_exceeded_baseline(rwb_b, stat)
sm = latency_exceeded_mean_candidate(rwb_m, stat)
wb_timer_action(rwb_b, sb)
wb_timer_action(rwb_m, sm)

print(f"  stat: r_samples=10, r_min=50us (< 2ms target), r_mean=8ms (> 2ms target), w_samples=1")
print(f"  baseline       : status={LAT_NAMES[sb]}, step={rwb_b.rq_depth.scale_step}")
print(f"  mean_candidate : status={LAT_NAMES[sm]}, step={rwb_m.rq_depth.scale_step}")
tc11_pass = (sb == LAT_UNKNOWN_WRITES and sm == LAT_EXCEEDED)
print(f"[{'PASS' if tc11_pass else 'FAIL'}] TC-V11: mean-based predicate catches degraded distribution that min-based misses")
print(f"  NOTE: This is a SPECIFICATION TEST for a proposed design — not yet in any kernel build")
all_passed = all_passed and tc11_pass
print()

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print("=" * 72)
if all_passed:
    print("ALL V5 RECLASSIFICATION TESTS PASSED")
    print()
    print("Summary of v5 behavioral changes:")
    print("  1. Pure write windows (no reads): UNCHANGED -> LAT_UNKNOWN_WRITES -> scale_up")
    print("  2. Mixed windows (reads + sparse writes): CHANGED -> LAT_UNKNOWN -> noop/center")
    print("  3. Mixed windows (reads + sufficient writes): UNCHANGED via stat_sample_valid")
    print("  4. wb_recent_wait/inflight without reads: UNCHANGED -> LAT_UNKNOWN_WRITES")
    print("  5. LAT_UNKNOWN BUMP logic now handles read-concurrent-sparse-write windows")
    print()
    print("Key difference from prototype hold-depth guard:")
    print("  Prototype: intercepts LAT_UNKNOWN_WRITES in wb_timer_fn, adds conditional break")
    print("  v5:        prevents LAT_UNKNOWN_WRITES classification when reads are present")
    print("  v5 consequence: no scale_up AND no hold — falls to existing UNKNOWN center logic")
    sys.exit(0)
else:
    print("SOME V5 TESTS FAILED")
    sys.exit(1)
