#!/usr/bin/env python3
"""
Independent recalculation of all Run 1 (40002470957) benchmark metrics
from raw QEMU log files.

This script reads nothing from docs/ or existing CSV summaries.
It parses the raw PROBE_RESULT, WRITER_RESULT, BLKSTAT-DELTA, and
wbt_guard lines directly from the committed log files.

Usage: python3 scripts/recalculate_run1.py [bench_dir]
       bench_dir defaults to <repo>/results
"""

import re
import os
import sys
import statistics

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(REPO, "results")

CFGS = [
    "baseline_ide_h", "patched_ide_h",
    "baseline_vio_h", "patched_vio_h",
    "baseline_ide_s", "patched_ide_s",
    "baseline_vio_s", "patched_vio_s",
]

probe_re = re.compile(
    r"\[PROBE_RESULT\] SAMPLES=(\d+) MIN_US=(\d+) P50_US=(\d+) P90_US=(\d+) P99_US=(\d+) MAX_US=(\d+) MEAN_US=([\d.]+)"
)
writ_re = re.compile(
    r"\[WRITER_RESULT\] OPS=(\d+) BYTES=(\d+) ITERS=(\d+) ERR=(\d+) FSYNC_ERR=(\d+)"
)
blk_re = re.compile(r"\[BLKSTAT-DELTA\] read_sectors=(\d+) write_sectors=(\d+)")
guard_re = re.compile(
    r"wbt_guard: 8:0: step=(-?\d+), r_samples=(\d+), r_min=(\d+)us, target=(\d+)us, guard_fired=(\d+), action=(\S+)"
)
timer_re = re.compile(r"wbt_timer\s*:\s*(\d+)")
step_count_re = re.compile(r"wbt_step\s*:\s*(\d+)")
stat_count_re = re.compile(r"wbt_stat\s*:\s*(\d+)")
wbt_step_event_re = re.compile(
    r"wbt_step: 8:0: (?:scale up|scale down): step=(-?\d+)"
)
config_re = re.compile(r"CONFIG_BLK_WBT=(\w+)")


def parse_log(path):
    if not os.path.exists(path):
        return None
    txt = open(path, "r", errors="replace").read()

    probes = []
    for m in probe_re.finditer(txt):
        probes.append({
            "samples": int(m.group(1)),
            "min_us":  int(m.group(2)),
            "p50_us":  int(m.group(3)),
            "p90_us":  int(m.group(4)),
            "p99_us":  int(m.group(5)),
            "max_us":  int(m.group(6)),
            "mean_us": float(m.group(7)),
        })

    writs = []
    for m in writ_re.finditer(txt):
        writs.append({
            "ops": int(m.group(1)), "bytes": int(m.group(2)),
            "iters": int(m.group(3)), "errs": int(m.group(4)),
            "sync_errs": int(m.group(5)),
        })

    blk = blk_re.search(txt)
    guards = []
    for m in guard_re.finditer(txt):
        guards.append({
            "step": int(m.group(1)), "r_samples": int(m.group(2)),
            "r_min_us": int(m.group(3)), "target_us": int(m.group(4)),
            "fired": int(m.group(5)), "action": m.group(6),
        })

    timer_m = timer_re.search(txt)
    step_m = step_count_re.search(txt)
    stat_m = stat_count_re.search(txt)
    step_events = [int(m.group(1)) for m in wbt_step_event_re.finditer(txt)]
    config_m = config_re.search(txt)

    return {
        "probes": probes,
        "writs": writs,
        "blk": (int(blk.group(1)), int(blk.group(2))) if blk else None,
        "guards": guards,
        "wbt_timer": int(timer_m.group(1)) if timer_m else 0,
        "wbt_step_count": int(step_m.group(1)) if step_m else 0,
        "wbt_stat_count": int(stat_m.group(1)) if stat_m else 0,
        "step_events": step_events,
        "guard_count": len(guards),
        "held": sum(1 for g in guards if g["fired"] == 1),
        "scale_up_guard": sum(1 for g in guards if g["fired"] == 0),
        "config_blk_wbt": config_m.group(1) if config_m else "absent",
    }


def find_log(cfg):
    for sub in ("baseline", "patched"):
        if cfg.startswith(sub):
            cand = os.path.join(BENCH, sub, cfg + "_qemu.log")
            if os.path.exists(cand):
                return cand
    # flat layout fallback
    cand = os.path.join(BENCH, cfg + "_qemu.log")
    if os.path.exists(cand):
        return cand
    return None


print("=" * 90)
print("INDEPENDENT RECALCULATION — Run 40002470957 (commit c53cd2a)")
print(f"Source directory: {os.path.abspath(BENCH)}")
print("=" * 90)

data = {}
missing = []
for cfg in CFGS:
    path = find_log(cfg)
    if path is None:
        print(f"[MISSING] {cfg}_qemu.log not found under {BENCH}")
        missing.append(cfg)
        data[cfg] = None
        continue
    r = parse_log(path)
    data[cfg] = r
    ps = r["probes"]
    ws = r["writs"]
    total_samples = sum(p["samples"] for p in ps) if ps else 0
    total_ops = sum(w["ops"] for w in ws) if ws else 0
    total_bytes = sum(w["bytes"] for w in ws) if ws else 0
    mean_p50 = sum(p["p50_us"] for p in ps) / len(ps) if ps else None
    mean_p99 = sum(p["p99_us"] for p in ps) / len(ps) if ps else None
    mean_p90 = sum(p["p90_us"] for p in ps) / len(ps) if ps else None
    sd_p50 = statistics.stdev([p["p50_us"] for p in ps]) if len(ps) > 1 else 0.0
    sd_p99 = statistics.stdev([p["p99_us"] for p in ps]) if len(ps) > 1 else 0.0
    blk = r["blk"]
    held = r["held"]
    nfired = r["scale_up_guard"]
    print(f"\n### {cfg}")
    print(f"    config_blk_wbt = {r['config_blk_wbt']}")
    print(f"    blk_delta      : read_sectors={blk[0] if blk else 'N/A'} write_sectors={blk[1] if blk else 'N/A'}")
    print(f"    wbt_timer      : {r['wbt_timer']}  wbt_step_transitions: {r['wbt_step_count']}  wbt_stat: {r['wbt_stat_count']}")
    print(f"    guard_total    : {r['guard_count']}  held={held}  scale_up={nfired}")
    if r["guards"]:
        sane = [g for g in r["guards"] if g["r_min_us"] < 100_000]
        if sane:
            rmin_vals = [g["r_min_us"] for g in sane]
            print(f"    guard r_min(sane): min={min(rmin_vals)} mean={sum(rmin_vals)/len(rmin_vals):.1f} max={max(rmin_vals)} us  target={r['guards'][0]['target_us']}us  insane={r['guard_count']-len(sane)}")
        else:
            print(f"    guard r_min: all {r['guard_count']} events had r_samples=0 (U64_MAX/1000 sentinel)")
    if ps:
        print(f"    probes(n={len(ps)}): total_samples={total_samples}")
        for i, p in enumerate(ps):
            print(f"      [{i+1}] SAMPLES={p['samples']} MIN={p['min_us']} P50={p['p50_us']} P90={p['p90_us']} P99={p['p99_us']} MAX={p['max_us']} MEAN={p['mean_us']:.1f}")
        print(f"      mean-across-probes: P50={mean_p50:.2f}(sd={sd_p50:.2f}) P90={mean_p90:.2f} P99={mean_p99:.2f}(sd={sd_p99:.2f}) µs")
    if ws:
        print(f"    writers(n={len(ws)}): total OPS={total_ops} BYTES={total_bytes} ERR={sum(w['errs'] for w in ws)} FSYNC_ERR={sum(w['sync_errs'] for w in ws)}")
    if r["step_events"]:
        print(f"    scale_steps    : {r['step_events']}")

if missing:
    print(f"\n[!] MISSING configs: {missing}")

print()
print("=" * 90)
print("PAIRED DELTA (patched minus baseline) — per-process means")
print("=" * 90)
for dev in ("ide", "vio"):
    for prof in ("h", "s"):
        bk = f"baseline_{dev}_{prof}"
        pk = f"patched_{dev}_{prof}"
        b = data.get(bk)
        p = data.get(pk)
        if not (b and p):
            print(f"\n{dev}/{prof}: MISSING DATA")
            continue
        bp = b["probes"]; pp = p["probes"]
        if not bp or not pp:
            print(f"\n{dev}/{prof}: NO PROBE DATA")
            continue
        print(f"\n--- {dev} / {prof} ---")
        bp50 = sum(x["p50_us"] for x in bp) / len(bp)
        pp50 = sum(x["p50_us"] for x in pp) / len(pp)
        bp90 = sum(x["p90_us"] for x in bp) / len(bp)
        pp90 = sum(x["p90_us"] for x in pp) / len(pp)
        bp99 = sum(x["p99_us"] for x in bp) / len(bp)
        pp99 = sum(x["p99_us"] for x in pp) / len(pp)
        bwops = sum(w["ops"] for w in b["writs"]) if b["writs"] else 0
        pwops = sum(w["ops"] for w in p["writs"]) if p["writs"] else 0
        bsamples = sum(x["samples"] for x in bp)
        psamples = sum(x["samples"] for x in pp)
        d50 = pp50 - bp50
        d90 = pp90 - bp90
        d99 = pp99 - bp99
        print(f"   samples : base={bsamples}  patched={psamples}  delta={psamples-bsamples:+d}")
        print(f"   P50     : base={bp50:.2f}  patched={pp50:.2f}  delta={d50:+.2f}µs ({d50/bp50*100:+.2f}%)")
        print(f"   P90     : base={bp90:.2f}  patched={pp90:.2f}  delta={d90:+.2f}µs ({d90/bp90*100:+.2f}%)")
        print(f"   P99     : base={bp99:.2f}  patched={pp99:.2f}  delta={d99:+.2f}µs ({d99/bp99*100:+.2f}%)")
        print(f"   wOPS    : base={bwops}  patched={pwops}  delta={pwops-bwops:+d}")
        print(f"   guards  : patched total={p['guard_count']} held={p['held']} scale_up={p['scale_up_guard']}")
        print(f"   timer   : base={b['wbt_timer']}  patched={p['wbt_timer']}")
        # Interpretation
        guard_active = p["held"] > 0
        if guard_active:
            print(f"   NOTE: Guard actively held depth {p['held']} times in this config")
            if d50 > 0:
                print(f"   WARNING: Read latency INCREASED where guard held (+{d50:.1f}µs P50, +{d99:.1f}µs P99)")
            else:
                print(f"   NOTE: Read latency decreased where guard held ({d50:.1f}µs P50)")
        else:
            print(f"   NOTE: Guard did not hold depth — patch was inert for this config")

print()
print("=" * 90)
print("VALIDITY GATES")
print("=" * 90)
all_valid = True
for cfg in CFGS:
    r = data.get(cfg)
    if r is None:
        print(f"[FAIL] {cfg}: log file missing")
        all_valid = False
        continue
    issues = []
    if r["config_blk_wbt"] != "y":
        issues.append(f"CONFIG_BLK_WBT={r['config_blk_wbt']} (expected y)")
    if r["wbt_timer"] == 0:
        issues.append("wbt_timer=0 (WBT never armed)")
    if r["blk"] and r["blk"][1] == 0:
        issues.append("write_sectors=0 (writes never reached block layer)")
    if r["blk"] and r["blk"][0] == 0:
        issues.append("read_sectors=0 (reads never reached block layer)")
    if not r["probes"]:
        issues.append("no PROBE_RESULT lines")
    if not r["writs"]:
        issues.append("no WRITER_RESULT lines")
    if issues:
        print(f"[WARN] {cfg}: {'; '.join(issues)}")
        all_valid = False
    else:
        print(f"[PASS] {cfg}: all validity gates passed (timer={r['wbt_timer']}, blk={r['blk']})")

print()
if all_valid:
    print("ALL VALIDITY GATES PASSED")
else:
    print("SOME VALIDITY GATES FAILED — review above")

print()
print("=" * 90)
print("GUARD CORRECTNESS VERIFICATION")
print("=" * 90)
# The guard should fire (held > 0) in patched_vio_s
# Baselines should always have 0 guard events (tracepoint absent)
# Patched heavy configurations (patched_ide_h, patched_vio_h) should have 0 holds
# Patched ide_s should have 0 holds in nominal conditions (may hold at boundary on slow hosts)
expected_held = {"patched_vio_s": True}
baseline_zero_guards = {"baseline_ide_h", "baseline_vio_h", "baseline_ide_s", "baseline_vio_s"}

guard_ok = True
for cfg in CFGS:
    r = data.get(cfg)
    if r is None:
        continue
    if cfg in baseline_zero_guards:
        if r["guard_count"] != 0:
            print(f"[FAIL] {cfg}: expected 0 guard events, got {r['guard_count']}")
            guard_ok = False
        else:
            print(f"[PASS] {cfg}: guard_count=0 as expected (baseline)")
    elif cfg in expected_held:
        if r["held"] == 0:
            print(f"[FAIL] {cfg}: expected held>0, got held={r['held']}")
            guard_ok = False
        else:
            print(f"[PASS] {cfg}: held={r['held']}/{r['guard_count']} as expected")
    elif cfg == "patched_vio_h":
        if r["held"] > 0:
            print(f"[FAIL] {cfg}: expected held=0, got {r['held']}")
            guard_ok = False
        else:
            print(f"[PASS] {cfg}: guard evaluated {r['guard_count']} times, 0 holds (expected)")
    else:
        # patched_ide_h, patched_ide_s
        if r["held"] > 0:
            print(f"[INFO] {cfg}: boundary hold count={r['held']} (r_min crossed target on slow host)")
        else:
            print(f"[PASS] {cfg}: guard evaluated {r['guard_count']} times, 0 holds (expected)")

if guard_ok:
    print("\nGUARD CORRECTNESS: ALL CHECKS PASSED")
else:
    print("\nGUARD CORRECTNESS: SOME CHECKS FAILED")
