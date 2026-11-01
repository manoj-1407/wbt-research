#!/bin/bash
# controlled_benchmark.sh — Run 5+ alternating baseline/v5 repetitions
# under identical KVM + virtio-scsi conditions.
#
# Usage: bash controlled_benchmark.sh <baseline_bzimage> <v5_bzimage> <output_dir> [repetitions]
# Default repetitions: 5
#
# Run order: v5, baseline, v5, baseline, v5, baseline, v5, baseline, v5, baseline
# (alternating, starting with v5 to avoid systematic bias)
#
# Each run: 1 buffered writer (4KB, 80ms sleep) + 6 O_DIRECT readers, 20s, wbt_lat_usec=150
# Fresh disk image per kernel per run (to avoid cache effects)

set -uo pipefail

BASELINE_BZ="$1"
V5_BZ="$2"
OUTDIR="$3"
REPS="${4:-5}"

mkdir -p "$OUTDIR"

REPO="/mnt/d/io_investigation/wbt_patch"

# Build benchmark binaries once
WORK="/tmp/cbench"
rm -rf "$WORK"
mkdir -p "$WORK/bin" "$WORK/proc" "$WORK/sys" "$WORK/dev" "$WORK/tmp" "$WORK/run" "$WORK/etc"

gcc -static -O2 -Wall "$REPO/benchmarks/read_latency_probe.c" -o "$WORK/bin/read_latency_probe"
gcc -static -O2 -Wall "$REPO/benchmarks/wbt_writer.c" -o "$WORK/bin/wbt_writer"

cp /usr/bin/busybox "$WORK/bin/busybox"
for cmd in sh ls cat echo mount umount sleep poweroff grep zcat dd printf date chmod mknod sync kill ps wc cut basename; do
    ln -sf busybox "$WORK/bin/$cmd"
done

# Init script template — parameterized by kernel label
cat > "$WORK/init" << 'INIT'
#!/bin/sh
export PATH=/bin
mount -t proc none /proc
mount -t sysfs none /sys
mount -t devtmpfs none /dev 2>/dev/null || true
mkdir -p /tmp /run
mount -t tmpfs none /tmp 2>/dev/null || true
mount -t tmpfs none /run 2>/dev/null || true
rm -f /run/stop_writer /tmp/stop_writer
mount -t tracefs none /sys/kernel/tracing 2>/dev/null || mount -t debugfs none /sys/kernel/debug 2>/dev/null || true

TARGET_DEV=""
for b in /sys/block/*; do
    bname=$(basename "$b")
    case "$bname" in
        loop*|ram*|sr*|dm-*|zram*) continue ;;
        *) TARGET_DEV="$bname"; break ;;
    esac
done
if [ -z "$TARGET_DEV" ]; then echo "FATAL: no block device"; poweroff -f; fi

echo "LABEL=__LABEL__"
echo "REP=__REP__"
echo "DEV=$TARGET_DEV"
echo "NRQ=$(cat /sys/block/$TARGET_DEV/queue/nr_requests 2>/dev/null)"
echo "ROT=$(cat /sys/block/$TARGET_DEV/queue/rotational 2>/dev/null)"

echo "150" > /sys/block/$TARGET_DEV/queue/wbt_lat_usec 2>/dev/null || true
echo "WBT_LAT=$(cat /sys/block/$TARGET_DEV/queue/wbt_lat_usec 2>/dev/null)"

echo 0 > /sys/kernel/tracing/tracing_on
echo "nop" > /sys/kernel/tracing/current_tracer 2>/dev/null || true
echo "" > /sys/kernel/tracing/trace
echo 16384 > /sys/kernel/tracing/buffer_size_kb 2>/dev/null || true
echo 1 > /sys/kernel/tracing/events/wbt/enable
echo 1 > /sys/kernel/tracing/tracing_on

BLKSTAT_BEFORE=$(cat /sys/block/$TARGET_DEV/stat 2>/dev/null)

# 1 buffered writer: 4KB chunks, 4MB window, 80ms sleep, 20s
/bin/wbt_writer "/dev/$TARGET_DEV" 20 0 4 4 80 &

# 6 O_DIRECT readers: random 4KB, paced 1000us, 20s
RPIDS=""
p=0
while [ $p -lt 6 ]; do
    start=$((64 + p * 8))
    /bin/read_latency_probe "/dev/$TARGET_DEV" 20 "$start" 8 1000 &
    RPIDS="$RPIDS $!"
    p=$((p + 1))
done

for pid in $RPIDS; do wait "$pid" 2>/dev/null || true; done
touch /run/stop_writer
sleep 1
for p in $(jobs -p); do kill "$p" 2>/dev/null || true; done
wait 2>/dev/null || true
sleep 1

echo 0 > /sys/kernel/tracing/tracing_on

BLKSTAT_AFTER=$(cat /sys/block/$TARGET_DEV/stat 2>/dev/null)
set -- $BLKSTAT_BEFORE; RD_B=$3; WR_B=$7
set -- $BLKSTAT_AFTER;  RD_A=$3;  WR_A=$7
echo "BLKSTAT_DELTA read=$((RD_A - RD_B)) write=$((WR_A - WR_B))"

echo "WBT_TIMER=$(grep -c wbt_timer /sys/kernel/tracing/trace 2>/dev/null || echo 0)"
echo "WBT_STEP=$(grep -c wbt_step /sys/kernel/tracing/trace 2>/dev/null || echo 0)"
echo "WBT_STAT=$(grep -c wbt_stat /sys/kernel/tracing/trace 2>/dev/null || echo 0)"

echo "=== TRACE ==="
cat /sys/kernel/tracing/trace 2>/dev/null
echo "=== STATUS_DIST ==="
grep "wbt_timer:" /sys/kernel/tracing/trace 2>/dev/null | sed 's/.*status=\([0-9]\).*/\1/' | sort | uniq -c | sort -rn
echo "=== STEP_EVENTS ==="
grep "wbt_step:" /sys/kernel/tracing/trace 2>/dev/null
echo "=== END ==="
poweroff -f
INIT
chmod +x "$WORK/init"

# Pack initramfs
(cd "$WORK" && find . | cpio -o -H newc 2>/dev/null | gzip -9 > "$WORK/initramfs.cpio.gz")

run_one() {
    local bzimage="$1"
    local label="$2"
    local rep="$3"
    local outfile="$OUTDIR/${label}_rep${rep}.log"

    # Fresh disk image per run
    local disk="/tmp/cbench_disk_${label}_${rep}.img"
    dd if=/dev/zero of="$disk" bs=1M count=128 2>/dev/null
    dd if=/dev/urandom of="$disk" bs=1M seek=64 count=64 conv=notrunc 2>/dev/null

    # Substitute label and rep into init
    sed -i "s/__LABEL__/$label/g; s/__REP__/$rep/g" "$WORK/init"
    (cd "$WORK" && find . | cpio -o -H newc 2>/dev/null | gzip -9 > "$WORK/initramfs_${label}_${rep}.cpio.gz")

    echo ">>> Running $label rep $rep..."
    timeout 120s qemu-system-x86_64 \
        -enable-kvm -cpu host -m 1024 \
        -kernel "$bzimage" \
        -initrd "$WORK/initramfs_${label}_${rep}.cpio.gz" \
        -drive "file=$disk,format=raw,if=none,id=hd0,cache=none,aio=threads" \
        -device virtio-scsi-pci,id=scsi0,num_queues=1 \
        -device scsi-hd,bus=scsi0.0,drive=hd0 \
        -append "console=ttyS0 panic=1" \
        -nographic -no-reboot -serial mon:stdio \
        < /dev/null > "$outfile" 2>&1 || true

    # Reset init for next run
    sed -i "s/$label/__LABEL__/g; s/$rep/__REP__/g" "$WORK/init"

    # Clean up disk
    rm -f "$disk"

    echo ">>> $label rep $rep done. Output: $outfile"
    grep -E "LABEL=|REP=|DEV=|WBT_LAT=|PROBE_RESULT|WRITER_RESULT|BLKSTAT_DELTA|WBT_TIMER|WBT_STEP|WBT_STAT|STATUS_DIST|END" "$outfile" | head -30
    echo "---"
}

# Alternating order: v5, baseline, v5, baseline, ...
for rep in $(seq 1 $REPS); do
    if [ $((rep % 2)) -eq 1 ]; then
        run_one "$V5_BZ" "v5" "$rep"
        run_one "$BASELINE_BZ" "baseline" "$rep"
    else
        run_one "$BASELINE_BZ" "baseline" "$rep"
        run_one "$V5_BZ" "v5" "$rep"
    fi
done

echo "=== ALL RUNS COMPLETE ==="
echo "Output directory: $OUTDIR"
ls -la "$OUTDIR"/*.log
