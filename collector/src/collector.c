/*
 * collector.c — ThreadLens pthread interposition library (libthreadlens.so).
 *
 * Usage:  THREADLENS_SOCKET=/path/to.sock LD_PRELOAD=./libthreadlens.so ./program
 *    or:  THREADLENS_TRACE=trace.tlt      LD_PRELOAD=./libthreadlens.so ./program
 *
 * How it works (see docs/architecture.md for the full explanation):
 *
 *  1. Because the library is preloaded, the dynamic linker binds the program's
 *     calls to pthread_mutex_lock & friends to the wrappers below. Each wrapper
 *     forwards to the real glibc function, obtained once with dlsym(RTLD_NEXT).
 *
 *  2. Each wrapper writes fixed-size 64-byte records (tl_wire.h) into a bounded,
 *     lock-free multi-producer ring buffer (Dmitry Vyukov's bounded queue). The
 *     index of the claimed ring slot IS the event's sequence number, so the
 *     sequence numbers form one total order of everything the program did.
 *
 *  3. Ordering rule that makes the analysis trustworthy:
 *        - LOCK_REQUEST  is recorded before calling the real lock,
 *        - LOCK_ACQUIRED is recorded after the real lock returns,
 *        - LOCK_RELEASE  is recorded BEFORE calling the real unlock.
 *     Therefore, for any mutex, a release always has a smaller sequence number
 *     than the next acquisition by another thread. Read-write locks follow
 *     the same rule (RW_REQUEST / RW_ACQUIRED / RW_RELEASE), and so do
 *     semaphores: SEM_POST is recorded before the real sem_post, so a post
 *     always precedes the SEM_WAIT_END of the waiter it lets through.
 *
 *  4. A private flusher thread drains the ring in sequence order and writes
 *     batches to a Unix-domain socket (or a trace file). Application threads
 *     never perform I/O. If the ring is full, a producer waits for at most
 *     THREADLENS_BLOCK_US microseconds and then drops the event; drops are
 *     counted and reported (EVENTS_LOST / HEARTBEAT records) so the backend
 *     can downgrade its verdicts instead of trusting incomplete data.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <link.h>
#include <pthread.h>
#include <sched.h>
#include <semaphore.h>
#include <stdatomic.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <sys/time.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#include "threadlens/tl_wire.h"

#define TL_EXPORT __attribute__((visibility("default")))
#define TL_TLS    __thread __attribute__((tls_model("initial-exec")))
#define TL_LIKELY(x)   __builtin_expect(!!(x), 1)
#define TL_UNLIKELY(x) __builtin_expect(!!(x), 0)

#define TL_DEFAULT_RING      (1u << 16)  /* 65536 records = 8 MiB with padding */
#define TL_DEFAULT_BLOCK_US  100000      /* max producer wait (100 ms) when the ring is full */
#define TL_DEFAULT_HB_MS     100         /* heartbeat period */
#define TL_BATCH             512         /* records per write() */

/* ------------------------------------------------------------------------ */
/* Real glibc functions                                                      */
/* ------------------------------------------------------------------------ */

static struct {
    int  (*mutex_init)(pthread_mutex_t *, const pthread_mutexattr_t *);
    int  (*mutex_destroy)(pthread_mutex_t *);
    int  (*mutex_lock)(pthread_mutex_t *);
    int  (*mutex_trylock)(pthread_mutex_t *);
    int  (*mutex_timedlock)(pthread_mutex_t *, const struct timespec *);
    int  (*mutex_clocklock)(pthread_mutex_t *, clockid_t, const struct timespec *);
    int  (*mutex_unlock)(pthread_mutex_t *);
    int  (*cond_wait)(pthread_cond_t *, pthread_mutex_t *);
    int  (*cond_timedwait)(pthread_cond_t *, pthread_mutex_t *, const struct timespec *);
    int  (*cond_clockwait)(pthread_cond_t *, pthread_mutex_t *, clockid_t, const struct timespec *);
    int  (*cond_signal)(pthread_cond_t *);
    int  (*cond_broadcast)(pthread_cond_t *);
    int  (*create)(pthread_t *, const pthread_attr_t *, void *(*)(void *), void *);
    int  (*join)(pthread_t, void **);
    int  (*detach)(pthread_t);
    int  (*cancel)(pthread_t);
    void (*exit_)(void *);
    int  (*setname)(pthread_t, const char *);
    int  (*barrier_wait)(pthread_barrier_t *);
    int  (*rwlock_init)(pthread_rwlock_t *, const pthread_rwlockattr_t *);
    int  (*rwlock_destroy)(pthread_rwlock_t *);
    int  (*rwlock_rdlock)(pthread_rwlock_t *);
    int  (*rwlock_wrlock)(pthread_rwlock_t *);
    int  (*rwlock_tryrdlock)(pthread_rwlock_t *);
    int  (*rwlock_trywrlock)(pthread_rwlock_t *);
    int  (*rwlock_timedrdlock)(pthread_rwlock_t *, const struct timespec *);
    int  (*rwlock_timedwrlock)(pthread_rwlock_t *, const struct timespec *);
    int  (*rwlock_clockrdlock)(pthread_rwlock_t *, clockid_t, const struct timespec *);
    int  (*rwlock_clockwrlock)(pthread_rwlock_t *, clockid_t, const struct timespec *);
    int  (*rwlock_unlock)(pthread_rwlock_t *);
    int  (*sem_init)(sem_t *, int, unsigned int);
    int  (*sem_destroy)(sem_t *);
    int  (*sem_wait)(sem_t *);
    int  (*sem_trywait)(sem_t *);
    int  (*sem_timedwait)(sem_t *, const struct timespec *);
    int  (*sem_clockwait)(sem_t *, clockid_t, const struct timespec *);
    int  (*sem_post)(sem_t *);
} real;

static void *tl_sym(const char *name, const char *version)
{
    void *p = NULL;
    if (version) p = dlvsym(RTLD_NEXT, name, version);
    if (!p) p = dlsym(RTLD_NEXT, name);
    return p;
}

/* pthread_cond_* exist in two symbol versions in glibc (the 2.2.5 "old"
 * condvar and the 2.3.2 one). Plain dlsym may return the old one, which has
 * an incompatible pthread_cond_t layout, so ask for the modern version first. */
#if defined(__x86_64__)
#define TL_COND_VER "GLIBC_2.3.2"
#else
#define TL_COND_VER NULL
#endif

static void tl_resolve(void)
{
    real.mutex_init      = tl_sym("pthread_mutex_init", NULL);
    real.mutex_destroy   = tl_sym("pthread_mutex_destroy", NULL);
    real.mutex_lock      = tl_sym("pthread_mutex_lock", NULL);
    real.mutex_trylock   = tl_sym("pthread_mutex_trylock", NULL);
    real.mutex_timedlock = tl_sym("pthread_mutex_timedlock", NULL);
    real.mutex_clocklock = tl_sym("pthread_mutex_clocklock", NULL);
    real.mutex_unlock    = tl_sym("pthread_mutex_unlock", NULL);
    real.cond_wait       = tl_sym("pthread_cond_wait", TL_COND_VER);
    real.cond_timedwait  = tl_sym("pthread_cond_timedwait", TL_COND_VER);
    real.cond_clockwait  = tl_sym("pthread_cond_clockwait", NULL);
    real.cond_signal     = tl_sym("pthread_cond_signal", TL_COND_VER);
    real.cond_broadcast  = tl_sym("pthread_cond_broadcast", TL_COND_VER);
    real.create          = tl_sym("pthread_create", NULL);
    real.join            = tl_sym("pthread_join", NULL);
    real.detach          = tl_sym("pthread_detach", NULL);
    real.cancel          = tl_sym("pthread_cancel", NULL);
    real.exit_           = tl_sym("pthread_exit", NULL);
    real.setname         = tl_sym("pthread_setname_np", NULL);
    real.barrier_wait    = tl_sym("pthread_barrier_wait", NULL);
    real.rwlock_init        = tl_sym("pthread_rwlock_init", NULL);
    real.rwlock_destroy     = tl_sym("pthread_rwlock_destroy", NULL);
    real.rwlock_rdlock      = tl_sym("pthread_rwlock_rdlock", NULL);
    real.rwlock_wrlock      = tl_sym("pthread_rwlock_wrlock", NULL);
    real.rwlock_tryrdlock   = tl_sym("pthread_rwlock_tryrdlock", NULL);
    real.rwlock_trywrlock   = tl_sym("pthread_rwlock_trywrlock", NULL);
    real.rwlock_timedrdlock = tl_sym("pthread_rwlock_timedrdlock", NULL);
    real.rwlock_timedwrlock = tl_sym("pthread_rwlock_timedwrlock", NULL);
    real.rwlock_clockrdlock = tl_sym("pthread_rwlock_clockrdlock", NULL);
    real.rwlock_clockwrlock = tl_sym("pthread_rwlock_clockwrlock", NULL);
    real.rwlock_unlock      = tl_sym("pthread_rwlock_unlock", NULL);
    real.sem_init           = tl_sym("sem_init", NULL);
    real.sem_destroy        = tl_sym("sem_destroy", NULL);
    real.sem_wait           = tl_sym("sem_wait", NULL);
    real.sem_trywait        = tl_sym("sem_trywait", NULL);
    real.sem_timedwait      = tl_sym("sem_timedwait", NULL);
    real.sem_clockwait      = tl_sym("sem_clockwait", NULL);
    real.sem_post           = tl_sym("sem_post", NULL);
}

/* Wrappers may run before the constructor (e.g. from another library's
 * constructor), so every wrapper makes sure the real symbol is known. */
#define TL_REAL(fn) (TL_LIKELY(real.fn != NULL) ? real.fn : (tl_resolve(), real.fn))

/* ------------------------------------------------------------------------ */
/* Global and per-thread state                                               */
/* ------------------------------------------------------------------------ */

typedef struct tl_cell {
    _Atomic uint64_t seq;          /* Vyukov sequence for this slot */
    char pad[56];                  /* keep the sequence on its own cache line */
    tl_wire_event ev;
} tl_cell;

static struct {
    _Atomic int       enabled;     /* 1 while streaming */
    int               fd;
    int               is_socket;
    uint64_t          token;
    uint64_t          load_bias;
    pid_t             pid;
    tl_cell          *ring;
    uint64_t          capacity;
    uint64_t          mask;
    long              block_ns;
    long              heartbeat_ns;
    pthread_key_t     exit_key;
    pthread_t         flusher;
    int               flusher_started;
    _Atomic int       stop;
    _Alignas(64) _Atomic uint64_t enq;   /* next slot to claim (producers) */
    _Alignas(64) uint64_t         deq;   /* next slot to drain (flusher only) */
    _Alignas(64) _Atomic uint64_t dropped;
    _Atomic uint32_t  next_index;
    _Atomic int32_t   live_threads;
    _Atomic uint32_t  threads_started;
    int               disconnect_reported;
} g = { .fd = -1 };

typedef struct tl_thread {
    uint32_t         tid;
    uint32_t         index;
    int              registered;
    int              internal;     /* collector-owned thread: never traced */
    uint32_t         exit_kind;
    pthread_cond_t  *cond;         /* cond var of an in-progress wait */
    pthread_mutex_t *cond_mutex;   /* mutex released by that wait */
} tl_thread;

static TL_TLS tl_thread tl_self;

static inline uint64_t tl_now(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static inline uint32_t tl_gettid(void)
{
    if (TL_UNLIKELY(tl_self.tid == 0)) tl_self.tid = (uint32_t)syscall(SYS_gettid);
    return tl_self.tid;
}

/* glibc-specific: the mutex kind and current owner are stored inside the
 * pthread_mutex_t. Reading them is a benign racy read of an int. */
static inline uint32_t tl_mutex_kind(pthread_mutex_t *m)
{
#ifdef __GLIBC__
    return m ? (uint32_t)__atomic_load_n(&m->__data.__kind, __ATOMIC_RELAXED) : 0;
#else
    (void)m; return 0;
#endif
}

static inline uint64_t tl_mutex_owner(pthread_mutex_t *m)
{
#ifdef __GLIBC__
    return m ? (uint64_t)(uint32_t)__atomic_load_n(&m->__data.__owner, __ATOMIC_RELAXED) : 0;
#else
    (void)m; return 0;
#endif
}

/* Same idea for rwlocks: glibc keeps the preference kind (__flags) and the
 * TID of the current writer (__cur_writer, 0 while readers or nobody hold it).
 * glibc does not record which threads hold read locks. */
static inline uint32_t tl_rw_kind(pthread_rwlock_t *rw)
{
#ifdef __GLIBC__
    return rw ? (uint32_t)__atomic_load_n(&rw->__data.__flags, __ATOMIC_RELAXED) : 0;
#else
    (void)rw; return 0;
#endif
}

static inline uint32_t tl_rw_writer(pthread_rwlock_t *rw)
{
#ifdef __GLIBC__
    return rw ? (uint32_t)__atomic_load_n(&rw->__data.__cur_writer, __ATOMIC_RELAXED) : 0;
#else
    (void)rw; return 0;
#endif
}

/* Semaphore count before the call (informational: it can change at any time). */
static inline uint64_t tl_sem_value(sem_t *s)
{
    int v = 0;
    if (!s || sem_getvalue(s, &v) != 0 || v < 0) v = 0;
    return (uint64_t)v;
}

/* ------------------------------------------------------------------------ */
/* Ring buffer (bounded MPSC, Vyukov)                                        */
/* ------------------------------------------------------------------------ */

static inline void tl_cpu_relax(void)
{
#if defined(__x86_64__) || defined(__i386__)
    __builtin_ia32_pause();
#endif
}

/* Claim one slot. Returns 1 and the slot position, or 0 if the ring stayed
 * full for longer than the configured blocking budget. */
static int tl_ring_claim(uint64_t *pos_out)
{
    uint64_t pos = atomic_load_explicit(&g.enq, memory_order_relaxed);
    uint64_t deadline = 0;
    unsigned spins = 0;
    for (;;) {
        tl_cell *c = &g.ring[pos & g.mask];
        uint64_t s = atomic_load_explicit(&c->seq, memory_order_acquire);
        int64_t dif = (int64_t)(s - pos);
        if (dif == 0) {
            if (atomic_compare_exchange_weak_explicit(&g.enq, &pos, pos + 1,
                                                      memory_order_relaxed,
                                                      memory_order_relaxed)) {
                *pos_out = pos;
                return 1;
            }
        } else if (dif < 0) {
            /* ring full: bounded back-pressure, then drop */
            if (g.block_ns <= 0) return 0;
            uint64_t t = tl_now();
            if (deadline == 0) deadline = t + (uint64_t)g.block_ns;
            else if (t >= deadline) return 0;
            if (++spins < 64) tl_cpu_relax(); else sched_yield();
            pos = atomic_load_explicit(&g.enq, memory_order_relaxed);
        } else {
            pos = atomic_load_explicit(&g.enq, memory_order_relaxed);
        }
    }
}

static inline tl_wire_event *tl_begin_record(uint64_t *pos, uint8_t type, uint16_t flags, uint64_t obj)
{
    if (!tl_ring_claim(pos)) {
        atomic_fetch_add_explicit(&g.dropped, 1, memory_order_relaxed);
        return NULL;
    }
    tl_wire_event *e = &g.ring[*pos & g.mask].ev;
    e->magic = TL_WIRE_MAGIC;
    e->version = TL_WIRE_VERSION;
    e->type = type;
    e->flags = flags;
    e->reserved = 0;
    e->seq = *pos;
    e->ts_ns = tl_now();
    e->pid = (uint32_t)g.pid;
    e->tid = tl_gettid();
    e->obj = obj;
    return e;
}

static inline void tl_publish(uint64_t pos)
{
    atomic_store_explicit(&g.ring[pos & g.mask].seq, pos + 1, memory_order_release);
}

static void tl_emit(uint8_t type, uint16_t flags, uint64_t obj, uint64_t aux,
                    const void *caller, int32_t result, uint32_t extra)
{
    uint64_t pos;
    tl_wire_event *e = tl_begin_record(&pos, type, flags, obj);
    if (!e) return;
    e->u.v.aux = aux;
    e->u.v.caller = (uint64_t)(uintptr_t)caller;
    e->u.v.result = result;
    e->u.v.extra = extra;
    tl_publish(pos);
}

static void tl_emit_str(uint8_t type, uint16_t flags, uint64_t obj, const char *s)
{
    uint64_t pos;
    tl_wire_event *e = tl_begin_record(&pos, type, flags, obj);
    if (!e) return;
    memset(e->u.str, 0, TL_WIRE_STRLEN);
    if (s) strncpy(e->u.str, s, TL_WIRE_STRLEN - 1);
    tl_publish(pos);
}

/* ------------------------------------------------------------------------ */
/* Output (socket or trace file) — only the flusher and init/fini write.     */
/* ------------------------------------------------------------------------ */

static int tl_write_all(const void *buf, size_t len)
{
    const char *p = buf;
    while (len > 0) {
        ssize_t n = g.is_socket ? send(g.fd, p, len, MSG_NOSIGNAL) : write(g.fd, p, len);
        if (n < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        p += n;
        len -= (size_t)n;
    }
    return 0;
}

static void tl_disconnect(const char *why)
{
    atomic_store(&g.enabled, 0);
    if (!g.disconnect_reported) {
        g.disconnect_reported = 1;
        static const char msg[] = "[threadlens] output failed; monitoring stopped: ";
        ssize_t r = write(2, msg, sizeof msg - 1);
        r = write(2, why, strlen(why));
        r = write(2, "\n", 1);
        (void)r;
    }
}

static void tl_oob(uint8_t type, uint64_t seq, uint64_t obj, uint64_t aux, int32_t result, uint32_t extra)
{
    tl_wire_event e;
    memset(&e, 0, sizeof e);
    e.magic = TL_WIRE_MAGIC;
    e.version = TL_WIRE_VERSION;
    e.type = type;
    e.flags = TL_F_OOB;
    e.seq = seq;
    e.ts_ns = tl_now();
    e.pid = (uint32_t)g.pid;
    e.tid = 0;
    e.obj = obj;
    e.u.v.aux = aux;
    e.u.v.result = result;
    e.u.v.extra = extra;
    if (g.fd >= 0 && tl_write_all(&e, sizeof e) != 0) tl_disconnect(strerror(errno));
}

/* Copy published records (in sequence order) into out[]. Single consumer. */
static size_t tl_drain(tl_wire_event *out, size_t max)
{
    size_t n = 0;
    while (n < max) {
        tl_cell *c = &g.ring[g.deq & g.mask];
        uint64_t s = atomic_load_explicit(&c->seq, memory_order_acquire);
        if (s != g.deq + 1) break; /* not yet published */
        out[n++] = c->ev;
        atomic_store_explicit(&c->seq, g.deq + g.capacity, memory_order_release);
        g.deq++;
    }
    return n;
}

static void *tl_flusher_main(void *unused)
{
    (void)unused;
    static tl_wire_event batch[TL_BATCH];
    tl_self.internal = 1;
    prctl(PR_SET_NAME, "tl-flusher", 0, 0, 0);

    uint64_t last_hb = tl_now();
    uint64_t reported_drops = 0;
    int had_data = 0;

    for (;;) {
        int stopping = atomic_load(&g.stop);
        size_t n = tl_drain(batch, TL_BATCH);
        if (n && g.fd >= 0) {
            if (tl_write_all(batch, n * sizeof(tl_wire_event)) != 0) tl_disconnect(strerror(errno));
        }

        uint64_t drops = atomic_load_explicit(&g.dropped, memory_order_relaxed);
        if (drops != reported_drops) {
            tl_oob(TL_EV_EVENTS_LOST, g.deq, drops, drops - reported_drops, 0, 0);
            reported_drops = drops;
        }

        /* Periodic heartbeat, plus an early "drained" heartbeat when the ring
         * empties after activity: it tells the backend quickly that every
         * event up to seq g.deq has been delivered (used to confirm cycles). */
        uint64_t now = tl_now();
        if (n) had_data = 1;
        int drained_after_activity = (n == 0 && had_data && (long)(now - last_hb) >= 2000000L);
        if (drained_after_activity || (long)(now - last_hb) >= g.heartbeat_ns) {
            uint64_t depth = atomic_load(&g.enq) - g.deq;
            tl_oob(TL_EV_HEARTBEAT, g.deq, drops, depth, 0,
                   (uint32_t)atomic_load(&g.live_threads));
            last_hb = now;
            had_data = 0;
        }

        if (n == 0) {
            if (stopping) break;
            struct timespec ts = { 0, 500 * 1000 }; /* 0.5 ms idle poll */
            nanosleep(&ts, NULL);
        }
    }
    return NULL;
}

/* ------------------------------------------------------------------------ */
/* Thread registration                                                       */
/* ------------------------------------------------------------------------ */

static void tl_register_thread(uint16_t flags, const void *start_routine)
{
    tl_self.tid = (uint32_t)syscall(SYS_gettid);
    tl_self.index = atomic_fetch_add(&g.next_index, 1);
    tl_self.registered = 1;
    tl_self.exit_kind = TL_EXIT_UNKNOWN;
    atomic_fetch_add(&g.live_threads, 1);
    atomic_fetch_add(&g.threads_started, 1);
    pthread_setspecific(g.exit_key, (void *)1);
    tl_emit(TL_EV_THREAD_START, flags, (uint64_t)pthread_self(),
            (uint64_t)(uintptr_t)start_routine, NULL, 0, tl_self.index);
}

/* Returns 1 if the calling thread's operation should be recorded. */
static inline int tl_trace(void)
{
    if (TL_LIKELY(!atomic_load_explicit(&g.enabled, memory_order_relaxed))) return 0;
    if (tl_self.internal) return 0;
    if (TL_UNLIKELY(!tl_self.registered)) tl_register_thread(TL_F_LATE, NULL);
    return 1;
}

/* Runs at thread exit (return, pthread_exit or cancellation). */
static void tl_thread_exit_dtor(void *unused)
{
    (void)unused;
    if (!tl_self.registered || !atomic_load(&g.enabled)) return;
    if (tl_self.cond_mutex) {
        /* Thread was cancelled inside a cond wait: glibc re-acquired the
         * mutex before running cleanup handlers. Record the inferred end. */
        tl_emit(TL_EV_COND_WAIT_END, TL_F_INFERRED, (uint64_t)(uintptr_t)tl_self.cond,
                (uint64_t)(uintptr_t)tl_self.cond_mutex, NULL, ECANCELED, 0);
        tl_self.cond_mutex = NULL;
    }
    tl_emit(TL_EV_THREAD_EXIT, 0, (uint64_t)pthread_self(), 0, NULL, 0, tl_self.exit_kind);
    tl_self.registered = 0;
    atomic_fetch_sub(&g.live_threads, 1);
}

struct tl_trampoline_arg {
    void *(*fn)(void *);
    void *arg;
};

static void *tl_trampoline(void *p)
{
    struct tl_trampoline_arg t = *(struct tl_trampoline_arg *)p;
    free(p);
    if (atomic_load(&g.enabled)) tl_register_thread(0, (const void *)t.fn);
    void *ret = t.fn(t.arg);
    tl_self.exit_kind = TL_EXIT_RETURN;
    return ret; /* THREAD_EXIT is emitted by the TLS-key destructor */
}

/* ------------------------------------------------------------------------ */
/* Initialisation and shutdown                                               */
/* ------------------------------------------------------------------------ */

static uint64_t tl_env_u64(const char *name, uint64_t def)
{
    const char *v = getenv(name);
    if (!v || !*v) return def;
    char *end = NULL;
    unsigned long long x = strtoull(v, &end, 0);
    return (end && *end == '\0') ? (uint64_t)x : def;
}

static int tl_find_bias(struct dl_phdr_info *info, size_t size, void *data)
{
    (void)size;
    *(uint64_t *)data = (uint64_t)info->dlpi_addr; /* first entry = main program */
    return 1;
}

static void tl_atfork_child(void)
{
    /* The child is not monitored: its ring copy has no flusher and sharing
     * the socket would interleave two processes' records. */
    atomic_store(&g.enabled, 0);
    if (g.fd >= 0) close(g.fd);
    g.fd = -1;
    tl_self.tid = 0;
}

static int tl_open_output(void)
{
    const char *sock = getenv("THREADLENS_SOCKET");
    const char *trace = getenv("THREADLENS_TRACE");
    if (sock && *sock) {
        int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
        if (fd < 0) return -1;
        struct sockaddr_un addr;
        memset(&addr, 0, sizeof addr);
        addr.sun_family = AF_UNIX;
        if (strlen(sock) >= sizeof addr.sun_path) { close(fd); errno = ENAMETOOLONG; return -1; }
        strcpy(addr.sun_path, sock);
        if (connect(fd, (struct sockaddr *)&addr, sizeof addr) != 0) { close(fd); return -1; }
        /* never let a stalled backend block the target forever */
        struct timeval tv = { 2, 0 };
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
        g.is_socket = 1;
        return fd;
    }
    if (trace && *trace) {
        g.is_socket = 0;
        return open(trace, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
    }
    errno = 0;
    return -1;
}

__attribute__((constructor)) static void tl_init(void)
{
    tl_self.internal = 1; /* nothing in here is traced */
    tl_resolve();

    int fd = tl_open_output();
    if (fd < 0) {
        if (errno) {
            const char *msg = "[threadlens] cannot open output; running unmonitored\n";
            ssize_t r = write(2, msg, strlen(msg));
            (void)r;
        }
        tl_self.internal = 0;
        return;
    }
    g.fd = fd;
    g.pid = getpid();
    g.token = tl_env_u64("THREADLENS_TOKEN", 0);

    uint64_t cap = tl_env_u64("THREADLENS_RING", TL_DEFAULT_RING);
    if (cap < 1024 || (cap & (cap - 1)) != 0) cap = TL_DEFAULT_RING;
    g.capacity = cap;
    g.mask = cap - 1;
    g.block_ns = (long)tl_env_u64("THREADLENS_BLOCK_US", TL_DEFAULT_BLOCK_US) * 1000L;
    g.heartbeat_ns = (long)tl_env_u64("THREADLENS_HEARTBEAT_MS", TL_DEFAULT_HB_MS) * 1000000L;
    if (g.heartbeat_ns < 10000000L) g.heartbeat_ns = 10000000L;

    g.ring = aligned_alloc(64, cap * sizeof(tl_cell));
    if (!g.ring) { close(fd); g.fd = -1; tl_self.internal = 0; return; }
    for (uint64_t i = 0; i < cap; i++) atomic_init(&g.ring[i].seq, i);

    dl_iterate_phdr(tl_find_bias, &g.load_bias);

    /* children of the target are not monitored (see limitations.md) */
    unsetenv("THREADLENS_SOCKET");
    unsetenv("THREADLENS_TRACE");
    unsetenv("THREADLENS_TOKEN");

    pthread_key_create(&g.exit_key, tl_thread_exit_dtor);
    pthread_atfork(NULL, NULL, tl_atfork_child);

    /* handshake: identifies the session and tells the backend how to
     * translate addresses (PIE load bias) */
    tl_oob(TL_EV_HELLO, 0, g.token, g.load_bias, TL_WIRE_VERSION, (uint32_t)cap);
    if (g.fd < 0) { tl_self.internal = 0; return; }

    atomic_store(&g.enabled, 1);
    tl_emit(TL_EV_PROCESS_START, 0, (uint64_t)getppid(), g.load_bias, NULL, 0, 0);

    tl_self.internal = 0;
    tl_register_thread(0, NULL); /* main thread = index 0 */
    char name[16] = { 0 };
    if (pthread_getname_np(pthread_self(), name, sizeof name) == 0)
        tl_emit_str(TL_EV_THREAD_NAME, 0, (uint64_t)pthread_self(), name);

    tl_self.internal = 1;
    if (TL_REAL(create)(&g.flusher, NULL, tl_flusher_main, NULL) == 0) {
        g.flusher_started = 1;
    } else {
        tl_disconnect("cannot start flusher thread");
    }
    tl_self.internal = 0;
}

__attribute__((destructor)) static void tl_fini(void)
{
    if (!g.flusher_started) return;
    if (atomic_load(&g.enabled)) {
        if (tl_self.registered) {
            /* thread calling exit(): normally main */
            tl_emit(TL_EV_THREAD_EXIT, 0, (uint64_t)pthread_self(), 0, NULL, 0, TL_EXIT_PROCESS);
            tl_self.registered = 0;
        }
        tl_emit(TL_EV_PROCESS_EXIT, 0, atomic_load(&g.dropped), 0, NULL, 0,
                atomic_load(&g.threads_started));
    }
    /* Let the flusher drain everything already published, then stop it. */
    tl_self.internal = 1;
    atomic_store(&g.stop, 1);
    TL_REAL(join)(g.flusher, NULL);
    atomic_store(&g.enabled, 0);
    uint64_t drops = atomic_load(&g.dropped);
    tl_oob(TL_EV_HEARTBEAT, g.deq, drops, atomic_load(&g.enq) - g.deq, 0,
           (uint32_t)atomic_load(&g.live_threads));
    if (g.fd >= 0) close(g.fd);
    g.fd = -1;
}

/* ------------------------------------------------------------------------ */
/* Public annotation API (see threadlens.h)                                  */
/* ------------------------------------------------------------------------ */

TL_EXPORT void threadlens_name_lock(const void *lock, const char *name)
{
    if (tl_trace()) tl_emit_str(TL_EV_LOCK_NAME, 0, (uint64_t)(uintptr_t)lock, name);
}

TL_EXPORT void threadlens_mark(const char *label)
{
    if (tl_trace()) tl_emit_str(TL_EV_USER_MARK, 0, 0, label);
}

TL_EXPORT int threadlens_active(void)
{
    return atomic_load(&g.enabled);
}

/* ------------------------------------------------------------------------ */
/* Mutex wrappers                                                            */
/* ------------------------------------------------------------------------ */

TL_EXPORT int pthread_mutex_init(pthread_mutex_t *m, const pthread_mutexattr_t *attr)
{
    int rc = TL_REAL(mutex_init)(m, attr);
    if (tl_trace())
        tl_emit(TL_EV_MUTEX_INIT, 0, (uint64_t)(uintptr_t)m, 0, __builtin_return_address(0),
                rc, rc == 0 ? tl_mutex_kind(m) : 0);
    return rc;
}

TL_EXPORT int pthread_mutex_destroy(pthread_mutex_t *m)
{
    int rc = TL_REAL(mutex_destroy)(m);
    if (tl_trace())
        tl_emit(TL_EV_MUTEX_DESTROY, 0, (uint64_t)(uintptr_t)m, 0, __builtin_return_address(0), rc, 0);
    return rc;
}

static inline void tl_lock_result(pthread_mutex_t *m, int rc, uint16_t flags, uint64_t t0,
                                  const void *caller, uint32_t kind)
{
    uint64_t waited = tl_now() - t0;
    if (rc == 0 || rc == EOWNERDEAD)
        tl_emit(TL_EV_LOCK_ACQUIRED, flags, (uint64_t)(uintptr_t)m, waited, caller, rc, kind);
    else
        tl_emit(TL_EV_LOCK_FAILED, flags, (uint64_t)(uintptr_t)m, waited, caller, rc, kind);
}

TL_EXPORT int pthread_mutex_lock(pthread_mutex_t *m)
{
    if (!tl_trace()) return TL_REAL(mutex_lock)(m);
    const void *caller = __builtin_return_address(0);
    uint32_t kind = tl_mutex_kind(m);
    uint64_t t0 = tl_now();
    tl_emit(TL_EV_LOCK_REQUEST, 0, (uint64_t)(uintptr_t)m, tl_mutex_owner(m), caller, 0, kind);
    int rc = TL_REAL(mutex_lock)(m);
    tl_lock_result(m, rc, 0, t0, caller, kind);
    return rc;
}

TL_EXPORT int pthread_mutex_trylock(pthread_mutex_t *m)
{
    if (!tl_trace()) return TL_REAL(mutex_trylock)(m);
    /* A trylock never blocks, so only its outcome is recorded. */
    uint32_t kind = tl_mutex_kind(m);
    uint64_t t0 = tl_now();
    int rc = TL_REAL(mutex_trylock)(m);
    tl_lock_result(m, rc, TL_F_TRY, t0, __builtin_return_address(0), kind);
    return rc;
}

TL_EXPORT int pthread_mutex_timedlock(pthread_mutex_t *m, const struct timespec *abstime)
{
    if (!tl_trace()) return TL_REAL(mutex_timedlock)(m, abstime);
    const void *caller = __builtin_return_address(0);
    uint32_t kind = tl_mutex_kind(m);
    uint64_t t0 = tl_now();
    tl_emit(TL_EV_LOCK_REQUEST, TL_F_TIMED, (uint64_t)(uintptr_t)m, tl_mutex_owner(m), caller, 0, kind);
    int rc = TL_REAL(mutex_timedlock)(m, abstime);
    tl_lock_result(m, rc, TL_F_TIMED, t0, caller, kind);
    return rc;
}

TL_EXPORT int pthread_mutex_clocklock(pthread_mutex_t *m, clockid_t clk, const struct timespec *abstime)
{
    if (!TL_REAL(mutex_clocklock)) return ENOSYS;
    if (!tl_trace()) return real.mutex_clocklock(m, clk, abstime);
    const void *caller = __builtin_return_address(0);
    uint32_t kind = tl_mutex_kind(m);
    uint64_t t0 = tl_now();
    tl_emit(TL_EV_LOCK_REQUEST, TL_F_TIMED, (uint64_t)(uintptr_t)m, tl_mutex_owner(m), caller, 0, kind);
    int rc = real.mutex_clocklock(m, clk, abstime);
    tl_lock_result(m, rc, TL_F_TIMED, t0, caller, kind);
    return rc;
}

TL_EXPORT int pthread_mutex_unlock(pthread_mutex_t *m)
{
    if (!tl_trace()) return TL_REAL(mutex_unlock)(m);
    const void *caller = __builtin_return_address(0);
    if (TL_UNLIKELY(tl_self.cond_mutex == m)) {
        /* cancellation cleanup after a cond wait: glibc re-acquired m */
        tl_emit(TL_EV_COND_WAIT_END, TL_F_INFERRED, (uint64_t)(uintptr_t)tl_self.cond,
                (uint64_t)(uintptr_t)m, caller, ECANCELED, 0);
        tl_self.cond_mutex = NULL;
    }
    uint32_t kind = tl_mutex_kind(m);
    /* recorded BEFORE the real unlock: see ordering rule at top of file */
    tl_emit(TL_EV_LOCK_RELEASE, 0, (uint64_t)(uintptr_t)m, 0, caller, 0, kind);
    int rc = TL_REAL(mutex_unlock)(m);
    if (rc != 0) tl_emit(TL_EV_UNLOCK_FAILED, 0, (uint64_t)(uintptr_t)m, 0, caller, rc, kind);
    return rc;
}

/* ------------------------------------------------------------------------ */
/* Condition variables                                                       */
/* ------------------------------------------------------------------------ */

static inline void tl_cond_begin(pthread_cond_t *c, pthread_mutex_t *m, uint16_t flags, const void *caller)
{
    tl_emit(TL_EV_COND_WAIT_BEGIN, flags, (uint64_t)(uintptr_t)c, (uint64_t)(uintptr_t)m,
            caller, 0, tl_mutex_kind(m));
    tl_self.cond = c;
    tl_self.cond_mutex = m;
}

static inline void tl_cond_end(pthread_cond_t *c, pthread_mutex_t *m, uint16_t flags, int rc, const void *caller)
{
    tl_self.cond_mutex = NULL;
    tl_self.cond = NULL;
    tl_emit(TL_EV_COND_WAIT_END, flags, (uint64_t)(uintptr_t)c, (uint64_t)(uintptr_t)m, caller, rc, 0);
}

TL_EXPORT int pthread_cond_wait(pthread_cond_t *c, pthread_mutex_t *m)
{
    if (!tl_trace()) return TL_REAL(cond_wait)(c, m);
    const void *caller = __builtin_return_address(0);
    tl_cond_begin(c, m, 0, caller);
    int rc = TL_REAL(cond_wait)(c, m);
    tl_cond_end(c, m, 0, rc, caller);
    return rc;
}

TL_EXPORT int pthread_cond_timedwait(pthread_cond_t *c, pthread_mutex_t *m, const struct timespec *abstime)
{
    if (!tl_trace()) return TL_REAL(cond_timedwait)(c, m, abstime);
    const void *caller = __builtin_return_address(0);
    tl_cond_begin(c, m, TL_F_TIMED, caller);
    int rc = TL_REAL(cond_timedwait)(c, m, abstime);
    tl_cond_end(c, m, TL_F_TIMED, rc, caller);
    return rc;
}

TL_EXPORT int pthread_cond_clockwait(pthread_cond_t *c, pthread_mutex_t *m, clockid_t clk,
                                     const struct timespec *abstime)
{
    if (!TL_REAL(cond_clockwait)) return ENOSYS;
    if (!tl_trace()) return real.cond_clockwait(c, m, clk, abstime);
    const void *caller = __builtin_return_address(0);
    tl_cond_begin(c, m, TL_F_TIMED, caller);
    int rc = real.cond_clockwait(c, m, clk, abstime);
    tl_cond_end(c, m, TL_F_TIMED, rc, caller);
    return rc;
}

TL_EXPORT int pthread_cond_signal(pthread_cond_t *c)
{
    if (tl_trace())
        tl_emit(TL_EV_COND_SIGNAL, 0, (uint64_t)(uintptr_t)c, 0, __builtin_return_address(0), 0, 0);
    return TL_REAL(cond_signal)(c);
}

TL_EXPORT int pthread_cond_broadcast(pthread_cond_t *c)
{
    if (tl_trace())
        tl_emit(TL_EV_COND_BROADCAST, 0, (uint64_t)(uintptr_t)c, 0, __builtin_return_address(0), 0, 0);
    return TL_REAL(cond_broadcast)(c);
}

/* ------------------------------------------------------------------------ */
/* Threads                                                                   */
/* ------------------------------------------------------------------------ */

TL_EXPORT int pthread_create(pthread_t *th, const pthread_attr_t *attr, void *(*fn)(void *), void *arg)
{
    if (!tl_trace()) return TL_REAL(create)(th, attr, fn, arg);
    struct tl_trampoline_arg *t = malloc(sizeof *t);
    if (!t) return TL_REAL(create)(th, attr, fn, arg);
    t->fn = fn;
    t->arg = arg;
    int rc = TL_REAL(create)(th, attr, tl_trampoline, t);
    if (rc != 0) free(t);
    tl_emit(TL_EV_THREAD_CREATE, 0, rc == 0 ? (uint64_t)*th : 0, (uint64_t)(uintptr_t)fn,
            __builtin_return_address(0), rc, 0);
    return rc;
}

TL_EXPORT int pthread_join(pthread_t th, void **ret)
{
    if (!tl_trace()) return TL_REAL(join)(th, ret);
    const void *caller = __builtin_return_address(0);
    tl_emit(TL_EV_JOIN_BEGIN, 0, (uint64_t)th, 0, caller, 0, 0);
    int rc = TL_REAL(join)(th, ret);
    tl_emit(TL_EV_JOIN_END, 0, (uint64_t)th, 0, caller, rc, 0);
    return rc;
}

TL_EXPORT int pthread_detach(pthread_t th)
{
    int rc = TL_REAL(detach)(th);
    if (tl_trace())
        tl_emit(TL_EV_THREAD_DETACH, 0, (uint64_t)th, 0, __builtin_return_address(0), rc, 0);
    return rc;
}

TL_EXPORT int pthread_cancel(pthread_t th)
{
    if (tl_trace())
        tl_emit(TL_EV_CANCEL_REQUEST, 0, (uint64_t)th, 0, __builtin_return_address(0), 0, 0);
    return TL_REAL(cancel)(th);
}

TL_EXPORT void pthread_exit(void *retval)
{
    tl_self.exit_kind = TL_EXIT_PTHREAD_EXIT;
    TL_REAL(exit_)(retval);
    __builtin_unreachable();
}

TL_EXPORT int pthread_setname_np(pthread_t th, const char *name)
{
    int rc = TL_REAL(setname)(th, name);
    if (rc == 0 && tl_trace()) tl_emit_str(TL_EV_THREAD_NAME, 0, (uint64_t)th, name);
    return rc;
}

TL_EXPORT int pthread_barrier_wait(pthread_barrier_t *b)
{
    if (!tl_trace()) return TL_REAL(barrier_wait)(b);
    const void *caller = __builtin_return_address(0);
    tl_emit(TL_EV_BARRIER_WAIT_BEGIN, 0, (uint64_t)(uintptr_t)b, 0, caller, 0, 0);
    int rc = TL_REAL(barrier_wait)(b);
    int serial = (rc == PTHREAD_BARRIER_SERIAL_THREAD);
    tl_emit(TL_EV_BARRIER_WAIT_END, serial ? TL_F_SERIAL : 0, (uint64_t)(uintptr_t)b, 0, caller,
            serial ? 0 : rc, 0);
    return rc;
}

/* ------------------------------------------------------------------------ */
/* Read-write locks                                                          */
/* ------------------------------------------------------------------------ */

TL_EXPORT int pthread_rwlock_init(pthread_rwlock_t *rw, const pthread_rwlockattr_t *attr)
{
    int rc = TL_REAL(rwlock_init)(rw, attr);
    if (tl_trace())
        tl_emit(TL_EV_RWLOCK_INIT, 0, (uint64_t)(uintptr_t)rw, 0, __builtin_return_address(0),
                rc, rc == 0 ? tl_rw_kind(rw) : 0);
    return rc;
}

TL_EXPORT int pthread_rwlock_destroy(pthread_rwlock_t *rw)
{
    int rc = TL_REAL(rwlock_destroy)(rw);
    if (tl_trace())
        tl_emit(TL_EV_RWLOCK_DESTROY, 0, (uint64_t)(uintptr_t)rw, 0, __builtin_return_address(0), rc, 0);
    return rc;
}

/* Request (before the real call) for the blocking variants. Returns t0. */
static inline uint64_t tl_rw_request(pthread_rwlock_t *rw, uint32_t extra, uint16_t flags, const void *caller)
{
    uint64_t t0 = tl_now();
    tl_emit(TL_EV_RW_REQUEST, flags, (uint64_t)(uintptr_t)rw, tl_rw_writer(rw), caller, 0, extra);
    return t0;
}

static inline void tl_rw_result(pthread_rwlock_t *rw, int rc, uint16_t flags, uint64_t t0,
                                const void *caller, uint32_t extra)
{
    uint64_t waited = tl_now() - t0;
    tl_emit(rc == 0 ? TL_EV_RW_ACQUIRED : TL_EV_RW_FAILED, flags, (uint64_t)(uintptr_t)rw, waited,
            caller, rc, extra);
}

TL_EXPORT int pthread_rwlock_rdlock(pthread_rwlock_t *rw)
{
    if (!tl_trace()) return TL_REAL(rwlock_rdlock)(rw);
    const void *caller = __builtin_return_address(0);
    uint32_t extra = TL_RW_EXTRA(TL_RW_READ, tl_rw_kind(rw));
    uint64_t t0 = tl_rw_request(rw, extra, 0, caller);
    int rc = TL_REAL(rwlock_rdlock)(rw);
    tl_rw_result(rw, rc, 0, t0, caller, extra);
    return rc;
}

TL_EXPORT int pthread_rwlock_wrlock(pthread_rwlock_t *rw)
{
    if (!tl_trace()) return TL_REAL(rwlock_wrlock)(rw);
    const void *caller = __builtin_return_address(0);
    uint32_t extra = TL_RW_EXTRA(TL_RW_WRITE, tl_rw_kind(rw));
    uint64_t t0 = tl_rw_request(rw, extra, 0, caller);
    int rc = TL_REAL(rwlock_wrlock)(rw);
    tl_rw_result(rw, rc, 0, t0, caller, extra);
    return rc;
}

TL_EXPORT int pthread_rwlock_tryrdlock(pthread_rwlock_t *rw)
{
    if (!tl_trace()) return TL_REAL(rwlock_tryrdlock)(rw);
    /* never blocks: only the outcome is recorded (like pthread_mutex_trylock) */
    uint32_t extra = TL_RW_EXTRA(TL_RW_READ, tl_rw_kind(rw));
    uint64_t t0 = tl_now();
    int rc = TL_REAL(rwlock_tryrdlock)(rw);
    tl_rw_result(rw, rc, TL_F_TRY, t0, __builtin_return_address(0), extra);
    return rc;
}

TL_EXPORT int pthread_rwlock_trywrlock(pthread_rwlock_t *rw)
{
    if (!tl_trace()) return TL_REAL(rwlock_trywrlock)(rw);
    uint32_t extra = TL_RW_EXTRA(TL_RW_WRITE, tl_rw_kind(rw));
    uint64_t t0 = tl_now();
    int rc = TL_REAL(rwlock_trywrlock)(rw);
    tl_rw_result(rw, rc, TL_F_TRY, t0, __builtin_return_address(0), extra);
    return rc;
}

TL_EXPORT int pthread_rwlock_timedrdlock(pthread_rwlock_t *rw, const struct timespec *abstime)
{
    if (!tl_trace()) return TL_REAL(rwlock_timedrdlock)(rw, abstime);
    const void *caller = __builtin_return_address(0);
    uint32_t extra = TL_RW_EXTRA(TL_RW_READ, tl_rw_kind(rw));
    uint64_t t0 = tl_rw_request(rw, extra, TL_F_TIMED, caller);
    int rc = TL_REAL(rwlock_timedrdlock)(rw, abstime);
    tl_rw_result(rw, rc, TL_F_TIMED, t0, caller, extra);
    return rc;
}

TL_EXPORT int pthread_rwlock_timedwrlock(pthread_rwlock_t *rw, const struct timespec *abstime)
{
    if (!tl_trace()) return TL_REAL(rwlock_timedwrlock)(rw, abstime);
    const void *caller = __builtin_return_address(0);
    uint32_t extra = TL_RW_EXTRA(TL_RW_WRITE, tl_rw_kind(rw));
    uint64_t t0 = tl_rw_request(rw, extra, TL_F_TIMED, caller);
    int rc = TL_REAL(rwlock_timedwrlock)(rw, abstime);
    tl_rw_result(rw, rc, TL_F_TIMED, t0, caller, extra);
    return rc;
}

TL_EXPORT int pthread_rwlock_clockrdlock(pthread_rwlock_t *rw, clockid_t clk, const struct timespec *abstime)
{
    if (!TL_REAL(rwlock_clockrdlock)) return ENOSYS;
    if (!tl_trace()) return real.rwlock_clockrdlock(rw, clk, abstime);
    const void *caller = __builtin_return_address(0);
    uint32_t extra = TL_RW_EXTRA(TL_RW_READ, tl_rw_kind(rw));
    uint64_t t0 = tl_rw_request(rw, extra, TL_F_TIMED, caller);
    int rc = real.rwlock_clockrdlock(rw, clk, abstime);
    tl_rw_result(rw, rc, TL_F_TIMED, t0, caller, extra);
    return rc;
}

TL_EXPORT int pthread_rwlock_clockwrlock(pthread_rwlock_t *rw, clockid_t clk, const struct timespec *abstime)
{
    if (!TL_REAL(rwlock_clockwrlock)) return ENOSYS;
    if (!tl_trace()) return real.rwlock_clockwrlock(rw, clk, abstime);
    const void *caller = __builtin_return_address(0);
    uint32_t extra = TL_RW_EXTRA(TL_RW_WRITE, tl_rw_kind(rw));
    uint64_t t0 = tl_rw_request(rw, extra, TL_F_TIMED, caller);
    int rc = real.rwlock_clockwrlock(rw, clk, abstime);
    tl_rw_result(rw, rc, TL_F_TIMED, t0, caller, extra);
    return rc;
}

TL_EXPORT int pthread_rwlock_unlock(pthread_rwlock_t *rw)
{
    if (!tl_trace()) return TL_REAL(rwlock_unlock)(rw);
    /* glibc decides between read and write unlock the same way: by comparing
     * __cur_writer with the caller's TID. The backend uses its own ownership
     * state and only cross-checks this hint. */
    uint32_t mode = tl_rw_writer(rw) == tl_gettid() ? TL_RW_WRITE : TL_RW_READ;
    /* recorded BEFORE the real unlock: see ordering rule at top of file */
    tl_emit(TL_EV_RW_RELEASE, 0, (uint64_t)(uintptr_t)rw, 0, __builtin_return_address(0), 0,
            TL_RW_EXTRA(mode, tl_rw_kind(rw)));
    return TL_REAL(rwlock_unlock)(rw); /* glibc's rwlock unlock always returns 0 */
}

/* ------------------------------------------------------------------------ */
/* POSIX semaphores (unnamed and named: sem_open'ed semaphores are waited on  */
/* and posted with the same functions; only their creation is not seen).     */
/* These functions report errors through errno, which the wrappers preserve. */
/* ------------------------------------------------------------------------ */

TL_EXPORT int sem_init(sem_t *s, int pshared, unsigned int value)
{
    int rc = TL_REAL(sem_init)(s, pshared, value);
    if (tl_trace()) {
        int err = rc == 0 ? 0 : errno;
        tl_emit(TL_EV_SEM_INIT, 0, (uint64_t)(uintptr_t)s, value, __builtin_return_address(0), err,
                (uint32_t)pshared);
        if (rc != 0) errno = err;
    }
    return rc;
}

TL_EXPORT int sem_destroy(sem_t *s)
{
    int rc = TL_REAL(sem_destroy)(s);
    if (tl_trace()) {
        int err = rc == 0 ? 0 : errno;
        tl_emit(TL_EV_SEM_DESTROY, 0, (uint64_t)(uintptr_t)s, 0, __builtin_return_address(0), err, 0);
        if (rc != 0) errno = err;
    }
    return rc;
}

static inline void tl_sem_end(sem_t *s, int rc, uint16_t flags, uint64_t t0, const void *caller)
{
    int err = rc == 0 ? 0 : errno;
    tl_emit(TL_EV_SEM_WAIT_END, flags, (uint64_t)(uintptr_t)s, tl_now() - t0, caller, err, 0);
    if (err) errno = err;
}

TL_EXPORT int sem_wait(sem_t *s)
{
    if (!tl_trace()) return TL_REAL(sem_wait)(s);
    const void *caller = __builtin_return_address(0);
    uint64_t t0 = tl_now();
    tl_emit(TL_EV_SEM_WAIT_BEGIN, 0, (uint64_t)(uintptr_t)s, tl_sem_value(s), caller, 0, 0);
    int rc = TL_REAL(sem_wait)(s);
    tl_sem_end(s, rc, 0, t0, caller);
    return rc;
}

TL_EXPORT int sem_trywait(sem_t *s)
{
    if (!tl_trace()) return TL_REAL(sem_trywait)(s);
    /* never blocks: only the outcome is recorded */
    uint64_t t0 = tl_now();
    int rc = TL_REAL(sem_trywait)(s);
    tl_sem_end(s, rc, TL_F_TRY, t0, __builtin_return_address(0));
    return rc;
}

TL_EXPORT int sem_timedwait(sem_t *s, const struct timespec *abstime)
{
    if (!tl_trace()) return TL_REAL(sem_timedwait)(s, abstime);
    const void *caller = __builtin_return_address(0);
    uint64_t t0 = tl_now();
    tl_emit(TL_EV_SEM_WAIT_BEGIN, TL_F_TIMED, (uint64_t)(uintptr_t)s, tl_sem_value(s), caller, 0, 0);
    int rc = TL_REAL(sem_timedwait)(s, abstime);
    tl_sem_end(s, rc, TL_F_TIMED, t0, caller);
    return rc;
}

TL_EXPORT int sem_clockwait(sem_t *s, clockid_t clk, const struct timespec *abstime)
{
    if (!TL_REAL(sem_clockwait)) { errno = ENOSYS; return -1; }
    if (!tl_trace()) return real.sem_clockwait(s, clk, abstime);
    const void *caller = __builtin_return_address(0);
    uint64_t t0 = tl_now();
    tl_emit(TL_EV_SEM_WAIT_BEGIN, TL_F_TIMED, (uint64_t)(uintptr_t)s, tl_sem_value(s), caller, 0, 0);
    int rc = real.sem_clockwait(s, clk, abstime);
    tl_sem_end(s, rc, TL_F_TIMED, t0, caller);
    return rc;
}

TL_EXPORT int sem_post(sem_t *s)
{
    if (!tl_trace()) return TL_REAL(sem_post)(s);
    const void *caller = __builtin_return_address(0);
    /* recorded BEFORE the real post: see ordering rule at top of file */
    tl_emit(TL_EV_SEM_POST, 0, (uint64_t)(uintptr_t)s, tl_sem_value(s), caller, 0, 0);
    int rc = TL_REAL(sem_post)(s);
    if (rc != 0) {
        int err = errno;
        tl_emit(TL_EV_SEM_POST_FAILED, 0, (uint64_t)(uintptr_t)s, 0, caller, err, 0);
        errno = err;
    }
    return rc;
}
