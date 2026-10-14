// bank_transfer_cpp_fixed.cpp — CORRECTED C++ program.
//
// FIX: both account mutexes are acquired together with C++17
// std::scoped_lock, which uses std::lock's deadlock-avoidance algorithm
// (lock one mutex, try_lock the others, back off and retry on failure).
// In the ThreadLens trace this appears as LOCK_ACQUIRED / LOCK_FAILED records
// flagged TRY. Try-lock acquisitions never block, so ThreadLens does not add
// them as lock-order edges and reports no potential deadlock.
//
// Expected ThreadLens result: no deadlock, no lock-order cycle, exit code 0.
#include "../common/scenario.h"

#include <mutex>
#include <thread>

struct Account {
    std::mutex m;
    long balance = 1000;
    int id = 0;
};

static Account acc[2];
static sc_gate gate;

static void transfer(Account &from, Account &to, long amount, bool gated)
{
    if (gated) sc_gate_wait(&gate, 150);         // both tellers start together
    std::scoped_lock both(from.m, to.m);         // FIX: deadlock-free multi-lock
    from.balance -= amount;
    to.balance += amount;
}

int main(int argc, char **argv)
{
    sc_watchdog(30);
    int rounds = argc > 1 && std::atoi(argv[1]) > 0 ? std::atoi(argv[1]) : 5;
    acc[0].id = 0;
    acc[1].id = 1;
    tl_name_lock(&acc[0].m, "account[A]");
    tl_name_lock(&acc[1].m, "account[B]");
    sc_gate_init(&gate, 2);

    uint64_t t0 = sc_now_ns();
    std::thread t1([&] {
        sc_name_self("teller-1");
        for (int r = 0; r < rounds; r++) transfer(acc[0], acc[1], 10, r == 0);
    });
    std::thread t2([&] {
        sc_name_self("teller-2");
        for (int r = 0; r < rounds; r++) transfer(acc[1], acc[0], 10, r == 0);
    });
    t1.join();
    t2.join();
    SC_RESULT("{\"scenario\":\"bank_scoped_lock_cpp\",\"completed\":true,\"elapsed_ms\":%.3f}",
              (sc_now_ns() - t0) / 1e6);
    return 0;
}
