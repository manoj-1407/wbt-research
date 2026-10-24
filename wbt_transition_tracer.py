"""
Source-Faithful WBT State Transition Tracer
Directly derived from Linux kernel 6.6.y (block/blk-wbt.c and block/blk-stat.c)
at pinned Microsoft WSL2 commit a07f9ea8a99139913acbcc1c160b132cb2a49c81.

Traces exact classifier evaluation order, intermediate predicates,
and controller action for both Baseline and Patched WBT.
"""

from dataclasses import dataclass
from typing import Optional, Tuple, Dict, Any

# Constants from block/blk-wbt.c
RWB_MIN_WRITE_SAMPLES = 3
RWB_UNKNOWN_BUMP = 3
WBT_NUM_RWQ = 2

# Status codes from block/blk-wbt.c
LAT_OK = 1
LAT_UNKNOWN = 2
LAT_UNKNOWN_WRITES = 3
LAT_EXCEEDED = 4

STATUS_NAMES = {
    LAT_OK: "LAT_OK",
    LAT_UNKNOWN: "LAT_UNKNOWN",
    LAT_UNKNOWN_WRITES: "LAT_UNKNOWN_WRITES",
    LAT_EXCEEDED: "LAT_EXCEEDED",
}

READ = 0
WRITE = 1

@dataclass
class WindowInput:
    window_id: int
    read_samples: int
    read_min_ns: int
    write_samples: int
    write_min_ns: int
    recent_writeback_wait: bool
    writes_inflight: int
    sync_read_issue_lat_ns: int  # 0 if no pending sync read

@dataclass
class TraceResult:
    window_id: int
    watchdog_fired: bool
    sample_valid: bool
    classifier_status: str
    action_baseline: str
    scale_step_baseline: int
    action_patched: str
    scale_step_patched: int
    notes: str

class RqWbState:
    def __init__(self, min_lat_nsec: int = 2_000_000, win_nsec: int = 100_000_000):
        self.min_lat_nsec = min_lat_nsec
        self.win_nsec = win_nsec
        self.cur_win_nsec = win_nsec
        self.scale_step = 0
        self.unknown_cnt = 0

    def scale_up(self):
        # rq_depth_scale_up() decrements step towards negative (more permissive)
        self.scale_step -= 1
        self.unknown_cnt = 0

    def scale_down(self, hard_throttle: bool):
        # rq_depth_scale_down() increments step towards positive (more throttled)
        self.scale_step += 1
        self.unknown_cnt = 0

def trace_window(rwb_b: RqWbState, rwb_p: RqWbState, inp: WindowInput) -> TraceResult:
    # -------------------------------------------------------------------------
    # 1. Watchdog check (block/blk-wbt.c:319-324)
    # -------------------------------------------------------------------------
    thislat = inp.sync_read_issue_lat_ns
    watchdog_fired = False
    status_baseline = None
    status_patched = None

    if thislat > rwb_b.cur_win_nsec or (thislat > rwb_b.min_lat_nsec and not inp.read_samples):
        watchdog_fired = True
        status_baseline = LAT_EXCEEDED
        status_patched = LAT_EXCEEDED

    # -------------------------------------------------------------------------
    # 2. Sample validity check (block/blk-wbt.c:264-274, 329)
    # -------------------------------------------------------------------------
    sample_valid = (inp.read_samples >= 1 and inp.write_samples >= RWB_MIN_WRITE_SAMPLES)

    if not watchdog_fired:
        if not sample_valid:
            # -----------------------------------------------------------------
            # 3. Invalid sample fallback (block/blk-wbt.c:336-340)
            # -----------------------------------------------------------------
            if inp.write_samples or inp.recent_writeback_wait or inp.writes_inflight:
                status_baseline = LAT_UNKNOWN_WRITES
                status_patched = LAT_UNKNOWN_WRITES
            else:
                status_baseline = LAT_UNKNOWN
                status_patched = LAT_UNKNOWN
        else:
            # -----------------------------------------------------------------
            # 4. Valid sample: compare read minimum against target (blk-wbt.c:345)
            # -----------------------------------------------------------------
            if inp.read_min_ns > rwb_b.min_lat_nsec:
                status_baseline = LAT_EXCEEDED
                status_patched = LAT_EXCEEDED
            else:
                status_baseline = LAT_OK
                status_patched = LAT_OK

    # -------------------------------------------------------------------------
    # 5. Controller Action (wb_timer_fn, blk-wbt.c:441-471)
    # -------------------------------------------------------------------------
    # BASELINE action
    action_b = "noop"
    if status_baseline == LAT_EXCEEDED:
        rwb_b.scale_down(hard_throttle=True)
        action_b = "scale_down(hard=True)"
    elif status_baseline == LAT_OK:
        rwb_b.scale_up()
        action_b = "scale_up()"
    elif status_baseline == LAT_UNKNOWN_WRITES:
        rwb_b.scale_up()
        action_b = "scale_up()"
    elif status_baseline == LAT_UNKNOWN:
        rwb_b.unknown_cnt += 1
        if rwb_b.unknown_cnt >= RWB_UNKNOWN_BUMP:
            if rwb_b.scale_step > 0:
                rwb_b.scale_up()
                action_b = "scale_up(return_to_center)"
            elif rwb_b.scale_step < 0:
                rwb_b.scale_down(hard_throttle=False)
                action_b = "scale_down(return_to_center)"

    # PATCHED action
    action_p = "noop"
    if status_patched == LAT_EXCEEDED:
        rwb_p.scale_down(hard_throttle=True)
        action_p = "scale_down(hard=True)"
    elif status_patched == LAT_OK:
        rwb_p.scale_up()
        action_p = "scale_up()"
    elif status_patched == LAT_UNKNOWN_WRITES:
        # ---- PATCH GUARD ----
        if inp.read_samples and inp.read_min_ns > rwb_p.min_lat_nsec:
            # Hold current depth: break; no scale_up, no scale_down.
            action_p = "hold()[PATCHED]"
        else:
            rwb_p.scale_up()
            action_p = "scale_up()"
    elif status_patched == LAT_UNKNOWN:
        rwb_p.unknown_cnt += 1
        if rwb_p.unknown_cnt >= RWB_UNKNOWN_BUMP:
            if rwb_p.scale_step > 0:
                rwb_p.scale_up()
                action_p = "scale_up(return_to_center)"
            elif rwb_p.scale_step < 0:
                rwb_p.scale_down(hard_throttle=False)
                action_p = "scale_down(return_to_center)"

    notes = []
    if status_baseline == LAT_UNKNOWN_WRITES:
        if inp.read_samples and inp.read_min_ns > rwb_b.min_lat_nsec:
            notes.append("DIVERGENCE: elevated reads discarded due to sparse writes")
        elif not inp.read_samples:
            notes.append("Pure write: both scale up as intended")

    return TraceResult(
        window_id=inp.window_id,
        watchdog_fired=watchdog_fired,
        sample_valid=sample_valid,
        classifier_status=STATUS_NAMES[status_baseline],
        action_baseline=action_b,
        scale_step_baseline=rwb_b.scale_step,
        action_patched=action_p,
        scale_step_patched=rwb_p.scale_step,
        notes="; ".join(notes) if notes else "nominal",
    )

def main():
    print("=" * 80)
    print("SOURCE-FAITHFUL WBT TRANSITION TRACE")
    print("Pinned Commit: a07f9ea8a99139913acbcc1c160b132cb2a49c81 (WSL2 6.6.y)")
    print("Configured min_lat_nsec = 2,000,000 ns (2.0 ms), window = 100 ms")
    print("=" * 80)

    test_cases = [
        (
            "Case 1: Reads present, sparse writes (1 sample), read latency elevated (8 ms)",
            WindowInput(
                window_id=1,
                read_samples=50,
                read_min_ns=8_000_000,  # 8 ms (> 2 ms target)
                write_samples=1,       # < 3 (sparse write completion)
                write_min_ns=50_000_000,
                recent_writeback_wait=False,
                writes_inflight=1,
                sync_read_issue_lat_ns=0,
            )
        ),
        (
            "Case 2: Reads present, sufficient writes (5 samples), read latency elevated (8 ms)",
            WindowInput(
                window_id=2,
                read_samples=50,
                read_min_ns=8_000_000,  # 8 ms (> 2 ms target)
                write_samples=5,       # >= 3 (sufficient writes)
                write_min_ns=15_000_000,
                recent_writeback_wait=False,
                writes_inflight=2,
                sync_read_issue_lat_ns=0,
            )
        ),
        (
            "Case 3: No completed reads, slow in-flight sync read (15 ms), watchdog trigger",
            WindowInput(
                window_id=3,
                read_samples=0,        # No completions
                read_min_ns=0,
                write_samples=10,
                write_min_ns=5_000_000,
                recent_writeback_wait=False,
                writes_inflight=5,
                sync_read_issue_lat_ns=15_000_000, # 15 ms (> 2 ms target & 0 read samples)
            )
        ),
        (
            "Case 4: No completed reads, pure streaming write traffic (10 write samples)",
            WindowInput(
                window_id=4,
                read_samples=0,
                read_min_ns=0,
                write_samples=10,
                write_min_ns=10_000_000,
                recent_writeback_wait=False,
                writes_inflight=4,
                sync_read_issue_lat_ns=0,
            )
        ),
        (
            "Case 5: Zero writes completed, zero recent wait, zero writes in-flight (pure idle/read)",
            WindowInput(
                window_id=5,
                read_samples=10,
                read_min_ns=1_000_000,  # 1 ms (< 2 ms)
                write_samples=0,
                write_min_ns=0,
                recent_writeback_wait=False,
                writes_inflight=0,
                sync_read_issue_lat_ns=0,
            )
        ),
    ]

    for title, inp in test_cases:
        rwb_b = RqWbState()
        rwb_p = RqWbState()
        res = trace_window(rwb_b, rwb_p, inp)

        print(f"\n--- {title} ---")
        print(f"  Inputs: read_samples={inp.read_samples}, read_min={inp.read_min_ns/1e6:.1f}ms, "
              f"write_samples={inp.write_samples}, in_flight={inp.writes_inflight}, "
              f"sync_issue={inp.sync_read_issue_lat_ns/1e6:.1f}ms")
        print(f"  Watchdog Fired : {res.watchdog_fired}")
        print(f"  Sample Valid   : {res.sample_valid}")
        print(f"  Classifier     : {res.classifier_status}")
        print(f"  Baseline Action: {res.action_baseline:<32} (scale_step: {res.scale_step_baseline})")
        print(f"  Patched Action : {res.action_patched:<32} (scale_step: {res.scale_step_patched})")
        print(f"  Analysis       : {res.notes}")

if __name__ == "__main__":
    main()
