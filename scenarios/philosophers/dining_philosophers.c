/*
 * dining_philosophers.c — Dijkstra's dining philosophers (5 threads, 5 forks).
 *
 * Default (buggy) mode: every philosopher picks up the LEFT fork, then the
 * RIGHT fork. The timed gate lets all five hold their left fork first, so the
 * five-thread circular wait  P0 -> P1 -> P2 -> P3 -> P4 -> P0  forms.
 *
 * "--fixed" mode (resource ordering): every philosopher picks up the
 * lower-numbered fork first, so philosopher 4 reaches for fork 0 before
 * fork 4. The cycle cannot close; the gate times out and everyone eats.
 *
 * Expected ThreadLens result:
 *   default : CONFIRMED DEADLOCK, 5-thread cycle
 *   --fixed : no deadlock, no lock-order cycle, exit code 0
 *
 * usage: dining_philosophers [--fixed] [meals]
 */
#include "../common/scenario.h"

#define N 5

static pthread_mutex_t forks[N];
static sc_gate gate;
static int fixed_order;
static int meals = 3;
static long eaten[N];

static void *philosopher(void *p)
{
    long i = (long)p;
    char name[16];
    snprintf(name, sizeof name, "philosopher-%ld", i);
    sc_name_self(name);

    int left = (int)i, right = (int)((i + 1) % N);
    int first = left, second = right;
    if (fixed_order && first > second) { first = right; second = left; } /* FIX */

    for (int m = 0; m < meals; m++) {
        pthread_mutex_lock(&forks[first]);
        if (m == 0) sc_gate_wait(&gate, 150);
        pthread_mutex_lock(&forks[second]);
        eaten[i]++;
        sc_spin_us(300);
        pthread_mutex_unlock(&forks[second]);
        pthread_mutex_unlock(&forks[first]);
        sc_spin_us(200); /* think */
    }
    return NULL;
}

int main(int argc, char **argv)
{
    sc_watchdog(30);
    for (int a = 1; a < argc; a++) {
        if (strcmp(argv[a], "--fixed") == 0) fixed_order = 1;
        else if (atoi(argv[a]) > 0) meals = atoi(argv[a]);
    }
    for (int i = 0; i < N; i++) {
        char name[16];
        pthread_mutex_init(&forks[i], NULL);
        snprintf(name, sizeof name, "fork[%d]", i);
        tl_name_lock(&forks[i], name);
    }
    sc_gate_init(&gate, N);

    uint64_t t0 = sc_now_ns();
    pthread_t th[N];
    for (long i = 0; i < N; i++) pthread_create(&th[i], NULL, philosopher, (void *)i);
    for (int i = 0; i < N; i++) pthread_join(th[i], NULL);

    long total = 0;
    for (int i = 0; i < N; i++) total += eaten[i];
    SC_RESULT("{\"scenario\":\"dining_philosophers\",\"fixed\":%s,\"completed\":true,\"elapsed_ms\":%.3f,\"meals\":%ld}",
              fixed_order ? "true" : "false", (sc_now_ns() - t0) / 1e6, total);
    return 0;
}
