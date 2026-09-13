# Investigation: list()/sum()/tuple()/set() consume DynType via len+getitem, silently yielding empty/wrong results for iterator-only objects (generators)

## Status
active

## 2026-09-13: remaining set constructor path blocks native gateway compilation

Current-source evidence supersedes the broad historical inventory below.
The source snapshot b39 is
`c634dcbc98d246fdedeb8780fd1fb34391be60fc77cf461955cdf7abc103a943`;
native stage1-R is
`d44da0173e2811aca8567b6cec9445fc0f731a1b9e2d6d738a1e493347932533`.
Evidence lives in `/private/tmp/pcc-owned-perf-20260912-rbrioqup`.

`gateway-comparison-r.json` records host compilation in 7.825 seconds, then
native compilation failing in 3.010 seconds with a missing managed delegation
slot for `TaskScope._retire_child`. No QPS sweep ran; the short failure time is
not a speedup. The compiler still has the separate normal 30-second smoke
timeout. `vthread-frame-probe/{plain,quoted}.py` reproduces the frame error in
13 lines with both ordinary and quoted class annotations.

LLDB traces distinguish the two analyses. During effect discovery,
`_receiver_class_name` sees `{'Scope'}` and `{'scope': 'Scope'}`. During frame
planning, it sees an empty class set and hint dictionary. Incoming method-call
arguments still contain the correct AST module and ordinary None defaults.
See `vthread-receiver-lldb.stdout`, `vthread-planner-receiver-lldb.stdout` and
`vthread-method-args-lldb.stdout`. After a heterogeneous cache lookup, the
method dictionary is dynamically typed. The remaining `set(x)` fallback
performs integer indexing, losing that dictionary's keys. This is not a
gateway-specific effect-analysis requirement.

The new `test_native_set_iterable_construction.py` initially returned empty
sets for dictionaries and generators, and silently accepted None and integers.
Nonliteral set/frozenset construction now uses `py_set_from_iterable`; set
unpacking and update use the widened `py_set_update`. C and Python runtimes
retain the existing set-to-set snapshot path and consume other inputs through
`iter`/`next`. Owned iterator/item roots are balanced on success and failure;
StopIteration from hashing propagates, while iteration exhaustion is cleared.
Partial update remains observable after an iterator raises. Existing literal
lowering remains separate; this does not qualify frozenset immutability.

Lifetime tests exposed two adjacent implementation gaps:

- Generic comprehensions did not release their iterator or each owned next
  result. They now use the existing rooted local/loop-target protocol, with
  cleanup on normal exhaustion, iterator errors and body errors. Temporary
  source ownership is consumed after the iterator has a rooted owner.
- Generator destruction discarded suspended frames without executing pending
  finally bodies. Shared finalization now closes started unfinished generators
  once, preserves the caller's exception and supports resurrection before
  metadata is retired. GC0's cyclic finalizer filter now includes generators;
  the tracing collectors already call the shared finalizer before clearing
  fields and rechecking reachability. This follows the ordering and exception
  requirements in [PEP 442](https://peps.python.org/pep-0442/). Unstarted
  generators do not enter their bodies. The shared unraisable-error reporting
  channel and the existing approximate GeneratorExit tag remain open.

`gen-finalizer-tests.stdout` records three focused tests passing with emitted
execution under GC0–4, including last-reference release, pending exception
identity, cycles, reentrant collection, resurrection and one-shot cleanup.
`park-finalizer-regressions-v2.stdout` records 48 passing checks and six
deselected native gates. Earlier expanded runs stopped on stale diagnostic
expectations: the current implementation already includes receiver type/module
and chained rejection causes; the three assertions now check those exact
messages. These diagnostics were not shortened to hide a boundary.

Actual C differentials are separate receipts. `set-iterable-c-oracle-v2` uses
the complete C set object with its graph-lock calls bound to the production
Python collector provider. `gen-finalizer-c-oracle` substitutes C generator and
finalizer dispatch objects while retaining the shared Python refcount/collector
entry hooks. Both execute their regressions on all five GCs. External cc is an
oracle only; these are not cold owned-runtime construction proofs.

The next frozen compiler source b40 is
`575ee975da1944d46eb984380966e3fccc5094b21219b60421762a2cb288ce67`;
runtime archive SHA-256 is
`923a7bd54b0c02a500bd0d8bbdb4b9fd6e050e1867a24436372aecaa3b7d4db7`.
The full stage1-S rebuild and native gateway replay are pending. Host-generated
test programs do not replace those native compiler gates. Runtime overlays
still use diagnostic Make/ar orchestration.

## Problem Description
Under strict no-libpython (`--backend self --python-libpython=off`, DEFAULT
runtime ports), `list(gen())` and `sum(gen())` over a generator return an empty
list / `0` instead of the generator's values. The generator itself is fine — a
direct `for v in gen(): ...` loop iterates it correctly via the iterator
protocol (`py_obj_iter`/`py_obj_next`). The gap is in the **sequence builtins'
consumption of a DynType argument**.

Root cause: `list_builtin_lowering._maybe_emit_list_builtin`'s DynType/Tuple/
Class arm (≈ line 188) iterates the source via `py_obj_len(src)` +
`py_obj_getitem(src, i)` (length + integer index). A generator is iterator-only:
it has no length and no `__getitem__`, so `py_obj_len(gen)` returns 0 and the
copy loop runs zero times → empty list. `sum()` (`_maybe_emit_sum_literal`)
has the same shape. This diverges from CPython, where `list(x)`/`sum(x)` always
use `iter(x)`/`next()`.

This is a silent WRONG-OUTPUT correctness gap (obligation 1): the program
compiles and runs under `=off`, producing wrong results, with no diagnostic.

## Repro
```python
def gen(limit):
    i = 0
    while i < limit:
        yield i * i
        i += 1
def main():
    for v in gen(3):
        print(v)            # pcc: 0,1,4  CPython: 0,1,4   (IDENTICAL — generator works)
    print(list(gen(3)))     # pcc: []          CPython: [0, 1, 4]   WRONG
    print(sum(gen(4)))      # pcc: 0           CPython: 14          WRONG
main()
```
```
env -u LC_ALL uv run pcc --backend self --python-libpython=off --ir-scaffold=on /tmp/gen.py -o /tmp/gen_bin
/tmp/gen_bin           # list(gen())==[] , sum(gen())==0
python3 /tmp/gen.py    # list(gen())==[0,1,4] , sum(gen())==14
```

## Update (2026-05-30): scope is BROADER + exact root cause localized
The gap is not limited to `list()`/`sum()`. A discriminating probe shows EVERY
DynType-iterable consumer except the statement `for`-loop is broken on a
generator under `=off`:

| consumer | pcc | CPython |
|---|---|---|
| `for x in gen(): t+=x` (stmt, var or inline) | 14 | 14 (WORKS) |
| `[x for x in gen()]` (comprehension) | `[]` | `[0,1,4,9]` |
| `sorted(gen())` | `[]` | `[0,1,4,9]` |
| `set(gen())` | `set()` | `{0,1,4,9}` |
| `tuple(gen())` | `()` | `(0,1,4,9)` |
| `list(gen())` | `[]` | `[0,1,4,9]` |
| `sum(gen())` | `0` | `14` |
| `max(gen())` | `0` | `9` |

Root cause (single shared pattern): the DynType iterable dispatch routes to a
**len + integer-getitem** loop instead of the **iterator protocol**
(`py_obj_iter`/`py_obj_next`). A generator has no length / `__getitem__`, so the
loop runs zero times.

- `comprehension_lowering._emit_comprehension_generator` line 910:
  `isinstance(iter_ty, DynType)` -> `_emit_comprehension_obj_indexed` (len+getitem,
  BROKEN). Line 924 `ClassType` -> `_emit_comprehension_obj_iterator`
  (`py_obj_iter`/`next`, CORRECT). So ClassType already iterates correctly;
  DynType does not.
- `list_builtin_lowering._maybe_emit_list_builtin` DynType arm (~188): same
  len+getitem.
- `for_loop_lowering` routes DynType through `py_obj_iter`/`py_obj_next` — which
  is exactly why the statement `for`-loop WORKS. **for_loop is the correct
  reference**; the comprehension + builtins should match it for DynType.

Revised recommended fix: make the DynType dispatch use the iterator protocol
(the `_emit_comprehension_obj_iterator` path / `py_obj_iter`+`py_obj_next`),
matching `for_loop` and the existing `ClassType` arm. This is a small dispatch
change per site (comprehension line 910 is one line: DynType -> obj_iterator),
but the behavioral blast radius is large (DynType comprehensions are pervasive
in pcc's own source), so the FULL bootstrap is the required safety net (the
for-loop already proves `py_obj_iter` works for the DynType iterables in the
pcc closure). Per-site, in priority order: comprehension (910), list builtin
(188 arm), then sum/sorted/set/tuple. If the full bootstrap regresses, fall back
to the tag-branch (No.1): only route generators (tag 20) through iter/next.

## Test [CONFIRMED]
Run 2026-05-30: the direct for-loop is diff-IDENTICAL; `list(gen())` and
`sum(gen())` DIVERGE (empty/0). Localized: the for-loop path uses the iterator
protocol (`for_loop_lowering` py_obj_iter/next) and works; `_maybe_emit_list_builtin`
DynType arm uses py_obj_len+py_obj_getitem and fails for iterator-only objects.
A focused regression must assert `list(gen())`/`sum(gen())` match CPython.

## Proposals
- No.1 runtime tag-branch in the builtins' DynType arm (iter protocol for tag-20 generators)  [pending — SAFEST]
- No.2 route DynType through the iterator protocol unconditionally  [pending — most CPython-correct, higher regression risk]
- No.3 add a `py_list_from_iterable(obj)` runtime helper, route DynType list()/tuple()/set()/sum() through it  [pending — cleanest, runtime change]

## No.1 runtime tag-branch (iter protocol for generators only)
### Code Change (described, NOT yet applied)
In the DynType arm of `_maybe_emit_list_builtin` (and the sum/tuple/set
equivalents), emit a runtime `tag = py_type_of(src)`; if `tag == 20`
(PY_TYPE_COROUTINE = generator/coroutine) emit an iterator-protocol loop
(py_obj_iter + py_obj_next until NULL/StopIteration, appending), else keep the
existing len+getitem loop. Lowest regression risk: only the currently-broken
generator case changes; indexable DynType (lists/tuples/`__getitem__`-only
classes) keep working exactly as today.
### Trade-off
Per-builtin duplication of the two-path branch; covers generators but not other
iterator-only objects (custom `__iter__`-only classes). Bounded + safe.

## No.2 iterator protocol unconditionally for DynType
### Code Change (described)
Replace the DynType len+getitem loop with a single iter/next loop. Matches
CPython exactly and fixes ALL iterator-only objects.
### Trade-off
`list()` is bootstrap-critical; this changes behavior for EVERY `list(DynType)`
in pcc's own source. Risk: a DynType with `__getitem__` but no `__iter__`
(sequence-protocol-only) would break, where CPython's list() falls back to the
sequence protocol. Must prove via full bootstrap that no such object exists in
the pcc closure. Higher risk; do only with the full bootstrap as the safety net.

## No.3 py_list_from_iterable runtime helper
### Code Change (described)
Add `py_list_from_iterable(obj)` (C + py-port mirror) doing CPython's list()
logic: try iter/next; this is the single source of truth, called from
list()/tuple()/set()/sum() DynType arms. Runtime change -> rebuild + full
bootstrap (~6 min gate).
### Trade-off
Cleanest and most reusable, but a runtime change (expensive gate, both C and
the pcc-Python port must mirror). Best long-term answer.

## Notes
Found by the realistic-program CPython-diff methodology (a closures/generators/
%-format program), same session as #18-#21 idiom shrinks. Distinct from those
additive fixes: this is a silent-wrong-output correctness hole in shared,
bootstrap-critical builtin lowering, so it gets the investigation workflow + a
careful proposal choice + full-bootstrap gating rather than a tail-of-loop
patch. The `max(3,7,2)` NameError found in the same program was the bounded,
low-risk sibling and was fixed separately (#21, frontend-only).
Recommended start: No.1 (safe tag-branch) for list() + sum(), gated by the repro
+ a focused regression + full bootstrap; escalate to No.3 if tuple()/set() and
custom-iterator classes need the same treatment.

## Update (2026-05-30): related but distinct gap — __getitem__-only iteration fallback
A realistic-program probe (real5.py: custom-class dunders) confirmed a related
but DISTINCT gap: `for x in obj` / `[x for x in obj]` where `obj` defines
`__getitem__` but NOT `__iter__` raises `TypeError` under =off, whereas CPython
falls back to the SEQUENCE protocol (calls `obj[0]`, `obj[1]`, ... until
`IndexError`). This is the `__getitem__`-only iterable case flagged as a risk in
the #22 fix — confirmed it was ALREADY broken (the fixes #22/#23/#24 route
DynType through `py_obj_iter`, which itself lacks the `__getitem__` sequence-
iterator fallback; the bootstrap passed because pcc's own code has no
`__getitem__`-only iterables).

- WORKS (verified same probe): `__len__`, `__getitem__` direct (`obj[i]`),
  `__contains__` (`x in obj`), `__repr__`, `__eq__`, `__bool__` (`bool(obj)`,
  `if obj:`, `if not obj:`) — all DEFAULT-mode diff-IDENTICAL.
- GAP: `py_obj_iter` does not synthesize a sequence-iterator for a
  `__getitem__`-only object. FIX (runtime, deferred): `py_obj_iter` (py_obj
  runtime + port) should, when the object has no `__iter__` but has
  `__getitem__`, return a small sequence-iterator object whose `__next__` calls
  `obj[i++]` and converts `IndexError` to `StopIteration`. Niche (modern code
  defines `__iter__`); lower priority than the generator-consumption sites.

## Update (2026-08-27): the same pattern from the MAPPING side, and what it cost

This investigation approached the pattern from iterator-only objects, where
`py_obj_len(gen)` is 0 so the loop runs zero times. A dict fails the *same*
lowering differently and more quietly: it **has** a length and it **has** a
`__getitem__`, so `py_obj_len(d)` returns the right count and the loop runs the
right number of times — but `py_obj_getitem(d, i)` is a **key lookup for the
integer key `i`**, so every probe misses. `set(d)` returned an empty set with
no error at all.

Sibling investigation with the full write-up:
[`set-and-frozenset-of-dict-lower-to-empty.md`](set-and-frozenset-of-dict-lower-to-empty.md).

### What it cost, so the priority of proposal No.2 can be judged

`pcc/backend/self_backend_precise_stackmaps.py` computes
`managed_names = frozenset(managed_origins) | ambiguous_managed`, and
`managed_origins` is a dict. **Inside pcc1 — which is pcc-compiled code — that
frozenset was empty.** So `_managed_live_after` tracked nothing and
`_planned_managed_reloads` returned `()` at every safepoint: pcc1 emitted
**zero managed-value reloads** into pcc2, pcc3 and every program it compiled,
for however long the bug existed. Managed reloads are what refresh a spill slot
from its root after a safepoint the collector may have moved objects across —
the mechanism GC backend #3 (generational forwarding) and #4 (relocating)
depend on. Emitting one real module: pcc1 0 reload triples, host pcc 1200.

This is the concrete answer to "is a silent wrong answer in a shared builtin
lowering worth the bootstrap gate": it hid a GC-soundness hole in the
self-hosted compiler for months, and no host-side test could see it because
CPython answers `frozenset(some_dict)` natively.

### Static `DictType` sites are now fixed; the `DynType` residue is not

Fixed by routing the statically known mapping to its keys (`py_dict_keys`), or
by delegating to a correct sibling implementation that already existed:

```text
set(d) / frozenset(d)   set_lowering.py                -> _materialize_dict_keys_view_set
dict(d)                 literal_lowering.py            -> dict_lowering._maybe_emit_dict_builtin
any(d) / all(d)         numeric_builtin_lowering.py    -> py_dict_keys normalisation
zip(d, ...)             tuple_zip_lowering.py          -> py_dict_keys normalisation
for a,b in zip(d, ...)  for_normalization_lowering.py  -> decline the index rewrite
```

Regression: `tests/python/test_native_set_from_dict_keys.py`, confirmed red
first. Focused gate: 48 passed across the zip/enumerate/dict/comprehension/set
families. Strict no-libpython closure rc=0 on every changed file.

Still open, and measured under pcc-native today:

```text
                pcc-native   CPython
set(dyn)        0            2
dict(dyn)       0            2
any(dyn)        False        True
list(dyn)       2            2   <- correct, via py_obj_iter
```

`list()` is right because its `DynType` arm already uses the iterator protocol.
**Proposal No.2 of this file is therefore the correct fix for the rest**, and it
now has a second, independent motivation: `py_obj_iter` on a mapping yields its
keys, so one dispatch change closes the generator case and the mapping case
together. It remains gated on the full bootstrap for the reason recorded above,
and that gate has not been run — so it is deliberately NOT applied yet.
Task-board row: `PY-P1-SET-FROM-DYN-MAPPING`.
