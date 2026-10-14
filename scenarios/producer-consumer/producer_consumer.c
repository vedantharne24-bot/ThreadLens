/*
 * producer_consumer.c — bounded buffer with condition variables (correct).
 *
 * Two producers and two consumers share a 4-slot ring buffer protected by
 * one mutex and two condition variables (not_full, not_empty). Every wait is
 * inside a while loop that re-checks its predicate, which handles spurious
 * wake-ups and signals sent before the waiter arrived. Consumers stop when
 * they receive a poison pill (-1).
 *
 * pthread_cond_wait() releases the mutex while waiting and re-acquires it
 * before returning; ThreadLens records COND_WAIT_BEGIN/END for this.
 *
 * Expected ThreadLens result: no issue detected, exit code 0.
 *
 * usage: producer_consumer [items_per_producer]
 */
#include "../common/scenario.h"

#define CAP 4
#define PRODUCERS 2
#define CONSUMERS 2

static int buf[CAP];
static int head, tail, count;
static pthread_mutex_t mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t not_full = PTHREAD_COND_INITIALIZER;
static pthread_cond_t not_empty = PTHREAD_COND_INITIALIZER;
static int items_per_producer = 60;
static long consumed_sum, consumed_count;

static void put(int v)
{
    pthread_mutex_lock(&mu);
    while (count == CAP) pthread_cond_wait(&not_full, &mu);
    buf[tail] = v;
    tail = (tail + 1) % CAP;
    count++;
    pthread_cond_signal(&not_empty);
    pthread_mutex_unlock(&mu);
}

static int take(void)
{
    pthread_mutex_lock(&mu);
    while (count == 0) pthread_cond_wait(&not_empty, &mu);
    int v = buf[head];
    head = (head + 1) % CAP;
    count--;
    pthread_cond_signal(&not_full);
    pthread_mutex_unlock(&mu);
    return v;
}

static void *producer(void *p)
{
    long id = (long)p;
    char name[16];
    snprintf(name, sizeof name, "producer-%ld", id);
    sc_name_self(name);
    for (int i = 0; i < items_per_producer; i++) {
        sc_spin_us(50);
        put((int)(id * 1000 + i));
    }
    return NULL;
}

static void *consumer(void *p)
{
    long id = (long)p;
    char name[16];
    snprintf(name, sizeof name, "consumer-%ld", id);
    sc_name_self(name);
    for (;;) {
        int v = take();
        if (v < 0) break;                  /* poison pill */
        sc_spin_us(80);
        pthread_mutex_lock(&mu);
        consumed_sum += v;
        consumed_count++;
        pthread_mutex_unlock(&mu);
    }
    return NULL;
}

int main(int argc, char **argv)
{
    if (argc > 1 && atoi(argv[1]) > 0) items_per_producer = atoi(argv[1]);
    tl_name_lock(&mu, "queue.mu");

    uint64_t t0 = sc_now_ns();
    pthread_t pt[PRODUCERS], ct[CONSUMERS];
    for (long i = 0; i < CONSUMERS; i++) pthread_create(&ct[i], NULL, consumer, (void *)i);
    for (long i = 0; i < PRODUCERS; i++) pthread_create(&pt[i], NULL, producer, (void *)i);
    for (int i = 0; i < PRODUCERS; i++) pthread_join(pt[i], NULL);
    for (int i = 0; i < CONSUMERS; i++) put(-1);
    for (int i = 0; i < CONSUMERS; i++) pthread_join(ct[i], NULL);

    SC_RESULT("{\"scenario\":\"producer_consumer\",\"completed\":true,\"elapsed_ms\":%.3f,\"consumed\":%ld}",
              (sc_now_ns() - t0) / 1e6, consumed_count);
    return consumed_count == (long)PRODUCERS * items_per_producer ? 0 : 1;
}
