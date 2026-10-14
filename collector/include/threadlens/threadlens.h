/*
 * threadlens.h — optional annotation API for programs monitored by ThreadLens.
 *
 * Programs do NOT need this header to be monitored: all pthread calls are
 * intercepted through LD_PRELOAD. The annotations only add human-readable
 * names and markers to the trace.
 *
 * The functions are looked up at runtime with dlsym(RTLD_DEFAULT, ...), so the
 * same binary runs unchanged without the collector (the calls become no-ops).
 * Requires <dlfcn.h>; on glibc >= 2.34 dlsym lives in libc (no -ldl needed).
 */
#ifndef THREADLENS_THREADLENS_H
#define THREADLENS_THREADLENS_H

#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include <dlfcn.h>
#include <pthread.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef void (*tl_name_lock_fn)(const void *, const char *);
typedef void (*tl_mark_fn)(const char *);
typedef int (*tl_active_fn)(void);

/* Give a mutex a readable name ("account[0]") in the dashboard. */
static inline void tl_name_lock(const void *lock, const char *name)
{
    tl_name_lock_fn fn = (tl_name_lock_fn)dlsym(RTLD_DEFAULT, "threadlens_name_lock");
    if (fn) fn(lock, name);
}

/* Emit a user marker ("phase 2 starts") into the event stream. */
static inline void tl_mark(const char *label)
{
    tl_mark_fn fn = (tl_mark_fn)dlsym(RTLD_DEFAULT, "threadlens_mark");
    if (fn) fn(label);
}

/* 1 if the collector is loaded and streaming. */
static inline int tl_active(void)
{
    tl_active_fn fn = (tl_active_fn)dlsym(RTLD_DEFAULT, "threadlens_active");
    return fn ? fn() : 0;
}

#ifdef __cplusplus
}
#endif

#endif /* THREADLENS_THREADLENS_H */
