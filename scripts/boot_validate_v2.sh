#!/bin/bash
set -uo pipefail

BZIMAGE="/home/manoj/kernel-src-v5/arch/x86/boot/bzImage"
WORK="/tmp/wbt_v5_boot"

rm -rf "$WORK"
mkdir -p "$WORK"/bin "$WORK"/proc "$WORK"/sys "$WORK"/dev "$WORK"/tmp

cp /usr/bin/busybox "$WORK/bin/busybox"
for c in sh ls cat echo mount umount sleep poweroff grep zcat; do
    ln -sf busybox "$WORK/bin/$c"
done

cat > "$WORK/init" << 'INIT'
#!/bin/sh
export PATH=/bin
mount -t proc none /proc
mount -t sysfs none /sys
mount -t devtmpfs none /dev 2>/dev/null || true
mount -t tracefs none /sys/kernel/tracing 2>/dev/null || true
mount -t debugfs none /sys/kernel/debug 2>/dev/null || true
echo "BOOT_OK"
uname -a
echo "WBT_CONFIG_CHECK"
zcat /proc/config.gz 2>/dev/null | grep -E "CONFIG_BLK_WBT" || echo "CONFIG_GZ_UNAVAILABLE"
echo "WBT_TRACEPOINT_CHECK"
if [ -d /sys/kernel/tracing/events/wbt ]; then
    echo "WBT_TP_EXISTS"
    ls /sys/kernel/tracing/events/wbt/
else
    echo "WBT_TP_MISSING"
fi
echo "BLOCK_DEVICES"
ls /sys/block/ 2>/dev/null || echo "none"
echo "BOOT_DONE"
poweroff -f
INIT
chmod +x "$WORK/init"

(cd "$WORK" && find . | cpio -o -H newc 2>/dev/null | gzip -9 > "$WORK/../wbt_initrd.cpio.gz")

echo "INITRAMFS_BUILT"
ls -la "$WORK/../wbt_initrd.cpio.gz"

# Boot with KVM
timeout 30s qemu-system-x86_64 \
    -enable-kvm \
    -cpu host \
    -m 512M \
    -kernel "$BZIMAGE" \
    -initrd "$WORK/../wbt_initrd.cpio.gz" \
    -append "console=ttyS0 panic=1" \
    -nographic \
    -no-reboot \
    -serial mon:stdio \
    < /dev/null 2>&1

echo ""
echo "BOOT_SCRIPT_EXIT:$?"
