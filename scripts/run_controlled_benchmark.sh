#!/bin/bash
# run_controlled_benchmark.sh
# Run 5 alternating baseline/v5 repetitions under identical KVM conditions.
# Alternating order: v5, baseline, baseline, v5, v5, baseline, baseline, v5, v5, baseline
# (to avoid systematic bias from first/last position)
#
# Each run: 1 buffered writer (4KB, 80ms sleep) + 6 O_DIRECT readers, 20s, wbt_lat_usec=150
# Fresh disk image per run. Identical initramfs binaries for both kernels.

set -uo pipefail

BASELINE_BZ="/home/manoj/kernel-src-baseline/arch/x86/boot/bzImage"
V5_BZ="/home/manoj/kernel-src-v5/arch/x86/boot/bzImage"
OUTDIR="/tmp/cbench_results"
REPO="/mnt/d/io_investigation/wbt_patch"
REPS=5

mkdir -p "$OUTDIR"

# Build benchmark binaries once (shared by all runs)
WORK="/tmp/cbench"
rm -rf "$WORK"
mkdir -p "$WORK/bin" "$WORK/proc" "$WORK/sys" "$WORK/dev" "$WORK/tmp" "$WORK/run" "$WORK/etc"

gcc -static -O2 -Wall "$REPO/benchmarks/read_latency_probe.c" -o "$WORK/bin/read_latency_probe"
gcc -static -O2 -Wall "$REPO/benchmarks/wbt_writer.c" -o "$WORK/bin/wbt_writer"

cp /usr/bin/busybox "$WORK/bin/busybox"
for cmd in sh ls cat echo mount umount sleep poweroff grep zcat dd printf date chmod mknod sync kill ps wc cut basename; do
    ln -sf busybox "$WORK/bin/$cmd"
done

# Init script — parameterized by __LABEL__ and __REP__
cat > "$WORK/init_template" << 'INIT'
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
chmod +x "$WORK/init_template"

run_one() {
    local bzimage="$1"
    local label="$2"
    local rep="$3"
    local outfile="$OUTDIR/${label}_rep${rep}.log"

    # Fresh disk image per run
    local disk="/tmp/cbench_disk_${label}_${rep}.img"
    dd if=/dev/zero of="$disk" bs=1M count=128 2>/dev/null
    dd if=/dev/urandom of="$disk" bs=1M seek=64 count=64 conv=notrunc 2>/dev/null

    # Create init with label/rep substituted
    sed "s/__LABEL__/$label/g; s/__REP__/$rep/g" "$WORK/init_template" > "$WORK/init"
    chmod +x "$WORK/init"
    (cd "$WORK" && find . | cpio -o -H newc 2>/dev/null | gzip -9 > "$WORK/initramfs.cpio.gz")

    echo ">>> Running $label rep $rep..."
    timeout 120s qemu-system-x86_64 \
        -enable-kvm -cpu host -m 1024 \
        -kernel "$bzimage" \
        -initrd "$WORK/initramfs.cpio.gz" \
        -drive "file=$disk,format=raw,if=none,id=hd0,cache=none,aio=threads" \
        -device virtio-scsi-pci,id=scsi0,num_queues=1 \
        -device scsi-hd,bus=scsi0.0,drive=hd0 \
        -append "console=ttyS0 panic=1" \
        -nographic -no-reboot -serial mon:stdio \
        < /dev/null > "$outfile" 2>&1 || true

    rm -f "$disk"

    echo ">>> $label rep $rep done. Key results:"
    grep -E "LABEL=|REP=|PROBE_RESULT|WRITER_RESULT|BLKSTAT_DELTA|WBT_TIMER|WBT_STEP|WBT_STAT|STATUS_DIST|END" "$outfile" | head -20
    echo "---"
}

# Alternating order: v5, baseline, baseline, v5, v5, baseline, baseline, v5, v5, baseline
# This avoids always starting with the same kernel
echo "=== CONTROLLED BENCHMARK: 5 reps x 2 kernels = 10 runs ==="
echo "Order: v5, baseline, baseline, v5, v5, baseline, baseline, v5, v5, baseline"
echo ""

for rep in 1 2 3 4 5; do
    if [ $rep -eq 1 ] || [ $rep -eq 4 ] || [ $rep -eq 5 ]; then
        run_one "$V5_BZ" "v5" "$rep"
        run_one "$BASELINE_BZ" "baseline" "$rep"
    else
        run_one "$BASELINE_BZ" "baseline" "$rep"
        run_one "$V5_BZ" "v5" "$rep"
    fi
done

echo "=== ALL RUNS COMPLETE ==="
echo "Output directory: $OUTDIR"
ls -lh "$OUTDIR"/*.log
