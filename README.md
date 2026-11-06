# WBT Sparse-Write Investigation — Validated Research Record

**Subsystem:** Linux Block Layer / Writeback Throttling (`block/blk-wbt.c`)
**Kernel version:** Microsoft WSL2 Linux kernel `6.6.123.2-microsoft-standard-WSL2`
(base commit `a07f9ea8a99139913acbcc1c160b132cb2a49c81`). Controller logic
verified identical in Linux mainline master (`block/blk-wbt.c:304–471`).
**Result:** Negative. The proposed reclassification worsened reader P99 latency.

> [!NOTE]
> This branch (`research/validated-wbt-study`) is the primary validated research record.
> It contains the complete documentation, 10 raw benchmark logs, independent recalculation
> scripts, and the experimental candidate patch (`0005-*.patch`). The `experiment/v5-reclassification`
> branch preserves the early prototype development history up to PR #1 merge (`1656fa3`).
> Neither branch represents a recommended kernel change. The investigation produced a negative result.

---

## 1. What Was Investigated

Linux WBT controls writeback I/O queue depth to protect read latency.
It accumulates read and write completion statistics over 100 ms windows.
At each window boundary, `wb_timer_fn()` calls `latency_exceeded()` to decide
whether to scale write depth up, down, or hold.

The specific observation:

1. `stat_sample_valid()` requires ≥ 1 read completion **and** ≥ 3 write
   completions (`RWB_MIN_WRITE_SAMPLES`).
2. When reads are present but write completions are sparse (1 or 2),
   `latency_exceeded()` returns `LAT_UNKNOWN_WRITES`.
3. `wb_timer_fn()` handles `LAT_UNKNOWN_WRITES` by calling `scale_up(rwb)`,
   widening write queue depth.
4. This occurs even when read completions in the same window recorded latency
   above the configured target.

**Hypothesis tested:** Reclassifying these mixed windows as `LAT_UNKNOWN`
(holding depth instead of scaling up) would protect concurrent read latency.

---

## 2. What Was Found

The hypothesis was not supported.

A controlled QEMU/KVM benchmark ran six direct-I/O readers alongside one sparse
buffered writer. Five paired runs compared the unmodified kernel (baseline)
against the reclassified kernel (v5 patch).

| Metric | Baseline | v5 (patch) | Direction |
|---|---|---|---|
| Reader P99 latency | lower | **higher in all 5 pairs** | ❌ Regression |
| Reader P50 latency | — | approximately flat | — |
| Writer throughput (OPS) | — | approximately flat | — |
| Controller step (peak) | −3 (wb_normal=48) | 0 (wb_normal=8) | Patch held depth |

**Mean paired P99 increase under v5: approximately +1.76 ms.**
Two-sided sign test: p = 0.0625 (n = 5). This does not reach the conventional
5% significance threshold. The result is directionally consistent across all
five pairs, but the sample is small and the experiment is limited to one
virtualized workload configuration.

The post-reader delayed-writeback-burst hypothesis was independently tested and
falsified: the P99 regression was captured during active reader execution,
not in a post-completion burst (see `docs/FINAL-INVESTIGATION-SUMMARY.md`).

**Root cause of the regression: unresolved.**
Plausible explanations (none established):
- Synchronization stalls during `fsync()` at restricted queue depth
- Intentional design: sparse writes do not threaten read bandwidth;
  throttling them adds friction without benefit
- QEMU/KVM scheduling noise as a contributing factor

---

## 3. Repository Structure

```
docs/
  FINAL-INVESTIGATION-SUMMARY.md     ← Start here. Full narrative summary.
  reproducibility-guide.md           ← How to reproduce the benchmark.
  controlled-benchmark-results.md    ← Paired run results with controller state.
  independent-benchmark-validation.md← Independent recalculation (not the original script).
  p99-root-cause-evidence-review.md  ← Evidence review for each root-cause candidate.
  maintainer-inquiry-reassessment.md ← Reassessment of the original maintainer inquiry.
  upstream-rfc-inquiry.md            ← Archived inquiry draft (burst theory annotated superseded).
  methodology.md                     ← Workload configuration, kernel builds, tracepoints.
  limitations.md                     ← Explicit limitations of the study.
  final-decision.md                  ← Decision record: v5 not submitted upstream.

results/
  controlled_benchmark/              ← 10 raw benchmark logs (5 baseline, 5 v5). Do not modify.
  independent_audit/                 ← Independent recalculation summary (JSON).
  comparison/                        ← Bench matrices for earlier multi-configuration runs.
  baseline/, patched/                ← Earlier single-configuration QEMU logs.
  reps/                              ← Three-repetition reconciliation data.

scripts/
  independent_benchmark_audit.py     ← Reproduces all paired statistics from raw logs.
  analyze_controlled.py              ← Original analysis script.
  causation_analysis.py              ← Timeline analysis for burst hypothesis falsification.
  recalculate_all_runs.py            ← Multi-run recalculation.

0005-blk-wbt-reclassify-sparse-writes-with-active-reads.patch   ← The experimental patch.
```

---

## 4. Reproducing the Results

The independent benchmark audit script (`scripts/independent_benchmark_audit.py`)
recalculates all paired metrics directly from raw logs via an independent parsing
implementation, verifying mathematical reproducibility without depending on the original
analysis script. (Note: this is an independent software-level recalculation/audit of
recorded logs, not an external hardware re-run).

```bash
python3 scripts/independent_benchmark_audit.py
```

Expected output: all 10 logs pass integrity checks; five paired comparisons
all show v5 P99 ≥ baseline P99; mean delta ≈ +1763 µs.

For full reproduction including kernel builds, see `docs/reproducibility-guide.md`.
Kernel worktrees used:
- Baseline: `/home/manoj/kernel-src-baseline` (commit `a07f9ea8a`, detached HEAD)
- v5 patch: `/home/manoj/kernel-src-v5`, branch `candidate-v5-reclassification`

---

## 5. Branch Guide

| Branch | Contents |
|---|---|
| `main` | Original development history: CI workflows, early guard prototype, three-run reconciliation |
| `research/validated-wbt-study` | **This branch.** Validated negative-result research record, documentation, raw logs, and v5 patch. |
| `experiment/v5-reclassification` | Early prototype and benchmark iteration commit history up to PR #1 merge (`1656fa3`) |
| `exp/hold-depth-wbt-guard` | Earlier guard implementation with GitHub Actions CI |
| `exp/qemu-guard-audit` | QEMU validation experiments and stashes |
| `audit/upstream-readiness-2026-10-25` | Local-only audit branch (source of this branch's content) |

---

## 6. Statistical Caveats

Throughout this repository, the following precision commitments apply:

- P99 values are **unweighted averages of six individual reader P99s**, not
  globally pooled P99 across all requests. This distinction is documented in
  each analysis.
- The sign test result is **always reported as two-sided, p = 0.0625**.
  One-sided p = 0.03125 is not used, as the direction of effect was part of
  the hypothesis.
- The result is **not described as statistically significant** at the
  conventional 5% level.
- The investigation is described as: *a controlled study of Linux block-layer
  writeback throttling under sparse-write mixed-I/O workloads, producing a
  negative optimization result.*

---

## 7. What This Is Not

- This is **not** a kernel bug report.
- This is **not** a proposed upstream patch. The v5 patch (`0005-*.patch`) is
  an experimental candidate that was tested and rejected.
- This is **not** a performance improvement claim.
- This is a reproducible record of a negative experimental result, with full
  methodology and independently validated measurements.

---

## 8. Maintainer Inquiry & Author

**Author:** G Manoj Kumar (`manojkumar148700@gmail.com`)
**Repository:** `https://github.com/manoj-1407/wbt-research`
**Maintainer inquiry status:** Prepared and drafted for `linux-block@vger.kernel.org`
(CC: `axboe@kernel.dk`); inquiry text archived in `docs/maintainer-inquiry-reassessment.md`.
Delivery pending plain-text retransmission following mailing list formatting bounce.
