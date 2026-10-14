/*
 * tracecheck.c — validates a ThreadLens binary trace file.
 *
 * Checks the invariants the analysis engine relies on:
 *   1. every record has the right magic/version and a known type;
 *   2. the first record is HELLO;
 *   3. in-band sequence numbers are contiguous from 0 (unless loss reported);
 *   4. every blocking LOCK_ACQUIRED follows an unmatched LOCK_REQUEST of the
 *      same thread on the same mutex;
 *   5. lock ownership is consistent: no thread acquires a non-recursive mutex
 *      that the trace says another thread still holds (this is exactly the
 *      "release recorded before unlock, acquire recorded after lock" rule);
 *   6. THREAD_START and THREAD_EXIT records balance.
 *
 * usage: tl_tracecheck <trace.tlt> [min_threads]
 */
#define _GNU_SOURCE
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "threadlens/tl_wire.h"

#define MAX_MUTEX 4096
#define MAX_THREADS 4096

struct mstate { uint64_t addr; uint32_t owner; int count; };
struct tstate { uint32_t tid; uint64_t pending_req; int has_pending; };

static struct mstate mtx[MAX_MUTEX];
static int n_mtx;
static struct tstate thr[MAX_THREADS];
static int n_thr;

static struct mstate *get_mutex(uint64_t addr)
{
    for (int i = 0; i < n_mtx; i++) if (mtx[i].addr == addr) return &mtx[i];
    if (n_mtx == MAX_MUTEX) return NULL;
    mtx[n_mtx].addr = addr;
    return &mtx[n_mtx++];
}

static struct tstate *get_thread(uint32_t tid)
{
    for (int i = 0; i < n_thr; i++) if (thr[i].tid == tid) return &thr[i];
    if (n_thr == MAX_THREADS) return NULL;
    thr[n_thr].tid = tid;
    return &thr[n_thr++];
}

static int is_recursive(uint32_t kind) { return (kind & 3u) == 1u; }

int main(int argc, char **argv)
{
    if (argc < 2) { fprintf(stderr, "usage: %s trace.tlt [min_threads]\n", argv[0]); return 2; }
    int min_threads = argc > 2 ? atoi(argv[2]) : 1;
    FILE *f = fopen(argv[1], "rb");
    if (!f) { perror(argv[1]); return 2; }

    tl_wire_event e;
    uint64_t records = 0, expect_seq = 0, errors = 0, lost = 0;
    uint64_t by_type[256] = { 0 };
    long starts = 0, exits = 0;

#define FAIL(...) do { errors++; if (errors <= 20) { fprintf(stderr, "seq %llu: ", (unsigned long long)e.seq); fprintf(stderr, __VA_ARGS__); fputc('\n', stderr); } } while (0)

    while (fread(&e, sizeof e, 1, f) == 1) {
        records++;
        if (e.magic != TL_WIRE_MAGIC || e.version != TL_WIRE_VERSION) { FAIL("bad magic/version"); break; }
        by_type[e.type]++;
        if (records == 1 && e.type != TL_EV_HELLO) FAIL("first record is not HELLO");
        if (e.flags & TL_F_OOB) {
            if (e.type == TL_EV_EVENTS_LOST) lost = e.obj;
            continue;
        }
        if (e.seq != expect_seq && lost == 0) FAIL("sequence gap: expected %llu", (unsigned long long)expect_seq);
        expect_seq = e.seq + 1;

        struct tstate *t = get_thread(e.tid);
        struct mstate *m;
        switch (e.type) {
        case TL_EV_THREAD_START: starts++; break;
        case TL_EV_THREAD_EXIT: exits++; break;
        case TL_EV_LOCK_REQUEST:
            if (t->has_pending) FAIL("tid %u: second request while one is pending", e.tid);
            t->has_pending = 1;
            t->pending_req = e.obj;
            break;
        case TL_EV_LOCK_ACQUIRED:
            if (!(e.flags & TL_F_TRY)) {
                if (!t->has_pending || t->pending_req != e.obj) FAIL("tid %u: acquire without request", e.tid);
                t->has_pending = 0;
            }
            m = get_mutex(e.obj);
            if (m && m->count > 0 && m->owner != e.tid)
                FAIL("tid %u acquired mutex %#llx still held by tid %u", e.tid, (unsigned long long)e.obj, m->owner);
            if (m) {
                if (m->count > 0 && m->owner == e.tid && !is_recursive(e.u.v.extra))
                    FAIL("tid %u re-acquired non-recursive mutex", e.tid);
                m->owner = e.tid;
                m->count++;
            }
            break;
        case TL_EV_LOCK_FAILED:
            if (!(e.flags & TL_F_TRY)) t->has_pending = 0;
            break;
        case TL_EV_LOCK_RELEASE:
            m = get_mutex(e.obj);
            if (m && m->count > 0 && m->owner == e.tid) m->count--;
            break;
        case TL_EV_UNLOCK_FAILED:
            /* the preceding RELEASE did not happen; restore if we just decremented */
            m = get_mutex(e.obj);
            if (m && m->count == 0 && m->owner == e.tid) m->count = 1;
            break;
        case TL_EV_COND_WAIT_BEGIN:
            m = get_mutex(e.u.v.aux);
            if (m && m->count > 0 && m->owner == e.tid) m->count--;
            break;
        case TL_EV_COND_WAIT_END:
            m = get_mutex(e.u.v.aux);
            if (m) { m->owner = e.tid; m->count++; }
            break;
        default: break;
        }
    }
    fclose(f);

    printf("records: %llu (in-band %llu), threads started: %ld, exited: %ld, lost: %llu\n",
           (unsigned long long)records, (unsigned long long)expect_seq, starts, exits,
           (unsigned long long)lost);
    printf("requests %llu, acquired %llu, failed %llu, released %llu, cond begin %llu, cond end %llu\n",
           (unsigned long long)by_type[TL_EV_LOCK_REQUEST], (unsigned long long)by_type[TL_EV_LOCK_ACQUIRED],
           (unsigned long long)by_type[TL_EV_LOCK_FAILED], (unsigned long long)by_type[TL_EV_LOCK_RELEASE],
           (unsigned long long)by_type[TL_EV_COND_WAIT_BEGIN], (unsigned long long)by_type[TL_EV_COND_WAIT_END]);
    if (starts < min_threads) { fprintf(stderr, "expected at least %d threads\n", min_threads); errors++; }
    if (starts != exits) { fprintf(stderr, "thread start/exit imbalance\n"); errors++; }
    if (by_type[TL_EV_HEARTBEAT] == 0) { fprintf(stderr, "no heartbeat records\n"); errors++; }
    printf("%s (%llu violations)\n", errors ? "FAILED" : "OK", (unsigned long long)errors);
    return errors ? 1 : 0;
}
