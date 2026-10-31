"""
test_wbt_v5_correctness.py
---------------------------
Phase C correctness investigation for the v5 reclassification patch.

Commit: ebd45314d7e26ddb71110bb4702c8d82925de9b8
Change: In latency_exceeded(), gate LAT_UNKNOWN_WRITES on !stat[READ].nr_samples.
        Windows with read samples that fail stat_sample_valid() now return
        LAT_UNKNOWN instead of LAT_UNKNOWN_WRITES.

This file investigates whether this reclassification is correct and safe across
every relevant workload scenario. It covers:

  1. Pure-write workload correctness (must be unchanged)
  2. Write-depth recovery after throttling (must not stall)
  3. Transient read samples suppressing write scaling
  4. LAT_UNKNOWN center-return logic under sustained mixed traffic
  5. unknown_cnt reset behavior
  6. Scale_step negative state behavior
  7. wb_recent_wait edge cases
  8. kswapd / high-priority write path (not subject to throttling, but should
     not interact with the reclassification)
  9. Comparison of v5 vs prototype guard vs mean-candidate across scenarios

Run with: python3 test_wbt_v5_correctness.py
"""

import sys

# ---------------------------------------------------------------------------
# Kernel model (faithful to block/blk-wbt.c, upstream 6.6.y)
# ---------------------------------------------------------------------------

RWB_MIN_WRITE_SAMPLES = 3
RWB_UNKNOWN_BUMP      = 5
RWB_DEF_DEPTH         = 16

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
        self.min  = min_ns
        self.mean = mean_ns
        self.max  = max_ns


class RqDepth:
    def __init__(self, scale_step=0):
        self.scale_step = scale_step

    def scale_up(self):
        self.scale_step -= 1

    def scale_down(self):
        self.scale_step += 1


class RqWb:
    def __init__(self, min_lat_nsec: int = 2_000_000):
        self.min_lat_nsec    = min_lat_nsec
        self.rq_depth        = RqDepth()
        self.unknown_cnt     = 0
        self._recent_wait    = False
        self._inflight       = 0
        self._sync_issue_lat = 0

    @property
    def scale_step(self):
        return self.rq_depth.scale_step


def make_stat(r_samples=0, r_min=0, r_mean=0, w_samples=0, w_min=0):
    return [
        BlkRqStat(nr_samples=r_samples, min_ns=r_min, mean_ns=r_mean),
        BlkRqStat(nr_samples=w_samples, min_ns=w_min),
    ]


def stat_sample_valid(stat):
    return (stat[READ].nr_samples >= 1 and
            stat[WRITE].nr_samples >= RWB_MIN_WRITE_SAMPLES)


# ---------------------------------------------------------------------------
# Three implementations: baseline, v5, prototype guard
# ---------------------------------------------------------------------------

def latency_exceeded_baseline(rwb, stat):
    if rwb._sync_issue_lat > rwb.min_lat_nsec and not stat[READ].nr_samples:
        return LAT_EXCEEDED
    if not stat_sample_valid(stat):
        if stat[WRITE].nr_samples or rwb._recent_wait or rwb._inflight:
            return LAT_UNKNOWN_WRITES
        return LAT_UNKNOWN
    if stat[READ].min > rwb.min_lat_nsec:
        return LAT_EXCEEDED
    return LAT_OK


def latency_exceeded_v5(rwb, stat):
    if rwb._sync_issue_lat > rwb.min_lat_nsec and not stat[READ].nr_samples:
        return LAT_EXCEEDED
    if not stat_sample_valid(stat):
        # v5: only LAT_UNKNOWN_WRITES when no read samples
        if (not stat[READ].nr_samples and
                (stat[WRITE].nr_samples or rwb._recent_wait or rwb._inflight)):
            return LAT_UNKNOWN_WRITES
        return LAT_UNKNOWN
    if stat[READ].min > rwb.min_lat_nsec:
        return LAT_EXCEEDED
    return LAT_OK


def wb_timer_action(rwb, status):
    """Faithful to wb_timer_fn() switch block. Both scale_up and scale_down
    reset unknown_cnt to 0 (via scale_up()/scale_down() in the kernel)."""
    if status == LAT_EXCEEDED:
        rwb.rq_depth.scale_down()
        rwb.unknown_cnt = 0
        return "scale_down"
    elif status == LAT_OK:
        rwb.rq_depth.scale_up()
        rwb.unknown_cnt = 0
        return "scale_up"
    elif status == LAT_UNKNOWN_WRITES:
        rwb.rq_depth.scale_up()
        rwb.unknown_cnt = 0
        return "scale_up"
    elif status == LAT_UNKNOWN:
        rwb.unknown_cnt += 1
        if rwb.unknown_cnt < RWB_UNKNOWN_BUMP:
            return "noop"
        # BUMP reached: return toward center; scale_up/down resets unknown_cnt
        if rwb.rq_depth.scale_step > 0:
            rwb.rq_depth.scale_up()
            rwb.unknown_cnt = 0
            return "scale_up(center)"
        elif rwb.rq_depth.scale_step < 0:
            rwb.rq_depth.scale_down()
            rwb.unknown_cnt = 0
            return "scale_down(center)"
        # At step==0: BUMP fires but does nothing (no-op), unknown_cnt stays at BUMP
        return "noop(at_center)"
    return "noop"


def simulate(rwb, classify_fn, stat_windows, label=""):
    """Run multiple stat windows and return step/action history."""
    history = []
    for i, stat in enumerate(stat_windows):
        status = classify_fn(rwb, stat)
        action = wb_timer_action(rwb, status)
        history.append((i, LAT_NAMES[status], action, rwb.scale_step, rwb.unknown_cnt))
    return history


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------

passed = 0
failed = 0

def check(name, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  [PASS] {name}")
        passed += 1
    else:
        print(f"  [FAIL] {name}{': ' + detail if detail else ''}")
        failed += 1
    return condition


# ---------------------------------------------------------------------------
# TC-C1: Pure write workload — v5 must not change behavior
# ---------------------------------------------------------------------------
print("=" * 70)
print("TC-C1: Pure write workload (no reads) — must be unchanged")
print("=" * 70)

# 5 windows with writes in-flight, no reads
stat_pure_write = make_stat(r_samples=0, w_samples=1)
rwb_b = RqWb(); rwb_b._inflight = 1
rwb_v = RqWb(); rwb_v._inflight = 1

hist_b = simulate(rwb_b, latency_exceeded_baseline, [stat_pure_write]*5)
hist_v = simulate(rwb_v, latency_exceeded_v5,       [stat_pure_write]*5)

check("TC-C1a: baseline returns LAT_UNKNOWN_WRITES every window",
      all(h[1] == "LAT_UNKNOWN_WRITES" for h in hist_b))
check("TC-C1b: v5 returns LAT_UNKNOWN_WRITES every window",
      all(h[1] == "LAT_UNKNOWN_WRITES" for h in hist_v))
check("TC-C1c: baseline scale_step after 5 windows == -5",
      rwb_b.scale_step == -5, f"got {rwb_b.scale_step}")
check("TC-C1d: v5 scale_step after 5 windows == -5",
      rwb_v.scale_step == -5, f"got {rwb_v.scale_step}")
print()

# ---------------------------------------------------------------------------
# TC-C2: Write-depth recovery — v5 must not prevent returning to center
# From step=-3, after throttling window, scale_step should recover.
# The LAT_UNKNOWN path has a center-return mechanism (BUMP).
# ---------------------------------------------------------------------------
print("=" * 70)
print("TC-C2: Write-depth recovery via LAT_UNKNOWN BUMP mechanism")
print("=" * 70)

# Mixed read+sparse-write windows: v5 → LAT_UNKNOWN
# After RWB_UNKNOWN_BUMP windows at step<0, should return one step toward 0
stat_mixed = make_stat(r_samples=5, r_min=500_000, r_mean=800_000, w_samples=1)
rwb_v = RqWb(); rwb_v._inflight = 1; rwb_v.rq_depth.scale_step = -3

# Run BUMP windows
for _ in range(RWB_UNKNOWN_BUMP):
    sv = latency_exceeded_v5(rwb_v, stat_mixed)
    wb_timer_action(rwb_v, sv)

check("TC-C2a: after BUMP windows from step=-3, step returns toward center (expect -2)",
      rwb_v.scale_step == -2, f"got {rwb_v.scale_step}")
check("TC-C2b: unknown_cnt was reset after BUMP action",
      rwb_v.unknown_cnt == 0, f"got {rwb_v.unknown_cnt}")
print()

# ---------------------------------------------------------------------------
# TC-C3: Recovery continues — after first BUMP, another BUMP cycle moves again
# ---------------------------------------------------------------------------
print("=" * 70)
print("TC-C3: Sustained LAT_UNKNOWN continues returning to center each BUMP cycle")
print("=" * 70)

rwb_v = RqWb(); rwb_v._inflight = 1; rwb_v.rq_depth.scale_step = -4
steps_after_each_bump = []

for cycle in range(4):   # 4 BUMP cycles: -4 -> -3 -> -2 -> -1 -> 0
    for _ in range(RWB_UNKNOWN_BUMP):
        sv = latency_exceeded_v5(rwb_v, stat_mixed)
        wb_timer_action(rwb_v, sv)
    steps_after_each_bump.append(rwb_v.scale_step)

check("TC-C3a: step progression returns to 0 over 4 BUMP cycles",
      steps_after_each_bump == [-3, -2, -1, 0],
      f"got {steps_after_each_bump}")
print()

# ---------------------------------------------------------------------------
# TC-C4: At center (step==0) — BUMP fires but does nothing
# This is correct: at center, no drift; the controller stays neutral.
# This is NOT a stall — it's the intended stable state.
# ---------------------------------------------------------------------------
print("=" * 70)
print("TC-C4: At step=0 with LAT_UNKNOWN — BUMP fires, controller stays neutral")
print("=" * 70)

rwb_v = RqWb(); rwb_v._inflight = 1; rwb_v.rq_depth.scale_step = 0
actions = []
for _ in range(RWB_UNKNOWN_BUMP + 3):   # past the BUMP
    sv = latency_exceeded_v5(rwb_v, stat_mixed)
    a = wb_timer_action(rwb_v, sv)
    actions.append(a)

check("TC-C4a: step remains 0 throughout (no drift from LAT_UNKNOWN at center)",
      rwb_v.scale_step == 0, f"got {rwb_v.scale_step}")
# BUMP fires but does noop(at_center); unknown_cnt stays at BUMP
check("TC-C4b: BUMP fires noop at center (neither scale_up nor scale_down)",
      "noop(at_center)" in actions,
      f"actions={actions[RWB_UNKNOWN_BUMP-1:]}")
print()

# ---------------------------------------------------------------------------
# TC-C5: Transient read sample concern
# A single incidental read must not permanently suppress write scaling.
# In v5, a single read flips to LAT_UNKNOWN which noop-s.
# Once the read workload leaves (r_samples drops to 0), write scaling resumes.
# ---------------------------------------------------------------------------
print("=" * 70)
print("TC-C5: Transient read sample — write scaling resumes when reads leave")
print("=" * 70)

rwb_v = RqWb(); rwb_v._inflight = 1; rwb_v.rq_depth.scale_step = 0

# 3 windows with 1 read sample (transient) — LAT_UNKNOWN, noop
stat_transient_read = make_stat(r_samples=1, r_min=500_000, w_samples=1)
for _ in range(3):
    sv = latency_exceeded_v5(rwb_v, stat_transient_read)
    wb_timer_action(rwb_v, sv)

step_after_transient = rwb_v.scale_step
check("TC-C5a: step held at 0 during transient reads (no false suppression of write scaling)",
      step_after_transient == 0, f"got {step_after_transient}")

# Now reads leave — pure write windows resume
stat_pure = make_stat(r_samples=0, w_samples=1)
for _ in range(3):
    sv = latency_exceeded_v5(rwb_v, stat_pure)
    wb_timer_action(rwb_v, sv)

check("TC-C5b: write scaling resumes (scale_step goes negative) once reads leave",
      rwb_v.scale_step < 0, f"got {rwb_v.scale_step}")
print()

# ---------------------------------------------------------------------------
# TC-C6: Pathological case — alternating single read, then no read
# Does the controller oscillate in an unhealthy way?
# ---------------------------------------------------------------------------
print("=" * 70)
print("TC-C6: Alternating read/no-read windows — no pathological oscillation")
print("=" * 70)

rwb_b = RqWb(); rwb_b._inflight = 1
rwb_v = RqWb(); rwb_v._inflight = 1

# Alternating: 1 window with read, 1 window without, 10 cycles
steps_b = [0]; steps_v = [0]
for i in range(10):
    if i % 2 == 0:
        stat = make_stat(r_samples=1, w_samples=1)   # read present
    else:
        stat = make_stat(r_samples=0, w_samples=1)   # no reads

    sb = latency_exceeded_baseline(rwb_b, stat)
    sv = latency_exceeded_v5(rwb_v, stat)
    wb_timer_action(rwb_b, sb)
    wb_timer_action(rwb_v, sv)
    steps_b.append(rwb_b.scale_step)
    steps_v.append(rwb_v.scale_step)

# Baseline: every window is LAT_UNKNOWN_WRITES → scale_up; step escalates uniformly
# v5: alternates LAT_UNKNOWN (noop) and LAT_UNKNOWN_WRITES (scale_up)
#     scale_up resets unknown_cnt; so v5 escalates slower but still escalates
check("TC-C6a: baseline escalates uniformly over 10 alternating windows",
      rwb_b.scale_step == -10, f"got {rwb_b.scale_step}")
check("TC-C6b: v5 escalates only on write-only windows (half rate, step=-5)",
      rwb_v.scale_step == -5, f"got {rwb_v.scale_step}")
print(f"  Baseline steps: {steps_b}")
print(f"  v5 steps:       {steps_v}")
print()

# ---------------------------------------------------------------------------
# TC-C7: Concern — does v5 suppress depth recovery from positive scale_step?
# When scale_step > 0 (throttled), LAT_UNKNOWN_WRITES was used to rescue.
# v5 converts that to LAT_UNKNOWN which still has the BUMP recovery path.
# ---------------------------------------------------------------------------
print("=" * 70)
print("TC-C7: Recovery from positive scale_step (throttled) via BUMP")
print("=" * 70)

# Scenario: device was throttled (step=+2), reads return with sparse writes
rwb_v = RqWb(); rwb_v._inflight = 1; rwb_v.rq_depth.scale_step = 2

for _ in range(RWB_UNKNOWN_BUMP):
    sv = latency_exceeded_v5(rwb_v, stat_mixed)
    wb_timer_action(rwb_v, sv)

check("TC-C7a: from step=+2 with LAT_UNKNOWN BUMP, moves toward center (expect +1)",
      rwb_v.scale_step == 1, f"got {rwb_v.scale_step}")
print()

# ---------------------------------------------------------------------------
# TC-C8: kswapd / REQ_HIPRIO path — write scaling exempt from reclassification
# In the kernel, wbt_should_throttle returns false for O_DIRECT writes.
# The reclassification only affects the stat-sampling window decision.
# kswapd writes bypass throttling entirely (get_limit returns max_depth).
# Model: this is not about classification, but about whether r_samples
# being 0 vs >0 affects a purely write-pressure scenario.
# The test verifies that wb_recent_wait=True with r_samples=0 is unchanged.
# ---------------------------------------------------------------------------
print("=" * 70)
print("TC-C8: wb_recent_wait=True path — must not be affected by v5 gate")
print("=" * 70)

# Case: dirty page writeback occurred (wb_recent_wait=True), no active readers
stat = make_stat(r_samples=0, w_samples=0)
rwb_b = RqWb(); rwb_b._recent_wait = True
rwb_v = RqWb(); rwb_v._recent_wait = True

sb = latency_exceeded_baseline(rwb_b, stat)
sv = latency_exceeded_v5(rwb_v, stat)

check("TC-C8a: baseline with recent_wait, no reads -> LAT_UNKNOWN_WRITES",
      sb == LAT_UNKNOWN_WRITES, f"got {LAT_NAMES[sb]}")
check("TC-C8b: v5 with recent_wait, no reads -> LAT_UNKNOWN_WRITES (unchanged)",
      sv == LAT_UNKNOWN_WRITES, f"got {LAT_NAMES[sv]}")

# Case: dirty page writeback + concurrent readers (unusual but possible)
stat = make_stat(r_samples=3, r_min=500_000, w_samples=0)
rwb_b = RqWb(); rwb_b._recent_wait = True
rwb_v = RqWb(); rwb_v._recent_wait = True

sb = latency_exceeded_baseline(rwb_b, stat)
sv = latency_exceeded_v5(rwb_v, stat)

check("TC-C8c: baseline with recent_wait AND reads -> LAT_UNKNOWN_WRITES",
      sb == LAT_UNKNOWN_WRITES, f"got {LAT_NAMES[sb]}")
check("TC-C8d: v5 with recent_wait AND reads -> LAT_UNKNOWN (reclassified)",
      sv == LAT_UNKNOWN, f"got {LAT_NAMES[sv]}")
print("  NOTE: wb_recent_wait + reads is correctly reclassified to LAT_UNKNOWN in v5.")
print("  This means dirty-page writeback during concurrent reads will not cause")
print("  unconditional scale_up. This is the intended behavioral change.")
print()

# ---------------------------------------------------------------------------
# TC-C9: Prototype guard vs v5 — head-to-head comparison on the KEY scenario
# TC-3 from the prototype tests: sparse writes, reads present, r_min > target
# ---------------------------------------------------------------------------
print("=" * 70)
print("TC-C9: Prototype guard vs v5 — key scenario comparison")
print("=" * 70)

# Prototype guard: LAT_UNKNOWN_WRITES → break (hold, no scale_up or scale_down)
def wb_timer_prototype(rwb, stat):
    """Prototype guard: adds conditional break in LAT_UNKNOWN_WRITES."""
    status = latency_exceeded_baseline(rwb, stat)
    if status == LAT_UNKNOWN_WRITES:
        # Prototype guard condition
        if stat[READ].nr_samples and stat[READ].min > rwb.min_lat_nsec:
            return "hold"   # break; no scale change
    # Dispatch normal action
    if status == LAT_EXCEEDED:
        rwb.rq_depth.scale_down(); rwb.unknown_cnt = 0; return "scale_down"
    elif status in (LAT_OK, LAT_UNKNOWN_WRITES):
        rwb.rq_depth.scale_up(); rwb.unknown_cnt = 0; return "scale_up"
    elif status == LAT_UNKNOWN:
        rwb.unknown_cnt += 1
        if rwb.unknown_cnt < RWB_UNKNOWN_BUMP: return "noop"
        if rwb.rq_depth.scale_step > 0:
            rwb.rq_depth.scale_up(); rwb.unknown_cnt = 0; return "scale_up(center)"
        elif rwb.rq_depth.scale_step < 0:
            rwb.rq_depth.scale_down(); rwb.unknown_cnt = 0; return "scale_down(center)"
        return "noop(at_center)"
    return "noop"

# Scenario A: r_min > target (guard fires in prototype, LAT_UNKNOWN in v5)
stat_elevated = make_stat(r_samples=5, r_min=8_000_000, r_mean=12_000_000, w_samples=1)
rwb_b = RqWb(); rwb_b._inflight = 1
rwb_p = RqWb(); rwb_p._inflight = 1   # prototype
rwb_v = RqWb(); rwb_v._inflight = 1   # v5

for _ in range(5):
    sb = latency_exceeded_baseline(rwb_b, stat_elevated)
    wb_timer_action(rwb_b, sb)
    wb_timer_prototype(rwb_p, stat_elevated)
    sv = latency_exceeded_v5(rwb_v, stat_elevated)
    wb_timer_action(rwb_v, sv)

print(f"  Scenario A (r_min > target): 5 windows")
print(f"    baseline step={rwb_b.scale_step} (escalated to -5)")
print(f"    prototype step={rwb_p.scale_step} (held at 0, no scale)")
print(f"    v5 step={rwb_v.scale_step} (held at 0, LAT_UNKNOWN noop)")

check("TC-C9a: prototype holds step at 0 (guard fires)",
      rwb_p.scale_step == 0, f"got {rwb_p.scale_step}")
check("TC-C9b: v5 holds step at 0 (LAT_UNKNOWN noop for 5 windows)",
      rwb_v.scale_step == 0, f"got {rwb_v.scale_step}")

# Scenario B: r_min < target (no guard firing; v5 still reclassifies)
stat_below = make_stat(r_samples=5, r_min=500_000, r_mean=800_000, w_samples=1)
rwb_b = RqWb(); rwb_b._inflight = 1
rwb_p = RqWb(); rwb_p._inflight = 1
rwb_v = RqWb(); rwb_v._inflight = 1

for _ in range(3):
    sb = latency_exceeded_baseline(rwb_b, stat_below)
    wb_timer_action(rwb_b, sb)
    wb_timer_prototype(rwb_p, stat_below)
    sv = latency_exceeded_v5(rwb_v, stat_below)
    wb_timer_action(rwb_v, sv)

print(f"\n  Scenario B (r_min < target): 3 windows")
print(f"    baseline step={rwb_b.scale_step} (escalated: LAT_UNKNOWN_WRITES)")
print(f"    prototype step={rwb_p.scale_step} (escalated: guard not fired)")
print(f"    v5 step={rwb_v.scale_step} (held: LAT_UNKNOWN noop)")

check("TC-C9c: baseline escalates when r_min < target",
      rwb_b.scale_step == -3, f"got {rwb_b.scale_step}")
check("TC-C9d: prototype escalates when r_min < target (guard not fired)",
      rwb_p.scale_step == -3, f"got {rwb_p.scale_step}")
check("TC-C9e: v5 holds at 0 even when r_min < target (LAT_UNKNOWN noop)",
      rwb_v.scale_step == 0, f"got {rwb_v.scale_step}")

print()
print("  KEY FINDING: v5 is more conservative than the prototype guard.")
print("  Prototype: only blocks scale_up when r_min > target.")
print("  v5:        blocks scale_up on ALL windows with ANY read samples,")
print("             regardless of r_min. Even when reads are fast, v5 prevents")
print("             escalation as long as reads are present and writes are sparse.")
print("  This is a stronger suppression of write scaling in mixed workloads.")
print()

# ---------------------------------------------------------------------------
# TC-C10: The write starvation question
# Can v5 prevent write depth from ever going negative in a sustained
# mixed workload where reads are always present and writes are always sparse?
# Answer: only until BUMP triggers center-return, then recovery proceeds.
# But at step=0 and step<0, the behavior differs.
# ---------------------------------------------------------------------------
print("=" * 70)
print("TC-C10: Write starvation analysis — sustained mixed workload, step starts at 0")
print("=" * 70)

rwb_v = RqWb(); rwb_v._inflight = 1

# 20 windows of mixed reads + sparse writes (the "normal" steady state scenario)
stat_sustained = make_stat(r_samples=6, r_min=400_000, r_mean=700_000, w_samples=1)
steps = [0]
actions = []
for i in range(20):
    sv = latency_exceeded_v5(rwb_v, stat_sustained)
    a = wb_timer_action(rwb_v, sv)
    steps.append(rwb_v.scale_step)
    actions.append(a)

print(f"  20 windows, reads always present, sparse writes:")
print(f"  Final step={rwb_v.scale_step}, unknown_cnt={rwb_v.unknown_cnt}")
print(f"  Step history: {steps}")
print(f"  Actions: {actions}")

check("TC-C10a: step stays at 0 throughout (LAT_UNKNOWN noop + BUMP noop at center)",
      all(s == 0 for s in steps), f"steps={steps}")
check("TC-C10b: BUMP fires periodically (noop(at_center) in actions)",
      actions.count("noop(at_center)") >= 3, f"center_noops={actions.count('noop(at_center)')}")

print()
print("  IMPORTANT: step stays at 0 (center) indefinitely for sustained mixed workload.")
print("  This is correct and intentional: the controller has no latency evidence to act on.")
print("  Write depth stays at RWB_DEF_DEPTH (wb_normal), not suppressed below it.")
print("  Compare baseline: step would go to -1, -2, -3 etc, expanding write depth.")
print("  v5 prevents write depth expansion beyond default — NOT starvation.")
print()

# ---------------------------------------------------------------------------
# TC-C11: Latency spike with v5 — does LAT_UNKNOWN delay response?
# If read latency suddenly exceeds target WITH sufficient writes, stat_sample_valid
# becomes true and the normal LAT_EXCEEDED path fires immediately.
# If reads are present but writes are sparse (stat_sample_valid=False), v5 gives
# LAT_UNKNOWN (no response). This is the identified limitation.
# ---------------------------------------------------------------------------
print("=" * 70)
print("TC-C11: Latency spike response — v5 limitation under sparse writes")
print("=" * 70)

# Window with elevated reads AND sparse writes (stat_sample_valid = False)
# v5 returns LAT_UNKNOWN; there is no throttle response in this window
stat_spike_sparse = make_stat(r_samples=5, r_min=50_000_000, r_mean=80_000_000, w_samples=1)
rwb_b = RqWb(); rwb_b._inflight = 1
rwb_v = RqWb(); rwb_v._inflight = 1

sb = latency_exceeded_baseline(rwb_b, stat_spike_sparse)
sv = latency_exceeded_v5(rwb_v, stat_spike_sparse)

check("TC-C11a: baseline classifies spike as LAT_UNKNOWN_WRITES (not LAT_EXCEEDED)",
      sb == LAT_UNKNOWN_WRITES, f"got {LAT_NAMES[sb]}")
check("TC-C11b: v5 classifies spike as LAT_UNKNOWN (also not LAT_EXCEEDED)",
      sv == LAT_UNKNOWN, f"got {LAT_NAMES[sv]}")

print(f"  baseline: {LAT_NAMES[sb]} — scale_up (makes things worse!)")
print(f"  v5:       {LAT_NAMES[sv]} — noop (neutral; doesn't help or hurt)")
print()
print("  CRITICAL FINDING: Neither baseline nor v5 can detect a latency spike")
print("  when write samples are sparse (stat_sample_valid=False).")
print("  The stat[READ].min comparison at line ~347 is only reached when")
print("  stat_sample_valid()==True, which requires >= 3 write completions.")
print("  Under sparse writes, no implementation here can return LAT_EXCEEDED.")
print("  Baseline makes it worse (scale_up). v5 is neutral (noop).")
print("  The mean-candidate proposal (TC-C12) would address this.")
print()

# ---------------------------------------------------------------------------
# TC-C12: Mean-based candidate — correctness baseline
# The proposed next step from the audit: in the !stat_sample_valid branch,
# use r_mean > target AND r_samples >= 3 to return LAT_EXCEEDED.
# This would give the controller a throttle response under sparse writes.
# ---------------------------------------------------------------------------
print("=" * 70)
print("TC-C12: Mean-based candidate — latency spike detection under sparse writes")
print("=" * 70)

def latency_exceeded_mean_cand(rwb, stat):
    if rwb._sync_issue_lat > rwb.min_lat_nsec and not stat[READ].nr_samples:
        return LAT_EXCEEDED
    if not stat_sample_valid(stat):
        if not stat[READ].nr_samples and (stat[WRITE].nr_samples or rwb._recent_wait or rwb._inflight):
            return LAT_UNKNOWN_WRITES
        # Proposed extension: mean-based read latency detection
        if stat[READ].nr_samples >= 3 and stat[READ].mean > rwb.min_lat_nsec:
            return LAT_EXCEEDED
        return LAT_UNKNOWN
    if stat[READ].min > rwb.min_lat_nsec:
        return LAT_EXCEEDED
    return LAT_OK

stat_spike = make_stat(r_samples=5, r_min=50_000, r_mean=80_000_000, w_samples=1)
rwb_m = RqWb(); rwb_m._inflight = 1
sm = latency_exceeded_mean_cand(rwb_m, stat_spike)
check("TC-C12a: mean-candidate returns LAT_EXCEEDED for spike (r_mean=80ms > 2ms target)",
      sm == LAT_EXCEEDED, f"got {LAT_NAMES[sm]}")

# Normal fast reads — must not false-trigger
stat_fast = make_stat(r_samples=5, r_min=100_000, r_mean=400_000, w_samples=1)
rwb_m = RqWb(); rwb_m._inflight = 1
sm = latency_exceeded_mean_cand(rwb_m, stat_fast)
check("TC-C12b: mean-candidate returns LAT_UNKNOWN for fast reads (r_mean=0.4ms < 2ms target)",
      sm == LAT_UNKNOWN, f"got {LAT_NAMES[sm]}")

# Insufficient read samples (< 3) — must not trigger
stat_few = make_stat(r_samples=2, r_min=100_000, r_mean=80_000_000, w_samples=1)
rwb_m = RqWb(); rwb_m._inflight = 1
sm = latency_exceeded_mean_cand(rwb_m, stat_few)
check("TC-C12c: mean-candidate does not trigger with < 3 read samples",
      sm == LAT_UNKNOWN, f"got {LAT_NAMES[sm]}")

print()
print("  DESIGN NOTE: mean-candidate requires kernel blk_rq_stat.mean field,")
print("  which EXISTS in struct blk_rq_stat (verified in kernel source).")
print("  Implementation would require adding the mean check to latency_exceeded().")
print()

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print("=" * 70)
print(f"RESULTS: {passed} passed, {failed} failed")
print("=" * 70)

if failed == 0:
    print()
    print("ALL CORRECTNESS TESTS PASSED")
    print()
    print("Phase C Summary — v5 reclassification correctness findings:")
    print()
    print("SAFE behaviors (verified):")
    print("  1. Pure write workloads: identical to baseline (LAT_UNKNOWN_WRITES preserved)")
    print("  2. Write-depth recovery: BUMP mechanism still works from any scale_step")
    print("  3. wb_recent_wait without reads: unchanged (LAT_UNKNOWN_WRITES preserved)")
    print("  4. No write starvation: step stays at center (0), not suppressed below")
    print("  5. Transient reads: write scaling resumes immediately when reads leave")
    print()
    print("CHANGED behaviors (intentional):")
    print("  6. Mixed read+sparse-write windows: LAT_UNKNOWN instead of LAT_UNKNOWN_WRITES")
    print("     Consequence: no scale_up during concurrent read activity with sparse writes")
    print("  7. Alternating read/no-read: write depth expands at half rate vs baseline")
    print("  8. wb_recent_wait WITH reads: LAT_UNKNOWN (was LAT_UNKNOWN_WRITES)")
    print()
    print("LIMITATIONS (not fixed by v5):")
    print("  9. Latency spikes under sparse writes: neither baseline nor v5 detects them")
    print("     (stat_sample_valid=False prevents reaching stat[READ].min comparison)")
    print(" 10. r_min predicate blindness: still in the stat_sample_valid path; unchanged")
    print()
    print("STRONGER than prototype guard:")
    print(" 11. v5 suppresses ALL write scaling when reads present, not just elevated reads")
    print("     Prototype: only blocks scale_up when r_min > target")
    print("     v5: blocks scale_up on ALL mixed windows (r_min < or > target)")
    print()
    print("NEXT: mean-based predicate (TC-C12) is the correct follow-on,")
    print("      addressing limitation #9 above.")
    sys.exit(0)
else:
    print("\nSOME TESTS FAILED — review output above")
    sys.exit(1)
