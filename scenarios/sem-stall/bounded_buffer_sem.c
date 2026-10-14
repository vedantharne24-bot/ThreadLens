/*
 * bounded_buffer_sem.c — the textbook semaphore bounded buffer, with the
 * classic ordering bug (Silberschatz: "swap wait(empty) and wait(mutex)").
 *
 *   slots  counts free buffer slots   (initially CAPACITY)
 *   items  counts filled buffer slots (initially 0)
 *   buffer.mu protects the ring buffer itself
 *
 * Buggy producer:   lock(buffer.mu); sem_wait(slots); put; unlock; sem_post(items)
 * Correct producer: sem_wait(slots); lock(buffer.mu); put; unlock; sem_post(items)
 *
 * The consumer spends ~1 ms on every item, so the producer runs ahead and
 * fills the buffer. Its next sem_wait(slots) blocks WHILE it holds buffer.mu.
 * The consumer, the only thread that posts `slots`, now blocks on buffer.mu.
 * main is joining the producer. Every thread is blocked, forever.
 *
 * The wait-for graph has only consumer -> producer (the mutex) and
 * main -> producer (the join). The producer's wait is on a semaphore, and a
 * semaphore has no owner — any thread could post it — so there is no edge
 * and no cycle. ThreadLens therefore reports a STALL (every thread blocked,
 * no progress) and explains the chain, instead of claiming a deadlock it
 * cannot prove from the observed operations.
 *
 * "--fixed": the producer waits on `slots` BEFORE locking the buffer, so the
 * consumer can always reach the buffer and post; the program completes.
 *
 * Expected ThreadLens result:
 *   default : STALL (cause inconclusive); terminated by the session timeout
 *   --fixed : no issue, exit code 0
 *
 * usage: bounded_buffer_sem [--fixed] [items]
 */
#include "../common/scenario.h"

#include <semaphore.h>

#define CAPACITY 4

static sem_t slots;
static sem_t items;
static pthread_mutex_t buffer_mu = PTHREAD_MUTEX_INITIALIZER;
static int ring[CAPACITY];
static int head, tail;
static int fixed_order;
static int total = 40;
static long consumed_sum;

static void *producer(void *p)
{
    (void)p;
    sc_name_self("producer");
    for (int i = 1; i <= total; i++) {
        if (fixed_order) {
            sem_wait(&slots);                    /* FIX: wait for a free slot first ... */
            pthread_mutex_lock(&buffer_mu);      /* ... then enter the critical section */
        } else {
            pthread_mutex_lock(&buffer_mu);
            sem_wait(&slots);                    /* BUG: blocks while holding buffer.mu */
        }
        ring[tail] = i;
        tail = (tail + 1) % CAPACITY;
        pthread_mutex_unlock(&buffer_mu);
        sem_post(&items);
    }
    return NULL;
}

static void *consumer(void *p)
{
    (void)p;
    sc_name_self("consumer");
    for (int i = 1; i <= total; i++) {
        sem_wait(&items);
        pthread_mutex_lock(&buffer_mu);
        int v = ring[head];
        head = (head + 1) % CAPACITY;
        pthread_mutex_unlock(&buffer_mu);
        sem_post(&slots);
        consumed_sum += v;
        sc_spin_us(1000);                        /* process the item */
    }
    return NULL;
}

int main(int argc, char **argv)
{
    sc_watchdog(30);
    for (int a = 1; a < argc; a++) {
        if (strcmp(argv[a], "--fixed") == 0) fixed_order = 1;
        else if (atoi(argv[a]) > 0) total = atoi(argv[a]);
    }
    sem_init(&slots, 0, CAPACITY);
    sem_init(&items, 0, 0);
    tl_name_lock(&slots, "slots");
    tl_name_lock(&items, "items");
    tl_name_lock(&buffer_mu, "buffer.mu");

    uint64_t t0 = sc_now_ns();
    pthread_t prod, cons;
    pthread_create(&cons, NULL, consumer, NULL);
    pthread_create(&prod, NULL, producer, NULL);
    pthread_join(prod, NULL);                    /* hangs in the buggy version */
    pthread_join(cons, NULL);
    sem_destroy(&items);
    sem_destroy(&slots);

    long expect = (long)total * (total + 1) / 2;
    SC_RESULT("{\"scenario\":\"bounded_buffer_sem\",\"fixed\":%s,\"completed\":true,\"elapsed_ms\":%.3f,"
              "\"consumed_sum\":%ld,\"ok\":%s}",
              fixed_order ? "true" : "false", (sc_now_ns() - t0) / 1e6, consumed_sum,
              consumed_sum == expect ? "true" : "false");
    return consumed_sum == expect ? 0 : 1;
}
