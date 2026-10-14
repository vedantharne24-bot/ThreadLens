/*
 * tl_wire.h — ThreadLens wire format (protocol version 1).
 *
 * Every record is exactly 64 bytes, little-endian, no padding. The Python
 * backend decodes the same layout with struct format "<HBBHHQQIIQQQiI"
 * (see backend/app/models/wire.py). Keep both in sync.
 *
 *  offset size field     meaning
 *  0      2    magic     0x4C54 ('T','L' in little-endian memory order)
 *  2      1    version   TL_WIRE_VERSION
 *  3      1    type      tl_event_type
 *  4      2    flags     TL_F_* bit set
 *  6      2    reserved  0
 *  8      8    seq       per-process total order (ring slot index); for OOB records: see type
 *  16     8    ts_ns     CLOCK_MONOTONIC nanoseconds
 *  24     4    pid       process id
 *  28     4    tid       kernel thread id (gettid)
 *  32     8    obj       primary object (mutex/cond/barrier address, pthread_t, ...)
 *  40     8    aux       secondary value (see per-type notes)
 *  48     8    caller    return address of the intercepted call (for symbolization)
 *  56     4    result    return code / errno value (0 = success)
 *  60     4    extra     small per-type value (mutex kind, thread index, ...)
 *
 * String-carrying records (THREAD_NAME, LOCK_NAME, USER_MARK, HELLO) store a
 * NUL-padded string in bytes [40, 64) (24 bytes) instead of aux/caller/result/extra.
 */
#ifndef THREADLENS_TL_WIRE_H
#define THREADLENS_TL_WIRE_H

#include <stdint.h>

#define TL_WIRE_MAGIC   0x4C54u
#define TL_WIRE_VERSION 1u
#define TL_WIRE_SIZE    64u
#define TL_WIRE_STRLEN  24u

typedef enum tl_event_type {
    /* process / collector lifecycle */
    TL_EV_HELLO            = 1,  /* OOB. obj=session token, aux=exe load bias, extra=ring capacity, result=protocol */
    TL_EV_PROCESS_START    = 2,  /* obj=ppid, aux=exe load bias */
    TL_EV_PROCESS_EXIT     = 3,  /* obj=total dropped events, extra=threads started */
    TL_EV_HEARTBEAT        = 4,  /* OOB. seq=next undelivered seq, obj=total dropped, aux=ring depth, extra=live threads */
    TL_EV_EVENTS_LOST      = 5,  /* OOB. obj=total dropped, aux=dropped since previous report */

    /* thread lifecycle */
    TL_EV_THREAD_CREATE    = 10, /* tid=creator. obj=child pthread_t, aux=start routine, result=rc */
    TL_EV_THREAD_START     = 11, /* obj=pthread_self(), aux=start routine, extra=thread index */
    TL_EV_THREAD_EXIT      = 12, /* obj=pthread_self(), extra=tl_exit_kind */
    TL_EV_THREAD_NAME      = 13, /* obj=named pthread_t, string payload */
    TL_EV_JOIN_BEGIN       = 14, /* obj=target pthread_t */
    TL_EV_JOIN_END         = 15, /* obj=target pthread_t, result=rc */
    TL_EV_THREAD_DETACH    = 16, /* obj=target pthread_t, result=rc */
    TL_EV_CANCEL_REQUEST   = 17, /* obj=target pthread_t, result=rc */

    /* mutexes */
    TL_EV_MUTEX_INIT       = 20, /* obj=mutex, extra=kind, result=rc */
    TL_EV_MUTEX_DESTROY    = 21, /* obj=mutex, result=rc */
    TL_EV_LOCK_REQUEST     = 22, /* obj=mutex, aux=owner tid seen in glibc mutex, extra=kind, flags TIMED/TRY */
    TL_EV_LOCK_ACQUIRED    = 23, /* obj=mutex, aux=wait ns, extra=kind, result=0|EOWNERDEAD, flags TIMED/TRY */
    TL_EV_LOCK_FAILED      = 24, /* obj=mutex, aux=wait ns, result=errno (EBUSY, ETIMEDOUT, EDEADLK, EINVAL...) */
    TL_EV_LOCK_RELEASE     = 25, /* obj=mutex, extra=kind (recorded BEFORE the real unlock) */
    TL_EV_UNLOCK_FAILED    = 26, /* obj=mutex, result=errno (EPERM...) */
    TL_EV_LOCK_NAME        = 27, /* obj=mutex, string payload (tl_name_lock) */

    /* condition variables */
    TL_EV_COND_WAIT_BEGIN  = 30, /* obj=cond, aux=mutex, flags TIMED (mutex released inside) */
    TL_EV_COND_WAIT_END    = 31, /* obj=cond, aux=mutex, result=rc (mutex re-acquired) */
    TL_EV_COND_SIGNAL      = 32, /* obj=cond */
    TL_EV_COND_BROADCAST   = 33, /* obj=cond */

    /* barriers (state only, never wait-for edges) */
    TL_EV_BARRIER_WAIT_BEGIN = 40, /* obj=barrier */
    TL_EV_BARRIER_WAIT_END   = 41, /* obj=barrier, result=rc (PTHREAD_BARRIER_SERIAL_THREAD = -1 -> stored as 0, extra=1) */

    /* annotations */
    TL_EV_USER_MARK        = 50  /* string payload (tl_mark) */
} tl_event_type;

/* flags */
#define TL_F_TIMED     0x0001u /* timed variant (timedlock / clocklock / cond_timedwait) */
#define TL_F_TRY       0x0002u /* trylock variant */
#define TL_F_OOB       0x0004u /* out-of-band collector record; seq is not a ring position */
#define TL_F_INFERRED  0x0008u /* event inferred by the collector (e.g. cond wait ended by cancellation) */
#define TL_F_LATE      0x0010u /* thread discovered late (not created through an intercepted pthread_create) */
#define TL_F_SERIAL    0x0020u /* barrier: this thread got PTHREAD_BARRIER_SERIAL_THREAD */

typedef enum tl_exit_kind {
    TL_EXIT_RETURN       = 0, /* start routine returned */
    TL_EXIT_PTHREAD_EXIT = 1, /* pthread_exit() */
    TL_EXIT_UNKNOWN      = 2, /* cancellation or other path (TLS destructor saw no explicit kind) */
    TL_EXIT_PROCESS      = 3  /* process exit while thread still alive (main or others) */
} tl_exit_kind;

#pragma pack(push, 1)
typedef struct tl_wire_event {
    uint16_t magic;
    uint8_t  version;
    uint8_t  type;
    uint16_t flags;
    uint16_t reserved;
    uint64_t seq;
    uint64_t ts_ns;
    uint32_t pid;
    uint32_t tid;
    uint64_t obj;
    union {
        struct {
            uint64_t aux;
            uint64_t caller;
            int32_t  result;
            uint32_t extra;
        } v;
        char str[TL_WIRE_STRLEN];
    } u;
} tl_wire_event;
#pragma pack(pop)

#if defined(__STDC_VERSION__) && __STDC_VERSION__ >= 201112L
_Static_assert(sizeof(tl_wire_event) == TL_WIRE_SIZE, "wire record must be 64 bytes");
#elif defined(__cplusplus)
static_assert(sizeof(tl_wire_event) == TL_WIRE_SIZE, "wire record must be 64 bytes");
#endif

#endif /* THREADLENS_TL_WIRE_H */
