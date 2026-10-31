"""
test_candidate_state_transition.py
----------------------------------
Source-faithful model of WBT latency_exceeded(), wb_timer_fn(), and scaling helpers.
Tests 5 critical state transitions:
  1. no reads + writes (pure writes)
  2. reads + sparse writes (< RWB_MIN_WRITE_SAMPLES)
  3. reads + sufficient writes (>= RWB_MIN_WRITE_SAMPLES)
  4. negative scaling followed by reads
  5. writer completion
"""

RWB_MIN_WRITE_SAMPLES = 3
RWB_UNKNOWN_BUMP      = 5
RWB_DEF_DEPTH         = 16

READ  = 0
WRITE = 1

# Latency status enum (block/blk-wbt.c)
LAT_OK             = 1
LAT_UNKNOWN        = 2
LAT_UNKNOWN_WRITES = 3
LAT_EXCEEDED       = 4

class BlkRqStat:
    def __init__(self, nr_samples=0, min_ns=0, mean_ns=0, max_ns=0):
        self.nr_samples = nr_samples
        self.min = min_ns
        self.mean = mean_ns
        self.max = max_ns

class RqDepth:
    def __init__(self, queue_depth=64, default_depth=RWB_DEF_DEPTH):
        self.queue_depth = queue_depth
        self.default_depth = default_depth
        self.scale_step = 0
        self.scaled_max = False
        self.max_depth = default_depth

    def calc_max_depth(self) -> bool:
        ret = False
        if self.queue_depth == 1:
            if self.scale_step > 0:
                self.max_depth = 1
            else:
                self.max_depth = 2
                ret = True
        else:
            depth = min(self.default_depth, self.queue_depth)
            if self.scale_step > 0:
                depth = 1 + ((depth - 1) >> min(31, self.scale_step))
            elif self.scale_step < 0:
                maxd = 3 * self.queue_depth // 4
                depth = 1 + ((depth - 1) << -self.scale_step)
                if depth > maxd:
                    depth = maxd
                    ret = True
            self.max_depth = depth
        return ret

    def scale_up(self) -> bool:
        if self.scaled_max:
            return False
        self.scale_step -= 1
        self.scaled_max = self.calc_max_depth()
        return True

    def scale_down(self, hard_throttle: bool) -> bool:
        if self.max_depth == 1:
            return False
        if self.scale_step < 0 and hard_throttle:
            self.scale_step = 0
        else:
            self.scale_step += 1
        self.scaled_max = False
        self.calc_max_depth()
        return True

class RqWb:
    def __init__(self, min_lat_nsec=2_000_000, queue_depth=64):
        self.min_lat_nsec = min_lat_nsec
        self.rq_depth = RqDepth(queue_depth=queue_depth)
        self.unknown_cnt = 0
        self.wb_normal = 0
        self.wb_background = 0
        self.calc_wb_limits()

    def calc_wb_limits(self):
        if self.min_lat_nsec == 0:
            self.wb_normal = self.wb_background = 0
        elif self.rq_depth.max_depth <= 2:
            self.wb_normal = self.rq_depth.max_depth
            self.wb_background = 1
        else:
            self.wb_normal = (self.rq_depth.max_depth + 1) // 2
            self.wb_background = (self.rq_depth.max_depth + 3) // 4

    def scale_up(self):
        if not self.rq_depth.scale_up():
            return False
        self.calc_wb_limits()
        self.unknown_cnt = 0
        return True

    def scale_down(self, hard_throttle: bool):
        if not self.rq_depth.scale_down(hard_throttle):
            return False
        self.calc_wb_limits()
        self.unknown_cnt = 0
        return True

def stat_sample_valid(stat) -> bool:
    return (stat[READ].nr_samples >= 1 and
            stat[WRITE].nr_samples >= RWB_MIN_WRITE_SAMPLES)

# ---------------------------------------------------------------------------
# Baseline implementations
# ---------------------------------------------------------------------------
def latency_exceeded_baseline(rwb: RqWb, stat, inflight_writes: int = 0) -> int:
    if not stat_sample_valid(stat):
        if stat[WRITE].nr_samples or inflight_writes:
            return LAT_UNKNOWN_WRITES
        return LAT_UNKNOWN
    if stat[READ].min > rwb.min_lat_nsec:
        return LAT_EXCEEDED
    return LAT_OK

def wb_timer_fn_baseline(rwb: RqWb, stat, inflight_writes: int = 0) -> str:
    status = latency_exceeded_baseline(rwb, stat, inflight_writes)
    if status == LAT_EXCEEDED:
        rwb.scale_down(hard_throttle=True)
        return "scale_down(hard=True)"
    elif status == LAT_OK:
        rwb.scale_up()
        return "scale_up"
    elif status == LAT_UNKNOWN_WRITES:
        rwb.scale_up()
        return "scale_up"
    elif status == LAT_UNKNOWN:
        rwb.unknown_cnt += 1
        if rwb.unknown_cnt < RWB_UNKNOWN_BUMP:
            return "unknown_wait"
        if rwb.rq_depth.scale_step > 0:
            rwb.scale_up()
            return "scale_up_center"
        elif rwb.rq_depth.scale_step < 0:
            rwb.scale_down(hard_throttle=False)
            return "scale_down_center"
        return "unknown_center_noop"
    return "noop"

# ---------------------------------------------------------------------------
# Candidate Fix: Classification fix in latency_exceeded()
# ---------------------------------------------------------------------------
def latency_exceeded_candidate(rwb: RqWb, stat, inflight_writes: int = 0) -> int:
    if not stat_sample_valid(stat):
        # Only classify as LAT_UNKNOWN_WRITES if there are truly NO read samples
        if not stat[READ].nr_samples and (stat[WRITE].nr_samples or inflight_writes):
            return LAT_UNKNOWN_WRITES
        return LAT_UNKNOWN
    if stat[READ].min > rwb.min_lat_nsec:
        return LAT_EXCEEDED
    return LAT_OK

def wb_timer_fn_candidate(rwb: RqWb, stat, inflight_writes: int = 0) -> str:
    status = latency_exceeded_candidate(rwb, stat, inflight_writes)
    if status == LAT_EXCEEDED:
        rwb.scale_down(hard_throttle=True)
        return "scale_down(hard=True)"
    elif status == LAT_OK:
        rwb.scale_up()
        return "scale_up"
    elif status == LAT_UNKNOWN_WRITES:
        rwb.scale_up()
        return "scale_up"
    elif status == LAT_UNKNOWN:
        rwb.unknown_cnt += 1
        if rwb.unknown_cnt < RWB_UNKNOWN_BUMP:
            return "unknown_wait"
        if rwb.rq_depth.scale_step > 0:
            rwb.scale_up()
            return "scale_up_center"
        elif rwb.rq_depth.scale_step < 0:
            rwb.scale_down(hard_throttle=False)
            return "scale_down_center"
        return "unknown_center_noop"
    return "noop"

def main():
    print("=" * 80)
    print("VERIFICATION OF 5 WBT STATE TRANSITIONS: BASELINE vs CANDIDATE FIX")
    print("=" * 80)

    # 1. no reads + writes (pure writes)
    print("\n--- Test 1: No Reads + Writes (Pure Writes) ---")
    stat = [BlkRqStat(nr_samples=0), BlkRqStat(nr_samples=10, min_ns=100_000)]
    rwb_b = RqWb(); rwb_c = RqWb()
    res_b = wb_timer_fn_baseline(rwb_b, stat)
    res_c = wb_timer_fn_candidate(rwb_c, stat)
    print(f"Baseline:  action={res_b:<15} step={rwb_b.rq_depth.scale_step} depth={rwb_b.rq_depth.max_depth}")
    print(f"Candidate: action={res_c:<15} step={rwb_c.rq_depth.scale_step} depth={rwb_c.rq_depth.max_depth}")
    assert res_b == res_c == "scale_up", "Test 1 failed: pure writes must scale up"
    assert rwb_b.rq_depth.scale_step == rwb_c.rq_depth.scale_step == -1
    print("[PASS] Pure writes allow negative scaling in both baseline and candidate.")

    # 2. reads + sparse writes (< RWB_MIN_WRITE_SAMPLES)
    print("\n--- Test 2: Active Reads + Sparse Writes (e.g. 50 reads, 1 write) ---")
    # Read latency elevated (5 ms > 2 ms target), but write samples = 1 (< 3)
    stat = [BlkRqStat(nr_samples=50, min_ns=5_000_000), BlkRqStat(nr_samples=1, min_ns=100_000)]
    rwb_b = RqWb(); rwb_c = RqWb()
    res_b = wb_timer_fn_baseline(rwb_b, stat)
    res_c = wb_timer_fn_candidate(rwb_c, stat)
    print(f"Baseline:  action={res_b:<15} step={rwb_b.rq_depth.scale_step} depth={rwb_b.rq_depth.max_depth}")
    print(f"Candidate: action={res_c:<15} step={rwb_c.rq_depth.scale_step} depth={rwb_c.rq_depth.max_depth}")
    assert res_b == "scale_up", "Baseline should scale up (demonstrating the bug)"
    assert rwb_b.rq_depth.scale_step == -1, "Baseline incorrectly decremented scale_step"
    assert res_c == "unknown_wait", "Candidate should treat sparse write mix as LAT_UNKNOWN (wait)"
    assert rwb_c.rq_depth.scale_step == 0, "Candidate preserved scale_step == 0"
    print("[PASS] Candidate prevents negative scaling when active reads are present.")

    # 3. reads + sufficient writes (>= RWB_MIN_WRITE_SAMPLES)
    print("\n--- Test 3: Active Reads + Sufficient Writes (stat_sample_valid == True) ---")
    # Subcase 3a: Latency exceeded (5ms > 2ms)
    stat_exc = [BlkRqStat(nr_samples=50, min_ns=5_000_000), BlkRqStat(nr_samples=5, min_ns=100_000)]
    rwb_b = RqWb(); rwb_c = RqWb()
    res_b = wb_timer_fn_baseline(rwb_b, stat_exc)
    res_c = wb_timer_fn_candidate(rwb_c, stat_exc)
    print(f"3a (Latency Exceeded):")
    print(f"  Baseline:  action={res_b:<15} step={rwb_b.rq_depth.scale_step} depth={rwb_b.rq_depth.max_depth}")
    print(f"  Candidate: action={res_c:<15} step={rwb_c.rq_depth.scale_step} depth={rwb_c.rq_depth.max_depth}")
    assert res_b == res_c == "scale_down(hard=True)"
    assert rwb_b.rq_depth.scale_step == rwb_c.rq_depth.scale_step == 1

    # Subcase 3b: Latency OK (500us < 2ms)
    stat_ok = [BlkRqStat(nr_samples=50, min_ns=500_000), BlkRqStat(nr_samples=5, min_ns=100_000)]
    rwb_b = RqWb(); rwb_c = RqWb()
    res_b = wb_timer_fn_baseline(rwb_b, stat_ok)
    res_c = wb_timer_fn_candidate(rwb_c, stat_ok)
    print(f"3b (Latency OK):")
    print(f"  Baseline:  action={res_b:<15} step={rwb_b.rq_depth.scale_step} depth={rwb_b.rq_depth.max_depth}")
    print(f"  Candidate: action={res_c:<15} step={rwb_c.rq_depth.scale_step} depth={rwb_c.rq_depth.max_depth}")
    assert res_b == res_c == "scale_up"
    print("[PASS] Full read/write mix behavior is completely identical between baseline and candidate.")

    # 4. negative scaling followed by reads
    print("\n--- Test 4: Prior Negative Scaling Followed by Sparse Writes + Reads ---")
    # Simulate system that ran pure writes and reached scale_step = -2, max_depth = 48
    rwb_b = RqWb(); rwb_b.rq_depth.scale_step = -2; rwb_b.rq_depth.calc_max_depth()
    rwb_c = RqWb(); rwb_c.rq_depth.scale_step = -2; rwb_c.rq_depth.calc_max_depth()
    print(f"Initial state: scale_step={rwb_b.rq_depth.scale_step}, max_depth={rwb_b.rq_depth.max_depth}")

    # Now reads appear with sparse writes (50 reads, 1 write)
    stat = [BlkRqStat(nr_samples=50, min_ns=3_000_000), BlkRqStat(nr_samples=1, min_ns=100_000)]
    res_b = wb_timer_fn_baseline(rwb_b, stat)
    res_c = wb_timer_fn_candidate(rwb_c, stat)
    print(f"After Window 1:")
    print(f"  Baseline:  action={res_b:<15} step={rwb_b.rq_depth.scale_step} depth={rwb_b.rq_depth.max_depth}")
    print(f"  Candidate: action={res_c:<15} step={rwb_c.rq_depth.scale_step} depth={rwb_c.rq_depth.max_depth}")
    assert rwb_b.rq_depth.scale_step == -3, "Baseline decremented further negative (-2 -> -3)"
    assert rwb_c.rq_depth.scale_step == -2, "Candidate held -2 without escalating further"
    print("[PASS] Candidate prevents escalation of negative scaling during read traffic.")

    # 5. writer completion (all writers stop, queue drains)
    print("\n--- Test 5: Writer Completion (No Writes In Flight or Sampled) ---")
    # rwb starts at step = -2
    rwb_b = RqWb(); rwb_b.rq_depth.scale_step = -2; rwb_b.rq_depth.calc_max_depth()
    rwb_c = RqWb(); rwb_c.rq_depth.scale_step = -2; rwb_c.rq_depth.calc_max_depth()
    stat_empty = [BlkRqStat(nr_samples=0), BlkRqStat(nr_samples=0)]
    for w in range(1, 6):
        res_b = wb_timer_fn_baseline(rwb_b, stat_empty, inflight_writes=0)
        res_c = wb_timer_fn_candidate(rwb_c, stat_empty, inflight_writes=0)
        print(f"Window {w}: Baseline={res_b:<18} (step={rwb_b.rq_depth.scale_step}) | Candidate={res_c:<18} (step={rwb_c.rq_depth.scale_step})")
    assert rwb_b.rq_depth.scale_step == rwb_c.rq_depth.scale_step == -1
    print("[PASS] Both baseline and candidate gradually recover from negative scaling after 5 idle windows.")

if __name__ == '__main__':
    main()
