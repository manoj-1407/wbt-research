# Methodology

## 1. Build provenance

| Item | Value |
| :--- | :--- |
| Build workflow | `.github/workflows/build-wbt-kernels.yml` |
| Build run ID | `39987714631` |
| Kernel tree | Microsoft WSL2 Linux 6.6.y |
| Pinned commit | `a07f9ea8a99139913acbcc1c160b132cb2a49c81` |
| Baseline `bzImage` SHA-256 | `553EBD52A14829D103D41C55B56ADBC3A56768DAFFAEEB0E55007C7701FB002D` |
| Patched `bzImage` SHA-256 | `CB926FD7F526B8B081C4B6CEAD8EE4DF30C97E6E83CA513667A70F2B6F0EF46E` |
| Baseline `vmlinux` SHA-256 | `7E4233B50E3B360D684658A3F4EA434DBC6A756CA6D8DC2BA5BD9007AD9F4AA6` |
| Patched `vmlinux` SHA-256 | `4BF1ADDDFCE96D95EAFFA4B30B2FD11CB421F5501D8B6C23AFDC722E7F18F035` |
| `bzImage` size | 16 007 168 bytes (both) |

Hashes were recomputed locally from the downloaded artifacts, not copied
from CI output.

**Symbol gate (independently verified on the binaries).** Raw substring
counts over the two `vmlinux` files:

| Symbol string | baseline | patched |
| :--- | ---: | ---: |
| `wbt_guard` | 0 | 47 |
| `wbt_timer` | 47 | 47 |
| `guard_fired` | 0 | 6 |

The baseline kernel contains no `wbt_guard` string at all, so a guard event
can never be produced by it; the patched kernel contains the tracepoint.
`wbt_timer` is present in both, confirming both kernels have WBT
instrumentation and that the difference is attributable to the patch only.

The applied diff shipped with the artifact
(`results/build/wbt-patch-applied.diff`) matches
`0001-wbt-hold-depth-on-LAT_UNKNOWN_WRITES-when-reads-are-elevated.patch`
in content (hunk offsets only).

## 2. Runtime harness

`.github/workflows/qemu-wbt-smoke-test.yml` (v3), `ubuntu-22.04`,
`qemu-system-x86_64`, `-m 1024`, BusyBox static micro-initramfs.

The guest:

* prints `CONFIG_BLK_WBT` / `CONFIG_BLK_WBT_MQ` from `/proc/config.gz`;
* resolves the first non-loop block device (`/dev/sda` here) and prints
  `queue/nr_requests`, `queue/rotational`;
* writes the target latency into `queue/wbt_lat_usec`;
* arms `/sys/kernel/tracing/events/wbt/enable` with a 16 MiB trace buffer;
* records `/sys/block/<dev>/stat` before and after the workload.

### Workload split (why writes and reads differ)

* **Writers are buffered** (`benchmarks/wbt_writer.c`: `open(O_WRONLY)`,
  `pwrite()`, `fsync()`). O_DIRECT writes carry
  `REQ_SYNC|REQ_IDLE`, and `wbt_should_throttle()` returns `false` for them
  ("Don't throttle WRITE_ODIRECT"). Such bios never become `WBT_TRACKED`,
  so `wbt_wait()` is never called, and `rwb_arm_timer()` — which is called
  **only** from `wbt_wait()` — never runs. An all-O_DIRECT workload
  therefore leaves `wbt_timer == 0`. This was the cause of the earlier
  all-zero run `39996857389`.
* **Readers are O_DIRECT** (`benchmarks/read_latency_probe.c`). WBT never
  throttles reads; `bio_to_wbt_flags()` tags them `WBT_READ`, and they are
  merely accounted into `cb->stat[READ]` — which is exactly the guard's
  input. Buffered reads would be answered from the page cache and never
  reach the block layer.

### 2×2×2 matrix

One fresh disk image per kernel (upper 64 MiB `urandom`, writers confined to
the first 32 MiB so the read region is byte-identical), one shared
initramfs per load profile:

| Axis | Values |
| :--- | :--- |
| device | `if=ide` (QD == 1) · `virtio-scsi-pci,num_queues=1` (deep queue) |
| kernel | baseline · patched |
| profile | `heavy` · `sparse` |

| Profile | writers | chunk | window | stride | writer sleep | reader procs | reader pace | `wbt_lat_usec` |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `heavy` | 4 | 64 KiB | 8 MiB | 8 MiB | 0 ms | 2 | 1000 µs | 1000 |
| `sparse` | 1 | 4 KiB | 4 MiB | 4 MiB | 80 ms | 6 | 1000 µs | 150 |

Reader duration 30 s. Readers are pinned to a random 4 KiB block inside
their own 8 MiB region starting at 64 MiB, and are seeded per process
(`srand(getpid() ^ time(NULL))`) so concurrent readers do not walk an
identical block sequence in lockstep.

**Why `sparse` exists.** Only a read-dominated profile with fewer than three
write completions per 100 ms window can reach `LAT_UNKNOWN_WRITES` — the
sole branch the patch touches. `heavy` is retained as a control that should
*not* reach it.

**Why `ide` exists.** With `queue_depth == 1`,
`rq_depth_calc_max_depth()` takes a special case returning `max_depth = 2`
for any `scale_step <= 0`. The depth lever is degenerate, so `ide` is a
deliberate inertness control: if the guard changed anything measurable
there, it would have to be doing so through something other than write
depth.

### Validity gates (fail-closed)

* Patched artifact must contain the `wbt_guard` symbol; the job aborts
  otherwise, so a stale pre-guard kernel can never be benchmarked.
* `[BLKSTAT-DELTA]` is computed from `/sys/block/<dev>/stat`; a zero
  read-sectors or write-sectors delta prints `[!] WARNING` and the offline
  analyser reports `INVALID RUN`.
* Writers report their own open mode; the analyser flags any writer that is
  not `BUFFERED`.
* Event counts for all five tracepoints are echoed so that "no events" is
  distinguishable from "no WBT activity to report".

## 3. Measurement

`read_latency_probe.c` times each `pread()` with `CLOCK_MONOTONIC`,
accumulating in **signed** nanoseconds (an earlier unsigned per-field
`timespec` delta wrapped and produced ~1.8e16 µs maxima; fixed in commit
`9eac44f`), keeps up to 200 000 samples and prints
`SAMPLES / MIN / P50 / P90 / P99 / MAX / MEAN` in microseconds. One line per
reader process.

`wbt_writer.c` prints `[WRITER_RESULT] OPS BYTES ITERS ERR FSYNC_ERR`.

`ftrace_wbt_tracer.py analyze <log>` correlates `wbt_timer` (status),
`wbt_step` (depth transitions with `background/normal/max`), `wbt_stat`,
`wbt_lat` and `wbt_guard` on a common timeline.

## 4. Analysis rules adopted

* Raw traces, per-config analyser output, build logs, the applied diff and
  artifact hashes are all preserved under `results/`.
* A single-run P99 improvement is **not** treated as evidence.
* Mechanism evidence (`wbt_guard` firing, step timeline shifting) is
  reported separately from outcome evidence (latency, throughput), so the
  two cannot be conflated.
* Any difference observed in a configuration where the guard did **not**
  hold depth is attributed to run-to-run variation, not to the patch.
