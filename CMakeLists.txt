cmake_minimum_required(VERSION 3.16)
project(threadlens VERSION 1.0.0 LANGUAGES C CXX)

if(NOT CMAKE_SYSTEM_NAME STREQUAL "Linux")
    message(FATAL_ERROR "ThreadLens instruments Linux/glibc pthreads; build it on Linux (e.g. WSL2 Ubuntu).")
endif()

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE RelWithDebInfo)
endif()

option(THREADLENS_BUILD_TSAN "Also build ThreadSanitizer variants of the scenarios" ON)

set(THREADS_PREFER_PTHREAD_FLAG ON)
find_package(Threads REQUIRED)

enable_testing()
add_subdirectory(scenarios)
add_subdirectory(collector)
