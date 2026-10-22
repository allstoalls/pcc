# pcc virtual-thread 1M gate — logical baseline and production runtime measurement

Status: **production C-runtime 1M gate measured on 2026-07-16.** The original
logical-model harness remains as a bounded deterministic CI baseline. The
manual `real-runtime` runner now executes the no-libpython C scheduler and
records source-bound RSS, mean operation latency, throughput, and GC-pause
telemetry for GC0..4.

Track: `T-P0-VTHREAD-1M-GATE` (split out 2026-06-29 from `T-P0-VTHREAD-SCALE`;
see `docs/goal/goal-prompt.md`). Harness: `tests/benchmarks/vthread/`. Gate command:

```bash
env -u LC_ALL uv run pytest tests/benchmarks/vthread -q -n0

PCC_VTHREAD_1M=1 env -u LC_ALL uv run python \
  scripts/run_vthread_1m_gate.py --output /tmp/pcc-vthread-1m.json
```

The first slice deliberately measured only a **logical model**. That historical
surface still exists in `harness.py`; its `real-runtime` row remains skipped
because an in-process host-Python model cannot make a native runtime claim. The
separate `scripts/run_vthread_1m_gate.py` owner now fills the real-runtime mode
by compiling and executing `vthread_real_runtime.c` against the production C
runtime. Keeping these entrypoints separate prevents logical counts from being
silently relabeled as machine measurements.

---

## 1. Why a logical model first

The real virtual-thread scheduler lives in the C runtime
(`pcc/runtime/src/pcc_threads.c`) and owns three waitsets:

| Waitset | Real runtime ops | Counter |
|---|---|---|
| Ready queue | `pcc_vthread_enqueue_locked` / `pcc_vthread_dequeue_locked` | `pcc_vthread_ready_count_value` |
| Timer queue (deadline-sorted) | `pcc_vthread_timer_add_locked` / `py_virtual_thread_poll_timers` (expire when `deadline_ms <= now`) | `pcc_vthread_timer_count_value` |
| IO waitset | `pcc_vthread_poll_add_locked` (fd + events + deadline) / `py_virtual_thread_poll_io` | `pcc_vthread_io_wait_count_value` |

A real 1M-vthread measurement needs a pcc1 self-host binary linking that runtime,
executed on a current machine, reporting real resident-set bytes, real
enqueue/dequeue/timer/IO wait latencies, and real GC pause counts under a named
GC backend (`PCC_GC_BACKEND=0..4`). None of that exists as a gate today, and it
cannot be faked in host Python.

Design consequence, adopted here: **measure the logical operation shape first**.
The harness mirrors the *operation shape* of the three waitsets in a tiny,
deterministic, self-contained in-process model and reports only **logical
counters** plus two explicitly-named surrogate counters. This makes the scaling
contract (ops scale predictably with `N`; the live-set surrogate stays bounded)
testable before any real run exists, and it fixes the vocabulary
(`MEASURED` vs `SKIPPED_WITH_REASON`) that the real run will reuse.

---

## 2. Mode taxonomy

| Mode | Runs? | N | What it produces |
|---|---|---|---|
| `small-ci` | RUNS by default | `DEFAULT_SMALL_CI_N` = 10_000 | deterministic logical counters over the in-harness model |
| `large-manual` | Gated OFF; runs only under `PCC_VTHREAD_1M=1` | `LARGE_MANUAL_N` = 1_000_000 | the *same* logical counters at 1M — still not RSS/latency |
| `real-runtime` | Manual; requires `PCC_VTHREAD_1M=1` | 1_000_000 | production C scheduler RSS, amortized latency, throughput, and GC-pause telemetry for GC0..4 |

`small-ci` is the only mode that runs in CI. `large-manual` is never run by
default — it is skipped-with-reason unless the operator explicitly opts in via
`PCC_VTHREAD_1M=1`, and even then it produces only logical counts (the "1M" is a
1M-iteration *logical* run, not a 1M-OS-resource run). The logical harness still
skips its own `real-runtime` row; the separately gated production runner owns
that mode and never runs by default in CI.

---

## 3. The self-contained logical scheduler model

`simulate_logical_scheduler(n)` mirrors the operation shape of the three
waitsets. For each of `n` virtual threads it:

1. enqueues onto the ready queue, then later dequeues it (every thread);
2. if `i % 3 == 0`: inserts a timer that later expires (mirrors
   `py_virtual_thread_sleep` -> `poll_timers`);
3. else if `i % 5 == 0`: adds an IO wait that later becomes ready (mirrors
   `py_virtual_thread_block_on_fd` -> `poll_io`).

The ratios (1/3 timer, 1/5 IO-of-the-rest) are an **arbitrary but fixed** model
choice. This slice does not claim the ratios match any real workload; it claims
the counters scale *predictably* with `N` and are deterministic.

Reported metrics (all integers, all deterministic):

| Metric | Meaning | Real-runtime analogue |
|---|---|---|
| `enqueue_ops` / `dequeue_ops` | ready-queue logical op counts | ready-queue enqueue/dequeue |
| `timer_insert_ops` / `timer_expire_ops` | timer logical op counts | timer add / expire |
| `io_wait_add_ops` / `io_ready_ops` | IO-waitset logical op counts | poll add / ready |
| `peak_live_set` | **RSS surrogate** — max simultaneously-live logical scheduler nodes (a COUNT, not bytes) | resident set size |
| `gc_pause_count` | **GC-pause surrogate** — number of upward crossings of a bounded live-set threshold (a COUNT, not real pauses) | GC pause count |

`peak_live_set` and `gc_pause_count` are the two surrogate counters. They are
named surrogates precisely so no reader mistakes them for bytes or for real GC
pauses. Under the default immediate-dequeue workload the live set stays at 2 for
all `N` (a node is dequeued right after it is enqueued), so `peak_live_set` is
`N`-independent and `gc_pause_count` is 0 — which is the correct signal that the
logical model does not accumulate unbounded live scheduler state.

---

## 4. What `small-ci` proves vs what `real-runtime` needs

**`small-ci` proves (and only this):**

* the logical scheduler model's op counters scale predictably with `N`
  (ready-queue ops exactly linear; timer/IO ops linear up to a period-bounded
  O(1) boundary term);
* the model is deterministic — identical `N` yields byte-identical metrics;
* the live-set surrogate stays bounded and `N`-independent for the
  immediate-dequeue workload, and the GC-pause surrogate is threshold-driven
  (it fires when the live set crosses a configured threshold, and is 0 when it
  does not);
* the mode taxonomy and skip taxonomy are correct, and no timing / RSS-bytes /
  latency metric can be smuggled into a `MEASURED` result (the `VThreadBenchResult`
  claim-boundary guard rejects any key containing `latency`, `_ms`, `_ns`,
  `bytes`, `throughput`, `speedup`, etc.).

**`real-runtime` now supplies through the separate production runner:**

* an isolated no-libpython archive linking the runtime C scheduler
  (`py_virtual_thread_*` in `pcc_threads.c`);
* a live-machine run reporting real resident-set bytes (via the `py_os_rss.c`
  bridge or equivalent), real enqueue/dequeue/timer/IO wait latencies, and real
  GC pause counts, each carrying an exact backend + `PCC_GC_BACKEND` label;
* the completed `T-P0-VTHREAD-SCALE` substrate (O(1) scheduler-root removal,
  node-pool freelists, scalable timer, and production IO waitset) exercised at
  scale.

The source-bound result is persisted at
`docs/goal/evidence/2026-07-16-vthread-1m-results.json`. The pcc1/pcc-Python
runtime-high mode remains a separate claim and is not implied by this C-runtime
measurement.

---

## 5. Hard claim boundary

> A `small-ci` / `large-manual` result is a mode-labeled set of **deterministic
> logical counts** over a self-contained scheduler model running in ONE host
> Python process. `peak_live_set` is a live-node COUNT used as an RSS surrogate;
> `gc_pause_count` is a threshold-crossing COUNT used as a GC-pause surrogate.
> Neither is bytes; neither is time. This harness proves ONLY that the logical
> scheduler model's counters scale predictably with `N` and are deterministic.
> It does **NOT** prove 1M-vthread readiness, real RSS, real enqueue/dequeue/
> timer/IO latency, real GC pause behavior, or any virtual-thread performance
> completion. Those claims belong only to the separately gated production
> runner and its source-bound JSON artifact.

Consistent with `docs/goal/goal-prompt.md` T-P0-VTHREAD-1M-GATE: *do not run 1M by
default in CI, and do not claim readiness without a current machine run.* This
slice honors both — 1M is gated off, and readiness is explicitly disclaimed.

---

## 6. Files

| Path | Role |
|---|---|
| `tests/benchmarks/vthread/harness.py` | mode taxonomy, logical scheduler model, `VThreadBenchResult` / `VThreadBenchManifest`, per-mode runners, claim-boundary guard |
| `tests/benchmarks/vthread/__init__.py` | package facade re-exporting the harness API |
| `tests/benchmarks/vthread/test_vthread_1m_gate.py` | the gate: scaling, determinism, skip taxonomy, `large-manual` gated-off, JSON round-trip, claim-boundary guard, soft `pcc.vthread` import guard |
| `tests/benchmarks/vthread/vthread_real_runtime.c` | production no-libpython C scheduler workload and per-backend metrics |
| `tests/benchmarks/vthread/test_vthread_real_runtime.py` | small GC0..4 production matrix, manual gate, and ownership-law regression |
| `scripts/run_vthread_1m_gate.py` | source-digested cached build, per-backend process isolation, result validation, JSON manifest |
| `docs/design/pcc-vthread-1m-gate.md` | this document |

The gate soft-imports the sibling `pcc.vthread.*` oracle package (built
concurrently under `T-P0-VTHREAD-*`); it is an import guard
(try/except `ImportError` -> skip-with-reason), never a hard dependency. This
harness measures the self-contained logical model and stands alone.

---

## 7. Risk notes

* **Surrogate misreading (primary risk).** `peak_live_set` and `gc_pause_count`
  are COUNTS, not bytes or time. Mitigation: they are named "surrogate"
  everywhere, the claim boundary is repeated in the module docstring, the package
  docstring, and this doc, and the `MEASURED` guard rejects any real
  timing/RSS-bytes key so a future edit cannot quietly upgrade a count into a
  resource number without changing the guard.
* **Model drift from the real runtime.** The logical model mirrors the *shape*
  of `pcc_threads.c` waitsets, not their implementation. If the runtime scheduler
  ops change, this model does not automatically follow. Mitigation: the mirror is
  documented op-by-op in §1/§3; the `real-runtime` mode is the intended place for
  fidelity, and it is skipped rather than pretending fidelity.
* **CI cost.** `small-ci` at N=10_000 is a pure-Python integer loop with no
  allocation of runtime objects, no fds, no threads — cheap and bounded. The 1M
  `large-manual` loop is gated off precisely so a stray CI run cannot pay its
  cost.
* **False scope expansion.** The real gate proves one million scheduler objects,
  not one million fds, stackful Loom, percentile latency, or pcc-Python runtime
  parity. The manifest carries that boundary and the evidence preserves GC3's
  slow pause instead of reducing the result to a green boolean.

## 8. Measured production result

On macOS arm64, all five backends completed one million virtual threads and
ended with zero scheduler roots/queues. Peak RSS ranged from 264 MB (GC0) to
688 MB (GC4). GC0/1/2/4 completed in 0.67–1.32 seconds; GC3 completed in 28.18
seconds because its live collect accumulated 27.52 seconds of pause time. See
`docs/goal/evidence/2026-07-16-vthread-1m-production-runtime.md` for the full
table and claim boundary.
```
