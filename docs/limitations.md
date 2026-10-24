# Limitations

## 1. Three repetitions; no stable effect established

Three matched runs were executed and all three artifacts were retrieved and parsed during the 2026-10-24 reconciliation. Provenance and content-level summaries are in docs/three-run-reconciliation.md and docs/results.md §8.

Effect: the sparse latency deltas change direction across runs, so there is no stable improvement or regression. The runs share a QEMU TCG runner and were not randomized/alternated; drift and order effects remain possible. Three runs are insufficient for a robust variability estimate.

The archive/logging workflow defect remains a future-run concern; this reconciliation used downloaded Actions artifacts.

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
which is why `patched_ide_s` evaluated 288 times and never held (r_min
62–77 µs) while its P50 was ~540 µs. Any claim that "the guard protects read
latency" must be qualified: it reacts to minimum latency, not to tail
latency. This is arguably the most important open question in the whole
investigation.

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

## 6. Known harness defects (not fixed in this pass)

| # | Defect | Impact |
| :-- | :--- | :--- |
| 1 | `.gitignore` `*.log` makes the log-commit step a no-op | repetitions are not archived in-repo; only artifacts (auth-gated) hold them |
| 2 | Repetitions were dispatched back-to-back in the same order rather than alternating configuration order | a monotonically drifting runner would bias all repetitions identically |
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


## 8. Repeatability update (2026-10-24)

The previous single-content limitation is superseded: all three matched Actions artifacts were retrieved and parsed. Run 1's artifact digest was verified against the downloaded ZIP; run 2/3 hashes and run IDs are documented in docs/three-run-reconciliation.md. Per-run sparse metrics and guard counts are now available in docs/results.md §8 and the addendum.

The limitation is now **three repetitions only**, with workload order not randomized/alternated and a shared GitHub QEMU TCG runner. Results show mixed directions and cannot establish a stable latency benefit or regression. The heavy workload remains only two reader processes per configuration. Hardware/KVM validation and additional randomized repetitions remain necessary.
