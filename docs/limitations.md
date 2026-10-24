# Limitations

## 1. Cross-run comparability, not content availability

All three matched runs (`40002470957`, `40004718895`, `40004771234`) are
now content-available in the repository: run 1 under `results/baseline` +
`results/patched`, the repetitions under `results/reps/rep2` and
`results/reps/rep3` (see `results.md` §7).

Artifact download from GitHub still requires authentication — `401` on
`GET /actions/artifacts/<id>/zip`, `403` on `GET /actions/runs/<id>/logs` —
so the repetition payloads were supplied as ZIPs and their SHA-256
recomputed from the ZIP bytes. Each digest equals the one the run page
publishes for that artifact, and that match is what establishes provenance
(recorded in `results.md` §7.1). The previously exposed token was not
reused.

Effect: the third run does **not** confirm run 1 — it contradicts its sign,
and so does the `ide_s` control (`results.md` §8). Three limitations stand:

* **The three runs are not comparable replicates.** Runs 2/3 sit at a
  ~2.2× higher absolute latency than run 1, with per-probe read-sample
  counts and 100 ms window counts scaling down proportionally. On a shared
  `ubuntu-22.04` runner with no usable KVM this is a QEMU-**TCG** host-speed
  artifact, so the set is effectively `n = 1` fast host + `n = 2` slow host,
  not `n = 3`.
* **The pooled median is not an effect estimate.** It is arithmetically
  correct but pools two different operating regimes, so it cannot separate
  a real effect from runner drift.
* **The noise floor is measured and exceeds the effect.** `ide_h` is
  provably inert in the patched kernel (every evaluation returns
  `scale_up`, the same decision the baseline makes, on a degenerate depth
  lever), yet its writer throughput differs by a consistent **+7–8 %** in
  all three runs — the same magnitude as the largest reported latency
  delta. This is most likely the run-order bias of §6 defect 2.

Secondary defect found while investigating this, now fixed: the workflow's
`Commit benchmark logs to repository` step was inert because `.gitignore`
listed `*.log`. `.gitignore` now re-includes `results/**/*.log`, so the
repetition traces are tracked like run 1's.

## 2. Emulation, not hardware

`ubuntu-22.04` GitHub runners have no usable KVM for this workflow, so the
guest runs under QEMU **TCG**. Absolute latencies (hundreds of µs) are
dominated by emulation and are not transferable to real hardware. Only
ratios between matched configurations on the same runner are meaningful, and
even those can drift between runs. No physical device, no real NVMe/SATA
latency, no `blk-mq` scheduler effects on a real device were measured.

## 3. The guarded quantity is the window minimum, not a percentile

The guard compares `cb->stat[READ].min` — the **minimum** read latency
observed in a 100 ms window — against `rwb->min_lat_nsec`. That is the
kernel's own predicate, and the patch deliberately does not change it. But
it means the guard is only sensitive to the best-case read in each window,
which is why on run 1 `patched_ide_s` evaluated 288 times and never held
(`r_min` 62–77 µs) while its P50 was ~540 µs. Any claim that "the guard
protects read latency" must be qualified: it reacts to minimum latency, not
to tail latency.

The repetitions sharpen this rather than weakening it. On reps 2/3 the same
nominal `ide_s` configuration produces `r_min` 100–166 µs and **does** hold
3–4 times (`results.md` §7.3) — not because the reads changed, but because
the emulated host is slower and the whole latency floor moved up across the
150 µs target. The predicate is therefore tracking the host's speed floor,
not the read-quality situation it was written to detect. This is arguably
the most important open question in the whole investigation.

## 4. Inertness of the `ide` control is over-determined

`ide` is inert for two independent reasons: `queue_depth == 1` degenerates
the depth lever (`max_depth == 2` for all `scale_step <= 0`), **and** the
sparse profile's `r_min` (62–77 µs) sits below the 150 µs target. The
control confirms that no measurable change appears, but it does not isolate
which of the two reasons dominates. `patched_ide_h` adds a third condition
(`r_samples == 0`). A cleaner control would keep a deep queue and only vary
the target latency.

## 5. Small n in the heavy profile

`heavy` uses 2 reader processes and ~12 400 samples per config, versus 6
readers and ~16 000 samples in `sparse`. Its two probes are also visibly
unstable in the baseline (`ide_h` P99 sd 19 µs, `MAX` 644 675 µs twice).
Heavy-profile numbers are reported for completeness and are explicitly not
used as evidence of anything.

## 6. Known harness defects

| # | Defect | Impact |
| :-- | :--- | :--- |
| 1 | **Fixed.** `.gitignore` `*.log` made the log-commit step a no-op | no longer applies: `.gitignore` now re-includes `results/**/*.log`, so run 1 and reps 2/3 are archived in-repo (`results/baseline`, `results/patched`, `results/reps/rep2`, `results/reps/rep3`) |
| 2 | Repetitions are dispatched in the same configuration order rather than alternating | a monotonically drifting runner biases every repetition identically; this is the likely cause of the consistent **+7–8 %** `ide_h` writer-OPS bias measured in all three runs, which is the same magnitude as the largest latency delta (`results.md` §8) |
| 3 | `ide_h` / `vio_h` have only 2 reader processes | single-sample `MAX` outliers dominate any tail comparison |
| 4 | The guard's `r_min` for `r_samples == 0` prints `U64_MAX/1000` | confusing but harmless; the predicate short-circuits first |

## 7. Scope limits of the claim

* Nothing here tests `REQ_OP_DISCARD`, multi-queue (`num_queues > 1`)
  virtio-scsi, cgroup `io.latency`, or `blk-throttle` interaction.
* Nothing here measures write amplification, dirty-page accounting, or
  whether holding depth at step 0 risks OOM/stall under sustained load; the
  30 s run with a 4 MiB write window cannot expose that.
* The `wbt_guard` tracepoint adds a per-window `strscpy` + ring-buffer
  record. With 100 ms windows this is negligible, but the patched kernel is
  not byte-identical in cost to the baseline, so "no difference" in the
  inert configurations is an upper bound, not an equality.
