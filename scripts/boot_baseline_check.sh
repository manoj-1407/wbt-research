#!/bin/bash
set -uo pipefail
rm -rf /tmp/bboot
mkdir -p /tmp/bboot/bin /tmp/bboot/proc /tmp/bboot/sys /tmp/bboot/dev /tmp/bboot/tmp
cp /usr/bin/busybox /tmp/bboot/bin/busybox
for c in sh ls cat echo mount umount sleep poweroff grep zcat; do
    ln -sf busybox /tmp/bboot/bin/$c
done

cat > /tmp/bboot/init << 'INIT'
#!/bin/sh
export PATH=/bin
mount -t proc none /proc
mount -t sysfs none /sys
mount -t devtmpfs none /dev 2>/dev/null
mount -t tracefs none /sys/kernel/tracing 2>/dev/null
echo BOOT_OK_BASELINE
uname -a
zcat /proc/config.gz 2>/dev/null | grep CONFIG_BLK_WBT
if [ -d /sys/kernel/tracing/events/wbt ]; then
    echo WBT_TP_BASELINE_OK
    ls /sys/kernel/tracing/events/wbt/
else
    echo WBT_TP_BASELINE_MISSING
fi
echo BOOT_DONE_BASELINE
poweroff -f
INIT
chmod +x /tmp/bboot/init

(cd /tmp/bboot && find . | cpio -o -H newc 2>/dev/null | gzip -9 > /tmp/bboot.cpio.gz)

timeout 30s qemu-system-x86_64 \
    -enable-kvm -cpu host -m 512M \
    -kernel /home/manoj/kernel-src-baseline/arch/x86/boot/bzImage \
    -initrd /tmp/bboot.cpio.gz \
    -append "console=ttyS0 panic=1" \
    -nographic -no-reboot -serial mon:stdio \
    < /dev/null 2>&1
