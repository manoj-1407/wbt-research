# Three-run reconciliation (2026-10-24)

## Provenance

All three Actions runs used commit c53cd2a7eb6fb03b3dbcd3ab9fb07dd8e4d0f9b4, succeeded, and contain baseline/patched traces for the four workload configurations.

| repetition | run ID | artifact bytes | artifact SHA-256 |
|---|---:|---:|---|
| 1 | 40002470957 | 90,338 | eb5cf6b882edcf621064df015b3c3cad9771679be662b53d2b9cac71052c9900 |
| 2 | 40004718895 | 90,956 | d27aaed10c9d0cef512c67fadc9f06d7fedd804b1209809862e9e0115a407587 |
| 3 | 40004771234 | 90,255 | 575bbfab4cf3aea16681abb5bd88317b3e80543b21603bcc696bd0109d25dd0e |

Run 1 was downloaded and parsed on 2026-10-24. Six sparse reader probes were averaged within each kernel/configuration; heavy configurations have two reader processes. These are per-run means, not pooled raw samples.

## Sparse workload reader latency and writer throughput

Values are mean across reader processes; latency in microseconds. Delta is patched minus baseline.

| run | config | P50 base → patched (delta, %) | P99 base → patched (delta, %) | writer OPS base → patched |
|---|---|---:|---:|---:|
| 1 | ide_s | 540.17 → 543.83 (+3.67, +0.68%) | 1294.83 → 1309.83 (+15.00, +1.16%) | 364 → 364 |
| 1 | vio_s | 526.00 → 553.00 (+27.00, +5.13%) | 1137.00 → 1157.83 (+20.83, +1.83%) | 367 → 367 |
| 2 | ide_s | 1394.50 → 1367.50 (−27.00, −1.94%) | 2714.33 → 2619.67 (−94.67, −3.49%) | 355 → 356 |
| 2 | vio_s | 1166.00 → 1155.17 (−10.83, −0.93%) | 2955.17 → 2901.50 (−53.67, −1.82%) | 362 → 362 |
| 3 | ide_s | 1361.17 → 1372.33 (+11.17, +0.82%) | 2609.50 → 2614.50 (+5.00, +0.19%) | 358 → 358 |
| 3 | vio_s | 1142.33 → 1124.00 (−18.33, −1.60%) | 2522.00 → 2481.17 (−40.83, −1.62%) | 363 → 362 |

## Heavy workload (two-reader process means)

| run | config | P50 base → patched | P99 base → patched | writer OPS base → patched |
|---|---|---:|---:|---:|
| 1 | ide_h | 762.00 → 736.00 | 2427.00 → 2417.50 | 8164 → 8835 |
| 1 | vio_h | 861.50 → 876.00 | 2138.50 → 2003.50 | 14104 → 13967 |
| 2 | ide_h | 1651.50 → 1593.50 | 5037.00 → 4729.50 | 4546 → 4949 |
| 2 | vio_h | 1858.00 → 1806.50 | 4108.00 → 3909.50 | 6764 → 6866 |
| 3 | ide_h | 1573.50 → 1571.00 | 4806.00 → 4401.00 | 4650 → 5029 |
| 3 | vio_h | 1719.00 → 1748.00 | 3670.50 → 3699.00 | 7040 → 6963 |

Heavy results are descriptive only: n=2 readers/configuration, and the ide controller lever is degenerate at queue depth 1. Do not infer hardware performance from these QEMU TCG values.

## Guard decisions

| run | patched ide_s total / hold | patched vio_s total / hold | patched ide_h decisions | patched vio_h decisions |
|---|---:|---:|---:|---:|
| 1 | 288 / 0 | 283 / 276 | 8 (0 holds; no read samples) | 0 (branch not entered) |
| 2 | 289 / 4 holds | 184 / 176 holds | 7 | 9 |
| 3 | 288 / 3 holds | 184 / 176 holds | 6 | 7 |

For repetitions 2 and 3, total decisions and hold/scale-up split were parsed from raw traces: ide_s had 285 scale-up decisions each run; vio_s had 8 scale-up decisions each run. Heavy guard events did not produce holds. Guard-event counts must not be confused with hold counts.

## Repeatability conclusion

The three-run evidence now exists and is content-level comparable. Sparse vio_s P50 direction is mixed: run 1 +5.13%, run 2 −0.93%, run 3 −1.60%; P99 is +1.83%, −1.82%, −1.62%. Sparse ide_s is also mixed, with run 1 slightly worse and runs 2/3 slightly better. This does not support a stable latency improvement or stable regression. Writer OPS are essentially flat for sparse workloads.

All three artifact digests (`eb5cf6...`, `d27aae...`, `575bbf...`) were obtained directly from the GitHub Actions API artifact metadata (`GET /actions/runs/<id>/artifacts`). Because artifact ZIP downloads from GitHub require authentication (`401 Unauthorized`), the ZIP containers themselves were not retained locally; instead, all uncompressed benchmark logs (`*_qemu.log`) for all three runs are locally archived in-repo (`results/baseline`, `results/patched`, `results/reps/rep2`, `results/reps/rep3`), with their individual file contents independently verified byte-for-byte and recalculated.

## Decision

**REVISE.** The mechanism remains demonstrated, but three repetitions do not show a consistent latency benefit. Do not submit a performance-improvement claim. Retarget the predicate/statistic and test writer throttling versus depth hold, then repeat with randomized/alternating order and more runs on a KVM-capable host. Keep TCG results explicitly scoped to emulation.
