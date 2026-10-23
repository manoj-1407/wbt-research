# Limitations

## 1. Single repetition carries all content

Three matched runs were executed (see `results.md` §7) but only run 1's logs
were retrievable. GitHub Actions artifact download requires authentication
(`401`), and the run-logs endpoint returns `403`; this environment has no
GitHub credential. The token that was used earlier in the project must be
treated as compromised and rotated, so it is deliberately not reused.

Effect: every latency and throughput number in `results.md` §5 comes from
**one** execution. The direction of the `vio_s` effect is consistent across
six independent reader processes with a within-run spread far smaller than
the observed gap, but the experiment cannot separate a real +2…5 %
regression from run-to-run variation in shared-runner QEMU. No median,
no inter-run variability, and no per-repetition guard counts exist.

Secondary defect found while investigating this: the workflow's
`Commit benchmark logs to repository` step is inert because `.gitignore`
lists `*.log`. Fixing it (see §6) would make future repetitions
self-archiving without any token.

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
