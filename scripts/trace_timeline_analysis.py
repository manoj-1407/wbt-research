#!/usr/bin/env python3
"""Trace timeline analysis: extract WBT state transitions from raw benchmark logs
and compare baseline vs v5 patterns to understand the P99 regression.

Extracts from each log:
- wbt_timer events (timestamp, status, scale_step, inflight)
- wbt_step events (timestamp, action, step, normal, max)
- Writer PROBE_RESULT timing (when readers finish = workload end marker)
- Computes: step progression, escalation timing, de-escalation timing,
  number of scale_up vs scale_down, time spent at each step level

Usage: python3 scripts/trace_timeline_analysis.py <log_dir>
"""

import re
import os
import sys
import statistics

timer_re = re.compile(
    r"(\d+\.\d+):\s+wbt_timer:\s+8:0:\s+status=(\d+),\s+step=(-?\d+),\s+inflight=(\d+)"
)
step_re = re.compile(
    r"(\d+\.\d+):\s+wbt_step:\s+8:0:\s+(.+?):\s+step=(-?\d+),\s+window=(\d+)us,\s+background=(\d+),\s+normal=(\d+),\s+max=(\d+)"
)
probe_re = re.compile(
    r"\[PROBE_RESULT\] SAMPLES=(\d+) MIN_US=(\d+) P50_US=(\d+) P90_US=(\d+) P99_US=(\d+) MAX_US=(\d+) MEAN_US=([\d.]+)"
)


def parse_trace_timeline(path):
    with open(path, "r", errors="replace") as f:
        txt = f.read()

    timer_events = []
    for m in timer_re.finditer(txt):
        timer_events.append({
            "ts": float(m.group(1)),
            "status": int(m.group(2)),
            "step": int(m.group(3)),
            "inflight": int(m.group(4)),
        })

    step_events = []
    for m in step_re.finditer(txt):
        step_events.append({
            "ts": float(m.group(1)),
            "action": m.group(2).strip(),
            "step": int(m.group(3)),
            "window_us": int(m.group(4)),
            "background": int(m.group(5)),
            "normal": int(m.group(6)),
            "max_depth": int(m.group(7)),
        })

    probes = []
    for m in probe_re.finditer(txt):
        probes.append({
            "samples": int(m.group(1)),
            "p50": int(m.group(3)),
            "p99": int(m.group(5)),
            "max": int(m.group(6)),
            "mean": float(m.group(7)),
        })

    return {
        "timer_events": timer_events,
        "step_events": step_events,
        "probes": probes,
    }


def analyze_timeline(data, label):
    timers = data["timer_events"]
    steps = data["step_events"]

    # Step transitions
    scale_ups = [s for s in steps if "scale up" in s["action"]]
    scale_downs = [s for s in steps if "scale down" in s["action"]]

    # Time at each step level
    step_durations = {}
    for i in range(len(timers) - 1):
        step = timers[i]["step"]
        duration = timers[i + 1]["ts"] - timers[i]["ts"]
        step_durations[step] = step_durations.get(step, 0) + duration

    # Last timer to end of run (approximate using last step event or +0.1s)
    if timers:
        last_ts = timers[-1]["ts"]
        # Find approximate end: last step event or last timer + 1s
        end_ts = max(last_ts, steps[-1]["ts"] if steps else last_ts) + 1.0
        step = timers[-1]["step"]
        step_durations[step] = step_durations.get(step, 0) + (end_ts - last_ts)

    # Status distribution
    status_counts = {}
    for t in timers:
        status_counts[t["status"]] = status_counts.get(t["status"], 0) + 1

    # Escalation timing: when does step first go negative?
    first_negative = None
    for t in timers:
        if t["step"] < 0:
            first_negative = t["ts"]
            break

    # Deepest step reached
    min_step = min(t["step"] for t in timers) if timers else 0

    # How many times step changes
    step_changes = 0
    for i in range(len(timers) - 1):
        if timers[i]["step"] != timers[i + 1]["step"]:
            step_changes += 1

    return {
        "label": label,
        "n_timers": len(timers),
        "n_steps": len(steps),
        "n_scale_ups": len(scale_ups),
        "n_scale_downs": len(scale_downs),
        "status_counts": status_counts,
        "step_durations": step_durations,
        "first_negative_ts": first_negative,
        "min_step": min_step,
        "n_step_changes": step_changes,
        "scale_up_details": [(s["ts"], s["step"], s["normal"]) for s in scale_ups],
        "scale_down_details": [(s["ts"], s["step"], s["normal"]) for s in scale_downs],
        "mean_p99": sum(p["p99"] for p in data["probes"]) / len(data["probes"]) if data["probes"] else 0,
        "max_p99": max(p["p99"] for p in data["probes"]) if data["probes"] else 0,
        "max_max": max(p["max"] for p in data["probes"]) if data["probes"] else 0,
    }


def print_analysis(a):
    print(f"\n{'='*80}")
    print(f"  {a['label']}")
    print(f"{'='*80}")
    print(f"  Timer events:     {a['n_timers']}")
    print(f"  Step transitions: {a['n_steps']} ({a['n_scale_ups']} up, {a['n_scale_downs']} down)")
    print(f"  Step changes:     {a['n_step_changes']}")
    print(f"  Min step reached:  {a['min_step']}")
    print(f"  First negative at: {a['first_negative_ts']:.3f}s" if a['first_negative_ts'] else "  Never went negative")
    print(f"  Status distribution: {a['status_counts']}")
    print(f"  Mean P99:          {a['mean_p99']:.1f} us")
    print(f"  Max P99 (probe):   {a['max_p99']} us")
    print(f"  Max MAX (probe):   {a['max_max']} us")
    print(f"  Time at each step (s):")
    for step in sorted(a["step_durations"].keys()):
        print(f"    step={step:>3}: {a['step_durations'][step]:.3f}s")
    print(f"  Scale-up transitions:")
    for ts, step, normal in a["scale_up_details"]:
        print(f"    t={ts:.3f}s  step={step}  normal={normal}")
    print(f"  Scale-down transitions:")
    for ts, step, normal in a["scale_down_details"]:
        print(f"    t={ts:.3f}s  step={step}  normal={normal}")


if len(sys.argv) < 2:
    print("Usage: python3 trace_timeline_analysis.py <log_dir>")
    sys.exit(1)

logdir = sys.argv[1]

print("=" * 80)
print("WBT TRACE TIMELINE ANALYSIS")
print(f"Log directory: {logdir}")
print("=" * 80)

# Analyze all reps
baseline_analyses = []
v5_analyses = []

for rep in range(1, 6):
    for label, analyses_list in [("baseline", baseline_analyses), ("v5", v5_analyses)]:
        path = os.path.join(logdir, f"{label}_rep{rep}.log")
        if not os.path.exists(path):
            continue
        data = parse_trace_timeline(path)
        a = analyze_timeline(data, f"{label}_rep{rep}")
        print_analysis(a)
        analyses_list.append(a)

# Cross-run comparison
print(f"\n{'='*80}")
print("CROSS-RUN COMPARISON")
print(f"{'='*80}")

print(f"\n--- Baseline summary (5 reps) ---")
for metric in ["n_timers", "n_scale_ups", "n_scale_downs", "min_step", "n_step_changes", "mean_p99", "max_p99", "max_max"]:
    vals = [a[metric] for a in baseline_analyses]
    if all(isinstance(v, (int, float)) for v in vals):
        print(f"  {metric:>20}: mean={statistics.mean(vals):.1f}  min={min(vals)}  max={max(vals)}")

print(f"\n--- v5 summary (5 reps) ---")
for metric in ["n_timers", "n_scale_ups", "n_scale_downs", "min_step", "n_step_changes", "mean_p99", "max_p99", "max_max"]:
    vals = [a[metric] for a in v5_analyses]
    if all(isinstance(v, (int, float)) for v in vals):
        print(f"  {metric:>20}: mean={statistics.mean(vals):.1f}  min={min(vals)}  max={max(vals)}")

print(f"\n--- Key differences ---")
bm_su = statistics.mean(a["n_scale_ups"] for a in baseline_analyses)
vm_su = statistics.mean(a["n_scale_ups"] for a in v5_analyses)
bm_sd = statistics.mean(a["n_scale_downs"] for a in baseline_analyses)
vm_sd = statistics.mean(a["n_scale_downs"] for a in v5_analyses)
bm_ms = statistics.mean(a["min_step"] for a in baseline_analyses)
vm_ms = statistics.mean(a["min_step"] for a in v5_analyses)
bm_sc = statistics.mean(a["n_step_changes"] for a in baseline_analyses)
vm_sc = statistics.mean(a["n_step_changes"] for a in v5_analyses)

print(f"  Scale-ups:     baseline mean={bm_su:.1f}  v5 mean={vm_su:.1f}  delta={vm_su-bm_su:+.1f}")
print(f"  Scale-downs:   baseline mean={bm_sd:.1f}  v5 mean={vm_sd:.1f}  delta={vm_sd-bm_sd:+.1f}")
print(f"  Min step:      baseline mean={bm_ms:.1f}  v5 mean={vm_ms:.1f}  delta={vm_ms-bm_ms:+.1f}")
print(f"  Step changes:  baseline mean={bm_sc:.1f}  v5 mean={vm_sc:.1f}  delta={vm_sc-bm_sc:+.1f}")

# Time-at-step comparison
print(f"\n--- Time at each step level (averaged across 5 reps, seconds) ---")
all_steps = sorted(set(s for a in baseline_analyses + v5_analyses for s in a["step_durations"].keys()))
print(f"  {'Step':>6}  {'Baseline':>10}  {'V5':>10}  {'Delta':>10}")
for step in all_steps:
    b_val = statistics.mean([a["step_durations"].get(step, 0) for a in baseline_analyses])
    v_val = statistics.mean([a["step_durations"].get(step, 0) for a in v5_analyses])
    print(f"  {step:>6}  {b_val:>10.3f}  {v_val:>10.3f}  {v_val-b_val:>+10.3f}")

# P99 vs step transition correlation
print(f"\n--- Escalation timing vs P99 ---")
for a in baseline_analyses + v5_analyses:
    fn = a["first_negative_ts"]
    print(f"  {a['label']:>16}: first_neg={fn:.3f}s  mean_P99={a['mean_p99']:.0f}us  max_P99={a['max_p99']}us  max_MAX={a['max_max']}us  scale_ups={a['n_scale_ups']}  scale_downs={a['n_scale_downs']}")
