#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <time.h>
#include <errno.h>

#ifndef O_DIRECT
#define O_DIRECT 0
#endif

#ifndef CLOCK_MONOTONIC
#define CLOCK_MONOTONIC 1
#endif

#define MAX_SAMPLES 200000
#define BLOCK_SIZE 4096

static uint64_t latencies[MAX_SAMPLES];

static int cmp_uint64(const void *a, const void *b) {
    uint64_t ua = *(const uint64_t *)a;
    uint64_t ub = *(const uint64_t *)b;
    if (ua < ub) return -1;
    if (ua > ub) return 1;
    return 0;
}

/*
 * Compute elapsed time in microseconds.
 *
 * The previous implementation cast each field difference to uint64_t
 * independently, so a negative tv_nsec delta (when the nanosecond field
 * wraps) was reinterpreted as a value near 2^64, producing absurd
 * MAX/MEAN results (~1.84e16 us). Accumulate in signed nanoseconds and
 * only then convert.
 */
static inline uint64_t time_diff_us(struct timespec *start, struct timespec *end) {
    int64_t secs = (int64_t)(end->tv_sec - start->tv_sec);
    int64_t nsecs = (int64_t)(end->tv_nsec - start->tv_nsec);
    int64_t total_ns = secs * 1000000000LL + nsecs;

    if (total_ns < 0)
        total_ns = 0;

    return (uint64_t)(total_ns / 1000LL);
}

int main(int argc, char *argv[]) {
    if (argc < 4) {
        fprintf(stderr, "Usage: %s <device> <duration_sec> <region_start_mb> <region_size_mb> [pace_us]\n", argv[0]);
        fprintf(stderr, "Example: %s /dev/sda 15 64 64\n", argv[0]);
        return 1;
    }

    const char *dev_path = argv[1];
    int duration_sec = atoi(argv[2]);
    uint64_t start_offset = (uint64_t)atoi(argv[3]) * 1024ULL * 1024ULL;
    uint64_t region_size = (uint64_t)atoi(argv[4]) * 1024ULL * 1024ULL;
    int pace_us = (argc >= 6) ? atoi(argv[5]) : 1000;

    int open_flags = O_RDONLY;
    bool direct_ok = false;
    int direct_errno = 0;
#ifdef __linux__
    open_flags |= O_DIRECT;
#endif

    int fd = open(dev_path, open_flags);
    if (fd >= 0) {
        direct_ok = true;
    } else {
        direct_errno = errno;
        fd = open(dev_path, O_RDONLY);
        if (fd < 0) {
            perror("Failed to open device");
            return 1;
        }
    }

    /*
     * Report the real open mode. A silent fallback to buffered reads would
     * serve the probes out of the page cache, so the block layer would never
     * see them, cb->stat[READ] would stay empty, and the guard's input would
     * be absent rather than measured.
     */
    if (direct_ok)
        printf("[PROBE] dev=%s open_mode=O_DIRECT pace_us=%d\n", dev_path, pace_us);
    else
        printf("[PROBE] dev=%s open_mode=BUFFERED (O_DIRECT failed: %s) pace_us=%d\n",
               dev_path, strerror(direct_errno), pace_us);
    fflush(stdout);

    void *buf = malloc(BLOCK_SIZE * 2);
    if (!buf) {
        perror("malloc failed");
        close(fd);
        return 1;
    }
    /* Align buffer to 4096 bytes for direct I/O */
    uintptr_t addr = (uintptr_t)buf;
    void *aligned_buf = (void *)((addr + BLOCK_SIZE - 1) & ~(BLOCK_SIZE - 1));

    struct timespec start_time, current_time;
    clock_gettime(CLOCK_MONOTONIC, &start_time);

    size_t sample_count = 0;
    uint64_t sum_us = 0;
    uint64_t num_blocks = (region_size - BLOCK_SIZE) / BLOCK_SIZE;
    if (num_blocks == 0) num_blocks = 1;

    /*
     * Seed per process. rand() without srand() behaves exactly like
     * srand(1), so every concurrent reader would walk an identical block
     * sequence in lockstep instead of issuing independent random reads.
     */
    srand((unsigned int)(getpid() ^ (unsigned int)time(NULL)));

    while (sample_count < MAX_SAMPLES) {
        clock_gettime(CLOCK_MONOTONIC, &current_time);
        if (current_time.tv_sec - start_time.tv_sec >= duration_sec) {
            break;
        }

        uint64_t block_idx = (uint64_t)rand() % num_blocks;
        off_t offset = (off_t)(start_offset + (block_idx * BLOCK_SIZE));

        struct timespec t_req_start, t_req_end;
        clock_gettime(CLOCK_MONOTONIC, &t_req_start);

        ssize_t ret = pread(fd, aligned_buf, BLOCK_SIZE, offset);
        clock_gettime(CLOCK_MONOTONIC, &t_req_end);

        if (ret == (ssize_t)BLOCK_SIZE) {
            uint64_t lat_us = time_diff_us(&t_req_start, &t_req_end);
            latencies[sample_count++] = lat_us;
            sum_us += lat_us;
        }

        /* Pacing between probes; 0 = unpaced (saturating read load). */
        if (pace_us > 0)
            usleep((useconds_t)pace_us);
    }

    close(fd);
    free(buf);

    if (sample_count == 0) {
        printf("[PROBE_RESULT] SAMPLES=0 ERROR=\"No reads completed\"\n");
        return 0;
    }

    qsort(latencies, sample_count, sizeof(uint64_t), cmp_uint64);

    uint64_t min_us = latencies[0];
    uint64_t p50_us = latencies[sample_count * 50 / 100];
    uint64_t p90_us = latencies[sample_count * 90 / 100];
    uint64_t p99_us = latencies[sample_count * 99 / 100];
    uint64_t max_us = latencies[sample_count - 1];
    double mean_us = (double)sum_us / (double)sample_count;

    printf("[PROBE_RESULT] SAMPLES=%zu MIN_US=%lu P50_US=%lu P90_US=%lu P99_US=%lu MAX_US=%lu MEAN_US=%.1f\n",
           sample_count, (unsigned long)min_us, (unsigned long)p50_us,
           (unsigned long)p90_us, (unsigned long)p99_us, (unsigned long)max_us, mean_us);

    return 0;
}
