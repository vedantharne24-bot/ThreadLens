/*
 * bank_transfer_fixed.c — CORRECTED program.
 *
 * Identical to ../deadlock/bank_transfer.c except for transfer(): the two
 * account locks are always taken in one global order (lower account id
 * first). Both tellers therefore request account[A] first; whoever loses
 * simply waits, and no circular wait can form.
 *
 * The timed gate is kept so that both programs execute the same code path:
 * here the second teller cannot reach the gate (it waits for account[A]), so
 * the first teller continues after the gate timeout.
 *
 * Expected ThreadLens result: no deadlock, no lock-order cycle, exit code 0.
 *
 * usage: bank_ordered [rounds]
 */
#include "../common/scenario.h"

typedef struct account {
    pthread_mutex_t lock;
    long balance;
    int id;
} account;

static account accounts[2];
static sc_gate gate;
static int rounds = 5;

static void transfer(account *from, account *to, long amount, int gated)
{
    /* FIX: consistent global lock order — lower account id first. */
    account *first = from->id < to->id ? from : to;
    account *second = from->id < to->id ? to : from;

    pthread_mutex_lock(&first->lock);
    if (gated) sc_gate_wait(&gate, 150);
    pthread_mutex_lock(&second->lock);
    from->balance -= amount;
    to->balance += amount;
    pthread_mutex_unlock(&second->lock);
    pthread_mutex_unlock(&first->lock);
}

struct teller_arg {
    const char *name;
    account *from;
    account *to;
};

static void *teller(void *p)
{
    struct teller_arg *a = p;
    sc_name_self(a->name);
    for (int r = 0; r < rounds; r++) transfer(a->from, a->to, 10, r == 0);
    return NULL;
}

int main(int argc, char **argv)
{
    sc_watchdog(30);
    if (argc > 1) rounds = atoi(argv[1]) > 0 ? atoi(argv[1]) : rounds;

    for (int i = 0; i < 2; i++) {
        pthread_mutex_init(&accounts[i].lock, NULL);
        accounts[i].balance = 1000;
        accounts[i].id = i;
    }
    tl_name_lock(&accounts[0].lock, "account[A]");
    tl_name_lock(&accounts[1].lock, "account[B]");
    sc_gate_init(&gate, 2);

    uint64_t t0 = sc_now_ns();
    pthread_t t1, t2;
    struct teller_arg a1 = { "teller-1", &accounts[0], &accounts[1] };
    struct teller_arg a2 = { "teller-2", &accounts[1], &accounts[0] };
    pthread_create(&t1, NULL, teller, &a1);
    pthread_create(&t2, NULL, teller, &a2);
    pthread_join(t1, NULL);
    pthread_join(t2, NULL);

    SC_RESULT("{\"scenario\":\"bank_ordered\",\"completed\":true,\"elapsed_ms\":%.3f,\"total\":%ld}",
              (sc_now_ns() - t0) / 1e6, accounts[0].balance + accounts[1].balance);
    return 0;
}
