#!/usr/bin/env python3
"""Audit script: independently reparse all 10 controlled benchmark logs
and verify the analysis script's reported numbers.

Usage: python3 scripts/audit_benchmark.py <log_dir>
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
timer_re = re.compile(r"wbt_timer:\s+8:0:\s+status=(\d+)")
status_count_re = re.compile(r"^\s+(\d+)\s+(\d+)\s*$")


def parse_one(path):
    with open(path, "r", errors="replace") as f:
        txt = f.read()

    probes = []
    for m in probe_re.finditer(txt):
        probes.append({
            "samples": int(m.group(1)),
            "p50": int(m.group(3)),
            "p90": int(m.group(4)),
            "p99": int(m.group(5)),
            "mean": float(m.group(7)),
        })

    writs = []
    for m in writ_re.finditer(txt):
        writs.append({"ops": int(m.group(1)), "bytes": int(m.group(2))})

    # Count status codes from trace
    statuses = []
    for m in timer_re.finditer(txt):
        statuses.append(int(m.group(1)))

    return {
        "probes": probes,
        "writs": writs,
        "total_wops": sum(w["ops"] for w in writs) if writs else 0,
        "total_samples": sum(p["samples"] for p in probes) if probes else 0,
        "mean_p50": sum(p["p50"] for p in probes) / len(probes) if probes else 0,
        "mean_p90": sum(p["p90"] for p in probes) / len(probes) if probes else 0,
        "mean_p99": sum(p["p99"] for p in probes) / len(probes) if probes else 0,
        "n_probes": len(probes),
        "status_counts": {s: statuses.count(s) for s in set(statuses)},
        "n_timer_events": len(statuses),
    }


if len(sys.argv) < 2:
    print("Usage: python3 audit_benchmark.py <log_dir>")
    sys.exit(1)

logdir = sys.argv[1]

print("=" * 100)
print("BENCHMARK INTEGRITY AUDIT")
print(f"Log directory: {logdir}")
print("=" * 100)

# Expected values from the analysis script output
expected = {
    "baseline_rep1": {"p50": 1569.0, "p99": 6576.3, "wops": 228, "s2": 10, "s3": 192},
    "baseline_rep2": {"p50": 1623.0, "p99": 10053.3, "wops": 225, "s2": 10, "s3": 192},
    "baseline_rep3": {"p50": 1564.5, "p99": 7962.2, "wops": 228, "s2": 10, "s3": 192},
    "baseline_rep4": {"p50": 1614.0, "p99": 8236.7, "wops": 226, "s2": 10, "s3": 190},
    "baseline_rep5": {"p50": 1539.5, "p99": 7194.3, "wops": 228, "s2": 10, "s3": 192},
    "v5_rep1":       {"p50": 1571.5, "p99": 8022.3, "wops": 230, "s2": 122, "s3": 5},
    "v5_rep2":       {"p50": 1516.5, "p99": 10144.2, "wops": 223, "s2": 128, "s3": 5},
    "v5_rep3":       {"p50": 1602.0, "p99": 10127.3, "wops": 224, "s2": 129, "s3": 2},
    "v5_rep4":       {"p50": 1543.3, "p99": 8867.2, "wops": 226, "s2": 125, "s3": 6},
    "v5_rep5":       {"p50": 1668.5, "p99": 11679.2, "wops": 219, "s2": 130, "s3": 3},
}

all_ok = True
for name in sorted(expected.keys()):
    path = os.path.join(logdir, f"{name}.log")
    if not os.path.exists(path):
        print(f"[MISSING] {name}.log")
        all_ok = False
        continue

    r = parse_one(path)
    exp = expected[name]

    checks = []
    # P50
    p50_ok = abs(r["mean_p50"] - exp["p50"]) < 0.1
    checks.append(("P50", p50_ok, r["mean_p50"], exp["p50"]))
    # P99
    p99_ok = abs(r["mean_p99"] - exp["p99"]) < 0.1
    checks.append(("P99", p99_ok, r["mean_p99"], exp["p99"]))
    # wOPS
    wops_ok = r["total_wops"] == exp["wops"]
    checks.append(("wOPS", wops_ok, r["total_wops"], exp["wops"]))
    # Probe count
    probes_ok = r["n_probes"] == 6
    checks.append(("probes", probes_ok, r["n_probes"], 6))
    # Status counts
    s2_actual = r["status_counts"].get(2, 0)
    s3_actual = r["status_counts"].get(3, 0)
    s2_ok = s2_actual == exp["s2"]
    s3_ok = s3_actual == exp["s3"]
    checks.append(("s2(LAT_UNKNOWN)", s2_ok, s2_actual, exp["s2"]))
    checks.append(("s3(LAT_UNKNOWN_WRITES)", s3_ok, s3_actual, exp["s3"]))

    run_ok = all(c[1] for c in checks)
    if not run_ok:
        all_ok = False

    status = "OK" if run_ok else "MISMATCH"
    print(f"\n[{status}] {name} (probes={r['n_probes']}, timer_events={r['n_timer_events']})")
    for metric, ok, actual, exp_val in checks:
        mark = "✓" if ok else "✗"
        print(f"  {mark} {metric}: actual={actual} expected={exp_val}")

# Now independently compute paired deltas
print("\n" + "=" * 100)
print("INDEPENDENT PAIRED DELTA VERIFICATION")
print("=" * 100)

p50_deltas = []
p99_deltas = []
wops_deltas = []

for rep in range(1, 6):
    bpath = os.path.join(logdir, f"baseline_rep{rep}.log")
    vpath = os.path.join(logdir, f"v5_rep{rep}.log")
    b = parse_one(bpath)
    v = parse_one(vpath)

    d50 = v["mean_p50"] - b["mean_p50"]
    d99 = v["mean_p99"] - b["mean_p99"]
    dwops = v["total_wops"] - b["total_wops"]
    p50_deltas.append(d50)
    p99_deltas.append(d99)
    wops_deltas.append(dwops)

    print(f"  rep{rep}: P50 Δ={d50:+.1f}us  P99 Δ={d99:+.1f}us  wOPS Δ={dwops:+d}")

print(f"\n  P50: mean={statistics.mean(p50_deltas):+.1f}us  median={statistics.median(p50_deltas):+.1f}us  stdev={statistics.stdev(p50_deltas):.1f}us")
print(f"       worse={sum(1 for d in p50_deltas if d > 0)}  better={sum(1 for d in p50_deltas if d < 0)}")
print(f"  P99: mean={statistics.mean(p99_deltas):+.1f}us  median={statistics.median(p99_deltas):+.1f}us  stdev={statistics.stdev(p99_deltas):.1f}us")
print(f"       worse={sum(1 for d in p99_deltas if d > 0)}  better={sum(1 for d in p99_deltas if d < 0)}")
print(f"  wOPS: mean={statistics.mean(wops_deltas):+.1f}")

print("\n" + "=" * 100)
if all_ok:
    print("AUDIT PASSED: All 10 logs match expected values. Calculations verified.")
else:
    print("AUDIT FAILED: Some values do not match. Review above.")
print("=" * 100)
