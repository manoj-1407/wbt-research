#!/usr/bin/env python3
"""Consolidate the 8 matched QEMU benchmark logs into one matrix."""
import re, os, sys

BENCH = sys.argv[1] if len(sys.argv) > 1 else r"D:\io_investigation\_evidence\bench_40002470957"
CFGS = ["baseline_ide_h", "patched_ide_h", "baseline_vio_h", "patched_vio_h",
        "baseline_ide_s", "patched_ide_s", "baseline_vio_s", "patched_vio_s"]

probe_re = re.compile(r"\[PROBE_RESULT\] SAMPLES=(\d+) MIN_US=(\d+) P50_US=(\d+) P90_US=(\d+) P99_US=(\d+) MAX_US=(\d+) MEAN_US=([\d.]+)")
writ_re  = re.compile(r"\[WRITER_RESULT\] OPS=(\d+) BYTES=(\d+) ITERS=(\d+) ERR=(\d+) FSYNC_ERR=(\d+)")
blk_re   = re.compile(r"\[BLKSTAT-DELTA\] read_sectors=(\d+) write_sectors=(\d+)")
cnt_re   = re.compile(r"^(wbt_timer|wbt_step|wbt_stat|wbt_lat|wbt_guard)\s*:\s*(\d+)")
step_re  = re.compile(r"wbt_step: 8:0: status=(\d+), step=(-?\d+)")
guard_re = re.compile(r"wbt_guard: 8:0: step=(-?\d+), r_samples=(\d+), r_min=(\d+)us, target=(\d+)us, guard_fired=(\d+), action=(\S+)")
nrq_re   = re.compile(r"NR_requests=(\d+)", re.I)

rows = []
for cfg in CFGS:
    path = os.path.join(BENCH, cfg + "_qemu.log")
    if not os.path.exists(path):
        continue
    txt = open(path, "r", errors="replace").read()
    probes = [tuple(map(float, m.groups())) for m in probe_re.finditer(txt)]
    writs = [tuple(map(int, m.groups())) for m in writ_re.finditer(txt)]
    blk = blk_re.search(txt)
    cnts = {}
    for line in txt.splitlines():
        m = cnt_re.match(line.strip())
        if m:
            cnts[m.group(1)] = int(m.group(2))
    steps = [int(m.group(2)) for m in step_re.finditer(txt)]
    guards = [(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5)), m.group(6)) for m in guard_re.finditer(txt)]
    nrq = nrq_re.search(txt)
    rows.append(dict(cfg=cfg, probes=probes, writs=writs, blk=blk.groups() if blk else None,
                     cnts=cnts, steps=steps, guards=guards, nrq=nrq.group(1) if nrq else None))

out = []
def p(s=""):
    out.append(s)

p("=" * 96)
p("MATCHED BENCHMARK MATRIX - run 40002470957")
p("=" * 96)
for r in rows:
    p(f"\n### {r['cfg']}")
    if r["blk"]:
        p(f"    BLKSTAT-DELTA read={r['blk'][0]} write={r['blk'][1]}")
    p("    counters: " + " ".join(f"{k}={v}" for k, v in sorted(r["cnts"].items())))
    if r["writs"]:
        p(f"    writers: n={len(r['writs'])} OPS={sum(w[0] for w in r['writs'])} BYTES={sum(w[1] for w in r['writs'])} ERR={sum(w[3] for w in r['writs'])} FSYNC_ERR={sum(w[4] for w in r['writs'])}")
    if r["probes"]:
        p(f"    probes : n={len(r['probes'])}")
        for q in r["probes"]:
            p(f"      SAMPLES={int(q[0])} MIN={q[1]:.0f} P50={q[2]:.0f} P90={q[3]:.0f} P99={q[4]:.0f} MAX={q[5]:.0f} MEAN={q[6]:.1f}")
    if r["guards"]:
        sane = [g for g in r["guards"] if g[2] < 100000]
        fired = sum(1 for g in r["guards"] if g[4] == 1)
        nf = sum(1 for g in r["guards"] if g[4] == 0)
        p(f"    guard  : total={len(r['guards'])} FIRED={fired} NOT_FIRED={nf}")
        p(f"             r_min sane range {min(g[2] for g in sane) if sane else '-'}..{max(g[2] for g in sane) if sane else '-'}us; insane_events={len(r['guards'])-len(sane)}; target={r['guards'][0][3]}us")
        acts = {}
        for g in r["guards"]:
            acts[g[5]] = acts.get(g[5], 0) + 1
        p(f"             actions={acts}")
    elif r["cnts"].get("wbt_guard", 0) == 0:
        p("    guard  : none (baseline or not hit)")
    if r["steps"]:
        p(f"    steps  : distinct={sorted(set(r['steps']))} final={r['steps'][-1]} n_transitions={len(r['steps'])}")

p("\n" + "=" * 96)
p("PAIRED COMPARISON")
p("=" * 96)
def by(n):
    for r in rows:
        if r["cfg"] == n:
            return r
for dev in ("ide", "vio"):
    for prof in ("h", "s"):
        b = by(f"baseline_{dev}_{prof}"); q = by(f"patched_{dev}_{prof}")
        if not (b and q):
            continue
        p(f"\n-- {dev} / {prof} --")
        if b["probes"] and q["probes"]:
            p(f"   samples : base={int(sum(x[0] for x in b['probes']))} patched={int(sum(x[0] for x in q['probes']))}")
            for i, lbl in enumerate(("MIN", "P50", "P90", "P99", "MAX")):
                p(f"   {lbl:<8}: base={[x[i+1] for x in b['probes']]} patched={[x[i+1] for x in q['probes']]}")
        p(f"   wOPS    : base={sum(w[0] for w in b['writs']) if b['writs'] else 0} patched={sum(w[0] for w in q['writs']) if q['writs'] else 0}")
        p(f"   timer   : base={b['cnts'].get('wbt_timer')} patched={q['cnts'].get('wbt_timer')}")
        p(f"   guard   : base={b['cnts'].get('wbt_guard')} patched={q['cnts'].get('wbt_guard')}")
        p(f"   finstep : base={b['steps'][-1] if b['steps'] else None} patched={q['steps'][-1] if q['steps'] else None}")

txt = "\n".join(out)
open(r"C:\Users\vharr\AppData\Local\Temp\wbt_validate\bench_matrix.txt", "w", encoding="utf-8").write(txt)
print(txt)
