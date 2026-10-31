#!/bin/bash
# phase_e_workload.sh — Boot v5 candidate kernel with virtio-scsi disk,
# run sparse-write workload with concurrent O_DIRECT readers, capture
# WBT tracepoints to validate in-kernel behavior.
#
# This script must run in a single WSL session because /tmp doesn't persist.

set -uo pipefail

BZIMAGE="/home/manoj/kernel-src-v5/arch/x86/boot/bzImage"
REPO="/mnt/d/io_investigation/wbt_patch"
WORK="/tmp/phase_e"

rm -rf "$WORK"
mkdir -p "$WORK/initramfs/bin" "$WORK/initramfs/proc" "$WORK/initramfs/sys" \
         "$WORK/initramfs/dev" "$WORK/initramfs/tmp" "$WORK/initramfs/run" \
         "$WORK/initramfs/etc"

# --- Build benchmark binaries (static) ---
gcc -static -O2 -Wall "$REPO/benchmarks/read_latency_probe.c" \
    -o "$WORK/initramfs/bin/read_latency_probe"
gcc -static -O2 -Wall "$REPO/benchmarks/wbt_writer.c" \
    -o "$WORK/initramfs/bin/wbt_writer"
echo "Binaries built"

# --- BusyBox ---
cp /usr/bin/busybox "$WORK/initramfs/bin/busybox"
for cmd in sh ls cat echo mount umount sleep poweroff grep zcat dd \
           printf date chmod mknod sync kill ps wc cut basename; do
    ln -sf busybox "$WORK/initramfs/bin/$cmd"
done

# --- /init: sparse workload with WBT tracing ---
cat > "$WORK/initramfs/init" << 'INIT'
#!/bin/sh
export PATH=/bin

mount -t proc none /proc
mount -t sysfs none /sys
mount -t devtmpfs none /dev 2>/dev/null || true
mkdir -p /tmp /run
mount -t tmpfs none /tmp 2>/dev/null || true
mount -t tmpfs none /run 2>/dev/null || true
rm -f /run/stop_writer /tmp/stop_writer
mount -t tracefs none /sys/kernel/tracing 2>/dev/null || \
mount -t debugfs none /sys/kernel/debug 2>/dev/null || true

echo "=== [PHASE-E] KERNEL BOOTED ==="
uname -a
echo "--- CONFIG_BLK_WBT ---"
zcat /proc/config.gz 2>/dev/null | grep CONFIG_BLK_WBT || echo "(config.gz unavailable)"

# Find target block device
TARGET_DEV=""
for b in /sys/block/*; do
    bname=$(basename "$b")
    case "$bname" in
        loop*|ram*|sr*|dm-*|zram*) continue ;;
        *) TARGET_DEV="$bname"; break ;;
    esac
done

if [ -z "$TARGET_DEV" ]; then
    echo "FATAL: no usable block device found"
    poweroff -f
fi

echo "[+] Target disk: /dev/$TARGET_DEV"
echo "[i] nr_requests: $(cat /sys/block/$TARGET_DEV/queue/nr_requests 2>/dev/null)"
echo "[i] rotational: $(cat /sys/block/$TARGET_DEV/queue/rotational 2>/dev/null)"
echo "[i] queue_depth: $(cat /sys/block/$TARGET_DEV/queue/queue_depth 2>/dev/null || echo 'n/a')"

# Set WBT target latency (sparse profile: 150us)
WBT_NODE="/sys/block/$TARGET_DEV/queue/wbt_lat_usec"
if [ -f "$WBT_NODE" ]; then
    echo "150" > "$WBT_NODE" 2>/dev/null || true
    echo "[+] Active wbt_lat_usec: $(cat $WBT_NODE)"
else
    echo "FATAL: $WBT_NODE not present"
fi

# Check WBT tracepoints
if [ -d /sys/kernel/tracing/events/wbt ]; then
    echo "[+] WBT tracepoints present:"
    ls /sys/kernel/tracing/events/wbt/
else
    echo "FATAL: WBT tracepoints missing"
fi

# Arm tracing
echo 0 > /sys/kernel/tracing/tracing_on
echo "nop" > /sys/kernel/tracing/current_tracer 2>/dev/null || true
echo "" > /sys/kernel/tracing/trace
echo 16384 > /sys/kernel/tracing/buffer_size_kb 2>/dev/null || true
echo 1 > /sys/kernel/tracing/events/wbt/enable
echo 1 > /sys/kernel/tracing/tracing_on
echo "[+] WBT tracepoints armed"

# Block stat before
BLKSTAT_BEFORE=$(cat /sys/block/$TARGET_DEV/stat 2>/dev/null)
echo "[BLKSTAT] before: $BLKSTAT_BEFORE"

echo "--- Starting sparse workload (1 writer, 6 readers, 20s) ---"

# 1 buffered writer: 4KB chunks, 4MB window, 80ms sleep
/bin/wbt_writer "/dev/$TARGET_DEV" 20 0 4 4 80 &

# 6 O_DIRECT readers: random 4KB reads, paced at 1000us, 20s duration
RPIDS=""
p=0
while [ $p -lt 6 ]; do
    start=$((64 + p * 8))
    /bin/read_latency_probe "/dev/$TARGET_DEV" 20 "$start" 8 1000 &
    RPIDS="$RPIDS $!"
    p=$((p + 1))
done

for pid in $RPIDS; do
    wait "$pid" 2>/dev/null || true
done

touch /run/stop_writer
sleep 1
for p in $(jobs -p); do kill "$p" 2>/dev/null || true; done
wait 2>/dev/null || true
sleep 1

echo 0 > /sys/kernel/tracing/tracing_on

# Block stat after
BLKSTAT_AFTER=$(cat /sys/block/$TARGET_DEV/stat 2>/dev/null)
echo "[BLKSTAT] after : $BLKSTAT_AFTER"

set -- $BLKSTAT_BEFORE; RD_BEFORE=$3; WR_BEFORE=$7
set -- $BLKSTAT_AFTER;  RD_AFTER=$3;  WR_AFTER=$7
echo "[BLKSTAT-DELTA] read_sectors=$((RD_AFTER - RD_BEFORE)) write_sectors=$((WR_AFTER - WR_BEFORE))"

# Event counts
echo "=== [PHASE-E] EVENT COUNTS ==="
echo "wbt_timer : $(grep -c wbt_timer /sys/kernel/tracing/trace 2>/dev/null || echo 0)"
echo "wbt_step  : $(grep -c wbt_step  /sys/kernel/tracing/trace 2>/dev/null || echo 0)"
echo "wbt_stat  : $(grep -c wbt_stat  /sys/kernel/tracing/trace 2>/dev/null || echo 0)"
echo "wbt_lat   : $(grep -c wbt_lat   /sys/kernel/tracing/trace 2>/dev/null || echo 0)"

echo "=== [PHASE-E] TRACE BUFFER (first 200 lines) ==="
head -200 /sys/kernel/tracing/trace 2>/dev/null || cat /sys/kernel/tracing/trace 2>/dev/null

echo "=== [PHASE-E] STATUS DISTRIBUTION ==="
grep "wbt_timer:" /sys/kernel/tracing/trace 2>/dev/null | \
    sed 's/.*status=\([0-9]\).*/\1/' | sort | uniq -c | sort -rn

echo "=== [PHASE-E] DONE ==="
poweroff -f
INIT
chmod +x "$WORK/initramfs/init"

# Pack initramfs
(cd "$WORK/initramfs" && find . | cpio -o -H newc 2>/dev/null | gzip -9 > "$WORK/initramfs.cpio.gz")
echo "Initramfs: $(ls -lh $WORK/initramfs.cpio.gz)"

# Create fresh disk image: 128MB, upper 64MB filled with random data for reads
dd if=/dev/zero of="$WORK/disk.img" bs=1M count=128 2>/dev/null
dd if=/dev/urandom of="$WORK/disk.img" bs=1M seek=64 count=64 conv=notrunc 2>/dev/null
echo "Disk image: $(ls -lh $WORK/disk.img)"

# Boot with KVM + virtio-scsi
echo "=== Booting with KVM + virtio-scsi ==="
timeout 120s qemu-system-x86_64 \
    -enable-kvm \
    -cpu host \
    -m 1024 \
    -kernel "$BZIMAGE" \
    -initrd "$WORK/initramfs.cpio.gz" \
    -drive "file=$WORK/disk.img,format=raw,if=none,id=hd0,cache=none,aio=threads" \
    -device virtio-scsi-pci,id=scsi0,num_queues=1 \
    -device scsi-hd,bus=scsi0.0,drive=hd0 \
    -append "console=ttyS0 panic=1" \
    -nographic \
    -no-reboot \
    -serial mon:stdio \
    < /dev/null 2>&1

echo ""
echo "PHASE_E_EXIT:$?"
