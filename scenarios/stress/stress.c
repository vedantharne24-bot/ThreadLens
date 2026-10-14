/*
 * stress.c — high event-rate workload (also the overhead baseline).
 *
 * T threads perform N iterations each. Every iteration locks one of K
 * mutexes; a configurable fraction of iterations also takes a second mutex
 * with a HIGHER index (a global order), so the program can never deadlock.
 * Run it with the collector to measure throughput and event loss, and
 * without the collector (monitoring disabled) to obtain the baseline time.
 *
 * Per iteration the collector records ~3 events (request, acquired,
 * release), or ~6 when nested.
 *
 * usage: stress [threads=8] [iterations=20000] [locks=16] [nest_percent=30] [work_ns=200]
 */
#include "../common/scenario.h"

#define MAX_LOCKS 256
#define MAX_THREADS 256

static pthread_mutex_t locks[MAX_LOCKS];
static long counters[MAX_LOCKS];
static int n_threads = 8, iterations = 20000, n_locks = 16, nest_percent = 30, work_ns = 200;

static inline void tiny_work(void)
{
    if (work_ns <= 0) return;
    uint64_t end = sc_now_ns() + (uint64_t)work_ns;
    while (sc_now_ns() < end) {}
}

static void *worker(void *p)
{
    long id = (long)p;
    unsigned seed = (unsigned)(id * 2654435761u + 12345u);
    char name[16];
    snprintf(name, sizeof name, "stress-%ld", id);
    sc_name_self(name);
    for (int i = 0; i < iterations; i++) {
        int a = (int)(rand_r(&seed) % (unsigned)n_locks);
        int nested = (int)(rand_r(&seed) % 100u) < nest_percent && a < n_locks - 1;
        pthread_mutex_lock(&locks[a]);
        counters[a]++;
        if (nested) {
            int b = a + 1 + (int)(rand_r(&seed) % (unsigned)(n_locks - a - 1));
            pthread_mutex_lock(&locks[b]);   /* always higher index: no cycle possible */
            counters[b]++;
            tiny_work();
            pthread_mutex_unlock(&locks[b]);
        } else {
            tiny_work();
        }
        pthread_mutex_unlock(&locks[a]);
    }
    return NULL;
}

int main(int argc, char **argv)
{
    if (argc > 1) n_threads = atoi(argv[1]);
    if (argc > 2) iterations = atoi(argv[2]);
    if (argc > 3) n_locks = atoi(argv[3]);
    if (argc > 4) nest_percent = atoi(argv[4]);
    if (argc > 5) work_ns = atoi(argv[5]);
    if (n_threads < 1 || n_threads > MAX_THREADS) n_threads = 8;
    if (n_locks < 2 || n_locks > MAX_LOCKS) n_locks = 16;
    if (iterations < 1) iterations = 20000;

    for (int i = 0; i < n_locks; i++) pthread_mutex_init(&locks[i], NULL);

    uint64_t t0 = sc_now_ns();
    pthread_t th[MAX_THREADS];
    for (long i = 0; i < n_threads; i++) pthread_create(&th[i], NULL, worker, (void *)i);
    for (int i = 0; i < n_threads; i++) pthread_join(th[i], NULL);
    double ms = (sc_now_ns() - t0) / 1e6;

    long total = 0;
    for (int i = 0; i < n_locks; i++) total += counters[i];
    SC_RESULT("{\"scenario\":\"stress\",\"completed\":true,\"elapsed_ms\":%.3f,\"threads\":%d,"
              "\"iterations\":%d,\"locks\":%d,\"lock_ops\":%ld,\"ops_per_sec\":%.0f,\"monitored\":%s}",
              ms, n_threads, iterations, n_locks, total, total / (ms / 1000.0),
              tl_active() ? "true" : "false");
    return 0;
}
