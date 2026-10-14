/*
 * lock_order.c — potential deadlock (lock-order inversion) that does NOT hang.
 *
 * Modes:
 *   ab    one worker takes lock_a then lock_b             (run 1 of a pair)
 *   ba    one worker takes lock_b then lock_a             (run 2 of a pair)
 *   both  worker-ab takes a->b, finishes, then signals worker-ba, which
 *         takes b->a. The hand-off (condition variable) serializes the two
 *         critical sections, so THIS execution completes — but the two
 *         orders together form the cycle a->b->a in the lock-order graph.
 *
 * Expected ThreadLens result:
 *   both     : POTENTIAL lock-order deadlock (not confirmed), exit code 0
 *   ab / ba  : no issue in a single run; the cross-run lock-order analysis
 *              of an "ab" session plus a "ba" session reports the potential
 *              deadlock (lock identity uses the stable names lock_a/lock_b).
 *
 * usage: lock_order [ab|ba|both]
 */
#include "../common/scenario.h"

static pthread_mutex_t lock_a = PTHREAD_MUTEX_INITIALIZER;
static pthread_mutex_t lock_b = PTHREAD_MUTEX_INITIALIZER;
static pthread_mutex_t handoff_mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t handoff_cv = PTHREAD_COND_INITIALIZER;
static int ab_done;
static long shared;

static void take_ab(void)
{
    pthread_mutex_lock(&lock_a);
    pthread_mutex_lock(&lock_b);
    shared++;
    pthread_mutex_unlock(&lock_b);
    pthread_mutex_unlock(&lock_a);
}

static void take_ba(void)
{
    pthread_mutex_lock(&lock_b);
    pthread_mutex_lock(&lock_a);
    shared--;
    pthread_mutex_unlock(&lock_a);
    pthread_mutex_unlock(&lock_b);
}

static void *worker_ab(void *p)
{
    int signal_after = (int)(long)p;
    sc_name_self("worker-ab");
    take_ab();
    if (signal_after) {
        pthread_mutex_lock(&handoff_mu);
        ab_done = 1;
        pthread_cond_signal(&handoff_cv);
        pthread_mutex_unlock(&handoff_mu);
    }
    return NULL;
}

static void *worker_ba(void *p)
{
    int wait_first = (int)(long)p;
    sc_name_self("worker-ba");
    if (wait_first) {
        pthread_mutex_lock(&handoff_mu);
        while (!ab_done) pthread_cond_wait(&handoff_cv, &handoff_mu);
        pthread_mutex_unlock(&handoff_mu);
    }
    take_ba();
    return NULL;
}

int main(int argc, char **argv)
{
    const char *mode = argc > 1 ? argv[1] : "both";
    tl_name_lock(&lock_a, "lock_a");
    tl_name_lock(&lock_b, "lock_b");
    tl_name_lock(&handoff_mu, "handoff_mu");
    tl_mark(mode);

    uint64_t t0 = sc_now_ns();
    pthread_t t1, t2;
    if (strcmp(mode, "ab") == 0) {
        pthread_create(&t1, NULL, worker_ab, (void *)0L);
        pthread_join(t1, NULL);
    } else if (strcmp(mode, "ba") == 0) {
        pthread_create(&t2, NULL, worker_ba, (void *)0L);
        pthread_join(t2, NULL);
    } else {
        pthread_create(&t2, NULL, worker_ba, (void *)1L);
        pthread_create(&t1, NULL, worker_ab, (void *)1L);
        pthread_join(t1, NULL);
        pthread_join(t2, NULL);
    }
    SC_RESULT("{\"scenario\":\"lock_order\",\"mode\":\"%s\",\"completed\":true,\"elapsed_ms\":%.3f}",
              mode, (sc_now_ns() - t0) / 1e6);
    return 0;
}
