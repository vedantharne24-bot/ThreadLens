/*
 * misuse.c — pthread mutex misuse and error-handling cases.
 *
 * Default mode runs each case safely and checks the error code glibc returns:
 *   1. error-checking mutex locked twice by its owner      -> EDEADLK
 *   2. error-checking mutex unlocked by a non-owner        -> EPERM
 *   3. unlocking an unlocked error-checking mutex          -> EPERM
 *   4. recursive mutex locked 3x and unlocked 3x           -> OK (count tracked)
 *   5. destroying a locked mutex                           -> EBUSY
 *   6. thread exits while holding a normal mutex ("abandoned lock");
 *      main then uses pthread_mutex_timedlock and gets ETIMEDOUT
 *   7. same with a ROBUST mutex: the next locker gets EOWNERDEAD,
 *      repairs state, calls pthread_mutex_consistent and unlocks
 *
 * Mode "self-deadlock": a thread locks a NORMAL mutex it already holds.
 * POSIX leaves this undefined; glibc blocks forever. Expected ThreadLens
 * result: CONFIRMED DEADLOCK (self-loop in the wait-for graph).
 *
 * usage: misuse [self-deadlock]
 */
#include "../common/scenario.h"

static pthread_mutex_t ec;      /* error-checking */
static pthread_mutex_t rec;     /* recursive */
static pthread_mutex_t plain;   /* normal */
static pthread_mutex_t robust;  /* robust */
static pthread_mutex_t doomed;  /* destroyed while locked */

static int check(const char *what, int got, int want)
{
    printf("%-46s rc=%-3d (%s) %s\n", what, got, got ? strerror(got) : "ok",
           got == want ? "as expected" : "UNEXPECTED");
    return got == want;
}

static int foreign_rc;
static void *foreign_unlock_rc(void *p)
{
    (void)p;
    sc_name_self("intruder");
    foreign_rc = pthread_mutex_unlock(&ec); /* not the owner: EPERM for error-checking mutexes */
    return NULL;
}

static void *abandon_plain(void *p)
{
    (void)p;
    sc_name_self("abandoner");
    pthread_mutex_lock(&plain);
    return NULL; /* BUG: exits while holding `plain` */
}

static void *abandon_robust(void *p)
{
    (void)p;
    sc_name_self("robust-holder");
    pthread_mutex_lock(&robust);
    return NULL; /* exits while holding a robust mutex */
}

static void *self_deadlocker(void *p)
{
    (void)p;
    sc_name_self("self-locker");
    pthread_mutex_lock(&plain);
    pthread_mutex_lock(&plain); /* blocks forever on glibc */
    pthread_mutex_unlock(&plain);
    pthread_mutex_unlock(&plain);
    return NULL;
}

static void init_mutex(pthread_mutex_t *m, int type, int robust_flag, const char *name)
{
    pthread_mutexattr_t a;
    pthread_mutexattr_init(&a);
    pthread_mutexattr_settype(&a, type);
    if (robust_flag) pthread_mutexattr_setrobust(&a, PTHREAD_MUTEX_ROBUST);
    pthread_mutex_init(m, &a);
    pthread_mutexattr_destroy(&a);
    tl_name_lock(m, name);
}

int main(int argc, char **argv)
{
    sc_watchdog(30);
    init_mutex(&ec, PTHREAD_MUTEX_ERRORCHECK, 0, "errorcheck_mu");
    init_mutex(&rec, PTHREAD_MUTEX_RECURSIVE, 0, "recursive_mu");
    init_mutex(&plain, PTHREAD_MUTEX_NORMAL, 0, "plain_mu");
    init_mutex(&robust, PTHREAD_MUTEX_NORMAL, 1, "robust_mu");
    init_mutex(&doomed, PTHREAD_MUTEX_NORMAL, 0, "doomed_mu");

    pthread_t t;
    if (argc > 1 && strcmp(argv[1], "self-deadlock") == 0) {
        pthread_create(&t, NULL, self_deadlocker, NULL);
        pthread_join(t, NULL);
        return 0; /* never reached */
    }

    uint64_t t0 = sc_now_ns();
    int ok = 1;

    /* 1 */
    pthread_mutex_lock(&ec);
    ok &= check("1. errorcheck relock by owner", pthread_mutex_lock(&ec), EDEADLK);
    /* 2 */
    pthread_create(&t, NULL, foreign_unlock_rc, NULL);
    pthread_join(t, NULL);
    ok &= check("2. errorcheck unlock by non-owner", foreign_rc, EPERM);
    pthread_mutex_unlock(&ec);
    /* 3 */
    ok &= check("3. unlock of unlocked errorcheck mutex", pthread_mutex_unlock(&ec), EPERM);
    /* 4 */
    pthread_mutex_lock(&rec);
    pthread_mutex_lock(&rec);
    pthread_mutex_lock(&rec);
    pthread_mutex_unlock(&rec);
    pthread_mutex_unlock(&rec);
    ok &= check("4. recursive mutex 3x lock / 3x unlock", pthread_mutex_unlock(&rec), 0);
    /* 5 */
    pthread_mutex_lock(&doomed);
    ok &= check("5. destroy a locked mutex", pthread_mutex_destroy(&doomed), EBUSY);
    pthread_mutex_unlock(&doomed);
    pthread_mutex_destroy(&doomed);
    /* 6 */
    pthread_create(&t, NULL, abandon_plain, NULL);
    pthread_join(t, NULL);
    struct timespec dl;
    clock_gettime(CLOCK_REALTIME, &dl);
    dl.tv_nsec += 200 * 1000000L;
    if (dl.tv_nsec >= 1000000000L) { dl.tv_sec++; dl.tv_nsec -= 1000000000L; }
    ok &= check("6. timedlock on abandoned normal mutex", pthread_mutex_timedlock(&plain, &dl), ETIMEDOUT);
    /* 7 */
    pthread_create(&t, NULL, abandon_robust, NULL);
    pthread_join(t, NULL);
    int rc = pthread_mutex_lock(&robust);
    ok &= check("7. lock robust mutex of dead owner", rc, EOWNERDEAD);
    if (rc == EOWNERDEAD) pthread_mutex_consistent(&robust);
    pthread_mutex_unlock(&robust);

    SC_RESULT("{\"scenario\":\"misuse\",\"completed\":true,\"elapsed_ms\":%.3f,\"all_expected\":%s}",
              (sc_now_ns() - t0) / 1e6, ok ? "true" : "false");
    return ok ? 0 : 1;
}
