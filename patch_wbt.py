#!/usr/bin/env python3
"""
Applies the WBT LAT_UNKNOWN_WRITES guard and dedicated wbt_guard tracepoint
to the Linux kernel tree:
  1. include/trace/events/wbt.h — Adds TRACE_EVENT(wbt_guard, ...)
  2. block/blk-wbt.c           — Adds guard logic (hold depth on elevated reads)
"""

import sys
import os

def patch_wbt_header(header_file: str):
    if not os.path.exists(header_file):
        print(f"ERROR: Header file not found: {header_file}")
        sys.exit(1)

    with open(header_file, "r") as f:
        content = f.read()

    if "TRACE_EVENT(wbt_guard," in content:
        print(f"NOTICE: wbt_guard tracepoint already present in {header_file}")
        return

    TARGET = "#endif /* _TRACE_WBT_H */"
    if TARGET not in content:
        print(f"ERROR: Target '{TARGET}' not found in {header_file}")
        sys.exit(1)

    GUARD_TRACEPOINT = """/**
 * wbt_guard - trace guard evaluation on LAT_UNKNOWN_WRITES
 * @bdi: backing dev info
 * @step: current scale step before decision
 * @r_samples: number of read samples in window
 * @r_min: minimum read latency in window (nanoseconds)
 * @target: target latency (nanoseconds)
 * @guard_fired: whether guard condition was met (1=true, 0=false)
 * @action: action taken ("hold", "scale_up")
 */
TRACE_EVENT(wbt_guard,

\tTP_PROTO(struct backing_dev_info *bdi, int step,
\t\t unsigned long long r_samples, unsigned long long r_min,
\t\t unsigned long long target, int guard_fired, const char *action),

\tTP_ARGS(bdi, step, r_samples, r_min, target, guard_fired, action),

\tTP_STRUCT__entry(
\t\t__array(char, name, 32)
\t\t__field(int, step)
\t\t__field(unsigned long long, r_samples)
\t\t__field(unsigned long long, r_min_us)
\t\t__field(unsigned long long, target_us)
\t\t__field(int, guard_fired)
\t\t__string(action, action)
\t),

\tTP_fast_assign(
\t\tstrscpy(__entry->name, bdi_dev_name(bdi),
\t\t\tARRAY_SIZE(__entry->name));
\t\t__entry->step\t\t= step;
\t\t__entry->r_samples\t= r_samples;
\t\t__entry->r_min_us\t= div_u64(r_min, 1000);
\t\t__entry->target_us\t= div_u64(target, 1000);
\t\t__entry->guard_fired\t= guard_fired;
\t\t__assign_str(action, action);
\t),

\tTP_printk("%s: step=%d, r_samples=%llu, r_min=%lluus, target=%lluus, guard_fired=%d, action=%s",
\t\t  __entry->name, __entry->step, __entry->r_samples,
\t\t  __entry->r_min_us, __entry->target_us,
\t\t  __entry->guard_fired, __get_str(action))
);

#endif /* _TRACE_WBT_H */"""

    patched = content.replace(TARGET, GUARD_TRACEPOINT, 1)
    with open(header_file, "w") as f:
        f.write(patched)
    print(f"SUCCESS: wbt_guard tracepoint added to {header_file}")


def patch_wbt_source(source_file: str):
    if not os.path.exists(source_file):
        print(f"ERROR: Source file not found: {source_file}")
        sys.exit(1)

    with open(source_file, "r") as f:
        content = f.read()

    if "trace_wbt_guard" in content:
        print(f"NOTICE: trace_wbt_guard already present in {source_file}")
        return

    ORIGINAL = """\tcase LAT_UNKNOWN_WRITES:
\t\t/*
\t\t * We started a the center step, but don't have a valid
\t\t * read/write sample, but we do have writes going on.
\t\t * Allow step to go negative, to increase write perf.
\t\t */
\t\tscale_up(rwb);
\t\tbreak;"""

    REPLACEMENT = """\tcase LAT_UNKNOWN_WRITES:
\t\t/*
\t\t * We started a the center step, but don't have a valid
\t\t * read/write sample, but we do have writes going on.
\t\t * Allow step to go negative, to increase write perf.
\t\t *
\t\t * Guard: if concurrent reads are sampled in this window
\t\t * and their minimum observed latency already exceeds the
\t\t * configured target (rwb->min_lat_nsec), do not scale up
\t\t * write depth. Hold depth conservatively.
\t\t */
\t\tif (cb->stat[READ].nr_samples &&
\t\t    cb->stat[READ].min > rwb->min_lat_nsec) {
\t\t\ttrace_wbt_guard(rwb->rqos.disk->bdi, rqd->scale_step,
\t\t\t\t\tcb->stat[READ].nr_samples, cb->stat[READ].min,
\t\t\t\t\trwb->min_lat_nsec, 1, "hold");
\t\t\tbreak;
\t\t}

\t\ttrace_wbt_guard(rwb->rqos.disk->bdi, rqd->scale_step,
\t\t\t\tcb->stat[READ].nr_samples, cb->stat[READ].min,
\t\t\t\trwb->min_lat_nsec, 0, "scale_up");
\t\tscale_up(rwb);
\t\tbreak;"""

    if ORIGINAL not in content:
        print(f"ERROR: Target block not found in {source_file}")
        sys.exit(1)

    patched = content.replace(ORIGINAL, REPLACEMENT, 1)
    with open(source_file, "w") as f:
        f.write(patched)
    print(f"SUCCESS: WBT hold-depth guard and tracepoint call added to {source_file}")


def main():
    root = "."
    if len(sys.argv) > 1:
        root = sys.argv[1]

    # Handle if single file was passed
    if root.endswith("blk-wbt.c"):
        kernel_root = os.path.dirname(os.path.dirname(os.path.abspath(root)))
        header_file = os.path.join(kernel_root, "include", "trace", "events", "wbt.h")
        patch_wbt_header(header_file)
        patch_wbt_source(root)
    else:
        header_file = os.path.join(root, "include", "trace", "events", "wbt.h")
        source_file = os.path.join(root, "block", "blk-wbt.c")
        patch_wbt_header(header_file)
        patch_wbt_source(source_file)


if __name__ == "__main__":
    main()
