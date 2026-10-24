#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <time.h>
#include <errno.h>
#include <sys/types.h>

#define ALIGN 4096

static uint64_t now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

/*
 * Buffered writeback generator for the WBT harness.
 *
 * WHY BUFFERED AND NOT O_DIRECT
 * -----------------------------
 * blk-wbt.c only tracks I/O that it would throttle:
 *
 *   bio_to_wbt_flags():  WBT_TRACKED is set only if wbt_should_throttle(bio)
 *   wbt_should_throttle(): for REQ_OP_WRITE it returns false when
 *                          (bi_opf & (REQ_SYNC|REQ_IDLE)) == (REQ_SYNC|REQ_IDLE)
 *                          -- "Don't throttle WRITE_ODIRECT"
 *
 * O_DIRECT writes carry exactly that REQ_SYNC|REQ_IDLE combination, so they
 * are exempt, never become WBT_TRACKED, and therefore never call
 * rq_qos_wait()->wbt_wait(), which is the *only* place rwb_arm_timer() is
 * invoked. A workload made of O_DIRECT writes leaves the entire WBT state
 * machine dormant: wbt_timer/wbt_step/wbt_stat/wbt_lat/wbt_guard all stay at
 * zero no matter what the depth limits are.
 *
 * This writer therefore issues page-cache writes to the block device and
 * forces writeback with fsync(), which submits REQ_OP_WRITE bios without
 * REQ_IDLE. Those are throttled and tracked by WBT, so the window timer runs.
 */
int main(int argc, char *argv[])
{
    if (argc < 7) {
        fprintf(stderr, "Usage: %s <device> <duration_sec> <offset_mb> <chunk_kb> <window_mb> <sleep_ms>\n", argv[0]);
        fprintf(stderr, "Example: %s /dev/sda 30 0 64 8 0\n", argv[0]);
        return 2;
    }

    const char *dev = argv[1];
    int duration = atoi(argv[2]);
    uint64_t offset0 = (uint64_t)atoi(argv[3]) * 1024ULL * 1024ULL;
    size_t chunk = (size_t)atoi(argv[4]) * 1024ULL;
    uint64_t window = (uint64_t)atoi(argv[5]) * 1024ULL * 1024ULL;
    int sleep_ms = atoi(argv[6]);

    if (chunk < ALIGN)
        chunk = ALIGN;
    chunk = (chunk + (ALIGN - 1)) & ~((size_t)ALIGN - 1);
    if (window < chunk)
        window = chunk;

    void *buf = NULL;
    if (posix_memalign(&buf, ALIGN, chunk) != 0) {
        perror("posix_memalign");
        return 1;
    }
    memset(buf, 0x5a, chunk);

    int fd = open(dev, O_WRONLY);
    if (fd < 0) {
        fprintf(stderr, "[WRITER] FATAL open(%s): %s\n", dev, strerror(errno));
        return 1;
    }

    printf("[WRITER] dev=%s mode=BUFFERED chunk=%zuB offset=%lluMB window=%lluMB sleep=%dms duration=%ds\n",
           dev, chunk, (unsigned long long)(offset0 / 1024ULL / 1024ULL),
           (unsigned long long)(window / 1024ULL / 1024ULL), sleep_ms, duration);
    fflush(stdout);

    uint64_t ops = 0, errs = 0, bytes = 0, sync_errs = 0, iters = 0;
    uint64_t pos = 0;
    uint64_t t0 = now_ns();

    while (1) {
        if ((now_ns() - t0) / 1000000000ULL >= (uint64_t)duration)
            break;

        off_t off = (off_t)(offset0 + pos);
        ssize_t w = pwrite(fd, buf, chunk, off);
        if (w == (ssize_t)chunk) {
            ops++;
            bytes += (uint64_t)w;
        } else {
            errs++;
            if (errs <= 3)
                fprintf(stderr, "[WRITER] pwrite(%lld) failed: %s\n", (long long)off,
                        w < 0 ? strerror(errno) : "short write");
        }

        /* Force the dirty pages out of the page cache so writeback bios are
         * submitted while we are still running (that is what WBT observes). */
        if (fsync(fd) != 0) {
            sync_errs++;
            if (sync_errs <= 3)
                fprintf(stderr, "[WRITER] fsync failed: %s\n", strerror(errno));
        }

        iters++;
        pos += chunk;
        if (pos + chunk > window)
            pos = 0;

        if (sleep_ms > 0)
            usleep((useconds_t)sleep_ms * 1000);
    }

    printf("[WRITER_RESULT] OPS=%llu BYTES=%llu ITERS=%llu ERR=%llu FSYNC_ERR=%llu\n",
           (unsigned long long)ops, (unsigned long long)bytes, (unsigned long long)iters,
           (unsigned long long)errs, (unsigned long long)sync_errs);
    fflush(stdout);
    close(fd);
    return 0;
}
