#!/usr/bin/env python3
"""Analyze controlled benchmark logs from controlled_benchmark.sh.

Reads all *_rep*.log files from a directory, parses PROBE_RESULT,
WRITER_RESULT, BLKSTAT_DELTA, WBT tracepoint counts, and status
distribution, then produces a paired comparison table.

Usage: python3 scripts/analyze_controlled.py <log_dir>
"""

import re
import os
import sys
import statistics

probe_re = re.compile(
    r"\[PROBE_RESULT\] SAMPLES=(\d+) MIN_US=(\d+) P50_US=(\d+) P90_US=(\d+) P99_US=(\d+) MAX_US=(\d+) MEAN_US=([\d.]+)"
)
writ_re = re.compile(
    r"\[WRITER_RESULT\] OPS=(\d+) BYTES=(\d+) ITERS=(\d+) ERR=(\d+) FSYNC_ERR=(\d+)"
)
blk_re = re.compile(r"BLKSTAT_DELTA read=(\d+) write=(\d+)")
timer_re = re.compile(r"WBT_TIMER=(\d+)")
step_re = re.compile(r"WBT_STEP=(\d+)")
stat_re = re.compile(r"WBT_STAT=(\d+)")
status_re = re.compile(r"^\s+(\d+)\s+(\d+)\s*$")
label_re = re.compile(r"LABEL=(\S+)")
rep_re = re.compile(r"REP=(\d+)")


def parse_log(path):
    with open(path, "r", errors="replace") as f:
        txt = f.read()

    label = label_re.search(txt)
    rep = rep_re.search(txt)
    label = label.group(1) if label else "unknown"
    rep = rep.group(1) if rep else "?"

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
    for m in writ_re.finditer(txt):
        writs.append({"ops": int(m.group(1)), "bytes": int(m.group(2))})

    blk = blk_re.search(txt)
    timer = timer_re.search(txt)
    step = step_re.search(txt)
    stat = stat_re.search(txt)

    # Status distribution (from the STATUS_DIST section)
    status_dist = {}
    in_status = False
    for line in txt.splitlines():
        if "STATUS_DIST" in line:
            in_status = True
            continue
        if in_status:
            if "STEP_EVENTS" in line or "END" in line:
                in_status = False
                continue
            m = status_re.match(line)
            if m:
                status_dist[m.group(2)] = int(m.group(1))

    return {
        "label": label,
        "rep": rep,
        "probes": probes,
        "writs": writs,
        "blk": (int(blk.group(1)), int(blk.group(2))) if blk else None,
        "wbt_timer": int(timer.group(1)) if timer else 0,
        "wbt_step": int(step.group(1)) if step else 0,
        "wbt_stat": int(stat.group(1)) if stat else 0,
        "status_dist": status_dist,
    }


def mean_p50(r):
    if not r["probes"]:
        return None
    return sum(p["p50"] for p in r["probes"]) / len(r["probes"])

def mean_p99(r):
    if not r["probes"]:
        return None
    return sum(p["p99"] for p in r["probes"]) / len(r["probes"])

def mean_p90(r):
    if not r["probes"]:
        return None
    return sum(p["p90"] for p in r["probes"]) / len(r["probes"])

def total_wops(r):
    return sum(w["ops"] for w in r["writs"]) if r["writs"] else 0

def total_samples(r):
    return sum(p["samples"] for p in r["probes"]) if r["probes"] else 0


if len(sys.argv) < 2:
    print("Usage: python3 analyze_controlled.py <log_dir>")
    sys.exit(1)

logdir = sys.argv[1]
files = sorted([f for f in os.listdir(logdir) if f.endswith(".log") and "_rep" in f])

if not files:
    print(f"No log files found in {logdir}")
    sys.exit(1)

results = {}
for f in files:
    path = os.path.join(logdir, f)
    r = parse_log(path)
    key = (r["label"], r["rep"])
    results[key] = r

# Collect per-label per-rep metrics
labels = sorted(set(k[0] for k in results))
reps = sorted(set(k[1] for k in results), key=lambda x: int(x))

print("=" * 90)
print("CONTROLLED BENCHMARK ANALYSIS")
print(f"Log directory: {logdir}")
print(f"Labels: {labels}")
print(f"Repetitions: {reps}")
print("=" * 90)

# Per-run table
print("\n## Per-run results\n")
print(f"{'Run':<20} {'P50(µs)':>8} {'P90(µs)':>8} {'P99(µs)':>8} {'wOPS':>6} {'samples':>8} {'timer':>6} {'step':>5} {'stat':>5} {'s2':>4} {'s3':>4}")
print("-" * 90)

for label in labels:
    for rep in reps:
        key = (label, rep)
        if key not in results:
            continue
        r = results[key]
        p50 = mean_p50(r)
        p90 = mean_p90(r)
        p99 = mean_p99(r)
        wops = total_wops(r)
        samp = total_samples(r)
        s2 = r["status_dist"].get("2", 0)
        s3 = r["status_dist"].get("3", 0)
        print(f"{label+'_rep'+rep:<20} {p50:>8.1f} {p90:>8.1f} {p99:>8.1f} {wops:>6} {samp:>8} {r['wbt_timer']:>6} {r['wbt_step']:>5} {r['wbt_stat']:>5} {s2:>4} {s3:>4}")

# Paired comparison
print("\n## Paired deltas (v5 - baseline per rep)\n")
print(f"{'Rep':<6} {'P50 Δ':>10} {'P50 %':>8} {'P99 Δ':>10} {'P99 %':>8} {'wOPS Δ':>8} {'s2 Δ':>6} {'s3 Δ':>6}")
print("-" * 70)

p50_deltas = []
p99_deltas = []
wops_deltas = []

for rep in reps:
    bk = ("baseline", rep)
    vk = ("v5", rep)
    if bk not in results or vk not in results:
        continue
    b = results[bk]
    v = results[vk]
    bp50 = mean_p50(b)
    vp50 = mean_p50(v)
    bp99 = mean_p99(b)
    vp99 = mean_p99(v)
    bwops = total_wops(b)
    vwops = total_wops(v)

    d50 = vp50 - bp50 if bp50 and vp50 else None
    d99 = vp99 - bp99 if bp99 and vp99 else None
    dwops = vwops - bwops
    pct50 = (d50 / bp50 * 100) if d50 is not None and bp50 else None
    pct99 = (d99 / bp99 * 100) if d99 is not None and bp99 else None

    s2_b = b["status_dist"].get("2", 0)
    s2_v = v["status_dist"].get("2", 0)
    s3_b = b["status_dist"].get("3", 0)
    s3_v = v["status_dist"].get("3", 0)

    if d50 is not None:
        p50_deltas.append(d50)
    if d99 is not None:
        p99_deltas.append(d99)
    wops_deltas.append(dwops)

    d50s = f"{d50:+.1f}" if d50 is not None else "N/A"
    d99s = f"{d99:+.1f}" if d99 is not None else "N/A"
    p50s = f"{pct50:+.2f}%" if pct50 is not None else "N/A"
    p99s = f"{pct99:+.2f}%" if pct99 is not None else "N/A"

    print(f"{rep:<6} {d50s:>10} {p50s:>8} {d99s:>10} {p99s:>8} {dwops:>+8} {s2_v-s2_b:>+6} {s3_v-s3_b:>+6}")

# Summary statistics
print("\n## Summary\n")
if p50_deltas:
    print(f"P50 delta: mean={statistics.mean(p50_deltas):+.1f}µs  median={statistics.median(p50_deltas):+.1f}µs  stdev={statistics.stdev(p50_deltas) if len(p50_deltas)>1 else 0:.1f}µs")
    pos = sum(1 for d in p50_deltas if d > 0)
    neg = sum(1 for d in p50_deltas if d < 0)
    print(f"  direction: {pos} worse, {neg} better, {len(p50_deltas)-pos-neg} tied")
if p99_deltas:
    print(f"P99 delta: mean={statistics.mean(p99_deltas):+.1f}µs  median={statistics.median(p99_deltas):+.1f}µs  stdev={statistics.stdev(p99_deltas) if len(p99_deltas)>1 else 0:.1f}µs")
    pos = sum(1 for d in p99_deltas if d > 0)
    neg = sum(1 for d in p99_deltas if d < 0)
    print(f"  direction: {pos} worse, {neg} better, {len(p99_deltas)-pos-neg} tied")
if wops_deltas:
    print(f"wOPS delta: mean={statistics.mean(wops_deltas):+.1f}  median={statistics.median(wops_deltas):+.1f}")
    pos = sum(1 for d in wops_deltas if d > 0)
    neg = sum(1 for d in wops_deltas if d < 0)
    print(f"  direction: {pos} increased, {neg} decreased, {len(wops_deltas)-pos-neg} tied")

# Verdict
print("\n## Assessment\n")
if p50_deltas and p99_deltas:
    p50_better = sum(1 for d in p50_deltas if d < 0)
    p99_better = sum(1 for d in p99_deltas if d < 0)
    n = len(p50_deltas)
    if p50_better >= n * 0.8 and p99_better >= n * 0.8:
        print(f"CONSISTENT IMPROVEMENT: {p50_better}/{n} P50 better, {p99_better}/{n} P99 better")
    elif p50_better <= n * 0.2 and p99_better <= n * 0.2:
        print(f"CONSISTENT REGRESSION: {n-p50_better}/{n} P50 worse, {n-p99_better}/{n} P99 worse")
    else:
        print(f"MIXED RESULTS: {p50_better}/{n} P50 better, {p99_better}/{n} P99 better — no stable direction")
        print("No performance claim can be made from these repetitions.")
