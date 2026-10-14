/*
 * lost_wakeup.c — a hang that is NOT a lock cycle (condition-variable misuse).
 *
 * The notifier sets `ready` and signals BEFORE the waiter starts waiting.
 * The waiter has a bug: it calls pthread_cond_wait() without first checking
 * the predicate in a while loop, so the signal is lost and it waits forever.
 * main is blocked joining the waiter.
 *
 * Nobody holds a lock that anybody else is waiting for, so the wait-for graph
 * has no cycle. ThreadLens cannot know which thread "should" signal a
 * condition variable, so it reports the situation honestly as a STALL
 * (every observed thread is blocked and no progress is seen) with an
 * inconclusive classification — not as a confirmed deadlock.
 *
 * Expected ThreadLens result: STALL / inconclusive; terminated by timeout.
 */
#include "../common/scenario.h"

static pthread_mutex_t mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t cv = PTHREAD_COND_INITIALIZER;
static int ready;

static void *notifier(void *p)
{
    (void)p;
    sc_name_self("notifier");
    pthread_mutex_lock(&mu);
    ready = 1;
    pthread_cond_signal(&cv);           /* nobody is waiting yet: signal is lost */
    pthread_mutex_unlock(&mu);
    return NULL;
}

static void *waiter(void *p)
{
    (void)p;
    sc_name_self("waiter");
    pthread_mutex_lock(&mu);
    pthread_cond_wait(&cv, &mu);        /* BUG: should be while (!ready) wait */
    pthread_mutex_unlock(&mu);
    return NULL;
}

int main(void)
{
    sc_watchdog(30);
    tl_name_lock(&mu, "ready.mu");
    pthread_t n, w;
    pthread_create(&n, NULL, notifier, NULL);
    pthread_join(n, NULL);              /* the signal has definitely happened */
    pthread_create(&w, NULL, waiter, NULL);
    pthread_join(w, NULL);              /* hangs */
    SC_RESULT("{\"scenario\":\"lost_wakeup\",\"completed\":true,\"ready\":%d}", ready);
    return 0;
}
