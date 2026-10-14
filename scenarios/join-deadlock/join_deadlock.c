/*
 * join_deadlock.c — deadlock between a lock and pthread_join().
 *
 * main locks config_lock, starts a worker and then joins the worker while
 * STILL holding config_lock. The worker needs config_lock to finish.
 *     main   --join waits for-->  worker
 *     worker --lock waits for-->  main (owner of config_lock)
 * The wait-for graph therefore contains a cycle made of two different kinds
 * of edges. No timing assumption is needed: whichever thread runs first,
 * the cycle forms.
 *
 * Expected ThreadLens result: CONFIRMED DEADLOCK (join + lock cycle).
 */
#include "../common/scenario.h"

static pthread_mutex_t config_lock = PTHREAD_MUTEX_INITIALIZER;
static int config_version;

static void *reloader(void *p)
{
    (void)p;
    sc_name_self("reloader");
    pthread_mutex_lock(&config_lock);   /* blocks: main holds config_lock */
    config_version++;
    pthread_mutex_unlock(&config_lock);
    return NULL;
}

int main(void)
{
    sc_watchdog(30);
    tl_name_lock(&config_lock, "config_lock");

    pthread_mutex_lock(&config_lock);
    pthread_t t;
    pthread_create(&t, NULL, reloader, NULL);
    pthread_join(t, NULL);              /* BUG: joining while holding the lock */
    pthread_mutex_unlock(&config_lock);

    SC_RESULT("{\"scenario\":\"join_deadlock\",\"completed\":true,\"version\":%d}", config_version);
    return 0;
}
