# Investigation: Backend 1 auto step should sweep candidates and discharge debt

## Status
resolved

## Problem Description
Continue the Backend #1 Tier 5 productionization track from `goal.md`.

Backend #1 has allocation debt and a default debt threshold, but the after-fix
bootstrap benchmark showed default pcc-Python runtime programs doing tens of
thousands of automatic work-step calls during ordinary allocation churn:

```text
steps=38643
debt=1920320
```

That means the automatic step loop is not discharging debt once a mark cycle
has finished and objects are waiting in sweep-candidate state. This violates
the intended debt-threshold behavior from `goal.md`: allocation should
accumulate debt and only do bounded real work once debt crosses threshold, not
spin a no-op step for nearly every later allocation.

## Repro
Run the focused pcc-Python backend #1 churn gate:

```bash
env -u LC_ALL /opt/homebrew/bin/timeout 240s \
  uv run pytest \
  'tests/test_gc_backend_incremental.py::test_incremental_backend_collects_container_churn_under_pcc_python_runtime' \
  -q -n0
```

Expected pre-fix failure: generated program exits successfully but reports
default backend #1 `steps >= 500` and retained debt `>= 65536`.

## Test [CONFIRMED]
The focused gate fails before the fix:

```bash
env -u LC_ALL /opt/homebrew/bin/timeout 240s \
  uv run pytest \
  'tests/test_gc_backend_incremental.py::test_incremental_backend_collects_container_churn_under_pcc_python_runtime' \
  -q -n0
```

Observed result: `1 failed in 6.80s`.

Failure:

```text
assert 38641 < 500
```

## Proposals
- No.1 Let backend #1/#2 automatic steps sweep existing candidates     [DENIED]
- No.2 Clear automatic debt once tracing reaches sweep-candidate state     [CONFIRMED]

## No.1 Let backend #1/#2 automatic steps sweep existing candidates
### Code Change
Teach `pcc_gc_step()` to use remaining backend #1/#2 budget for
`pcc_gc_sweep_unreachable()` / `_sweep_unreachable()` whenever mark work is
done but sweep candidates exist.
### DENIED
This made the pcc-Python churn probe abort during the loop, after printing only
`backend 1`.

The attempted change exposed a safety boundary: current pcc-Python generated
programs are not yet proving all loop-local container roots to the tracing
backend during automatic collection. Sweeping candidates during allocation-time
auto-step can therefore reclaim objects still live in Python locals. Explicit
`gc.collect()` after the churn remains safe for this probe because those locals
are dead.

## No.2 Clear automatic debt once tracing reaches sweep-candidate state
### Code Change
Keep automatic backend #1/#2 work to mark/cut only. If a step processes no gray
objects and both `mark_active` and `cycle_requested` are false, clear
`pcc_gc_debt_bytes` even when sweep candidates exist. That prevents repeated
no-op auto-steps on every later allocation while leaving actual reclamation to
the explicit `gc.collect()` / tracing collect path.
### CONFIRMED
The focused pcc-Python churn gate now passes:

```bash
env -u LC_ALL /opt/homebrew/bin/timeout 240s \
  uv run pytest \
  'tests/test_gc_backend_incremental.py::test_incremental_backend_collects_container_churn_under_pcc_python_runtime' \
  -q -n0
```

Observed result: `1 passed in 23.42s`.

Backend #1 controls remain green:

```bash
env -u LC_ALL /opt/homebrew/bin/timeout 300s \
  uv run pytest tests/test_gc_backend_incremental.py -q -n0

env -u LC_ALL PCC_GC_BACKEND=1 /opt/homebrew/bin/timeout 300s \
  uv run pytest tests/test_gc_backend_incremental.py \
    tests/test_gc_g1_cycle_collector.py tests/test_gc_g2_finalizers.py \
    -q -n0 -rxX
```

Observed results: `3 passed in 2.05s` and `18 passed in 11.27s`.

Both runtime archives rebuild successfully:

```bash
env -u LC_ALL /opt/homebrew/bin/timeout 300s \
  make -B -C pcc/py_runtime libpy_runtime.a

env -u LC_ALL /opt/homebrew/bin/timeout 300s \
  make -B -C pcc/py_runtime PCC='uv run pcc' \
    PYTHON=/Users/jiamo/my/pcc/.venv/bin/python3 libpy_runtime_pcc_py.a
```

The current GC suite remains green:

```bash
env -u LC_ALL /opt/homebrew/bin/timeout 700s \
  uv run pytest tests/test_gc_*.py -q -n0 -rxX
```

Observed result: `172 passed, 14 xfailed in 156.07s`.

Self-bootstrap stage2 still builds no-libpython stage binaries:

```bash
env -u LC_ALL /opt/homebrew/bin/timeout 1800s \
  bash scripts/bootstrap.sh \
  --out-dir build/bootstrap-self-gc-autostep-1778162141 \
  --backend self --stage 2
```

Observed results: stage1 `9.371s`, stage2 `11.080s`.

The pcc-Python runtime matrix is green for all compiler stages and all five
backends:

| stage | backend 0 | backend 1 | backend 2 | backend 3 | backend 4 |
| --- | --- | --- | --- | --- | --- |
| `pcc0` | pass | pass | pass | pass | pass |
| `pcc1` | pass | pass | pass | pass | pass |
| `pcc2` | pass | pass | pass | pass | pass |

All 15 cells had `compile_rc=0`, `run_rc=0`, `collect True`, and
`allocs True`. Backend #1 now reports `steps 7` and `debt 120` in that matrix
instead of the pre-fix `steps 38641` failure.

The current pcc2-compiled 20k churn benchmark reports comparable medians across
all five backends:

| backend | median | notable telemetry |
| --- | ---: | --- |
| 0 refcount-cycle | `0.017220s` | `steps=0`, `debt=0` |
| 1 incremental-tricolor | `0.016305s` | `steps=7`, `debt=320` |
| 2 concurrent-mark-sweep | `0.016321s` | `steps=81`, `debt=320`, `cms_assists=69` |
| 3 generational-minor-major | `0.016614s` | `steps=20`, `minor_collections=1` |
| 4 colored-relocating | `0.017052s` | `steps=1`, `reloc_forwards=0` |

## Report (only when the investigation is closing)
Proposal No.2 landed. It fixes the backend #1 automatic-step debt spin without
letting allocation-time auto-step reclaim sweep candidates.

The denied alternative, sweeping candidates during automatic steps, reduced the
step spin but was unsafe for current pcc-Python generated programs because not
all loop-local container roots are proven to the tracing backend at allocation
time. Keeping automatic work to mark/cut and deferring reclamation to explicit
collection preserves the observed correctness envelope while restoring bounded
debt behavior.

## Correction (2026-10-22): automatic reclamation at application gates

Keeping automatic work to mark/cut meant GC1..GC4 never reclaimed a cycle
unless the program called `gc.collect()`. A loop dropping self-referencing
`__del__` objects finalized none of them before that call, and GC1/GC2's
collector thread (d58306406) began one full marking cycle after another,
since every allocation requests one: 200,000 such objects took 85 s against
5 s with `gc.disable()`.

The denied No.1 swept inside allocation-time steps, where loop-local
references were not yet rooted. Reclamation now runs where compiled code
reaches an application gate (`pcc_gc_safepoint`, polled through the
automatic-step flag): every managed reference is rooted there, as at an
explicit `gc.collect()` call, and GC4 already relocates only there. It
collects with the world stopped, exactly as an explicit collect does, but a
single cycle.

- GC1/GC2: the collector thread begins a cycle only once allocation debt
  reaches the pacing threshold or tracking has grown by
  max(threshold0, survivors of the last automatic collection), never while a
  completed mark waits for its sweep, and asks the next gate to sweep each
  mark it completes. `gc.disable()` and `gc.set_threshold(0)` pause it.
- GC3 (no collector thread): `py_gc_track` schedules one stop-the-world
  cycle at the next gate, as for GC0.
- GC4: its automatic step continues a mark within its budget until the mark
  completes (one call ends at each phase boundary, so a mark spanned several
  debt intervals and kept what died meanwhile); the gate then sweeps it.
- Pacing: the pause applies to the bytes the last completed mark found
  reachable (`pcc_gc_tracing_marked_bytes`), and a completed cycle pays the
  debt. Computed from the current live bytes, which include garbage awaiting
  a sweep, the default pause of 1000 made the threshold grow faster than the
  debt, and GC4 never stepped again once garbage accumulated.
- GC4 relocation: the collector's own evacuation
  (`pcc_gc_backend4_select_relocation_pages`) admits only objects a mark has
  reached, as in ZGC. It copied churned garbage, whose forwarding edge's hold
  kept the copy alive: 76,993 forwards in one 50,000-object loop. The
  explicit `pcc_gc_select_relocation_set` keeps its rule.
- GC4 explicit collect: retiring forwarding edges requests one more mark;
  garbage held only by an edge outlived `gc.collect()` (45,994 of 50,000
  finalized).

Measured with 50,000 objects (`node.self = node`, `__del__`), finalized
before the explicit collect, loop time and peak RSS: GC0 49,945, 0.49 s,
7.5 MB; GC1 41,889, 1.0 s, 21 MB; GC2 43,780, 1.0 s, 23 MB; GC3 49,945,
1.2 s, 9.2 MB; GC4 49,480, 1.5 s, 35 MB. Before: none for GC1..GC4; GC4
peaked at 116 MB.

Tests: `tests/python/test_tracing_gc_automatic_cycle_collection.py` (fails
before: "enabled False" on GC1), the pacing case of
`test_freestanding_gc_public_collection.py::test_pcc_python_cms_worker_starts_drains_and_joins`,
`test_gc4_idle_step_debt.py`. Five GC4 probes that relocate through the
automatic path now first mark their objects (`pcc_gc_set_mark_color(obj,
32)`), the state a completed mark leaves.
