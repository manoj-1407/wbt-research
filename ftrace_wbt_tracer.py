#!/usr/bin/env python3
"""
Ftrace WBT Empirical Kernel Evaluator
Captures and parses native Linux block-layer tracepoints:
  - wbt_timer:  status, step, inflight
  - wbt_step:   action msg, step, window, background, normal, max
  - wbt_stat:   rmean, rmin, rmax, rsamples, wmean, wmin, wmax, wsamples
  - wbt_lat:    latency (us)
  - wbt_guard:  step, r_samples, r_min_us, target_us, guard_fired, action

Matches the exact tracepoint definitions from include/trace/events/wbt.h (Linux 6.6.y).
Associates wbt_timer, wbt_guard, and subsequent wbt_step events without ungrounded inferences.
"""

import os
import sys
import time
import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from typing import List, Dict, Optional

TRACE_BASE = "/sys/kernel/debug/tracing"
if not os.path.exists(TRACE_BASE):
    TRACE_BASE = "/sys/kernel/tracing"

STATUS_MAP = {
    1: "LAT_OK",
    2: "LAT_UNKNOWN",
    3: "LAT_UNKNOWN_WRITES",
    4: "LAT_EXCEEDED",
}

# Regex parsers matching exact TP_printk format from include/trace/events/wbt.h
timer_pat = re.compile(
    r"(?:.*?(\d+\.\d+):)?.*?wbt_timer:\s+(.+?):\s+status=(\d+),\s+step=(-?\d+),\s+inflight=(\d+)"
)

step_pat = re.compile(
    r"(?:.*?(\d+\.\d+):)?.*?wbt_step:\s+(.+?):\s+(.+?):\s+step=(-?\d+),\s+window=(\d+)us,\s+background=(\d+),\s+normal=(\d+),\s+max=(\d+)"
)

stat_pat = re.compile(
    r"(?:.*?(\d+\.\d+):)?.*?wbt_stat:\s+(.+?):\s+rmean=(\d+),\s+rmin=(\d+),\s+rmax=(\d+),\s+rsamples=(\d+),\s+wmean=(\d+),\s+wmin=(\d+),\s+wmax=(\d+),\s+wsamples=(\d+)"
)

lat_pat = re.compile(
    r"(?:.*?(\d+\.\d+):)?.*?wbt_lat:\s+(.+?):\s+latency\s+(\d+)us"
)

# wbt_guard: TP_printk("%s: step=%d, r_samples=%llu, r_min=%lluus, target=%lluus, guard_fired=%d, action=%s", ...)
guard_pat = re.compile(
    r"(?:.*?(\d+\.\d+):)?.*?wbt_guard:\s+(.+?):\s+step=(-?\d+),\s+r_samples=(\d+),\s+r_min=(\d+)us,\s+target=(\d+)us,\s+guard_fired=(\d+),\s+action=(\w+)"
)


def write_trace_node(path_rel: str, val: str):
    p = os.path.join(TRACE_BASE, path_rel)
    if os.path.exists(p):
        try:
            with open(p, "w") as f:
                f.write(val)
        except PermissionError:
            print(f"[!] Root permissions required to write {p}")
            sys.exit(1)


def setup_tracer():
    print("[*] Configuring tracefs for WBT events...")
    write_trace_node("tracing_on", "0")
    write_trace_node("trace", "")  # Clear buffer
    write_trace_node("events/wbt/enable", "1")
    write_trace_node("tracing_on", "1")
    print("[+] WBT tracepoints active (wbt_timer, wbt_step, wbt_stat, wbt_lat, wbt_guard).")


def teardown_tracer(output_file: str):
    print("[*] Stopping trace collection...")
    write_trace_node("tracing_on", "0")
    write_trace_node("events/wbt/enable", "0")

    trace_path = os.path.join(TRACE_BASE, "trace")
    if os.path.exists(trace_path):
        with open(trace_path, "r", errors="ignore") as src, open(output_file, "w") as dst:
            dst.write(src.read())
        print(f"[+] Raw trace written to: {output_file}")
    write_trace_node("trace", "")


def analyze_wbt_trace(trace_file: str):
    print("=" * 80)
    print(f"WBT In-Kernel Trace Analysis: {trace_file}")
    print("=" * 80)

    timer_events = []
    step_events = []
    stat_events = []
    lat_events = []
    guard_events = []
    status_counts = Counter()

    with open(trace_file, "r", errors="ignore") as f:
        for line in f:
            if "wbt_timer:" in line:
                m = timer_pat.search(line)
                if m:
                    ts, dev, status, step, inflight = m.groups()
                    status = int(status)
                    step = int(step)
                    status_name = STATUS_MAP.get(status, f"UNKNOWN({status})")
                    status_counts[status_name] += 1
                    timer_events.append({
                        "ts": float(ts) if ts else 0.0,
                        "dev": dev.strip(),
                        "status_code": status,
                        "status_name": status_name,
                        "step": step,
                        "inflight": int(inflight),
                    })
            elif "wbt_guard:" in line:
                m = guard_pat.search(line)
                if m:
                    ts, dev, step, r_samples, r_min_us, target_us, guard_fired, action = m.groups()
                    guard_events.append({
                        "ts": float(ts) if ts else 0.0,
                        "dev": dev.strip(),
                        "step": int(step),
                        "r_samples": int(r_samples),
                        "r_min_us": int(r_min_us),
                        "target_us": int(target_us),
                        "guard_fired": int(guard_fired),
                        "action": action.strip(),
                    })
            elif "wbt_step:" in line:
                m = step_pat.search(line)
                if m:
                    ts, dev, action, step, win, bg, normal, max_d = m.groups()
                    step_events.append({
                        "ts": float(ts) if ts else 0.0,
                        "dev": dev.strip(),
                        "action": action.strip(),
                        "step": int(step),
                        "window_us": int(win),
                        "bg": int(bg),
                        "normal": int(normal),
                        "max_depth": int(max_d),
                    })
            elif "wbt_stat:" in line:
                m = stat_pat.search(line)
                if m:
                    ts, dev, rmean, rmin, rmax, rsamples, wmean, wmin, wmax, wsamples = m.groups()
                    stat_events.append({
                        "ts": float(ts) if ts else 0.0,
                        "dev": dev.strip(),
                        "rmin_ns": int(rmin),
                        "rmax_ns": int(rmax),
                        "rsamples": int(rsamples),
                        "wsamples": int(wsamples),
                    })
            elif "wbt_lat:" in line:
                m = lat_pat.search(line)
                if m:
                    ts, dev, lat_us = m.groups()
                    lat_events.append({
                        "ts": float(ts) if ts else 0.0,
                        "dev": dev.strip(),
                        "lat_us": int(lat_us),
                    })

    total_windows = len(timer_events)
    print(f"Total WBT Timer Windows: {total_windows}")
    print(f"Total Step Transitions:  {len(step_events)}")
    print(f"Total Stat Reports:      {len(stat_events)}")
    print(f"Total Latency Alerts:    {len(lat_events)}")
    print(f"Total Guard Decisions:   {len(guard_events)}")

    if total_windows == 0 and len(guard_events) == 0:
        print("[!] No WBT timer or guard events found in trace.")
        print("    Ensure WBT was enabled on target block device: /sys/block/<dev>/queue/wbt_lat_usec > 0")
        return

    if total_windows > 0:
        print("\n--- Status Distribution Across Windows ---")
        for s_name, count in status_counts.most_common():
            pct = (count / total_windows) * 100
            print(f"  {s_name:<20}: {count:5d} ({pct:5.1f}%)")

    lat_unknown_writes = [e for e in timer_events if e["status_code"] == 3]
    print(f"\n[CRITICAL EVALUATION]")
    print(f"  LAT_UNKNOWN_WRITES (status=3) Events: {len(lat_unknown_writes)} / {total_windows}")
    if lat_unknown_writes:
        print(f"  --> The kernel entered LAT_UNKNOWN_WRITES in {len(lat_unknown_writes)} window(s).")
    else:
        print(f"  --> LAT_UNKNOWN_WRITES was NOT entered during this workload.")

    print(f"\n[GUARD DECISION AUDIT]")
    if guard_events:
        guards_fired = [g for g in guard_events if g["guard_fired"] == 1]
        guards_scaled = [g for g in guard_events if g["guard_fired"] == 0]
        print(f"  Total wbt_guard Evaluations: {len(guard_events)}")
        print(f"    - Guard FIRED (Hold Depth): {len(guards_fired)}")
        print(f"    - Guard NOT FIRED (Scale Up): {len(guards_scaled)}")
        for idx, g in enumerate(guard_events[:5], 1):
            print(f"    [{idx}] ts={g['ts']:.4f} dev={g['dev']} step={g['step']} "
                  f"r_samples={g['r_samples']} r_min={g['r_min_us']}us target={g['target_us']}us "
                  f"--> action={g['action']} (fired={g['guard_fired']})")
    else:
        print("  --> No wbt_guard events recorded (baseline kernel or guard not hit).")

    # Chronological correlation per device
    print("\n--- Correlated Decision Timeline ---")
    correlated_transitions = []
    
    all_events = []
    for t in timer_events:
        all_events.append((t["ts"], "timer", t))
    for g in guard_events:
        all_events.append((g["ts"], "guard", g))
    for s in step_events:
        all_events.append((s["ts"], "step", s))
    for st in stat_events:
        all_events.append((st["ts"], "stat", st))
    for l in lat_events:
        all_events.append((l["ts"], "lat", l))
    
    all_events.sort(key=lambda x: x[0])

    current_stat = None
    last_timer = None
    last_guard = None

    for ts, ev_type, ev in all_events:
        if ev_type == "stat":
            current_stat = ev
        elif ev_type == "guard":
            last_guard = ev
            if last_timer:
                last_timer["associated_guard"] = ev
        elif ev_type == "timer":
            if last_timer and "subsequent_step" not in last_timer:
                last_timer["subsequent_step"] = None
                correlated_transitions.append(last_timer)
            ev["preceding_stat"] = current_stat
            ev["associated_guard"] = None
            last_timer = ev
        elif ev_type == "step":
            if last_timer and "subsequent_step" not in last_timer:
                last_timer["subsequent_step"] = ev
                correlated_transitions.append(last_timer)
                last_timer = None

    if last_timer and "subsequent_step" not in last_timer:
        last_timer["subsequent_step"] = None
        correlated_transitions.append(last_timer)

    for idx, tr in enumerate(correlated_transitions[:15], 1):
        status_name = tr["status_name"]
        pre_step = tr["step"]
        sub_step = tr.get("subsequent_step")
        action_str = f"{sub_step['action']} -> step={sub_step['step']}" if sub_step else "[NO STEP CHANGE LOGGED]"
        
        stat = tr.get("preceding_stat")
        stat_str = f"rsamples={stat['rsamples']}, rmin={stat['rmin_ns']}ns" if stat else "no stat"

        guard = tr.get("associated_guard")
        guard_str = f"guard={guard['action']} (fired={guard['guard_fired']}, r_min={guard['r_min_us']}us)" if guard else "no guard"

        print(f"  [{idx:2d}] ts={tr['ts']:.4f} dev={tr['dev']} status={status_name} pre_step={pre_step} | "
              f"Action: {action_str} | Guard: {guard_str} | Stat: {stat_str}")

    return correlated_transitions


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "analyze":
        trace_file = sys.argv[2] if len(sys.argv) > 2 else "wbt_run.trace"
        analyze_wbt_trace(trace_file)
    else:
        print("Usage:")
        print("  python3 ftrace_wbt_tracer.py analyze <trace_file>")
