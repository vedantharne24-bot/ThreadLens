/*
 * lifecycle.c — thread creation, joining and termination paths.
 *
 *   returner   : start routine returns a value; joined by main
 *   exiter     : calls pthread_exit(); joined by main
 *   detached   : detaches itself, reports completion through a cond var
 *   cancellee  : waits on a cond var (a cancellation point) with a cleanup
 *                handler that unlocks the mutex; main cancels and joins it
 *   spawner    : creates a grandchild thread and joins it (nested creation)
 *
 * Expected ThreadLens result: no issue detected, exit code 0. The Live
 * Threads page shows every thread's start, exit kind and lifetime.
 */
#include "../common/scenario.h"

static pthread_mutex_t mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t cv = PTHREAD_COND_INITIALIZER;
static pthread_cond_t never = PTHREAD_COND_INITIALIZER;
static int detached_done;
static int cancellee_ready;

static void *returner(void *p)
{
    (void)p;
    sc_name_self("returner");
    sc_spin_us(2000);
    return (void *)7L;
}

static void *exiter(void *p)
{
    (void)p;
    sc_name_self("exiter");
    sc_spin_us(2000);
    pthread_exit((void *)42L);
}

static void *detached(void *p)
{
    (void)p;
    sc_name_self("detached");
    pthread_detach(pthread_self());
    sc_spin_us(3000);
    pthread_mutex_lock(&mu);
    detached_done = 1;
    pthread_cond_broadcast(&cv);
    pthread_mutex_unlock(&mu);
    return NULL;
}

static void unlock_cleanup(void *m)
{
    pthread_mutex_unlock((pthread_mutex_t *)m);
}

static void *cancellee(void *p)
{
    (void)p;
    sc_name_self("cancellee");
    pthread_mutex_lock(&mu);
    cancellee_ready = 1;
    pthread_cond_broadcast(&cv);
    pthread_cleanup_push(unlock_cleanup, &mu);
    for (;;) pthread_cond_wait(&never, &mu); /* cancellation point */
    pthread_cleanup_pop(1);
    return NULL;
}

static void *grandchild(void *p)
{
    (void)p;
    sc_name_self("grandchild");
    sc_spin_us(1000);
    return NULL;
}

static void *spawner(void *p)
{
    (void)p;
    sc_name_self("spawner");
    pthread_t g;
    pthread_create(&g, NULL, grandchild, NULL);
    pthread_join(g, NULL);
    return NULL;
}

int main(void)
{
    sc_watchdog(30);
    tl_name_lock(&mu, "lifecycle.mu");
    uint64_t t0 = sc_now_ns();

    pthread_t r, e, d, c, s;
    void *rv = NULL, *ev = NULL, *cvv = NULL;
    pthread_create(&r, NULL, returner, NULL);
    pthread_create(&e, NULL, exiter, NULL);
    pthread_create(&d, NULL, detached, NULL);
    pthread_create(&c, NULL, cancellee, NULL);
    pthread_create(&s, NULL, spawner, NULL);

    pthread_join(r, &rv);
    pthread_join(e, &ev);
    pthread_join(s, NULL);

    pthread_mutex_lock(&mu);
    while (!cancellee_ready || !detached_done) pthread_cond_wait(&cv, &mu);
    pthread_mutex_unlock(&mu);

    pthread_cancel(c);
    pthread_join(c, &cvv);

    SC_RESULT("{\"scenario\":\"lifecycle\",\"completed\":true,\"elapsed_ms\":%.3f,"
              "\"returner\":%ld,\"exiter\":%ld,\"cancelled\":%s}",
              (sc_now_ns() - t0) / 1e6, (long)rv, (long)ev,
              cvv == PTHREAD_CANCELED ? "true" : "false");
    return 0;
}
