# Event schema

There are two representations of an event:

1. the **wire record** written by the collector (64 bytes, binary), and
2. the **public event object** returned by the API, exports and WebSocket
   (JSON, validated by Pydantic: `backend/app/models/events.py`).

## 1. Wire record (`collector/include/threadlens/tl_wire.h`, protocol version 1)

Little-endian, packed, exactly 64 bytes. Python struct format `<HBBHHQQIIQQQiI`.

| Offset | Size | Field | Meaning |
|---|---|---|---|
| 0 | 2 | `magic` | `0x4C54` ("TL") |
| 2 | 1 | `version` | `1` |
| 3 | 1 | `type` | event type (table below) |
| 4 | 2 | `flags` | `TIMED=1` (timed lock / rwlock / cond / sem wait), `TRY=2` (try-lock, `sem_trywait`), `OOB=4`, `INFERRED=8`, `LATE=16`, `SERIAL=32` |
| 6 | 2 | reserved | 0 |
| 8 | 8 | `seq` | in-band: ring slot index = per-process total order. Out-of-band: see type |
| 16 | 8 | `ts_ns` | `CLOCK_MONOTONIC` nanoseconds in the target |
| 24 | 4 | `pid` | process id |
| 28 | 4 | `tid` | kernel thread id (`gettid`) |
| 32 | 8 | `obj` | primary object (mutex / rwlock / cond / semaphore / barrier address, `pthread_t`, …) |
| 40 | 8 | `aux` | secondary value (per type) |
| 48 | 8 | `caller` | return address of the intercepted call |
| 56 | 4 | `result` | return code / errno (signed) |
| 60 | 4 | `extra` | small value (mutex kind, thread index, exit kind, …) |

String records (`THREAD_NAME`, `LOCK_NAME`, `USER_MARK`) store a NUL-padded
string of at most 23 characters in bytes 40–63 instead of aux/caller/result/extra.

### Event types

| Id | Type | Thread | `obj` | `aux` | `result` / `extra` / flags |
|---|---|---|---|---|---|
| 1 | `HELLO` (OOB) | – | session token | PIE load bias | protocol / ring capacity |
| 2 | `PROCESS_START` | main | parent pid | load bias | |
| 3 | `PROCESS_EXIT` | caller of `exit` | total dropped | | extra = threads started |
| 4 | `HEARTBEAT` (OOB) | – | total dropped | ring depth | `seq` = next undelivered seq; extra = live threads |
| 5 | `EVENTS_LOST` (OOB) | – | total dropped | dropped since last report | |
| 10 | `THREAD_CREATE` | creator | child `pthread_t` | start routine | result |
| 11 | `THREAD_START` | new thread | `pthread_self()` | start routine | extra = thread index; `LATE` if discovered late |
| 12 | `THREAD_EXIT` | exiting | `pthread_self()` | | extra = 0 return, 1 `pthread_exit`, 2 cancel/unknown, 3 process exit |
| 13 | `THREAD_NAME` | caller | named `pthread_t` | string | |
| 14 / 15 | `JOIN_BEGIN` / `JOIN_END` | joiner | target `pthread_t` | | result on END |
| 16 / 17 | `THREAD_DETACH` / `CANCEL_REQUEST` | caller | target | | result |
| 20 / 21 | `MUTEX_INIT` / `MUTEX_DESTROY` | caller | mutex | | result; extra = kind |
| 22 | `LOCK_REQUEST` | requester | mutex | owner TID read from glibc | extra = kind; `TIMED` |
| 23 | `LOCK_ACQUIRED` | owner | mutex | wait ns | result 0 or `EOWNERDEAD`; `TIMED`/`TRY` |
| 24 | `LOCK_FAILED` | requester | mutex | wait ns | errno (`EBUSY`, `ETIMEDOUT`, `EDEADLK`, …) |
| 25 | `LOCK_RELEASE` | caller | mutex | | extra = kind (recorded **before** the real unlock) |
| 26 | `UNLOCK_FAILED` | caller | mutex | | errno (`EPERM`) |
| 27 | `LOCK_NAME` | caller | mutex | string | |
| 30 / 31 | `COND_WAIT_BEGIN` / `END` | waiter | cond | mutex | result on END; `TIMED`; `INFERRED` (END after cancellation) |
| 32 / 33 | `COND_SIGNAL` / `COND_BROADCAST` | signaller | cond | | |
| 40 / 41 | `BARRIER_WAIT_BEGIN` / `END` | waiter | barrier | | `SERIAL` |
| 50 | `USER_MARK` | caller | – | string | |
| 60 / 61 | `RWLOCK_INIT` / `RWLOCK_DESTROY` | caller | rwlock | | result; extra = rwlock kind on INIT |
| 62 | `RW_REQUEST` | requester | rwlock | writer TID read from glibc (`__cur_writer`) | extra = mode \| kind≪8; `TIMED` |
| 63 | `RW_ACQUIRED` | new holder | rwlock | wait ns | extra = mode \| kind≪8; `TIMED`/`TRY` |
| 64 | `RW_FAILED` | requester | rwlock | wait ns | errno (`EBUSY`, `ETIMEDOUT`, `EDEADLK`, `EAGAIN`, …) |
| 65 | `RW_RELEASE` | caller | rwlock | | extra = mode glibc says the caller holds \| kind≪8 (recorded **before** the real unlock) |
| 70 / 71 | `SEM_INIT` / `SEM_DESTROY` | caller | semaphore | initial value (INIT) | errno; extra = `pshared` on INIT |
| 72 | `SEM_WAIT_BEGIN` | waiter | semaphore | count seen at the call | `TIMED` |
| 73 | `SEM_WAIT_END` | waiter | semaphore | wait ns | 0 or errno (`EAGAIN` for trywait, `ETIMEDOUT`, `EINTR`, `EINVAL`); `TIMED`/`TRY` |
| 74 | `SEM_POST` | poster | semaphore | count seen before the post | recorded **before** the real `sem_post` |
| 75 | `SEM_POST_FAILED` | poster | semaphore | | errno (`EOVERFLOW`, `EINVAL`); undoes the preceding `SEM_POST` |

Mutex kind (`extra`) is glibc's `__data.__kind`: low 2 bits = 0 normal, 1
recursive, 2 error-checking, 3 adaptive; bit 4 = robust.

rwlock records carry `extra = mode | kind << 8` (`TL_RW_EXTRA`): mode 0 = read,
1 = write; kind is glibc's `__data.__flags`: 0 prefer-reader (default),
1 prefer-writer (glibc treats it like prefer-reader), 2 writer-preferring
non-recursive. On `RW_RELEASE` the mode is only glibc's view (`__cur_writer ==
caller`); the analyzer decides from its own ownership state and counts a
disagreement as an owner-check mismatch.

Semaphore functions report errors through `errno`; the collector stores that
errno in `result` and restores `errno` for the program.

### Begin/end pairs
`LOCK_REQUEST → LOCK_ACQUIRED | LOCK_FAILED`, `RW_REQUEST → RW_ACQUIRED | RW_FAILED`,
`COND_WAIT_BEGIN → COND_WAIT_END`, `SEM_WAIT_BEGIN → SEM_WAIT_END`,
`JOIN_BEGIN → JOIN_END`, `BARRIER_WAIT_BEGIN → END`. Try-locks
(`pthread_mutex_trylock`, `pthread_rwlock_try*lock`) and `sem_trywait` emit only
their outcome (they never wait). The backend stores the begin event's sequence
number as the end event's **correlation id**.

### Ordering rule
Requests and wait begins are recorded before the real call, acquisitions and
wait ends after it returns, and releases **before** the real unlock — for
mutexes and rwlocks alike. `SEM_POST` is likewise recorded before the real
`sem_post`, so a post always has a smaller sequence number than the
`SEM_WAIT_END` it lets through, and for a semaphore whose `sem_init` was
observed the trace never shows more successful waits than initial value +
posts (checked by `tl_tracecheck`; a violation is a coverage anomaly).

## 2. Identity rules

* **Session** — `YYYYMMDD-HHMMSS-xxxxxx`; one target process per session.
* **Event** — `event_id = "<session>:<seq>"`.
* **Thread** — the collector-assigned **index** (`T0` = main, `T1`, …),
  stable for the session. Kernel TIDs and `pthread_t` values are attributes;
  both can be reused after a thread ends, so they are mapped to the most recent
  live thread.
* **Lock** — a session-local id (`L1`, `L2`, …) assigned the first time an
  address is seen and again after `pthread_mutex_destroy` /
  `pthread_rwlock_destroy` succeeds (address reuse). Mutexes and rwlocks share
  this id space (`primitive` tells them apart). Its *label* is the annotation
  (`tl_name_lock`), else the global symbol from `nm` (`lock_a`, `accounts+0x28`),
  else `mutex@0x…` / `rwlock@0x…`. The annotation or symbol is the **stable key**
  used to match locks across runs (addresses change with ASLR).
* **Semaphore** — identified by its address, like a condition variable; a
  successful `sem_init` starts a new instance. Labelled like locks
  (`tl_name_lock` also names semaphores), else `sem@0x…`.

## 3. Public event object (`EventOut`)

```json
{
  "session_id": "20261014-050218-594fac",
  "event_id": "20261014-050218-594fac:30",
  "seq": 30,
  "ts": 120629675680,
  "rel_ns": 691882,
  "tid": 561,
  "thread": 1,
  "thread_label": "teller-1",
  "type": "LOCK_REQUEST",
  "lock": 2,
  "lock_label": "account[B]",
  "obj": "0x5e2ce10920d8",
  "target_thread": null,
  "correlation": null,
  "result": 0,
  "result_name": "OK",
  "flags": [],
  "caller": "0x5e2ce108f3a1",
  "text": null,
  "metadata": {"glibc_owner_tid": 562, "mutex_kind": "normal"},
  "description": "teller-1 requests account[B]"
}
```

`metadata` depends on the type: `glibc_owner_tid` / `mutex_kind` / `wait_ns`
for mutex records; `rw_mode` (`"r"`/`"w"`), `rwlock_kind`, `glibc_writer_tid`
(requests) and `wait_ns` for rwlock records; `semaphore` (label), `count_seen`
(waits and posts), `wait_ns` (wait ends), `initial_value` / `pshared` (init) for
semaphore records; `cond` for condition-variable records.

## 4. Import schema (`EventIn`)

Used for NDJSON import (`POST /api/sessions/import`, "raw" export format).
Strict: unknown fields are rejected, integers are range-checked (u64/u32/i32),
event types and flags must be known, strings ≤ 23 characters.

```json
{"seq": 30, "ts": 120629675680, "tid": 561, "type": "LOCK_REQUEST", "flags": [], "obj": 103479263011032, "aux": 562, "caller": 0, "result": 0, "extra": 0}
```

## 5. Handling delayed, duplicate, incomplete and out-of-order data

| Situation | Handling | Effect on verdicts |
|---|---|---|
| Duplicate `seq` | dropped, counted (`coverage.duplicates`) | none |
| `seq` ahead of expected | held in a bounded reorder window (4096) | none if the gap fills |
| Gap never filled | declared missing (`coverage.seq_gaps`, `missing_events`) | coverage incomplete → **inconclusive** |
| Collector dropped events (ring full) | `EVENTS_LOST` / heartbeat totals | coverage incomplete → **inconclusive** |
| Acquire without request, acquire of a lock still owned by another thread, etc. | `coverage.anomalies` with examples | coverage incomplete → **inconclusive** |
| Event from a thread that never started | synthetic thread, `late_threads` | anomaly |
| Collector killed (no `PROCESS_EXIT`) | `coverage.clean_exit = false`; stream end still confirms persistent cycles | none by itself |

Missing information is never turned into a confirmed diagnosis.
