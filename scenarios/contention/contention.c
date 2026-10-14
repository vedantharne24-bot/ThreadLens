/*
 * contention.c — heavy lock contention WITHOUT deadlock.
 *
 * Eight workers repeatedly enter one "hot" critical section and keep the
 * lock for ~2 ms of CPU work. Most of each worker's lifetime is spent
 * waiting for hot_lock. The program always completes.
 *
 * Expected ThreadLens result: EXCESSIVE LOCK WAITING / CONTENTION on
 * hot_lock, and explicitly NOT a deadlock.
 *
 * usage: contention [workers] [iterations] [hold_us]
 */
#include "../common/scenario.h"

static pthread_mutex_t hot_lock = PTHREAD_MUTEX_INITIALIZER;
static long shared_counter;
static int iterations = 15;
static unsigned hold_us = 2000;

static void *worker(void *p)
{
    long id = (long)p;
    char name[16];
    snprintf(name, sizeof name, "hot-%ld", id);
    sc_name_self(name);
    for (int i = 0; i < iterations; i++) {
        pthread_mutex_lock(&hot_lock);
        sc_spin_us(hold_us);              /* long critical section */
        shared_counter++;
        pthread_mutex_unlock(&hot_lock);
        sc_spin_us(100);
    }
    return NULL;
}

int main(int argc, char **argv)
{
    int n = argc > 1 ? atoi(argv[1]) : 8;
    if (n < 1 || n > 64) n = 8;
    if (argc > 2 && atoi(argv[2]) > 0) iterations = atoi(argv[2]);
    if (argc > 3 && atoi(argv[3]) > 0) hold_us = (unsigned)atoi(argv[3]);
    tl_name_lock(&hot_lock, "hot_lock");

    uint64_t t0 = sc_now_ns();
    pthread_t th[64];
    for (long i = 0; i < n; i++) pthread_create(&th[i], NULL, worker, (void *)i);
    for (int i = 0; i < n; i++) pthread_join(th[i], NULL);

    SC_RESULT("{\"scenario\":\"contention\",\"completed\":true,\"elapsed_ms\":%.3f,\"counter\":%ld}",
              (sc_now_ns() - t0) / 1e6, shared_counter);
    return 0;
}
