#!/usr/bin/env python3
"""
Applies the WBT LAT_UNKNOWN_WRITES guard to block/blk-wbt.c.
"""

import sys
import os

target_file = "block/blk-wbt.c"
if len(sys.argv) > 1:
    target_file = sys.argv[1]

if not os.path.exists(target_file):
    print(f"ERROR: Target file not found: {target_file}")
    sys.exit(1)

with open(target_file, "r") as f:
    content = f.read()

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
\t\t * Exception: if reads are being sampled in this window
\t\t * and their minimum observed latency already exceeds the
\t\t * configured target (rwb->min_lat_nsec), scaling write
\t\t * depth upward is hypothesized to worsen read latency.
\t\t * In that case, scale down conservatively instead of up.
\t\t */
\t\tif (cb->stat[READ].nr_samples &&
\t\t    cb->stat[READ].min > rwb->min_lat_nsec) {
\t\t\tscale_down(rwb, false);
\t\t\tbreak;
\t\t}
\t\tscale_up(rwb);
\t\tbreak;"""

if ORIGINAL not in content:
    print(f"ERROR: Target block not found in {target_file}")
    idx = content.find("LAT_UNKNOWN_WRITES")
    if idx != -1:
        print("Found LAT_UNKNOWN_WRITES context:")
        print(repr(content[idx:idx+400]))
    sys.exit(1)

patched = content.replace(ORIGINAL, REPLACEMENT, 1)

with open(target_file, "w") as f:
    f.write(patched)

print(f"SUCCESS: Patch applied successfully to {target_file}")
