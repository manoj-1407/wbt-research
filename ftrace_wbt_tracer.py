#!/usr/bin/env python3
"""
Ftrace WBT Empirical Kernel Evaluator
Captures and parses native Linux block-layer tracepoints:
  - wbt_timer:  status, step, inflight
  - wbt_step:   action msg, step, window, background, normal, max
  - wbt_stat:   rmean, rmin, rmax, rsamples, wmean, wmin, wmax, wsamples
  - wbt_lat:    latency (us)

Matches the exact tracepoint definitions from include/trace/events/wbt.h (Linux 6.6.y).
Associates wbt_timer with the subsequent wbt_step event without inferring missing actions.
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
# wbt_timer: TP_printk("%s: status=%u, step=%d, inflight=%u", ...)
timer_pat = re.compile(
    r"(?:.*?(\d+\.\d+):)?.*?wbt_timer:\s+(.+?):\s+status=(\d+),\s+step=(-?\d+),\s+inflight=(\d+)"
)

# wbt_step: TP_printk("%s: %s: step=%d, window=%luus, background=%u, normal=%u, max=%u", ...)
step_pat = re.compile(
    r"(?:.*?(\d+\.\d+):)?.*?wbt_step:\s+(.+?):\s+(.+?):\s+step=(-?\d+),\s+window=(\d+)us,\s+background=(\d+),\s+normal=(\d+),\s+max=(\d+)"
)

# wbt_stat: TP_printk("%s: rmean=%llu, rmin=%llu, rmax=%llu, rsamples=%llu, wmean=%llu, wmin=%llu, wmax=%llu, wsamples=%llu", ...)
stat_pat = re.compile(
    r"(?:.*?(\d+\.\d+):)?.*?wbt_stat:\s+(.+?):\s+rmean=(\d+),\s+rmin=(\d+),\s+rmax=(\d+),\s+rsamples=(\d+),\s+wmean=(\d+),\s+wmin=(\d+),\s+wmax=(\d+),\s+wsamples=(\d+)"
)

# wbt_lat: TP_printk("%s: latency %lluus", ...)
lat_pat = re.compile(
    r"(?:.*?(\d+\.\d+):)?.*?wbt_lat:\s+(.+?):\s+latency\s+(\d+)us"
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
    print("[+] WBT tracepoints active (wbt_timer, wbt_step, wbt_stat, wbt_lat).")


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

    if total_windows == 0:
        print("[!] No WBT timer events found in trace.")
        print("    Ensure WBT was enabled on target block device: /sys/block/<dev>/queue/wbt_lat_usec > 0")
        return

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

    # Chronological correlation per device
    # Associate each timer window with:
    # 1. Preceding wbt_stat in the same window (to check rsamples and rmin)
    # 2. Subsequent wbt_step before next timer (to record actual scaling action)
    print("\n--- Correlated Decision Timeline ---")
    correlated_transitions = []
    
    # Sort all events chronologically
    all_events = []
    for t in timer_events:
        all_events.append((t["ts"], "timer", t))
    for s in step_events:
        all_events.append((s["ts"], "step", s))
    for st in stat_events:
        all_events.append((st["ts"], "stat", st))
    for l in lat_events:
        all_events.append((l["ts"], "lat", l))
    
    all_events.sort(key=lambda x: x[0])

    current_stat = None
    last_timer = None

    for ts, ev_type, ev in all_events:
        if ev_type == "stat":
            current_stat = ev
        elif ev_type == "timer":
            if last_timer and "subsequent_step" not in last_timer:
                # Timer fired, but no wbt_step occurred before next timer
                last_timer["subsequent_step"] = None
                correlated_transitions.append(last_timer)
            ev["preceding_stat"] = current_stat
            last_timer = ev
        elif ev_type == "step":
            if last_timer and "subsequent_step" not in last_timer:
                # Associated subsequent step with the timer that triggered it
                last_timer["subsequent_step"] = ev
                correlated_transitions.append(last_timer)
                last_timer = None

    if last_timer and "subsequent_step" not in last_timer:
        last_timer["subsequent_step"] = None
        correlated_transitions.append(last_timer)

    # Focus on transitions and report chronological detail
    for idx, tr in enumerate(correlated_transitions[:10], 1):
        status_name = tr["status_name"]
        pre_step = tr["step"]
        sub_step = tr.get("subsequent_step")
        action_str = f"{sub_step['action']} -> step={sub_step['step']}" if sub_step else "[NO STEP CHANGE LOGGED]"
        
        stat = tr.get("preceding_stat")
        stat_str = f"rsamples={stat['rsamples']}, rmin={stat['rmin_ns']}ns" if stat else "no stat event logged"

        print(f"  [{idx:2d}] ts={tr['ts']:.4f} dev={tr['dev']} status={status_name} pre_step={pre_step} | "
              f"Action: {action_str} | Stat: {stat_str}")

    return correlated_transitions


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "analyze":
        trace_file = sys.argv[2] if len(sys.argv) > 2 else "wbt_run.trace"
        analyze_wbt_trace(trace_file)
    else:
        print("Usage:")
        print("  python3 ftrace_wbt_tracer.py analyze <trace_file>")
