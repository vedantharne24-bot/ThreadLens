# Demonstration script (≈ 10 minutes)

Preparation (before the audience arrives):

```bash
# terminal 1 — Linux / WSL2
cd threadlens && scripts/run_backend.sh          # serves API + built UI on http://localhost:8000
```

Open <http://localhost:8000> in a browser. If you changed the frontend, rebuild
it first (`cd frontend && npm run build`) or use the dev server (`npm run dev`,
port 5173).

---

## 1. The problem (1 min) — landing page

* Scroll the landing page: *"See who waits for whom — and the evidence that
  proves it."*
* Point at the chip **"backend online · 23 real test programs built"**: the
  page is talking to the real backend.
* Scroll through **"Seven events. One circular wait."** — say clearly that this
  story is an *illustration*; everything after this point is real data.

## 2. Launch a real deadlock (2 min) — Test Lab

* *Open the debugger → Test Lab.* Show the card **Bank transfer deadlock**:
  `bank_deadlock` is an ordinary C program (open **Source & fix**, show
  `transfer()` locking the source account then the destination).
* Click **Run**. ThreadLens starts the binary with `LD_PRELOAD=libthreadlens.so`,
  so every `pthread_mutex_lock` call goes through the collector.
* A toast appears: **Deadlock confirmed** — within tens of milliseconds.

## 3. Explain the graph (2 min) — Lock Graph

* Resource view: teller-1 → *waits for* → account[B] → *held by* → teller-2 →
  *waits for* → account[A] → *held by* → teller-1. The red ring is the cycle.
* `main` is blocked joining teller-1 — a join edge.
* Switch to **Wait-for** (threads only) and **Lock order** (history — empty here,
  because the second acquisitions never succeeded: the cycle is visible only in
  the *current* state).
* Toggle **3D**, orbit, click teller-1: the evidence panel lists its recorded
  events. Toggle back to 2D: same data, lighter view.

## 4. Evidence and classification (2 min) — Diagnostics

* Finding F1: *Confirmed deadlock (under the observed synchronization model)*.
* Evidence timeline: `#15 teller-1 acquired account[A] (bank_transfer.c:32)`,
  `#22 teller-2 acquired account[B]`, `#27 teller-2 requests account[A]`,
  `#30 teller-1 requests account[B]` — trigger.
* Explain *why confirmed*: every edge an untimed observed wait, ownership from
  recorded acquisitions, no lost events, persisted after a heartbeat.
* Coffman conditions paragraph; remedies; limitations; coverage panel (0 lost).

## 5. Replay (1 min) — Replay & Compare → Replay

* Drag the scrubber to `#25`: teller-1 is waiting on the gate condition
  variable, no finding yet. Step with → to `#30`: the cycle closes and F1 appears.
* Say: *event replay* re-applies recorded events to the analysis engine; the
  program is not re-executed (that would be `rr`).

## 6. Fix and compare (1.5 min)

* Back to **Test Lab** → **Run original + fix** on the bank card. ThreadLens runs
  the buggy program, then `bank_ordered` (diff: lower account id first), and
  opens the comparison:
  *Program completed normally: no → yes*, *Confirmed deadlocks 1 → 0*,
  *Wait-for cycles 1 → 0*.
* Read the disclaimer: one clean run does not prove absence of bugs.

## 7. Potential vs. confirmed vs. stall (1 min, optional)

* Run **Lock-order inversion (potential)**: the program completes, yet the
  verdict is *Potential deadlock* — the lock-order graph has lock_a ↔ lock_b.
* Run **Lost wake-up**: verdict *Stall*, no lock cycle; the explanation points to
  a signal sent before the wait began.
* Run **Hot lock contention**: *Contention*, explicitly not a deadlock.
* Run **Reader/writer deadlock**: *Confirmed deadlock*. In the Lock Graph the
  rebalancer "waits to write" `routing_table`, which two lookups read — a writer
  waits for every reader, so its node has two outgoing edges and ThreadLens
  searches the graph depth-first.
* Run **Semaphore bounded buffer stall** (or **Run original + fix** on it): verdict
  *Stall*, not deadlock. The `slots` semaphore is drawn with a waiter but no
  holder; the explanation walks consumer → producer → "waiting on semaphore
  slots", and says why a semaphore cannot close a wait-for cycle.

## 8. Engineering credibility (30 s)

* `docs/results.md`: 115 evaluation runs, precision/recall per class, measured
  latency, overhead table, event-loss table, comparison with ThreadSanitizer.
* `pytest`: 124 backend tests including 20 scenarios through the live API.

### If something goes wrong

| Symptom | Fix |
|---|---|
| "backend offline" chip | start `scripts/run_backend.sh` in WSL/Linux |
| "collector not built" | `scripts/setup_linux.sh` |
| 3D view blank | Settings → default graph 2D, or effects "Reduced" |
| Graph empty for a finished clean run | toggle "exited threads" / "idle locks" |
