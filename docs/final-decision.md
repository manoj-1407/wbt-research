# Final Report — WBT `LAT_UNKNOWN_WRITES` guard

## Final Status

**REVISE.**

The guard is implemented, compiles into the kernel, is observable, and
demonstrably changes the controller's decision in exactly the branch it
patches. The *performance hypothesis it was built to test is not
supported*. Three matched runs of the same commit are now
content-available: in the only configuration where the guard actually held
depth (`vio_s`), read latency was **worse** in run 1 (+5.1 % P50,
+1.8 % P99) but **marginally better** in reps 2/3 (−0.93 % and −1.60 %
P50), while the inert `ide_s` control flipped sign too. The treatment's
direction does not reproduce, the runs sit at two different host-speed
operating points (~2.2× apart, a QEMU **TCG** artifact), and the measured
`ide_h` noise floor (+7–8 % writer throughput on a provably inert
configuration) is larger than the largest latency delta. No performance
effect is resolvable above cross-run variation. This is not a failure of
the mechanism — it is a failure of the premise, and it needs both a
redesign of the predicate and a controlled repetition design before any
upstream claim.

## Execution Summary

Work was resumed from the existing repository (`D:\io_investigation\wbt_patch`,
branches `exp/qemu-guard-audit` @ `c53cd2a`, `exp/hold-depth-wbt-guard`,
`main`) and GitHub state, not restarted.

| Phase | Status |
| :--- | :--- |
| 1 — build + patch audit | complete |
| 2 — matched baseline-vs-patched QEMU experiment | complete, valid (8/8 configs) |
| 3 — evidence analysis | complete |
| 4 — bounded repeatability (3 repetitions) | complete — 3/3 runs content-available; the runs are **not** comparable replicates (`results.md` §7.2) |
| 5 — research package | complete (`docs/`, `results/`, `scripts/`) |
| 6 — GO / REVISE / STOP | **REVISE** |

Root cause of the earlier all-zero result was identified and fixed in this
line of work: the previous harness issued writes as O_DIRECT, and
`wbt_should_throttle()` exempts `REQ_SYNC|REQ_IDLE` writes
("Don't throttle WRITE_ODIRECT"), so `wb_timer` could never arm. Writers are
now buffered + `fsync`; `wbt_timer` is non-zero in all 8 configurations.

## Build Evidence

* Build workflow `build-wbt-kernels.yml`, run **`39987714631`**.
* Kernel: WSL2 6.6.y @ `a07f9ea8a99139913acbcc1c160b132cb2a49c81`.
* `bzImage` SHA-256 — baseline `553EBD52…FB002D`, patched `CB926FD7…6F0EF46E`;
  both 16 007 168 bytes.
* `vmlinux` SHA-256 — baseline `7E4233B5…AD9F4AA6`, patched `4BF1ADDD…E7F18F035`.
* Guard symbol gate, recomputed locally on the binaries:
  `wbt_guard` occurrences **0 (baseline) vs 47 (patched)**;
  `guard_fired` **0 vs 6**; `wbt_timer` 47 in both.
* Guest confirmed `CONFIG_BLK_WBT=y`, `CONFIG_BLK_WBT_MQ=y`, a live
  `queue/wbt_lat_usec`, and `/sys/kernel/tracing/events/wbt`.

## Patch Audit

* `0001-wbt-hold-depth-on-LAT_UNKNOWN_WRITES-when-reads-are-elevated.patch`
  matches the diff actually applied in the artifact
  (`results/build/wbt-patch-applied.diff`); only hunk offsets and the
  illustrative index hashes differ.
* The patch touches exactly two files: `block/blk-wbt.c` (guard + 2 trace
  calls) and `include/trace/events/wbt.h` (new `TRACE_EVENT(wbt_guard)`).
* Reachability audit: `LAT_OK`/`LAT_EXCEEDED` require
  `stat_sample_valid() == true`, so `LAT_UNKNOWN_WRITES` is the only
  reachable evaluation point; the patch's only behavioural change is
  `break` instead of `scale_up(rwb)` under the new predicate.
* The tracepoint `TP_printk` format matches the analyser regex exactly, so
  a parse failure cannot be mistaken for "guard never fired".

## Runtime Evidence

Primary run **`40002470957`** (`head_sha c53cd2a`, success), 2×2×2 matrix
(ide/virtio-scsi × baseline/patched × heavy/sparse), 30 s reader window,
one fresh disk image per kernel.

* `wbt_timer` 293–304 in **every** configuration; non-zero read and write
  sectors at the block layer in every configuration → the workload is
  WBT-tracked and the measurement is real.
* `wbt_guard` = **288 (ide_s)** and **283 (vio_s)** on the patched kernel,
  **0** on every baseline kernel.
* Guard held depth **276/283** times in `patched_vio_s`; **0** times in
  `patched_ide_s` (r_min 62–77 µs vs 150 µs target), **0** times in
  `patched_ide_h` (r_samples == 0), and the branch was **never entered** in
  `patched_vio_h` (heavy has ≥ 3 write completions/window → `LAT_OK`).
* Depth escalation in `patched_vio_s` was delayed from **2.36 s to 8.56 s**
  (`scale_step` 0→−1→−2→−3). All seven other configurations matched their
  baselines within 0.12 s.

## Benchmark Results

Matched deltas (patched − baseline), µs:

| config | P50 | P90 | P99 | read samples | writer OPS |
| :--- | ---: | ---: | ---: | ---: | ---: |
| vio_s — guard held 276× | +27 (+5.1 %) | +31 (+3.9 %) | +21 (+1.8 %) | −2.3 % | 367 → 367 |
| ide_s — evaluated, never held | +4 (+0.7 %) | −4 (−0.4 %) | +15 (+1.2 %) | +0.2 % | 364 → 364 |
| vio_h — branch not entered | +14 (+1.6 %) | +20 (+1.4 %) | −134 (−6.3 %) | 0.0 % | −1.1 % |
| ide_h — never held | −26 (−3.4 %) | +5 | −9 (−0.4 %) | +6.2 % | +8.1 % |

The `vio_s` P50 gap of 27 µs is ~11× the pooled within-run standard
deviation (~1.9 µs across six concurrent reader processes), so it is not
probe noise within run 1 — but it is **not reproduced** in reps 2/3, which
move −0.93 % and −1.60 % (P50) at a ~2.2× higher absolute latency.

### Cross-run result (3 runs)

Same commit, same harness, same declared parameters. Averaged over the six
concurrent reader probes **within** each run (the probes are concurrent
observations of one execution, not six repetitions):

| run | `vio_s` ΔP50 | `vio_s` ΔP99 | `ide_s` ΔP50 | `ide_h` writer OPS |
| :--- | ---: | ---: | ---: | ---: |
| run 1 `40002470957` | +5.13 % | +1.83 % | +0.68 % | +8.1 % |
| rep 2 `40004718895` | −0.93 % | −1.82 % | −1.94 % | +7.2 % |
| rep 3 `40004771234` | −1.60 % | −1.62 % | +0.82 % | +7.2 % |

* **Both the treatment and the control change sign across runs.** `vio_s`
  P50 goes +5.13 % → −0.93 % → −1.60 %; the inert `ide_s` control goes
  +0.68 % → −1.94 % → +0.82 %.
* **Runs 2/3 sit at ≈2.2× run 1's absolute latency** on byte-identical
  workloads, kernel and parameters — a QEMU **TCG** host-speed artifact
  (no usable KVM on `ubuntu-22.04`). The set is effectively `n = 1` fast
  host + `n = 2` slow host, not `n = 3`.
* **The noise floor is measured and exceeds the effect.** `ide_h` is
  provably inert (every evaluation returns `scale_up`, the same decision the
  baseline makes, on a degenerate lever), yet its writer throughput differs
  by a consistent +7–8 % in **all three** runs.
* **The pooled median is not a defensible effect estimate.** −10.83 µs
  (P50) and −40.83 µs (P99) are correct medians of three numbers, but they
  pool two operating regimes and a regime whose own control flips sign.

Full detail: `results.md` §7 (repeatability) and §8 (cross-run result).

## Interpretation

The mechanism works and is correctly scoped. The hypothesis does not.

The guard removes write-side headroom (it delays `scale_step` from reaching
−3, i.e. delays `wb_normal` growing from 8 to 48) during the phase when
reads are being paced, and yet reads did not get faster. Three explanations
are consistent with the data, in decreasing order of confidence:

1. **The premise is too weak to matter.** The baseline's escalation happens
   at t ≈ 2.2 s, before the workload is even saturated, and the guard merely
   delays a saturation that is reached anyway at t ≈ 8.6 s. Over a 30 s run
   the guard restrains depth for ~21 % of the time, then behaves identically.
   A ~20 % duty cycle on the write-depth lever produced no measurable
   benefit, and possibly a small cost.
2. **The predicate measures the wrong statistic.** `cb->stat[READ].min` is
   the window *minimum*. It only exceeds a 150 µs target when the very
   fastest read is slower than target; in `patched_ide_s` it never did
   (62–77 µs) despite P50 ≈ 540 µs. Tail latency — the thing the hypothesis
   cares about — is not what the guard looks at. That the guard fires at all
   in `vio_s` is largely accidental: under TCG the paced O_DIRECT readers
   have a floor near the 150 µs target.
3. **Holding depth is not the right conservative action.** Retaining dirty
   pages in the page cache lengthens the writeback burst that eventually
   has to be drained, which can worsen read service. `scale_down`-style
   throttling of writers (slowing write submission) is a different and
   plausible alternative that this experiment did not test.

What is *not* in doubt: the null result of the earlier harness was a
harness bug (O_DIRECT writes are WBT-exempt) and has been fixed; the guard
code is correct with respect to its stated predicate, including the
boundary in run 1 (`scale_up` at r_min 146–149 µs, `hold` from 153 µs up);
and the patch is provably inert outside its branch. What the repetitions
add is that the *direction* of the effect in the branch is not stable — so
the hypothesis fails not merely on this run but across the available data.

## Limitations

* **Three runs are content-available, but they are not comparable
  replicates.** Reps 2 and 3 (`40004718895`, `40004771234`) completed
  `success` at the same commit and are now archived in-repo
  (`results/reps/rep2`, `results/reps/rep3`; `results.md` §7), with their
  SHA-256 digests recomputed from the ZIP bytes and shown to equal the
  digests the run pages publish — which is what fixes each payload to its
  run. They sit, however, at ~2.2× run 1's absolute latency on
  byte-identical workloads (a QEMU **TCG** host-speed artifact, since the
  runners have no usable KVM), so the set is `n = 1` fast host + `n = 2`
  slow host. Cross-run statistics therefore **do** exist now, but they show
  the treatment sign flipping (`vio_s` P50 +5.13 % → −0.93 % → −1.60 %) and
  the inert `ide_s` control flipping with it, while `ide_h` carries a
  consistent +7–8 % writer-throughput bias the patch cannot cause. The
  +2…5 % run-1 `vio_s` regression cannot be separated from runner drift,
  and the pooled median is not an effect estimate (`results.md` §8).
  GitHub retrieval remains auth-gated (`401` on artifact zip, `403` on run
  logs) and the previously exposed token was not reused, so future payloads
  must again be supplied out-of-band; `.gitignore` now re-includes
  `results/**/*.log` so they are tracked.
* TCG emulation only; KVM was unavailable. Absolute latencies are not
  transferable to hardware.
* The `ide` control is inert for two overlapping reasons (degenerate lever
  *and* low `r_min`), so it does not isolate which dominates. In reps 2/3 it
  does hold 3–4 times (the host-speed floor crosses the target) with still
  no measurable change, but the lever (`queue_depth == 1` → `max_depth == 2`)
  stays degenerate throughout, so inertness is not fully disentangled.
* Heavy profile has n=2 reader processes; its `MAX` values are single-sample
  artifacts and are reported only for completeness.
* Scope: single-queue virtio-scsi, no DISCARD, no cgroup `io.latency`, no
  sustained-load / OOM-stall testing.

Full detail: `docs/experiment.md`, `docs/methodology.md`, `docs/results.md`,
`docs/limitations.md`.

## Final Decision

**REVISE** — not GO, not STOP.

*Not GO*: the guard produced no reproducible reader-latency improvement —
its sign flips across the three content-available runs in the only
configuration where it engaged (`vio_s`: +5.13 % → −0.93 % → −1.60 % P50),
the inert `ide_s` control flips with it, and the guard is inert in the
configurations the hypothesis implicitly targets (deep-queue heavy I/O).
There is no defensible performance claim to make on the mailing list today.

*Not STOP*: the underlying observation is real and verified — WBT genuinely
discards completed-read evidence in `LAT_UNKNOWN_WRITES` and genuinely
responds by widening write depth, and this is now observable in-kernel with
`wbt_guard` (276 live holds in run 1's 30 s window; 176 in each of reps 2/3).
The branch deserves the attention; the current remediation does not.

## Next Action

1. **Remove the host-speed confound first** (cheap, unblocks everything).
   *Applied in this change:* `.gitignore` re-includes the workflow's log
   directory (`!benchmarks/smoke_test/*.log` and `!results/**/*.log`), so
   repetition traces are archived in-repo, and
   `scripts/summarize_bench.py` now takes
   `[bench_dir] [run_label] [out_name]` so the same generator emits each
   repetition's matrix without hand transcription (`results.md` §7.1).
   *Still to do:* re-run the repetitions **alternating configuration
   order**, and either obtain a KVM-accelerated runner or pin/record host
   speed so all repetitions sit at one operating point. Until the ~2.2×
   host-speed split is removed, extra runs add regimes, not replicates.
2. **Re-target the predicate.** Replace the window-minimum comparison with
   a statistic that reflects the hypothesis — e.g. require a meaningful read
   sample count and compare against a percentile, or gate `scale_up` on
   write-side pressure rather than read minimums. The `ide_s` result is
   direct evidence the predicate is blind to the situation it was written
   for: in run 1 it evaluated 288 times, never held, and had P50 ≈ 540 µs
   with `r_min` 62–77 µs; in reps 2/3 the same nominal configuration holds
   3–4 times purely because the slower host pushed the latency floor across
   the 150 µs target. The predicate is tracking the emulator's speed floor,
   not read quality.
3. **Test the alternative action.** Compare `hold` against throttling
   writers (rather than merely not widening them) in the same harness; the
   unstable `vio_s` sign makes "hold" the weaker candidate.
4. **Only then** re-evaluate GO/REVISE/STOP, and only with ≥ 3 repetitions
   that share one operating point.
5. **Security follow-up:** revoke/rotate the previously exposed GitHub token
   before any further CI work.
