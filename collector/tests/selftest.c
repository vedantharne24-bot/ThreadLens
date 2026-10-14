/*
 * selftest.c — exercises every intercepted pthread / semaphore function.
 * Run under the collector with THREADLENS_TRACE=<file>; then validate the
 * trace with tl_tracecheck (see CMakeLists.txt / ctest).
 */
#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <semaphore.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

#include "threadlens/threadlens.h"

#define WORKERS 6
#define ITERS 2000

static pthread_mutex_t a = PTHREAD_MUTEX_INITIALIZER;
static pthread_mutex_t b = PTHREAD_MUTEX_INITIALIZER;
static pthread_mutex_t rec;
static pthread_mutex_t mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t cv = PTHREAD_COND_INITIALIZER;
static pthread_barrier_t bar;
static pthread_rwlock_t table = PTHREAD_RWLOCK_INITIALIZER;
static pthread_rwlock_t prefs;        /* initialised with pthread_rwlock_init */
static sem_t tokens;                  /* counting semaphore: at most 2 workers inside */
static long counter;
static long rw_writes;
static long rw_reads;
static int turn;

static void deadline_in(struct timespec *dl, long ns)
{
    clock_gettime(CLOCK_REALTIME, dl);
    dl->tv_nsec += ns;
    while (dl->tv_nsec >= 1000000000L) { dl->tv_sec++; dl->tv_nsec -= 1000000000L; }
}

static void *worker(void *p)
{
    long id = (long)p;
    char name[16];
    snprintf(name, sizeof name, "self-%ld", id);
    pthread_setname_np(pthread_self(), name);
    pthread_barrier_wait(&bar);
    for (int i = 0; i < ITERS; i++) {
        pthread_mutex_lock(&a);
        if (i % 3 == 0) {
            pthread_mutex_lock(&b);   /* consistent order a -> b */
            counter++;
            pthread_mutex_unlock(&b);
        }
        counter++;
        pthread_mutex_unlock(&a);
        if (pthread_mutex_trylock(&b) == 0) pthread_mutex_unlock(&b);
        pthread_mutex_lock(&rec);
        pthread_mutex_lock(&rec);
        pthread_mutex_unlock(&rec);
        pthread_mutex_unlock(&rec);
        /* read-write lock: mostly readers, every 4th iteration a writer */
        if (i % 4 == 0) {
            pthread_rwlock_wrlock(&table);
            rw_writes++;
            pthread_rwlock_unlock(&table);
        } else {
            pthread_rwlock_rdlock(&table);
            __atomic_fetch_add(&rw_reads, 1, __ATOMIC_RELAXED);
            pthread_rwlock_rdlock(&table);   /* recursive read lock (allowed for readers) */
            pthread_rwlock_unlock(&table);
            pthread_rwlock_unlock(&table);
        }
        if (pthread_rwlock_tryrdlock(&prefs) == 0) pthread_rwlock_unlock(&prefs);
        if (i % 50 == 0 && pthread_rwlock_trywrlock(&prefs) == 0) pthread_rwlock_unlock(&prefs);
        /* counting semaphore */
        if (i % 10 == 0) {
            sem_wait(&tokens);
            sem_post(&tokens);
        }
        if (i % 25 == 0 && sem_trywait(&tokens) == 0) sem_post(&tokens);
    }
    /* ping-pong on a condition variable */
    for (int r = 0; r < 20; r++) {
        pthread_mutex_lock(&mu);
        while (turn % WORKERS != id) pthread_cond_wait(&cv, &mu);
        turn++;
        pthread_cond_broadcast(&cv);
        pthread_mutex_unlock(&mu);
    }
    struct timespec dl;
    deadline_in(&dl, 1000000);
    if (pthread_mutex_timedlock(&a, &dl) == 0) pthread_mutex_unlock(&a);
    deadline_in(&dl, 1000000);
    if (pthread_rwlock_timedrdlock(&prefs, &dl) == 0) pthread_rwlock_unlock(&prefs);
    deadline_in(&dl, 1000000);
    if (pthread_rwlock_timedwrlock(&prefs, &dl) == 0) pthread_rwlock_unlock(&prefs);
    deadline_in(&dl, 1000000);
    if (sem_timedwait(&tokens, &dl) == 0) sem_post(&tokens);
    if (id == 0) pthread_exit((void *)1);
    return NULL;
}

int main(void)
{
    pthread_mutexattr_t attr;
    pthread_mutexattr_init(&attr);
    pthread_mutexattr_settype(&attr, PTHREAD_MUTEX_RECURSIVE);
    pthread_mutex_init(&rec, &attr);
    pthread_mutexattr_destroy(&attr);
    pthread_barrier_init(&bar, NULL, WORKERS);
    pthread_rwlock_init(&prefs, NULL);
    sem_init(&tokens, 0, 2);
    tl_name_lock(&a, "selftest.a");
    tl_name_lock(&table, "selftest.table");
    tl_mark("selftest start");

    pthread_t th[WORKERS];
    for (long i = 0; i < WORKERS; i++) pthread_create(&th[i], NULL, worker, (void *)i);
    for (int i = 0; i < WORKERS; i++) pthread_join(th[i], NULL);

    int tokens_left = -1;
    sem_getvalue(&tokens, &tokens_left);
    pthread_barrier_destroy(&bar);
    pthread_mutex_destroy(&rec);
    pthread_rwlock_destroy(&prefs);
    sem_destroy(&tokens);
    printf("selftest: counter=%ld rw_writes=%ld rw_reads=%ld tokens=%d active=%d\n", counter, rw_writes,
           rw_reads, tokens_left, tl_active());
    int ok = counter == (long)WORKERS * (ITERS + (ITERS + 2) / 3)
             && rw_writes == (long)WORKERS * ((ITERS + 3) / 4)
             && rw_reads == (long)WORKERS * (ITERS - (ITERS + 3) / 4)
             && tokens_left == 2;
    return ok ? 0 : 1;
}
