#!/usr/bin/env python3
"""Causation analysis: do the P99 reader latency spikes actually overlap
with v5's delayed writeback burst?

For each of the 10 logs, extract:
1. The full wbt_timer timeline (ts, status, step, inflight)
2. When status transitions from 2→3 (the moment v5's gate fails)
3. When scale_up events occur (the burst)
4. Reader probe statistics (P50, P99, MAX per reader)
5. Whether inflight > 0 during the burst (readers still active?)

The critical question: are reader requests still in flight when v5
begins its write-depth escalation at t~23.4s?

Usage: python3 scripts/causation_analysis.py <log_dir>
"""

import re
import os
import sys

timer_re = re.compile(
    r"(\d+\.\d+):\s+wbt_timer:\s+8:0:\s+status=(\d+),\s+step=(-?\d+),\s+inflight=(\d+)"
)
step_re = re.compile(
    r"(\d+\.\d+):\s+wbt_step:\s+8:0:\s+(.+?):\s+step=(-?\d+)"
)
probe_re = re.compile(
    r"\[PROBE_RESULT\] SAMPLES=(\d+) MIN_US=(\d+) P50_US=(\d+) P90_US=(\d+) P99_US=(\d+) MAX_US=(\d+) MEAN_US=([\d.]+)"
)
writer_re = re.compile(r"\[WRITER_RESULT\] OPS=(\d+) BYTES=(\d+)")


def analyze_log(path):
    with open(path, "r", errors="replace") as f:
        txt = f.read()

    timers = []
    for m in timer_re.finditer(txt):
        timers.append({
            "ts": float(m.group(1)),
            "status": int(m.group(2)),
            "step": int(m.group(3)),
            "inflight": int(m.group(4)),
        })

    steps = []
    for m in step_re.finditer(txt):
        steps.append({
            "ts": float(m.group(1)),
            "action": m.group(2).strip(),
            "step": int(m.group(3)),
        })

    probes = []
    for m in probe_re.finditer(txt):
        probes.append({
            "samples": int(m.group(1)),
            "p50": int(m.group(3)),
            "p90": int(m.group(4)),
            "p99": int(m.group(5)),
            "max": int(m.group(6)),
            "mean": float(m.group(7)),
        })

    writs = []
    for m in writer_re.finditer(txt):
        writs.append({"ops": int(m.group(1)), "bytes": int(m.group(2))})

    # Find the transition from status=2 to status=3 (v5 gate failing)
    first_s3 = None
    for t in timers:
        if t["status"] == 3:
            first_s3 = t["ts"]
            break

    # Find first scale_up step event (the burst start)
    first_scale_up = None
    for s in steps:
        if "scale up" in s["action"]:
            first_scale_up = s["ts"]
            break

    # Find the last timer event (approximate end of traced activity)
    last_timer = timers[-1]["ts"] if timers else 0
    first_timer = timers[0]["ts"] if timers else 0

    # Check inflight values during the burst period
    # Burst = from first_s3 to first_s3 + 2s
    burst_inflight = []
    if first_s3:
        for t in timers:
            if first_s3 <= t["ts"] <= first_s3 + 2.0:
                burst_inflight.append((t["ts"], t["inflight"]))

    # Find when readers would have stopped:
    # The init script runs readers for 20s from when they start.
    # The first wbt_timer event gives approximate workload start.
    # Reader duration is 20s, so readers stop at first_timer + ~20s.
    # But the actual start may be a few seconds after boot.
    reader_start_approx = first_timer
    reader_end_approx = first_timer + 20.0  # 20s reader duration

    # Compute per-reader total measurement time
    # Each reader does samples * (mean pacing) ≈ samples * (mean_us/1e6) s
    # But mean_us is the latency, not the pacing. Pacing is 1000us = 1ms.
    # Total time ≈ samples * (pacing + latency) / 1e6
    reader_durations = []
    for p in probes:
        # Each read: pacing (1000us) + latency (mean_us)
        duration_s = p["samples"] * (1000 + p["mean"]) / 1e6
        reader_durations.append(duration_s)

    max_reader_duration = max(reader_durations) if reader_durations else 0
    min_reader_duration = min(reader_durations) if reader_durations else 0

    # Writer: total time = ops * sleep_ms / 1000 (ignoring actual I/O time)
    # With 80ms sleep, 20s duration: ops should be ~250
    writer_ops = sum(w["ops"] for w in writs) if writs else 0

    return {
        "path": os.path.basename(path),
        "first_timer": first_timer,
        "last_timer": last_timer,
        "first_s3": first_s3,
        "first_scale_up": first_scale_up,
        "reader_end_approx": reader_end_approx,
        "burst_inflight": burst_inflight,
        "probes": probes,
        "reader_durations": reader_durations,
        "max_reader_duration": max_reader_duration,
        "min_reader_duration": min_reader_duration,
        "writer_ops": writer_ops,
        "n_timers": len(timers),
        "n_steps": len(steps),
    }


if len(sys.argv) < 2:
    print("Usage: python3 causation_analysis.py <log_dir>")
    sys.exit(1)

logdir = sys.argv[1]

print("=" * 100)
print("CAUSATION ANALYSIS: Do P99 reader latency spikes overlap with v5's writeback burst?")
print("=" * 100)

for label in ["baseline", "v5"]:
    for rep in range(1, 6):
        path = os.path.join(logdir, f"{label}_rep{rep}.log")
        if not os.path.exists(path):
            continue
        a = analyze_log(path)

        print(f"\n{'='*100}")
        print(f"  {label}_rep{rep}")
        print(f"{'='*100}")
        print(f"  First wbt_timer:          t={a['first_timer']:.3f}s")
        print(f"  Last wbt_timer:           t={a['last_timer']:.3f}s")
        print(f"  Total timer events:       {a['n_timers']}")
        print(f"  Total step events:        {a['n_steps']}")
        s3_str = f"t={a['first_s3']:.3f}s" if a['first_s3'] else 'never'
        su_str = f"t={a['first_scale_up']:.3f}s" if a['first_scale_up'] else 'never'
        print(f"  First status=3 (UNKNOWN_WRITES): {s3_str}")
        print(f"  First scale_up:           {su_str}")

        # Reader timing
        print(f"\n  Reader probe analysis:")
        for i, (p, rd) in enumerate(zip(a["probes"], a["reader_durations"])):
            print(f"    reader[{i+1}]: samples={p['samples']} P50={p['p50']}us P99={p['p99']}us MAX={p['max']}us mean={p['mean']:.1f}us est_duration={rd:.2f}s")
        print(f"  Max reader est duration: {a['max_reader_duration']:.2f}s")
        print(f"  Min reader est duration: {a['min_reader_duration']:.2f}s")

        # Reader end estimate
        reader_end_low = a["first_timer"] + a["min_reader_duration"]
        reader_end_high = a["first_timer"] + a["max_reader_duration"]
        print(f"  Reader end estimate:     {reader_end_low:.2f}s - {reader_end_high:.2f}s (from first_timer + duration)")

        # Burst overlap check
        if a["first_s3"]:
            burst_start = a["first_s3"]
            burst_end = a["first_s3"] + 2.0
            print(f"\n  Burst period:             t={burst_start:.3f}s - {burst_end:.3f}s")
            print(f"  Inflight during burst:")
            for ts, inflight in a["burst_inflight"]:
                marker = " <-- WRITES IN FLIGHT" if inflight > 0 else ""
                print(f"    t={ts:.3f}s  inflight={inflight}{marker}")

            # Check overlap
            if reader_end_high > burst_start:
                overlap = reader_end_high - burst_start
                print(f"\n  OVERLAP: readers may still be active for ~{overlap:.2f}s after burst starts")
                print(f"  (reader_end_high={reader_end_high:.2f}s > burst_start={burst_start:.2f}s)")
            else:
                gap = burst_start - reader_end_high
                print(f"\n  NO OVERLAP: readers finished ~{gap:.2f}s before burst starts")
                print(f"  (reader_end_high={reader_end_high:.2f}s < burst_start={burst_start:.2f}s)")

            # Also check: is the P99 measured during or after the burst?
            # P99 is the 99th percentile of ALL samples collected by the reader.
            # If the reader ran from first_timer to reader_end, and the burst
            # starts at first_s3, then the burst overlaps the reader's measurement
            # window only if first_s3 < reader_end.
            if reader_end_high > burst_start:
                pct_overlap = (overlap / a["max_reader_duration"]) * 100
                print(f"  Burst overlaps ~{pct_overlap:.1f}% of the reader's measurement window")
            else:
                print(f"  Burst occurs AFTER all readers have completed their measurements")
        else:
            print(f"\n  No status=3 events (no burst)")

print(f"\n{'='*100}")
print("SUMMARY: Per-run overlap analysis")
print(f"{'='*100}")
print(f"{'Run':<18} {'Burst start':>12} {'Reader end (high est)':>22} {'Overlap?':>10} {'Gap/Overlap(s)':>15}")
print("-" * 80)

for rep in range(1, 6):
    for label in ["baseline", "v5"]:
        path = os.path.join(logdir, f"{label}_rep{rep}.log")
        if not os.path.exists(path):
            continue
        a = analyze_log(path)

        if a["first_s3"]:
            burst_start = a["first_s3"]
            reader_end_high = a["first_timer"] + a["max_reader_duration"]
            if reader_end_high > burst_start:
                overlap = reader_end_high - burst_start
                print(f"{label}_rep{rep:<8} {burst_start:>12.3f} {reader_end_high:>22.2f} {'YES':>10} {overlap:>+15.2f}")
            else:
                gap = burst_start - reader_end_high
                print(f"{label}_rep{rep:<8} {burst_start:>12.3f} {reader_end_high:>22.2f} {'NO':>10} {gap:>+15.2f}")
        else:
            # For baseline, the "burst" is at the beginning (early escalation)
            if a["first_scale_up"]:
                burst_start = a["first_scale_up"]
                reader_end_high = a["first_timer"] + a["max_reader_duration"]
                # Baseline escalates early, so it's always within the reader window
                print(f"{label}_rep{rep:<8} {burst_start:>12.3f} {reader_end_high:>22.2f} {'YES':>10} {'(early)':>15}")
            else:
                print(f"{label}_rep{rep:<8} {'N/A':>12} {reader_end_high:>22.2f} {'N/A':>10} {'N/A':>15}")
