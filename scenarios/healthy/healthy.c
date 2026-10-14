/*
 * healthy.c — correct multithreaded program (control case).
 *
 * Four workers update a shared statistics block. Every worker takes
 * stats_lock and, when it also needs the histogram, takes hist_lock while
 * holding stats_lock — always in that order, so nested locking is safe.
 * Critical sections are short; workers do most of their work unlocked.
 *
 * Expected ThreadLens result: no issue detected, exit code 0.
 *
 * usage: healthy [workers] [iterations]
 */
#include "../common/scenario.h"

static pthread_mutex_t stats_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_mutex_t hist_lock = PTHREAD_MUTEX_INITIALIZER;
static long total_items;
static long histogram[8];

static void *worker(void *p)
{
    long id = (long)p;
    char name[16];
    snprintf(name, sizeof name, "worker-%ld", id);
    sc_name_self(name);

    for (int i = 0; i < 40; i++) {
        sc_spin_us(200);                    /* work outside any lock */
        pthread_mutex_lock(&stats_lock);
        total_items++;
        if (i % 4 == 0) {                   /* nested, consistent order */
            pthread_mutex_lock(&hist_lock);
            histogram[(id + i) % 8]++;
            pthread_mutex_unlock(&hist_lock);
        }
        pthread_mutex_unlock(&stats_lock);
    }
    return (void *)id;
}

int main(int argc, char **argv)
{
    int n = argc > 1 ? atoi(argv[1]) : 4;
    if (n < 1 || n > 64) n = 4;
    tl_name_lock(&stats_lock, "stats_lock");
    tl_name_lock(&hist_lock, "hist_lock");

    uint64_t t0 = sc_now_ns();
    pthread_t th[64];
    for (long i = 0; i < n; i++) pthread_create(&th[i], NULL, worker, (void *)i);
    for (int i = 0; i < n; i++) pthread_join(th[i], NULL);

    SC_RESULT("{\"scenario\":\"healthy\",\"completed\":true,\"elapsed_ms\":%.3f,\"items\":%ld}",
              (sc_now_ns() - t0) / 1e6, total_items);
    return 0;
}
