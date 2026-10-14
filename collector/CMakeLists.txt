# libthreadlens.so — the LD_PRELOAD collector
add_library(threadlens SHARED src/collector.c)
target_include_directories(threadlens PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_compile_options(threadlens PRIVATE -Wall -Wextra -O2 -fvisibility=hidden -fno-omit-frame-pointer)
target_link_libraries(threadlens PRIVATE ${CMAKE_DL_LIBS} Threads::Threads)
set_target_properties(threadlens PROPERTIES
    C_STANDARD 11
    C_STANDARD_REQUIRED ON
    LIBRARY_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/lib)

# Self-test program and trace validator
add_executable(tl_selftest tests/selftest.c)
target_include_directories(tl_selftest PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_link_libraries(tl_selftest PRIVATE Threads::Threads)

add_executable(tl_tracecheck tests/tracecheck.c)
target_include_directories(tl_tracecheck PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/include)

set_target_properties(tl_selftest tl_tracecheck PROPERTIES RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin)

# ctest: run the self-test under the collector, then validate the trace
add_test(NAME collector_trace_invariants
    COMMAND sh -c "THREADLENS_TRACE=${CMAKE_BINARY_DIR}/selftest.tlt LD_PRELOAD=$<TARGET_FILE:threadlens> $<TARGET_FILE:tl_selftest> && $<TARGET_FILE:tl_tracecheck> ${CMAKE_BINARY_DIR}/selftest.tlt 7")
add_test(NAME collector_passthrough_without_output
    COMMAND sh -c "LD_PRELOAD=$<TARGET_FILE:threadlens> $<TARGET_FILE:tl_selftest>")
add_test(NAME collector_deadlock_trace
    COMMAND sh -c "THREADLENS_TRACE=${CMAKE_BINARY_DIR}/deadlock.tlt SCENARIO_WATCHDOG=2 LD_PRELOAD=$<TARGET_FILE:threadlens> ${CMAKE_BINARY_DIR}/bin/bank_deadlock; test $? -eq 142 && $<TARGET_FILE:tl_tracecheck> ${CMAKE_BINARY_DIR}/deadlock.tlt 3 2>&1 | grep -q 'thread start/exit imbalance'")
