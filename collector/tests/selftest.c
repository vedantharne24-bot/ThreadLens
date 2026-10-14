/*
 * selftest.c — exercises every intercepted pthread function.
 * Run under the collector with THREADLENS_TRACE=<file>; then validate the
 * trace with tl_tracecheck (see CMakeLists.txt / ctest).
 */
#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
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
static long counter;
static int turn;

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
    clock_gettime(CLOCK_REALTIME, &dl);
    dl.tv_nsec += 1000000;
    if (dl.tv_nsec >= 1000000000L) { dl.tv_sec++; dl.tv_nsec -= 1000000000L; }
    if (pthread_mutex_timedlock(&a, &dl) == 0) pthread_mutex_unlock(&a);
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
    tl_name_lock(&a, "selftest.a");
    tl_mark("selftest start");

    pthread_t th[WORKERS];
    for (long i = 0; i < WORKERS; i++) pthread_create(&th[i], NULL, worker, (void *)i);
    for (int i = 0; i < WORKERS; i++) pthread_join(th[i], NULL);

    pthread_barrier_destroy(&bar);
    pthread_mutex_destroy(&rec);
    printf("selftest: counter=%ld active=%d\n", counter, tl_active());
    return counter == (long)WORKERS * (ITERS + (ITERS + 2) / 3) ? 0 : 1;
}
