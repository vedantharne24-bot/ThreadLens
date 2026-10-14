/*
 * rwlock_deadlock.c — reader/writer deadlocks with pthread_rwlock_t.
 *
 * A routing table and a statistics block are each protected by a read-write
 * lock. Two lookup threads read the table and then record a hit in the
 * statistics (table:read -> stats:write). A rebalancer reads the statistics
 * and then rewrites the table (stats:read -> table:write): the opposite order.
 * The timed gate lets every thread take its first lock:
 *
 *   lookup-1   holds table (read)   waits for stats (write)  <- rebalancer reads stats
 *   lookup-2   holds table (read)   still working; releases later and exits
 *   rebalancer holds stats (read)   waits for table (write)  <- BOTH lookups read table
 *
 * A writer must wait for EVERY reader, so the rebalancer has two outgoing
 * wait-for edges (out-degree 2): the wait-for graph is no longer a functional
 * graph and ThreadLens searches it depth-first. lookup-2 eventually releases,
 * but lookup-1 never can: lookup-1 <-> rebalancer is a circular wait.
 *
 * "--fixed": the rebalancer takes the locks in the same global order as the
 * lookups (table, then stats). It blocks on the table until the lookups are
 * done with it; the gate times out and every thread completes.
 *
 * "upgrade": an updater read-locks a cache, finds a stale entry and tries to
 * upgrade to a write lock IN PLACE while still holding its read lock. A writer
 * waits for every reader, including itself, so it waits forever: a self-loop
 * in the wait-for graph. A second reader holds the cache as well and leaves
 * later, which shows that the self-loop alone explains the hang.
 *
 * Expected ThreadLens result:
 *   default  : CONFIRMED DEADLOCK, 2-thread cycle (through a writer blocked by two readers)
 *   --fixed  : no deadlock, no lock-order cycle, exit code 0
 *   upgrade  : CONFIRMED DEADLOCK, self-loop (read-to-write upgrade)
 *
 * usage: rwlock_deadlock [--fixed | upgrade] [lookups]
 */
#include "../common/scenario.h"

#define ROUTES 64

static pthread_rwlock_t table_lock;                              /* initialised in main */
static pthread_rwlock_t stats_lock = PTHREAD_RWLOCK_INITIALIZER;
static pthread_rwlock_t cache_lock = PTHREAD_RWLOCK_INITIALIZER;
static sc_gate gate;
static int fixed_order;
static int lookups = 200;
static long routes[ROUTES];
static long hits;
static long missed_stats;
static long rebalances;
static int cache_stale = 1;

static long lookup_route(int key)
{
    pthread_rwlock_rdlock(&table_lock);
    long r = routes[key % ROUTES];
    pthread_rwlock_unlock(&table_lock);
    return r;
}

static void *lookup(void *p)
{
    long id = (long)p;
    char name[16];
    snprintf(name, sizeof name, "lookup-%ld", id);
    sc_name_self(name);

    /* warm-up: plain lookups, and opportunistic statistics updates that never block */
    for (int i = 0; i < lookups; i++) {
        (void)lookup_route(i + (int)id);
        if (i % 10 == 0) {
            if (pthread_rwlock_trywrlock(&stats_lock) == 0) {
                hits++;
                pthread_rwlock_unlock(&stats_lock);
            } else {
                __atomic_fetch_add(&missed_stats, 1, __ATOMIC_RELAXED);
            }
        }
    }

    /* the critical section: read the table, then record a hit in the statistics */
    pthread_rwlock_rdlock(&table_lock);
    sc_gate_wait(&gate, 200);
    if (id == 1) {
        pthread_rwlock_wrlock(&stats_lock);          /* table(read) -> stats(write) */
        hits++;
        pthread_rwlock_unlock(&stats_lock);
    } else {
        sc_spin_us(50000);                            /* lookup-2 keeps reading for a while */
    }
    pthread_rwlock_unlock(&table_lock);
    return NULL;
}

static void *rebalancer(void *p)
{
    (void)p;
    sc_name_self("rebalancer");
    if (fixed_order) {
        /* FIX: the same global order as the lookups — table first, then stats */
        pthread_rwlock_wrlock(&table_lock);
        sc_gate_wait(&gate, 200);
        pthread_rwlock_rdlock(&stats_lock);
    } else {
        pthread_rwlock_rdlock(&stats_lock);
        sc_gate_wait(&gate, 200);
        pthread_rwlock_wrlock(&table_lock);          /* BUG: stats(read) -> table(write) */
    }
    for (int i = 0; i < ROUTES; i++) routes[i] = hits + i;
    rebalances++;
    pthread_rwlock_unlock(&stats_lock);
    pthread_rwlock_unlock(&table_lock);
    return NULL;
}

static void *updater(void *p)
{
    (void)p;
    sc_name_self("updater");
    pthread_rwlock_rdlock(&cache_lock);
    sc_gate_wait(&gate, 200);
    if (cache_stale) {
        pthread_rwlock_wrlock(&cache_lock);          /* BUG: upgrade while still holding the read lock */
        cache_stale = 0;
        pthread_rwlock_unlock(&cache_lock);
    }
    pthread_rwlock_unlock(&cache_lock);
    return NULL;
}

static void *cache_reader(void *p)
{
    (void)p;
    sc_name_self("cache-reader");
    pthread_rwlock_rdlock(&cache_lock);
    sc_gate_wait(&gate, 200);
    sc_spin_us(30000);
    pthread_rwlock_unlock(&cache_lock);
    return NULL;
}

int main(int argc, char **argv)
{
    sc_watchdog(30);
    int upgrade = 0;
    for (int a = 1; a < argc; a++) {
        if (strcmp(argv[a], "--fixed") == 0) fixed_order = 1;
        else if (strcmp(argv[a], "upgrade") == 0) upgrade = 1;
        else if (atoi(argv[a]) > 0) lookups = atoi(argv[a]);
    }
    pthread_rwlock_init(&table_lock, NULL);
    tl_name_lock(&table_lock, "routing_table");
    tl_name_lock(&stats_lock, "route_stats");
    tl_name_lock(&cache_lock, "cache");

    uint64_t t0 = sc_now_ns();
    if (upgrade) {
        sc_gate_init(&gate, 2);
        pthread_t u, r;
        pthread_create(&r, NULL, cache_reader, NULL);
        pthread_create(&u, NULL, updater, NULL);
        pthread_join(r, NULL);
        pthread_join(u, NULL);                         /* hangs: the updater waits for itself */
        SC_RESULT("{\"scenario\":\"rwlock_upgrade\",\"completed\":true,\"elapsed_ms\":%.3f}",
                  (sc_now_ns() - t0) / 1e6);
        return 0;
    }

    sc_gate_init(&gate, 3);
    pthread_t th[3];
    pthread_create(&th[0], NULL, lookup, (void *)1L);
    pthread_create(&th[1], NULL, lookup, (void *)2L);
    pthread_create(&th[2], NULL, rebalancer, NULL);
    for (int i = 0; i < 3; i++) pthread_join(th[i], NULL);
    pthread_rwlock_destroy(&table_lock);

    SC_RESULT("{\"scenario\":\"rwlock_deadlock\",\"fixed\":%s,\"completed\":true,\"elapsed_ms\":%.3f,"
              "\"hits\":%ld,\"missed_stats\":%ld,\"rebalances\":%ld}",
              fixed_order ? "true" : "false", (sc_now_ns() - t0) / 1e6, hits, missed_stats, rebalances);
    return 0;
}
