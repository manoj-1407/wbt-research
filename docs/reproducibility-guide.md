# Linux WBT Research — Reproducibility Guide

**Date:** 2026-11-02
**Target Repository:** `D:\io_investigation\wbt_patch` (`/mnt/d/io_investigation/wbt_patch`)
**Tested Operating System:** Ubuntu 24.04 LTS under WSL2 (Windows 11 host)
**Target Architecture:** x86_64

This guide provides verified, self-contained instructions to reproduce the Linux Writeback Throttling (WBT) baseline-versus-v5 evaluation, rebuild the kernels, execute the QEMU/KVM harness, and independently audit the resulting trace and latency data.

---

## 1. System & Software Prerequisites

The benchmark and analysis infrastructure requires:
- **Hypervisor:** QEMU emulator version 8.2.2 (`qemu-system-x86_64`) with KVM acceleration enabled (`/dev/kvm`).
- **Compiler:** GCC 13.3.0 or compatible (`gcc -static -O2`).
- **Host Tools:** `busybox`, `cpio`, `gzip`, `dd`, `make`, `bc`, `bison`, `flex`, `libelf-dev`, `libssl-dev`.
- **Analysis Runtime:** Python 3.10+ (standard library only; no external pip dependencies required).

Verify KVM availability:
```bash
kvm-ok
# Expected: INFO: /dev/kvm exists
# KVM acceleration can be used
```

---

## 2. Kernel Source Repositories & Worktrees

The research evaluation compares two kernel configurations built from the exact same Microsoft WSL2 6.6.y Linux kernel tree:

- **Upstream Source Base:**
  - Remote: `https://github.com/microsoft/WSL2-Linux-Kernel.git`
  - Base Commit: `a07f9ea8a99139913acbcc1c160b132cb2a49c81` (`WSL Kernel 6.6.123.2 (#256)`)

- **Main Worktree (Baseline):**
  - Path: `/home/manoj/kernel-src-baseline`
  - Commit: `a07f9ea8a99139913acbcc1c160b132cb2a49c81`
  - Kernel Config: Base WSL2 config with `CONFIG_BLK_WBT=y`, `CONFIG_BLK_WBT_MQ=y`.

- **Linked Worktree (v5 Candidate Patch):**
  - Path: `/home/manoj/kernel-src-v5`
  - Branch: `candidate-v5-reclassification`
  - Commit: `ebd45314d7e26ddb71110bb4702c8d82925de9b8`
  - Patch diff in `block/blk-wbt.c`:
    ```diff
    --- a/block/blk-wbt.c
    +++ b/block/blk-wbt.c
    @@ -333,8 +333,9 @@ static int latency_exceeded(struct rq_wb *rwb, struct blk_rq_stat *stat)
            * waited or still has writes in flights, consider us doing
            * just writes as well.
            */
    -       if (stat[WRITE].nr_samples || wb_recent_wait(rwb) ||
    -           wbt_inflight(rwb))
    +       if (!stat[READ].nr_samples &&
    +           (stat[WRITE].nr_samples || wb_recent_wait(rwb) ||
    +            wbt_inflight(rwb)))
                    return LAT_UNKNOWN_WRITES;
            return LAT_UNKNOWN;
     }
    ```

To compile both kernels:
```bash
# In baseline worktree
cd /home/manoj/kernel-src-baseline
make -j$(nproc) bzImage

# In v5 worktree
cd /home/manoj/kernel-src-v5
make -j$(nproc) bzImage
```

---

## 3. Workload Binaries & Benchmark Harness

The benchmark evaluates concurrent reader/writer I/O using two statically compiled C binaries:

1. **`read_latency_probe.c` (`benchmarks/read_latency_probe.c`):**
   Issues random 4 KB direct-I/O reads (`pread` with `O_DIRECT`) to dedicated disk regions, spaced by 1000 µs pacing. Records high-resolution elapsed time (`CLOCK_MONOTONIC`), sorts observations with `qsort()`, and logs sample count, min, P50, P90, P99, max, and mean latency.
2. **`wbt_writer.c` (`benchmarks/wbt_writer.c`):**
   Issues 4 KB buffered writes (`pwrite`) to an isolated 4 MB region followed immediately by `fsync()`, sleeping 80 ms between iterations to create sparse writeback activity.

### Compilation
The runner script automatically compiles these statically into a clean initramfs:
```bash
gcc -static -O2 -Wall benchmarks/read_latency_probe.c -o /tmp/read_latency_probe
gcc -static -O2 -Wall benchmarks/wbt_writer.c -o /tmp/wbt_writer
```

---

## 4. Executing the Controlled Benchmark

The controlled comparison runs **ten total virtual machine sessions** alternating between baseline and v5:
- **Execution sequence:** `v5_1`, `baseline_1`, `baseline_2`, `v5_2`, `baseline_3`, `v5_3`, `v5_4`, `baseline_4`, `v5_5`, `baseline_5`.
- **Per-run environment:** Fresh 128 MB disk image generated before each session (upper 64 MB initialized with random bytes for direct readers; lower 64 MB zeroed for the buffered writer).
- **QEMU invocation parameters:**
  ```bash
  qemu-system-x86_64 \
      -enable-kvm -cpu host -m 1024 \
      -kernel "$bzimage" \
      -initrd "$initramfs" \
      -drive "file=$disk,format=raw,if=none,id=hd0,cache=none,aio=threads" \
      -device virtio-scsi-pci,id=scsi0,num_queues=1 \
      -device scsi-hd,bus=scsi0.0,drive=hd0 \
      -append "console=ttyS0 panic=1" \
      -nographic -no-reboot -serial mon:stdio
  ```

To execute the complete 10-run suite:
```bash
cd /mnt/d/io_investigation/wbt_patch
bash scripts/run_controlled_benchmark.sh
```
Logs are emitted to `results/controlled_benchmark/` (`baseline_rep1.log` through `baseline_rep5.log` and `v5_rep1.log` through `v5_rep5.log`).

---

## 5. Independent Benchmark Audit & Analysis

To verify raw log integrity, recalculate paired deltas, and compute statistics independently:

```bash
cd /mnt/d/io_investigation/wbt_patch
python3 scripts/independent_benchmark_audit.py results/controlled_benchmark results/independent_audit
```

This clean-room script:
1. Re-computes SHA-256 hashes of all 10 logs.
2. Asserts zero writer errors, zero fsync errors, and exactly 6 reader probes per run.
3. Computes arithmetic means of reader percentiles (P50, P90, P99).
4. Generates paired deltas ($\Delta = \text{v5} - \text{baseline}$).
5. Evaluates two-tailed and one-tailed sign tests, Student's t 95% confidence intervals, and Cohen's $d$ effect sizes.
6. Emits `results/independent_audit/independent_audit_summary.json`.

---

## 6. Causation & Timeline Analysis

To inspect reader lifecycle duration versus write-depth escalation and confirm the absence of post-reader burst overlap:

```bash
cd /mnt/d/io_investigation/wbt_patch
python3 scripts/causation_analysis.py results/controlled_benchmark
```

Expected confirmation:
- In all 5 v5 runs, readers finish at $t \le 20.0\text{s}$.
- Delayed scale-up begins at $t \approx 23.3\text{s} - 23.5\text{s}$ ($\approx 3.5\text{s}$ gap, with `inflight = 0`).
