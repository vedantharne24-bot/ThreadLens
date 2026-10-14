/*
 * scenario.h — small helpers shared by the ThreadLens test programs.
 *
 * All scenarios are ordinary pthread programs. They run with or without the
 * ThreadLens collector; the collector is injected with LD_PRELOAD.
 *
 * Reproducibility: instead of relying on sleeps, the scenarios use a
 * "timed gate" (a rendezvous with a timeout, built from a mutex and a
 * condition variable) to force the interleaving that a demonstration needs.
 *   - If all N threads reach the gate, they continue together. In the buggy
 *     programs this deterministically produces the deadlock.
 *   - If some thread cannot reach the gate (because the corrected program
 *     makes it wait for a lock instead), the others continue after the
 *     timeout, so the corrected program runs the same code path to completion.
 */
#ifndef THREADLENS_SCENARIO_H
#define THREADLENS_SCENARIO_H

#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "threadlens/threadlens.h"

static inline uint64_t sc_now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

/* Busy work that keeps the CPU (and therefore any held lock) busy. */
static inline void sc_spin_us(unsigned us)
{
    uint64_t end = sc_now_ns() + (uint64_t)us * 1000ull;
    volatile unsigned long x = 0;
    while (sc_now_ns() < end) x++;
}

static inline void sc_sleep_ms(unsigned ms)
{
    struct timespec ts = { (time_t)(ms / 1000), (long)(ms % 1000) * 1000000L };
    while (nanosleep(&ts, &ts) == -1 && errno == EINTR) {}
}

static inline void sc_name_self(const char *name)
{
    pthread_setname_np(pthread_self(), name);
}

/* Safety net for programs that are expected to hang: SIGALRM's default
 * action terminates the process. The ThreadLens supervisor normally kills
 * the program much earlier; this protects standalone runs. */
static inline void sc_watchdog(unsigned default_seconds)
{
    const char *v = getenv("SCENARIO_WATCHDOG");
    unsigned s = v ? (unsigned)atoi(v) : default_seconds;
    if (s > 0) alarm(s);
}

typedef struct sc_gate {
    pthread_mutex_t mu;
    pthread_cond_t cv;
    int arrived;
    int needed;
} sc_gate;

static inline void sc_gate_init(sc_gate *g, int needed)
{
    pthread_condattr_t ca;
    pthread_condattr_init(&ca);
    pthread_condattr_setclock(&ca, CLOCK_MONOTONIC);
    pthread_mutex_init(&g->mu, NULL);
    pthread_cond_init(&g->cv, &ca);
    pthread_condattr_destroy(&ca);
    g->arrived = 0;
    g->needed = needed;
    tl_name_lock(&g->mu, "gate.mu");
}

/* Returns 1 if all participants arrived, 0 if the timeout expired first. */
static inline int sc_gate_wait(sc_gate *g, unsigned timeout_ms)
{
    struct timespec dl;
    clock_gettime(CLOCK_MONOTONIC, &dl);
    dl.tv_sec += timeout_ms / 1000;
    dl.tv_nsec += (long)(timeout_ms % 1000) * 1000000L;
    if (dl.tv_nsec >= 1000000000L) { dl.tv_sec++; dl.tv_nsec -= 1000000000L; }

    int all = 1;
    pthread_mutex_lock(&g->mu);
    g->arrived++;
    if (g->arrived >= g->needed) {
        pthread_cond_broadcast(&g->cv);
    } else {
        while (g->arrived < g->needed) {
            if (pthread_cond_timedwait(&g->cv, &g->mu, &dl) == ETIMEDOUT) { all = 0; break; }
        }
    }
    pthread_mutex_unlock(&g->mu);
    return all;
}

/* Machine-readable result line parsed by the ThreadLens backend. */
#define SC_RESULT(fmt, ...) \
    do { printf("TLRESULT " fmt "\n", __VA_ARGS__); fflush(stdout); } while (0)

#endif /* THREADLENS_SCENARIO_H */
