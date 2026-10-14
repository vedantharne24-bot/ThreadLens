// bank_transfer_cpp.cpp — ORIGINAL C++ program (std::thread, std::mutex,
// nested std::lock_guard). Same bug as bank_transfer.c: each teller locks
// its source account first, so opposite transfers invert the lock order.
//
// std::mutex::lock() compiles to pthread_mutex_lock() on glibc, so the
// ThreadLens collector observes it without any source changes.
//
// Expected ThreadLens result: CONFIRMED DEADLOCK (2-thread cycle).
// Corrected version: ../corrected/bank_transfer_cpp_fixed.cpp
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
    std::lock_guard<std::mutex> first(from.m);   // source account
    if (gated) sc_gate_wait(&gate, 150);
    std::lock_guard<std::mutex> second(to.m);    // destination account
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
    SC_RESULT("{\"scenario\":\"bank_deadlock_cpp\",\"completed\":true,\"elapsed_ms\":%.3f}",
              (sc_now_ns() - t0) / 1e6);
    return 0;
}
