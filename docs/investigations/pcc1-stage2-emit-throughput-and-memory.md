# Investigation: `pcc1 -> pcc2` emit throughput and native-worker memory

## Status

active — two fixes landed and measured; the remaining gap is the runtime
object model / allocator, which is NOT yet fixed.

## Problem Description

`pcc1 -> pcc2` (stage2) takes ~17-18 minutes while `pcc0 -> pcc1` (stage1,
host CPython) takes ~5.5 minutes.  The owner's target is ~5 minutes for
stage2 and for each backend of the five-GC matrix.  A secondary report: a
stage2 emit worker holds 4.8 GB RSS where the host worker holds ~1.5 GB.

Predecessor: [pcc1-self-host-module-init-startup-sigbus.md](pcc1-self-host-module-init-startup-sigbus.md)
(the startup SIGBUS that blocked the matrix; fixed).

## Repro

```bash
# one huge module through the native emit worker
./build/bootstrap/pcc1 --pcc-self-backend-emit-worker <27MB>.ll /tmp/out.result
```

Observed before this investigation: does not finish in 10 minutes; RSS climbs
past 9.5 GB at ~68 MB/s.  The host worker completes a *larger* (43 MB) module
in 69 s at 1.56 GB peak.

## Findings [CONFIRMED]

### 1. Lane widths are hard-capped, but they are not the main cost

`pipeline_self_backend_emit.py` runs emit in tiers: oversized, huge (>=4 MB),
medium, small.  On a 12-core / 96 GB host the widths are 1, 2, 8, 12 — the two
heaviest lanes are the narrowest.  The live stage2 had 25 modules >= 4 MB and
3 oversized.  `jobs_for_input_sizes` additionally collapses to 1 whenever any
input is at or above the split threshold, unless `PCC_SELF_BACKEND_JOBS` is
set, and `scripts/bootstrap.sh` never sets it.  All of it is deliberate, with
the stated reason "a compiled emitter needs several GiB for a multi-megabyte
LLVM module" — i.e. the widths are a *memory* mitigation, so per-worker memory
has to come down before widening is safe.

### 2. Regex call volume — FIXED, measured

Counting every `re` call for one 27 MB module (host, `re.compile` wrapped):

```text
regex calls      : 11,062,490
top by bytes:
   3,174,111 calls  '^-?\d+$'
   2,320,434 calls  '^0x[0-9A-Fa-f]+$'
   2,320,434 calls  float-literal pattern
```

7.8M of the 11.0M calls were three token classifiers.  Under pcc1 every call
enters the pcc-Python regex engine (pattern-cache walk + matcher), which is
far more expensive than a str method.  Replaced with `_is_int_token` /
`_is_hex_token` / `_is_float_token` built from whole-string runtime calls
(`isdecimal`/`find`/`strip`) — never `text[i]`, which allocates a fresh
one-character str in the pcc runtime.

Result: **11,062,490 -> 3,247,511 calls (-71%)**, and cold stage1 went
**328 s -> 222 s** even though changing `self_backend_parse.py` invalidates the
whole self-backend object cache (`self_backend*.py` glob) and forces a cold
emit of every module.

Equivalence is gated by `tests/c/test_self_backend_parse_token_classifiers.py`,
which keeps the retired patterns as the oracle over a hand-written corpus plus
a generated 3-character token space (68 cases, includes non-ASCII digits and
the `$`-before-trailing-newline case).

An earlier hypothesis — that the engine's per-call `while index < text_length`
ASCII precondition scan made whole-text `search(ir_text, pos)` loops O(n^2) —
was **refuted by measurement**: only 0.31 GB total is rescanned for the whole
module, because nearly all calls are on short tokens.

### 3. Native-worker memory — LOCATED, not yet fixed

Reading the allocator's own live accounting from a running worker at t=100 s
(RSS 9.69 GB):

```text
pcc_allocator_live_requested = 6,171,471,628   (6.17 GB actually requested)
pcc_allocator_live_usable    = 7,320,614,304   (+19%  size-class rounding)
pcc_allocator_mapped         = 9,831,882,752   (+59%  vs requested)
```

Two distinct components:

- **Object model (dominant).** 6.17 GB is *live requested* — the program
  genuinely holds it. The host holds ~1.5 GB peak for a larger module, so pcc's
  representation of the same parsed module is several times heavier. This is
  the main lever and is not yet addressed.
- **Allocator overhead (~1.6x).** `pcc_allocator_size_class` only pools sizes
  up to 2048 bytes. Everything larger goes to `pcc_allocator_allocate_raw`,
  which adds a 48-byte header, rounds up to a whole 4096-byte page, and takes
  a **private `page_alloc` (mmap) per object**. Note the `sample` profile shows
  mmap itself is only ~0.2% of time, so this is primarily a *memory* defect,
  not the time bottleneck.

The time profile of the native worker (macOS `sample`, symbolicated) is
dominated by allocation and zeroing rather than syscalls:

```text
calloc                              628
memset                              504
py_int_to_i64                       427
pcc_gc_managed_pointer_find_slot    250
py_int_from_i64                     245
   under: user_py_re_engine_runtime__pattern_method_call (2081)
          -> py_re_engine_truth_flags_from (2045)
```

### 4. Byte-at-a-time memory primitives — FIXED, measured

The runtime's memory primitives were all byte-at-a-time loops in compiled
pcc-Python.  `pcc_calloc` had its own inline fill, and `pcc_memset`,
`pcc_memcpy` and `pcc_memmove` each looped `store_i8` once per byte:

```python
i: i64 = 0
while i < total:
    store_i8(ptr, i, 0)     # one byte per iteration, for every allocation
    i = i + 1
```

Every managed object is allocated through `calloc`, so this single loop was
**~19% of all samples** on its own (`memset` a further ~4%).  `calloc` and
`memset` now fill eight bytes per iteration (`store_i64`), aligning the
destination first so no wide store is ever unaligned.  Constants are inlined
and no helper function is introduced: module-level ints are zeroed in stripped
freestanding objects, and a `__pcc_freestanding__` module rejects any function
without a `@c_abi_export`.

After the fix the profile is flat — the former 1845-sample `calloc` peak is
gone and the new leader is 447 samples:

```text
py_int_to_i64                    447 + 240   (int unboxing)
py_int_from_i64                  293         (int boxing)
pcc_gc_managed_pointer_find_slot 278
_tlv_get_addr                    198         (thread-local access)
```

`memcpy`/`memmove` were left byte-wise deliberately: they did not appear in
the profile, so widening them is unmeasured work.

### 5. Why it is slow: generated code quality, not the runtime [CONFIRMED]

The decisive measurement.  This function:

```python
def add_loop(n: int) -> int:
    total: int = 0
    i: int = 0
    while i < n:
        total = total + i
        i = i + 1
    return total
```

compiles to **689 instructions and ~95 runtime calls**: 21 `pcc_gc_unpin`,
18 `pcc_gc_load_ptr`, 13 `pcc_gc_pin`, 12 `pcc_gc_store_root`,
10 `pcc_gc_release`, 14 frame enter/leave, 3 `py_int_from_i64` — and only
**2 `py_int_add`**.  An optimizing compiler emits ~8 instructions.  `total:
int = 0` alone costs five runtime calls (`py_int_from_i64`, `pcc_gc_pin`,
`pcc_gc_store_root`, `pcc_gc_unpin`, `pcc_gc_release`).

`PCC_PYTHON_IR_PASSES=on` produces a **byte-identical 689 instructions**: the
IR passes cannot help, because GC barriers are opaque calls no pass may
remove.  The shape comes from frontend lowering, so the bootstrap's
`PCC_PYTHON_IR_PASSES=off` default is not the cause.

Most of that traffic is provably dead: `pcc_gc_pin` starts with
`is_tagged_int(o)` and returns immediately, so every pin/unpin/store_root/
release on a small int does nothing at all.

Visible in the disassembly, all classic missing passes:

```text
no mem2reg          every local round-trips through a stack slot
                    (`stur` then immediately `ldur` the same slot)
no copy propagation `mov x1,x11; mov x9,x1; mov x10,x9`
no immediate forms  `mov x10,#1; lsl x11,x9,x10`  not  `lsl x11,x9,#1`
no inlining         tiny helpers stay real calls
no shrink-wrapping  py_int_to_i64 saves 10 callee-saved registers even on a
                    5-instruction fast path
```

### 6. Runtime fast paths — FIXED, measured

`py_int_to_i64` was the hottest symbol (117 instructions).  Its "fast" path
made two calls: `_set_overflow` and the `py_int_value_i64` extern.  Since
pcc does not inline, that is the whole cost.  The module docstring blamed a
missing intrinsic, but `untag_int` has since become a recognized
`pcc.unsafe` intrinsic (`ptrtoint` + `ashr 1`).  A tagged value is
`(v << 1) | 1` — always odd, never null — so the tagged test may safely
precede the null check.

The fast path is now five instructions and zero calls:

```text
tbz  w20, #0x0, <slow>     ; tagged?
cbz  x19, +44 ; str wzr, [x19]   ; *overflow = 0
asr  x0, x20, #1                 ; untag
```

### 7. The emit path was not the whole story — profile the phases you skipped

The earlier rounds all profiled the **emit** worker.  Sampling stage2's *main*
process (a phase never profiled before) showed a completely different #1:

```text
user_py_compiled_module_runtime__cstr_equal   519 samples, 434 leaf occurrences
  caller chain: _compile_python_multi_codegen_parallel_uncached
                 -> _build_python_frontend_shared_exports_parallel -> ...
```

Both compiled-module registries (`pcc_compiled_module_inits` and
`pcc_compiled_modules`) were singly-linked lists walked with `_cstr_equal`
per node.  With 500+ modules and a lookup per import/export resolution that
is O(modules) string compares, repeated throughout the compile — not just at
startup (measured startup is only 0.07 s).

Fix: a 512-bucket hash index over each registry (djb2 over the name, one
extra `hash_next` pointer per node; nodes grew 32->40 and 24->32 bytes).  The
existing linear `next` chains are untouched, so registration order and any
external expectation are unchanged — the buckets are a pure index.

Also `_py_ast_wire_field` did **two** dict lookups per AST field
(`name not in fields` then `.get(name)`), each hashing and string-comparing
the key, for every field of every node of every module; now one (subscript,
because `dict.get` mis-lowers under pcc1).

Verified: `cstr_equal` disappeared from the profile entirely (was #1 at 519
samples, now absent from the top 7).  The new leaders are

```text
pcc_gc_managed_pointer_find_slot  372 + 294 = 666   (GC index table, now #1)
py_int_to_i64 / py_int_from_i64   411 + 435 = 846   (int box / unbox)
pcc_gc_unpin                      304
```

which is the same structural conclusion again: GC bookkeeping and int
boxing.  **Method note: the cost is spread across phases (frontend workers,
main-process export resolution, emit, link), and each phase has its own
distinct hotspot.  Profiling only one phase produced three rounds of fixes
that never moved the total.**

### 8. The link phase is a further unmeasured cost centre

A rebuild whose incremental link cache missed spent **16+ minutes in
`pcc_link_macho.py` at 9.3 GB RSS** for the 963 MB image, single-threaded.
The parallel-assembly change earlier in this work covers the *assemble* step
only; the link proper was never profiled and is not covered by any proposal
below.

### 9. The binary is 90% stack maps, and they are 97% redundant [CONFIRMED]

Section breakdown of a current `pcc1` (904 MB total):

```text
__DATA,__pcc_stackmaps   810.2 MB   89.7%
__TEXT,__text             88.3 MB    9.8%
everything else            5.9 MB    0.5%
```

The GC stack-map metadata is **9.2x the size of the code it describes**.
This is why the link phase processes ~900 MB (16+ min, 9.3 GB RSS on a cache
miss) and why every one of the hundreds of worker processes maps it.

Decoding the section (format is documented in `precise_stackmap.py`: 32-byte
records, 16-byte locations):

```text
functions                 9,580
records               2,366,390
locations            48,344,838   = 737.7 MB
distinct location-lists  47,310
location bytes if interned          21.5 MB   -> 34.3x smaller
```

2.37M records reference only **47,310 distinct root-set shapes**; the single
most common list is the *empty* one, repeated 504,992 times.  Interning the
location lists and having each record reference `(index, count)` takes the
whole section from 810 MB to roughly 97 MB (75.7 MB of records + 21.5 MB of
interned locations) — the binary drops from 904 MB to about 190 MB.

This is a pure *encoding* change: the same root sets are described, shared
instead of repeated, so no GC semantics change.  It is nonetheless a
coordinated producer/consumer change and must be done as one:

- producer `pcc/backend/precise_stackmap.py` (bump `VERSION` to 2)
- consumer `pcc/py_runtime/py/freestanding_gc_mapped_roots.py`, which walks
  functions -> records -> locations (lines ~306-345)
- `tests/python/test_precise_stackmap_abi.py` and the emit-side tests
- re-verify all five GC backends, since this is the root-scanning contract

### 10. Barrier elision landed, with an honest negative result

Compile-time small-int literals are now materialized as tagged constants
(`(v << 1) | 1`) instead of calling `py_int_from_i64`, and are recorded as
provably-not-GC-objects; 104 `pcc_gc_pin`/`pcc_gc_unpin` call sites were
routed through checked helpers (`_gc_pin`/`_gc_unpin`), and `_gc_release`
gained the same check.  All three helpers are registered in
`L1_CODEGEN_HOST_METHODS`.

On the `add_loop` microbenchmark: 689 -> 665 instructions, `pcc_gc_unpin`
21 -> 16, `pcc_gc_pin` 13 -> 10, `pcc_gc_release` 10 -> 6,
`py_int_from_i64` 3 -> 0, output still correct.

**Negative result:** the stack-map section barely moved (811.4 -> 810.2 MB).
The expectation that eliding barriers would shrink the root set was wrong —
the roots come from *object* locals, not from int constants.  The 810 MB has
to be attacked by finding No.9 directly.

## Proposals

- No.1 Replace the three hot token-classifier regexes [CONFIRMED, landed]
- No.2 Widen the emit lanes [pending — still blocked on per-worker memory]
- No.3 Pool allocations above 2048 bytes instead of one mmap per object
  [pending — memory-only defect, ~0.2% of time]
- No.4 Word-at-a-time `calloc`/`memset` [CONFIRMED, landed]
- No.5 Reduce int boxing / GC-index-table / TLS cost in the object model
  [pending — this is now the dominant cost and belongs to the value-model
  (V-track) work, not to a hotspot fix]
- No.6 Call-free tagged fast path in `py_int_to_i64` [CONFIRMED, landed]
- No.7 Do not emit GC barriers for values that cannot be GC objects
  (compile-time small-int constants, statically tagged values) [pending —
  pure deletion, no semantic change: those barriers are already no-ops]
- No.8 Value lane for non-escaping `int` locals: keep them as raw i64 in
  registers, deopt to bignum on overflow [pending — the structural fix]
- No.9 Function inlining for small runtime helpers, plus mem2reg / copy
  propagation / immediate-form selection in the self backend [pending]
- No.10 Hash-index both compiled-module registries; single-lookup AST wire
  fields [CONFIRMED, landed — removed the then-#1 hotspot]
- No.11 Profile and optimize `pcc_link_macho.py` (16+ min, 9.3 GB, single
  threaded on a cache miss) [pending — never profiled]
- No.12 Cut GC index-table traffic (`pcc_gc_managed_pointer_find_slot`, now
  the #1 leaf) — largely the same work as No.7, since barriers elided for
  tagged values never reach the table [pending]
- No.13 Intern stack-map location lists (34.3x measured on 89.7% of the
  binary) [pending — highest measured leverage in the whole system]
- No.14 Elide barriers for compile-time tagged constants [CONFIRMED, landed]

### 11. Stack-map interning landed: 904 MB -> 196 MB [CONFIRMED]

Format version 2.  Records keep their 32-byte shape and reuse the trailing
reserved word as `location-index`; the location lists are interned into one
table emitted after every function.  The header grew by 8 bytes to carry the
table length (`HEADER_SIZE` 16 -> 24, mirrored in
`freestanding_abi_constants.py`).  Producers: `encode_stack_map`, the
by-hand `render_aarch64_stack_map_section`, and `merge_stack_map_payloads`,
which re-interns globally across objects — that merge pass is what collapses
the whole closure onto one shared set of shapes.  Consumer:
`freestanding_gc_mapped_roots.py`, which now computes the table base from the
payload tail and still validates each record's slice against *that function's*
frame size (the table is shared across functions with different frames, so a
single validation pass over the table would have been wrong).

```text
binary          904.4 MB -> 195.9 MB
__pcc_stackmaps 810.2 MB ->  94.1 MB
__TEXT,__text    88.4 MB ->  88.4 MB   (unchanged, as expected)
```

Two pcc-closure lowering limits cost a full rebuild each to find, because a
failure there surfaces only as an opaque `compile failed`:

- `dict.get()` mis-lowers under pcc1 into a raising getitem — use
  `key in d` + subscript.
- hashing a dict keyed by a tuple of frozen dataclass instances is not proven
  in the closure — key on a string instead.

**Process note for the next agent:** validate closure compatibility with a
targeted single-module compile, not a full bootstrap:

```bash
env -u LC_ALL uv run pcc --backend self --python-libpython=off \
  --ir-scaffold=on pcc/backend/<changed>.py -o /tmp/probe
```

That returns in a fraction of the time a 10-25 minute stage1 takes.

### 12. Allocator size classes raised 2048 -> 16384 [CONFIRMED]

`__mmap` was the **#2 leaf (536 of ~10000 samples)** in a stage2 *frontend
codegen worker* — an earlier note in this file judged the one-mmap-per-object
path a memory-only defect at ~0.2% of time; that was measured in the emit
worker and does not hold for the frontend phase.  Classes 4096/8192/16384 now
carve the same 64 KiB slab (strides 4144/8240/16432, counts 15/7/3, all
verified to fit).

### 13. What is still NOT solved

stage2 remains ~30-50 minutes.  The last profile of it shows the parallel
frontend workers finishing and then a **long single-threaded phase in the main
process** (84.9% of one core, 5.1 GB RSS, tens of minutes).  That serial phase
is the next thing to identify — it was not sampled successfully before this
session ended.

Also unmeasured: `_tlv_get_addr` was the **#1 leaf (844 samples)** in the
frontend worker — every post-call error check reads thread-local state, and on
macOS each such access is a dyld function call.  Caching the TLS base per
function, or making the error flag non-TLS when threads are idle, is untried.

### 14. Where stage2 actually spends its time [CONFIRMED]

Sampling stage2 while it ran, rather than reasoning from the emit worker:

- At t=21 min it is **still in the frontend phase**, with 10
  `--pcc-python-multi-codegen-worker` processes at ~78% CPU each, each living
  ~22 s before the next module.  The frontend, not a serial tail, is the bulk
  of stage2.
- An earlier note in this file described "a long single-threaded phase in the
  main process" (84.9% of one core, 5.1 GB).  That phase is real but comes
  *after* the frontend; a watcher script that waited for the workers to drain
  did not catch it within its window, so it remains unsampled.

Re-sampling a worker with the current pcc1 confirms finding No.12 landed:
`__mmap` is **gone from the profile** after the allocator size classes were
raised.  The leader is now, by a wide margin:

```text
_tlv_get_addr                   403     (~3x the next entry)
pcc_gc_load_ptr                 134
memset                           99
pcc_py_gc_minor_graph_unlock     67
```

### 15. Thread-local lookups: the lock pair read the same TLS twice [FIXED]

Attributing the `_tlv_get_addr` samples to their callers gives
`pcc_py_gc_minor_graph_lock`/`unlock` ~261 of ~403, with
`pcc_gc_frame_node_pool_heads_get` a distant 31.

On Darwin every `global_addr` on a thread-local is a `_tlv_get_addr` **call**.
Both functions took the address of `g_tls_pcc_py_gc_minor_graph_lock_depth`
twice — once to read the depth and again to store it — so the re-entrant fast
path, which is the common case, paid for two lookups where one does.  Both now
hoist the address into a local and reuse it.

Generated code calls this pair through the GC frame protocol on essentially
every function with a rooted local, so the call frequency is the same order as
the frame enter/leave count.

Verified: the runtime archive rebuilds clean, stage1 216 s, and all five GC
backends still start, compile and run correctly.

**Faster validation loop (use this instead of a full bootstrap).**  A
`__pcc_freestanding__` runtime port is not standalone-compilable as a program,
so the single-module `uv run pcc ...` probe does not apply to it.  Build just
the archive:

```bash
env -u LC_ALL PATH="$PWD/.venv/bin:$PATH" make -C pcc/py_runtime libpy_runtime_pcc_py.a
```

That compiles the changed port in seconds instead of a 10-25 minute stage1.

## Technique gap versus other compilers

Each measured pcc problem has a named, documented technique behind it; none
of this is unreachable, it simply is not implemented yet:

```text
int locals heap-boxed + GC-rooted   escape analysis + scalar replacement
                                    (Go, HotSpot, Graal)
no inlining at all                  inlining — the enabling optimization that
                                    everything else in Graal/HotSpot builds on
locals round-trip through stack     mem2reg / SSA construction + regalloc
GC barriers are opaque calls        inline 2-3 instruction write barriers
                                    (this is also WHY IR passes are useless
                                    here: a pass may not touch opaque calls)
811 MB of stack maps                HotSpot/Graal oopmaps are a few percent of
                                    code size; interning + delta encoding
int boxing                          speculation + deopt (Graal), i.e. exactly
                                    the value lane already in AGENTS.md
```

Already present, so not on the list: cached string hashes (`PyStrObject.hash`,
FNV-1a with -1 meaning "not yet computed") and the tagged small-int
representation itself.

## Report

**Not closed, and the 5-minute target is not met.**  Three defects were found
and fixed, each verified:

```text
No.1 token classifiers   regex calls 11,062,490 -> 3,247,511 (-71%)
No.4 word-at-a-time zero calloc's 19% single-function peak removed
     (both also help the host: cold stage1 328 s -> 222 s)
```

A fourth fix (No.6, `py_int_to_i64`) removed the two calls from the hottest
symbol's fast path.

What did NOT move: **stage2 wall time**.  One 27 MB module still does not
finish in the native emit worker inside 10 minutes (11.4 GB RSS at t=10 min),
and a stage2 run was still in its frontend phase — 10 parallel
`--pcc-python-multi-codegen` workers — at 36 minutes before it was stopped
(that run was cold, since every fix invalidates the object-cache identity, so
it is not directly comparable to the ~18 min warm figure).

The honest conclusion is finding No.5: **micro-optimizing runtime helpers
cannot fix this.**  Four hotspot fixes landed, each verified, and the
end-to-end number did not move, because the generated code spends ~10 runtime
calls per loop iteration to perform one integer add.  Reaching the 5-minute
target requires No.7/No.8/No.9 — stop emitting dead GC barriers, keep
non-escaping ints in registers, and give the backend inlining + mem2reg —
not another hotspot.

Sequencing note that still holds: do not widen the emit lanes (No.2) before
the per-worker footprint comes down.  A worker was still at 9.4 GB RSS at
t=75 s; the current widths (oversized 1, huge 2) exist precisely because of
that.

Correctness after both changes: `pcc1 --help` rc=0 on `PCC_GC_BACKEND=0..4`,
a real program compiles and runs correctly, and the focused gates for every
touched subsystem pass (403 passed, 2 deselected: self-backend, verifier,
token classifiers, arm64 encoder, freestanding mem/allocator, bootstrap-gate
baseline).

## Update — No.10 CONFIRMED: the frontend, not the runtime, owned the compile

The four earlier fixes targeted runtime *leaf* symbols and did not move the
end-to-end number.  Attributing the same profile by **caller** instead of by
leaf changed the answer completely.

Technique (worth reusing — it is what made this finding possible):
`sample <pid>` emits a call graph whose tree lines are prefixed with
`+ ! : |`, so a naive `^(\s*)(\d+)` indentation parse silently matches
nothing.  Strip that prefix set, resolve each `load address + 0xOFF` against
`nm -n` of the same binary via bisect, and the tree becomes a real
caller/callee attribution.  Symbolization was spot-checked by confirming each
hot address lands well inside its symbol's extent.

What that showed, on `pcc1` emitting IR for `pcc/py_frontend/type_infer.py`:

```
_ensure_post_call_frame_block   30.9% inclusive of the codegen phase
  of which: find_slot 831, minor_graph_lock 472, store_root 215,
            py_int_to_i64 272, py_int_from_i64 184   (~2000 samples)
```

That function's whole body is a cache lookup.  The cost was the **key**:

* `key = (parent_fn.name, err_target.name, func_name, file_name, line)` built
  a 5-tuple on every call that may raise.  In pcc a tuple is a GC object, so
  each lookup paid allocation + `pcc_gc_pointer_register` + object-graph lock
  + managed-pointer slot probe, plus `py_int_from_i64` to box `span.line`,
  plus per-element hash dispatch.
* `_emit_exception_frame` **re-opened and re-`splitlines()`d the entire source
  file** for every traceback frame, to read one line — `_str_splitlines_impl`
  was 1.8% of all samples on its own, and each call allocated a fresh list of
  every line in the module.

Fixes, both verified byte-identical (host `pcc0` and self-hosted `pcc1` each
emit the same IR for `type_infer.py` as before):

1. `_post_call_frame_blocks` became nested cheap-key dicts:
   `id(parent_fn)` → `err_target.name` → `line`.  `func_name`/`file_name` drop
   out because one LLVM function comes from one def in one file.
2. `_source_file_lines_cache` reads and splits each source file once.

### A negative result worth keeping

The first attempt nested on `parent_fn.name` and made it **worse**:
30.9% → 34.3%.  Mangled LLVM function names are ~80 characters and nothing
caches a string's hash here, so keying on one re-hashes those bytes on every
lookup — more expensive than the tuple allocation it replaced.  `id()` is a
tagged int and allocates nothing.  **In self-hosted pcc code, prefer an
identity/int key over a long-string key.**

### Result

```
_ensure_post_call_frame_block   30.9%  ->   6.6%  of the codegen phase
_emit_post_call_err_check       38.8%  ->  17.2%
```

Wall-clock is deliberately not quoted for this slice: a `Virtualization.framework`
VM was consuming 79–241% CPU throughout, which stretched the same stage1 build
from 211 s to 429 s.  Profile-relative attribution is contention-insensitive;
wall time then was not.  Re-measure wall time on a quiet machine.

### Fallback ratchet breach from the earlier stackmap-v2 slice (fixed)

`test_per_module_fallbacks_under_ratchet` went red at
`pcc.backend.precise_stackmap: 27 vs baseline 15`.  Cause was this
investigation's own v2 interning work: `struct.pack_into` /
`struct.unpack_from` / `_FUNCTION.unpack_from` have no native lowering, so
each is a CPython fallback in the self-compiled closure.  Open-coding the
little-endian field reads and writes in `merge_stack_map_payloads` restored
the baseline exactly (27 → 15 actions, 162 → 63 total fallback calls) and
removes a `struct` call per safepoint record from the link's hot loop.

Two stale test artifacts surfaced while confirming that and were fixed:

* `test_freestanding_gc_mapped_roots.py`'s C harness template was a plain
  f-string, so `printf("...\n")` reached clang as a real newline inside a
  string literal — a hard C syntax error.  It is now `rf'''`.
* the same test corrupted `malformed[81]` to assert the consumer fails closed
  on an unmanaged location.  Since v2 moved locations into one shared table at
  the end of the payload, offset 81 is no longer a location, so that assertion
  had been passing without testing anything.  The offset is now derived from
  the payload's table count.

### Gates

`PCC_GC_BACKEND=0..4` each compile and run a real program correctly with the
rebuilt `pcc1`.  Green: `test_precise_stackmap_abi`,
`test_freestanding_gc_mapped_roots`, `test_macho_semantic_layout`,
`test_pcc_stdlib_struct` (119 passed), and `test_fallback_baseline`,
`test_ir_py_fallback_baseline`, `test_bootstrap_gate_baseline` (37 passed).
Two pre-existing HEAD-red tests are unrelated and unchanged by this slice:
`test_borrowed_object_local_rebind_keeps_gc_root` (asserts the pre-GC-rework
plain-`store ptr` shape; codegen now emits the stricter `pcc_gc_store_root`
barrier) and `test_absolute_from_import_detects_native_extension_alias`.

### Still open

The self-host gap is now the headline number: host `pcc0` emits IR for
`type_infer.py` in **4 s**; native `pcc1` needs **95 s** for the same work.
A ~24x penalty for the compiled compiler is the thing to explain next, and it
is not explained by any single hotspot — the remaining leaf profile is
GC provenance ~31%, type dispatch ~12%, int box/unbox ~12%, refcount ~7%,
which is the signature of No.7/No.8/No.9 (dead barriers, boxed locals, no
inlining/mem2reg), not of another cache to fix.

## Update — No.11 CONFIRMED: every GC barrier ran a linear type-object scan

Continuing the caller-attribution work from No.10 on the stage2 cost (pcc1
compiling pcc source).  Benchmark throughout: `pcc1 --python-library
--emit-llvm` on `pcc/py_frontend/type_infer.py`.

### The chain

`_is_type_object` was the #2 leaf at 8.1% and is a 24-way linear
`ptr_eq(o, global_addr("PyXxx_Type"))` scan.  The emitted IR of the function
it appeared under does not call it at all — the intervening frame is elided by
the tail-call pass.  The real chain is:

```
any GC barrier (pcc_gc_store_root / pin / _ptr_is_class / ...)
  -> pcc_gc_pointer_is_managed
    -> _pointer_is_managed_no_lock
      -> pcc_capi_is_type_object_value   -> _is_type_object   (24 compares)
```

`_pointer_is_managed_no_lock` is a disjunction of side-effect-free lookups,
and it ran the 24-compare scan **third**, before
`pcc_gc_managed_pointer_index_contains` — one hash probe, and the case that
actually hits for a real managed object.  So every barrier in every compiled
program walked the builtin type list before reaching its answer.

Fix: move the two O(1) index probes ahead of the linear scan, in both
`py/py_gc_backend.py` and the `src/py_gc_backend.c` oracle.  Reordering a pure
disjunction cannot change the result.

**54s, from 87s.**  `_is_type_object` left the top-14 leaves entirely.  IR
byte-identical to the pre-optimization `pcc1`; `PCC_GC_BACKEND=0..4` all
compile and run a real program correctly.

### Two smaller changes in the same area

* `run_compiled_default_tier` passed `_mem2reg_function` as a **function
  value** to `_rewrite_functions`.  A function value crossing a call boundary
  lowers to the fully dynamic path — build an argument tuple, `py_obj_call` to
  resolve the callable, enter its native adapter, unpack and marshal — once
  per `define` in the module.  Confirmed in the emitted IR
  (`call @py_obj_call(ptr %transform, ptr %call.args, ...)`), and fixed by
  calling the transforms directly; the IR now shows direct calls and
  `_mem2reg_function_native_adapter` is gone from the profile.
* The two transforms are strictly per-function, so `sroa(mem2reg(body))` per
  function replaces two full traversals of a multi-megabyte IR text with one.
  Equivalence checked on 33 MB of real IR across three files, and pinned by
  `test_fused_traversal_matches_two_separate_passes` with the historical
  two-pass shape kept as an executable oracle.

**Negative result worth recording:** neither of those two moved the number.
`run_compiled_default_tier` stayed at ~69% and its children were unchanged
after both.  The traversal was never the cost — the GC barriers inside it
were, which is what No.11 above found.  Halving a traversal that is 95%
barrier overhead halves nothing.

### Where stage2 stands now

```
95s  ->  87s  ->  54s      (same benchmark; machine contended throughout)
```

Leaf profile after this round:

```
15.9%  py_int_to_i64          | int box/unbox      26.2%
10.3%  py_int_from_i64        |
11.0%  pcc_gc_managed_pointer_find_slot   | GC provenance ~25%
 4.6%  pcc_gc_pin / 4.1% unpin / 4.2% graph_lock / 2.1% is_managed
 6.3%  py_incref / 5.2% py_decref         | refcount 11.5%
```

The next targets in order of measured size are int box/unbox and the
managed-pointer probe itself — both are the No.7/No.8 structural items (dead
barriers on values that cannot be GC objects; boxed integer locals), not
another mis-ordered predicate.

All wall-clock figures in this section were taken with a
`Virtualization.framework` VM consuming 65-88% CPU, so they understate the
gains; profile shares are unaffected.  Gates: 50 passed
(bootstrap-gate/fallback/ir-fallback/compiled-default-tier), 210 passed on the
GC backend files with the same 6 pre-existing HEAD-red text assertions as
before this work (`: int` expected where the unmodified sources say `: i64`).

## Update — No.12 CONFIRMED: scaffold constants were boxed and immediately unboxed

Found by reading emitted IR, not by profiling — the leaf profile only says
"int box/unbox is 26%", which is indistinguishable from real integer work.
The IR says what it actually is:

```llvm
%native.const.int = call ptr @py_int_from_i64(i64 16)      ; box the literal 16
%m.int_unbox      = call i64 @py_int_to_i64(%native.const.int, %ov.flag)
```

Every `load_i64(obj, SOME_OFFSET)` in the runtime ports hits this.  The offset
is a module-level Python int constant, so it is boxed into an object to
satisfy the name's object type, then unboxed again to satisfy the `i64`
parameter: **two runtime calls, one of them allocating, to pass a
compile-time integer.**  Struct offsets and type tags are everywhere in the
ports, which is why every per-line helper carried 4-8 of these.

Two changes, both in the frontend:

1. `_emit_native_module_constant` now materializes an int scaffold constant
   through `_emit_int_literal_object`, the same path ordinary int literals
   take — a small value becomes a tagged constant (`inttoptr((v << 1) | 1)`),
   so there is no call and no GC barrier.
2. `marshal_from_object` folds the unboxing away when the value is a recorded
   boxed literal.  The IR builder returns opaque handles with no operand
   access, so a value cannot be asked what it boxes; the producer records it
   in a side table in `marshal` keyed by `id`, holding the value itself so the
   id cannot be recycled.  `generation_lowering` clears it per module.

```
runtime port int box/unbox calls   10426  ->  5572   (-47%)
pcc/py_runtime/py/py_list.py         259  ->    49   (-81%)
benchmark                             54s ->    30s
```

`pcc1`'s emitted IR for `type_infer.py` is still byte-identical to the
pre-optimization compiler (scaffold constants appear in the runtime ports, not
in frontend modules, so this changes how pcc1 is *built*, not what it
*emits*).  `PCC_GC_BACKEND=0..4` all compile and run correctly.

### Build-cache hazard found while measuring this

`pcc/py_runtime/build_py/*.o` is keyed by **source** hash
(`source_sha256`, `ir_sha256` in each `.o.provenance.json`) and does not
invalidate when the *compiler* changes.  A frontend/codegen change therefore
leaves the runtime archive silently stale — the first rebuild attempt here
reported `rc=0` and produced a 6 MB archive with none of the change in it, and
the boxing count was unchanged at 10426.  Deleting `build_py/*.o` and
`*.provenance.json` is what forces re-emission.  Anyone measuring a codegen
change against the runtime must do that first or they will measure nothing.

### Running total for stage2

```
95s  ->  87s  ->  54s  ->  30s
```

Contributions, in the order they were found: the exception-frame cache key and
source-file re-read (No.10), the GC provenance predicate order (No.11), and
scaffold-constant boxing (No.12).  The two changes that came from reading the
profile rather than the code — fusing the pass traversals and nesting the
cache on function names — contributed nothing and one was a regression.

## Update — No.13: stage2 measured end-to-end, and it FAILS in self-backend emit

First completed full-bootstrap measurement of this investigation.

```
stage1     90s   (warm; cold is 700-780s)
stage2   5429s   = 90.5 min, then FAILED (EXIT=1)
stage3      —    never ran
```

The failure is not a compile error:

```
/bin/sh: line 9: 68889 Killed: 9  ( pcc1 --pcc-self-backend-emit-batch-worker .../self_backend_emit_oversized_0.manifest )
error: PCC-PY-COMPILE-001: [python-frontend] compile failed
```

**Who sent SIGKILL is unknown.** Ruled out: the system OOM killer (96 GB machine,
40 GB free at the time, no jetsam record for the pid), an `rlimit` (none set),
and any in-repo RSS cap or watchdog (none exists in
`pipeline_self_backend_emit.py` or the generated batch shell). Not recorded as
a cause because it is not established.

### Localizing without a 90-minute run

`self_backend_emit_oversized_0.manifest` lists four shards, processed serially
in one process:

```
split_72_shard_1  -> native_158.s   95MB  produced
split_75_shard_2  -> native_164.s   45MB  produced
split_189_shard_3 -> native_387.s   killed here
split_14_shard_2  -> native_59.s    never reached
```

The failing shard is the **smallest** of the group, which rules out raw size:

```
split_189_shard_3 (fails)   187949 lines  14357 blocks   31071 SSA  208 phi
split_72_shard_1  (ok)      582235 lines  72100 blocks  139804 SSA    0 phi
```

Running that one shard alone reproduces it in minutes instead of 90: RSS climbs
monotonically past 24.5 GB and it does not finish. Two hypotheses were formed
and both **DENIED** by that probe: it is not memory accumulated across the four
shards (one shard alone does it), and it is not phi handling (the phi-count
difference above is suggestive but the profile never enters phi code).

### What it actually is

```
build_stack_map_plans -> build_function_stack_map_plan -> add_record   97-99%
  py_func_call_kwargs                          ~71% of add_record
  pcc_gc_managed_pointer_find_slot             19-41% of add_record
```

`add_record` is a ~30-line closure that runs once per safepoint. Its real
lowered body, read from the bootstrap's own module IR, is **286 lines with 120
calls**, about 90 of them GC barriers.

(An earlier revision of this section reported 3648 lines / 1410 calls / ~800
barriers for `add_record`. That was an extraction error: the symbol's first
occurrence in the `.ll` is a *call site* inside
`build_function_stack_map_plan`, so searching backwards for `define` from it
yields the enclosing function's body, not the closure's. Those figures belong
to `build_function_stack_map_plan`, which runs once per function, not per
safepoint. Anchor on a `define .*@<exact symbol>\(` line, never on the first
textual occurrence of the name.)

### Fixed and verified

`PlannedSafepoint` was constructed with 8 keyword arguments per safepoint.
Making that positional (fields are in declaration order) **halved peak RSS on
the same shard and manifest**, and changed the shape of the curve from
monotonic growth to one the GC can claw back:

```
          before      after
t=120s   13600MB     6638MB
t=210s   21927MB    10551MB
peak     24509MB    12783MB
```

### Did not help — recorded so it is not retried

Three further keyword-only signatures were made positional
(`_local_label`, `add_record` itself, `_planned_managed_reloads`) and
`_locations(active)` was memoized on a version counter bumped only where
`active` can change (block boundary, and `_apply_frame_protocol` returning
true — that branch emits no record). Unit gates pass (32) and the logic is
sound, but **the shard emit did not get faster and `py_func_call_kwargs`
stayed at ~71% of `add_record`.**

The reason is not yet established, and four hypotheses have now been
**DENIED** by reading the real lowered IR:

* not accumulation across the four shards in the worker (one shard alone does it)
* not phi handling (the profile never enters phi code)
* not keyword call sites (removing every keyword-only signature changed nothing)
* not constructor dispatch — `add_record` lowers the frozen dataclass to
  `py_instance_new` plus a **direct** call of
  `PlannedSafepoint___init__`; its body contains **zero**
  `py_func_call_kwargs`, and so do `_locations`,
  `PlannedSafepoint___init__`, and in fact the **entire module** (0 call sites).

Intersecting `add_record`'s direct callees with every runtime-port function
that does emit `py_func_call_kwargs` leaves only `py_int_to_i64`, whose kwargs
path sits behind the C-extension number protocol — unreachable for a tagged
small int, which is what `ordinal` is.

So the profiler's attribution of `py_func_call_kwargs` to `add_record` is not
explained by any call `add_record` actually makes. It appears at 100% inclusive
at the top of the stack as well, which is consistent with it being an
**ancestor** frame on the worker entry path rather than a descendant, with the
tail-call pass flattening the tree between them. **Do not optimize against this
attribution until the frame is identified some other way** (a debugger
breakpoint on `py_func_call_kwargs` with a backtrace, or an emit built with the
tail-call pass disabled).

This is the third time in this investigation that a "direct child" in `sample`
output was actually a grandchild behind an elided tail-call frame
(`_is_type_object`, the int box/unbox helpers, and now
`py_func_call_kwargs`). Read the real lowered IR before acting on a caller
attribution.

### The two remaining blockers, stated precisely

1. **The `py_func_call_kwargs` frame is unidentified.** See the DENIED list
   above. It needs a debugger backtrace, not another guess from the profile.
2. **Barrier density.** ~90 GC barriers in a 30-line function, per safepoint,
   plus `pcc_gc_managed_pointer_find_slot` at 19-41% of the emit. The index
   itself is sound (load factor 0.5, back-shift deletion, power-of-two
   capacity, Fibonacci-mixed pointer hash), so this is allocation volume, i.e.
   the No.7/No.8 structural item (dead barriers on values that cannot be GC
   objects, boxed locals) — not a mis-ordered predicate and not a table bug.

Neither is a small edit, and neither was attempted here. `stage2` remains
**red**: 90.5 minutes and it does not complete.

## Update — No.14 ROOT CAUSE CONFIRMED: every dynamic method call leaked two objects

The `py_func_call_kwargs` attribution in No.13 was a dead end (an ancestor
frame flattened by the tail-call pass). The real cause was found by
discriminating **leak** from **slow**, which the profile cannot do.

### The experiment that turned it

Run the same failing shard under two GC backends:

```
gc0 (refcount)  180s -> 19.5 GB, monotonic growth
gc3 (tracing)   180s -> 11.1 GB, plateaus and falls back
```

A block of memory that refcounting cannot reclaim but tracing can means
**dropped references**, not fragmentation and not a big working set. That
split is what pointed at codegen ownership instead of at any backend pass.

An in-process heap census (lldb, sampling 739 of the 51327 VM_ALLOCATE slabs)
then named the garbage: tag=16 objects dominated, next tuples of length 2, then
short strings — and the string contents were `'groupdict'`, `'groups'`,
`'group'`, `'end'`, `'start'`, `'span'`. Those are exactly the six method names
`py_re_engine_runtime._new_match` attaches to every `re.Match`.

### Two independent leaks, both in the dynamic method-call path

`method_call_expression_lowering` lowers a DynType-receiver `obj.method(args)`
to `py_obj_getattr` + `py_obj_call`, and released neither result:

1. **The bound method object.** `py_obj_getattr` returns a NEW reference on
   every path — fields and `__dict__`/`__class__` incref, the dynamic-attr
   path goes through `py_dict_get` (which increfs), a descriptor `__get__`
   yields an owned result, and a plain method builds a fresh bound object via
   `py_instance_bind_method`. `py_obj_call` only borrows the callable, so the
   emitting frame owns it. **All 21 `py_obj_getattr` emission sites in codegen
   released nothing.**
2. **The call result.** `py_obj_call` unconditionally returns a new reference,
   but the statement-level `_gc_release_if_owned` asks
   `_expr_returns_owned_object`, which sees only a DynType Call and answers
   "not owned". The AST shape genuinely cannot decide this: a dozen native
   emitters intercept the same shape and return borrowed values. Only the
   emitter knows which path it took, so it now records the value in
   `_owned_dynamic_call_values` and the release consults that set first —
   mirroring the existing `_cpy_values` / `_owned_cpy_values` split.

### Minimal repros, each isolating one leak

```
2M x obj.method() where method returns None   2860 MB -> 2 MB   (leak 1 only:
                                                the result is immortal None)
300k x pat.match(text), result discarded      1728 MB -> 2 MB   (both leaks)
300k x pat.match(no-match)                       2 MB           (control: the
                                                leak scales exactly with the
                                                Match objects created)
```

The control matters: a non-matching pattern allocates no Match and stays flat,
which is what ties the leak to the returned object rather than to the engine.

### Scale

~1.4 KB leaked per dynamic method call. `self_backend_parse` alone emits 40
`py_obj_getattr` sites, and the IR parser runs one match per operand, so an
oversized stage2 shard leaked its way to 24 GB. This was never a stack-map
algorithm problem: `add_record` was 97% of the profile because it was the
allocation site being starved by a heap full of garbage the collector could not
free.

### Refuted along the way (do not retry)

Beyond No.13's list: `_locations` rebuild cost, the `.sort(key=lambda)`
keyword call, `_stable_id_bytes`, and the managed-pointer index itself (load
factor 0.5, back-shift deletion, power-of-two capacity, Fibonacci-mixed hash —
all sound; its 19-41% share was call volume driven by the leak). Also note the
host-CPython control that should have been the first clue: the identical
planning code over the identical shard completes in **9 seconds using under a
gigabyte** under CPython, versus >20 minutes and 24 GB under pcc1. A 167x gap
against an 8-24x baseline was itself evidence of a leak, not of a slow pass.

## Update — No.15 DENIED: host-CPython profiling is the wrong cost model for pcc1

Attempted the largest remaining item the host profile named, and it was a large
**net loss** under pcc1. Recording it in full because the failure mode
generalizes to every future optimization of this file.

### The change

`safepoint_id(identity, ordinal, kind)` hashes
`b"safepoint\0" + identity + b"\0" + str(ordinal) + b"\0" + str(kind)` one byte
at a time. `identity` is a ~80-character mangled symbol, so ~100 bytes are
re-hashed per safepoint while only the last few vary. FNV-1a is a streaming
hash, so the constant prefix can be absorbed once per function and resumed per
record — `stable_id_prefix_state` / `stable_id_resume`.

Bit-identity was proven exhaustively (5 symbols including a 200-character one x
6 ordinals x every `SAFEPOINT_KIND`, all equal).

### Host result: good

```
build_stack_map_plans   9.0s -> 8.1s   (this change alone)
add_record              2.32s -> 1.40s
_stable_id_bytes        0.97s -> out of the top 24
```

### pcc1 result: catastrophic

A stage-1 smoke input that normally compiles in seconds:

```
26+ minutes, RSS 15.6 GB, killed
  add_record                            100%
    stable_id_resume                     89.8%
      pcc_gc_managed_pointer_find_slot   59.2%
      pcc_gc_managed_pointer_index_insert 7.4%
```

### Why

Resuming needs `("\0" + part).encode("utf-8")` per part: a concatenation, an
encode, and the bytes object — **three fresh allocations per safepoint**, each
of which enters the global managed-pointer index. The original
`"\0".join(fields)` allocates **once**.

Host CPython's cost is dominated by interpreter-loop iterations, so trading
allocations for fewer loop iterations wins there. pcc1's cost is dominated by
**allocation count and GC index traffic**, so the same trade loses badly. The
two cost models point in opposite directions for this exact transformation.

**Rule for this file: measure against pcc1, never against host CPython.** A
host profile is still useful for finding *candidates* — it is not a basis for
accepting one. The hot path has been restored to `safepoint_id` with a comment
recording this verdict so the next agent does not re-derive it.

`stable_id_prefix_state` / `stable_id_resume` are left in `precise_stackmap.py`
(tested, unused by the hot path) since a future caller batching many ids behind
one prefix could still use them without the per-record allocations.

### Also found and avoided, not diagnosed

The first attempt failed differently: the field validation copied from
`scoped_stable_id` — `not isinstance(part, str) or not part or "\0" in part` —
**rejected plain decimal digit strings under pcc1** while the identical call
succeeded on host CPython. That is a real pcc1 semantic gap in `isinstance(x,
str)` or in `"\0" in x`, worth its own investigation. It is documented in
`stable_id_resume`'s docstring; nothing here depends on it any more.

### Kept from this round

Memoizing `is_local_value_ref` (pure function of the operand spelling; 346732
calls over ~35000 distinct strings) survives: it removes calls without adding
allocations, which is the only shape of win that helps both cost models. Host
went 8.1s -> 6.3s and 26.9M -> 18.8M calls; its pcc1 effect is unmeasured
because the `stable_id_resume` regression made that build unusable.

## Update — No.16: why No.15 lost, and a reusable pcc1 cost rule

No.15 was reverted on the strength of one pcc1 measurement, without a
mechanism. The mechanism turned out to be neither of the two things that were
plausible, and finding it needed three microbenchmarks — each compiled by pcc1
in ~5 seconds, which is the fast pcc1-side loop this investigation was missing.

### Hypothesis 1 — DENIED: "allocation beats loop iterations under pcc1"

That was the conclusion recorded in No.15. A microbenchmark of the two exact
spellings says the opposite:

```
                        pcc1        host
join whole string      441 ms      395 ms
cached prefix          102 ms       39 ms
```

The cached-prefix form is 4.3x FASTER under pcc1 too. So "pcc1 dislikes
allocations" does not explain the regression, and No.15's stated reason was
wrong.

### Hypothesis 2 — DENIED: "per-allocation cost scales with the live heap"

Plausible, because the real emit runs with ~10 GB live and a huge
managed-pointer index. Re-ran the same benchmark holding 900000 live tuples
first:

```
empty heap      441 ms / 102 ms
900k live       422 ms / 107 ms
```

Unchanged. The index size does not flip the ranking.

### Hypothesis 3 — CONFIRMED: the packed state left the tagged-int lane

`_stable_id_feed` returned the two 32-bit limbs packed as
`high * 0x100000000 + low`, which reaches ~2**63 — outside the +/-2**62 tagged
small-int lane — and the hot path packed and unpacked it twice per safepoint.

```
                                    pcc1        host
threading a packed (>2**62) state   604 ms      142 ms
keeping two in-lane limbs           247 ms      133 ms
ratio                               2.4x        1.07x
```

**Crossing the tagged lane costs 2.4x under pcc1 and is invisible on host
(1.07x).** That is the whole story: the streaming hash was a win on the string
work and a loss on the state threading, and only pcc1 charges for the latter.

### The rule this yields

pcc1's value model has a tagged small-int lane at +/-2**62 (project intent,
V-track: `int` has a value projection and an object projection). An
intermediate that exceeds it silently becomes a boxed bignum, and bignum shifts
and masks allocate. **Keep hot-path integer intermediates below 2**62**;
prefer several in-lane values over one packed wide value. Host CPython cannot
see this cost, so it will never appear in a host profile.

`stable_id_prefix_state` / `stable_id_resume` remain available and tested. A
future attempt should return the limbs *unpacked* (two in-lane ints) and avoid
varargs, which would keep the string win without the lane crossing. Not
attempted here: the host-side gain for this item was ~10%, and each pcc1
verification costs a full stage-1 rebuild.

### The fast loop, for the next agent

`~/.cache/pcc/test-artifacts/bench/micro/` holds the three benchmarks
(`alloc_vs_loop.py`, `alloc_vs_loop_heap.py`, `bignum_lane.py`). Each compiles
under pcc1 in ~5 s and runs in under a second, so a cost-model question can be
settled in seconds instead of a 10-minute rebuild. Use this before changing
code that pcc1 executes — and note that a microbenchmark must reproduce the
real *shape* (state threaded through the loop, values in the real magnitude
range), or it will mislead exactly as the first one did.

## Update — No.17 CONFIRMED: `sort(key=...)` lowered to insertion sort

The oversized shard that had never once finished now emits in **200 seconds**.

```
                        result              peak RSS
before the leak fix     never finished      24509 MB (monotonic)
after the leak fix      never finished      10683 MB
No.16 build             24 min, unfinished  19857 MB
this build              COMPLETED in 200s   16143 MB, 38 MB of asm
```

### The bug

`list.sort()` without a key routes to `py_obj_sorted`, whose comment records
that it *used* to be insertion sort and was changed to a bottom-up stable merge
sort because "O(n^2) comparisons dominated codegen-worker profiles via sorted
symbol lists".

**The keyed path was never given the same fix.**
`_emit_list_insertion_sort_by_key` still emitted insertion sort, and it is
strictly worse than the keyless version was: insertion sort re-evaluates the
key on *both sides of every comparison*, so the cost is O(n^2) **key calls**,
not O(n^2) comparisons.

`_locations` merges ~354 roots and sorts them 12186 times for one function:

```
insertion sort   354**2 / 2 x 2 key calls  ~= 124000 per sort
                 x 12186 sorts             ~= 1.5 billion key calls
merge sort       354 key calls + ~3000 tuple compares per sort
```

### The fix

The keyed path now emits a Schwartzian transform: build
`(key(elem), index, elem)` triples, hand the list to `py_obj_sorted`, write the
third field back. Tuple comparison is lexicographic, so ordering is by key with
the original index breaking ties — the same stable order insertion sort
produced. No new runtime surface; it reuses the merge sort that was already
there and already tested.

```
pcc1 microbenchmark, 200 rounds x 354 elements
  sort(key=) insertion   13787 ms
  Schwartzian + merge      260 ms      53x, identical result
```

### Where the profile pointed vs where the bug was

The profile said `add_record` was 94-97% of the emit, and it was — but not
because `add_record` was slow. It called `_locations`, which called
`list.sort(key=...)`. Three earlier rounds tried to make `add_record` itself
cheaper (positional args, memoized locations, a streamed identity hash) for a
combined gain of roughly nothing, because the actual cost was one level down in
a lowering decision, not in this file at all.

Also note what the microbenchmarks got wrong before they got it right: an
int-returning sort key instead of a tuple-returning one measured **no** gain
under pcc1 (133655 ms vs 137228 ms). That result is what forced the next
question — "is it the key or the sort?" — which decomposed as:

```
200 rounds x 354 elements    pcc1        host
plain int sort(), no key      66 ms       1 ms
obj sort(key=...)           8764 ms       5 ms
354 key calls, no sort        62 ms       5 ms
```

Key calls are cheap and sorting is cheap; only the two together explode. That
is the signature of a complexity bug, and it is invisible in any single-number
comparison.

### Verified

```
CPython differential      6 cases identical, including stability
                          (equal keys keep input order), empty, single
existing sort tests       15 passed
PCC_GC_BACKEND=0..4       all compile and run correctly
type_infer benchmark      29s, IR byte-identical to the pre-optimization pcc1
stage1                    90s
```

A regression test pins both properties — that the emitted IR contains
`@py_obj_sorted(` for a keyed sort, and the exact stable output order — so this
cannot silently return to insertion sort a third time.

### Defect I introduced and fixed while doing this

The first version emitted `py_list_set(pairs, i, triple)` into a list from
`py_list_new(n)`. That call returns a **length-0** list with capacity n, so
every indexed store was out of range and silently dropped: the whole result came
back `<null>`. `py_list_append` is correct. Two references (`key_val`, `elem`)
also needed releasing, since `py_tuple_set_item` takes its own. The CPython
differential caught this, not code reading — which is the argument for running
the differential before the expensive rebuild, not after.

## Update No.18 -- root cause found: pcc1 could not compile ANY `def` [CONFIRMED]

The `ssa-dominance` verifier errors reported in the previous update were
produced by a pcc1 that was itself broken. Minimizing the input showed the real
boundary:

```
x = 1; print(x)          OK      (module has no function -> no phi node)
def f(): pass            FAIL
def f() -> int: ...      FAIL
def main(): print(1)     FAIL
```

`PCC-PY-COMPILE-001 ... exception_type=Exception` with an empty message was the
only surface, so this had been invisible. Two diagnostics were needed before
the cause was reachable at all, and both are now committed to source:

* `pcc/py_frontend/pipeline.py`: the 22 `raise PyPipelineError(str(exc)) from exc`
  sites now raise `str(exc) or type(exc).__name__`, so a runtime-generated
  exception with no message at least reports its type.
* `pcc/backend/self_backend_aarch64_darwin.py`: `_emit_trace()`, gated on
  `PCC_DEBUG_SELF_BACKEND_TRACE`, marks prepare / order-profile / stack-map /
  globals / per-function phases.

With those, `PCC_DEBUG_CODEGEN_PHASES=1` showed codegen running to completion
(`module str end 55256`) and the failure landing in the self-backend. Dumping
that IR with `PCC_DEBUG_SELF_IR_DUMP_DIR` and feeding it to the **host**
backend gave `parsed funcs=4 / total phis=1 / verify OK / emit OK bytes=22198`
-- the IR was legal, so the defect was in pcc1's compiled emitter.

### Root cause

`build_function_stack_map_plan` in `self_backend_precise_stackmaps.py` memoized
merged location tuples in `interned_locations`, keyed on
`tuple(sorted(id(group) for group in active.values()))`. The code comment
asserted that "two equal-but-distinct groups simply miss ... and can never
return a wrong tuple". **That reasoning is wrong.** `id()` is only a stable key
while the keyed object is alive: a freed `_RootGroup` whose address is reused by
a different group makes a stale fingerprint *hit* and return the location tuple
of an unrelated root set. The host keeps those groups alive incidentally
(`_block_entry_states` holds one tuple of groups per block for the whole
function), which is why the host never reproduced it.

Fix: each cache entry keeps the groups it was keyed on alive next to the
answer -- `entry = (_locations(active), tuple(active.values()))`. That is 2465
small tuples for the oversized shard against 12186 avoided merges, so the
optimization is retained.

### Denied along the way

Each was disproved with a seconds-long pcc1 probe rather than a rebuild, and
each had looked convincing:

* `[DENIED]` `isinstance(block.phis, list)` mis-answering under pcc1 (a
  recorded pcc1 defect for `str`). Probe: `isinstance([], list)` -> True,
  `isinstance((), list)` -> False. Correct.
* `[DENIED]` `@dataclass(slots=True)` field reassignment failing under pcc1.
  Probe: init / reassign / append all correct.
* `[DENIED]` pcc1 losing exception messages, i.e. the empty message being a
  runtime gap. Probe: `raise ValueError("HELLO-MSG")` round-trips with
  `args is None` -> False. The message really was empty at the raise site.
* `[DENIED]` `dict.get()` mis-lowering under pcc1 (also a recorded defect).
  Probe with an int-tuple key and tuple value: miss -> None, hit -> correct
  value. The `in` + subscript rewrite was kept anyway as the recorded-safe
  idiom, at no cost.
* `[DENIED]` the shared `_EMPTY_SEQUENCE` for `ParsedBlock.phis` plus its
  `isinstance` promotion. Removed for simplicity, but it was not the defect --
  both mechanisms it depended on were probed correct above.

`block.raw_lines = _EMPTY_BLOCK_LINES` after parsing was **verified safe and
kept**: `rg raw_lines pcc/backend/` shows the field is written and read only
inside `_parse_block`.

### Why no gate caught it

Every existing pcc1 test stops at `--emit-llvm`, so the self-backend emit path
was only ever exercised from the host. The chained smoke check used
`print(1)` -- which happens to be the one shape with no function and therefore
no phi node, i.e. the only shape that still worked.
`tests/python/test_pcc1_emits_native_function_binary.py` now compiles four
shapes bracketing that boundary through to a native binary and runs each under
`PCC_DEBUG_RUNTIME=1`.

### Status

Root cause fixed and confirmed: all four shapes compile and run correctly under
a pcc1 rebuilt from the fixed source (stage1 491 s). Whether the three
`ssa-dominance` errors were purely a consequence of this defect is not yet
established -- a stage2 run on the fixed pcc1 is the outstanding evidence.

## Update No.19 -- the stage2 emit bottleneck: a quadratic in `collect_block_local_last_uses` [CONFIRMED]

With the `def`-compiling defect of No.18 fixed, stage2 reached the self-backend
emit phase for the first time and then sat on **one** oversized shard for 57+
minutes at 98% CPU. Measured on the live worker:

```
shard            self_backend_split_72_shard_1.ll   28 MB
one function     59402 blocks
RSS              1.9 GB
physical footprint   54.4 GB   <-- the "two pcc processes using 60 G" report
progress         0 of its 4 outputs after 57 min; 45 of 461 results overall
```

`sample` on the worker gave a single dominant chain (the binary is stripped, so
offsets were resolved against `nm` with static = `0x100000000 + offset`):

```
allocate_aarch64_block_registers
  -> collect_block_local_last_uses     6551 / 6573 samples
     -> text_key_mapping_get           6545 / 6573
```

### Root cause

`collect_block_local_last_uses` looked its own result dict up while filling it:

```python
block_mapping = text_key_mapping_get(block_local_last_uses, block_name)
if block_mapping is None:
    block_mapping = {}
    block_local_last_uses[block_name] = block_mapping   # grows the mapping
```

Every new block name grew the mapping, so the next lookup missed, fell into the
false-hash-miss fallback, and re-entered `_text_key_index` -- whose "incremental"
extension still did `keys_list = list(mapping)`, materialising **all** keys.
That is O(definitions x blocks) time *and* a fresh multi-hundred-KB list per
definition, which is what produced the 54.4 GB footprint: pcc1's cost is
dominated by allocation count, so the host penalty is amplified enormously.

### Fix

Two changes, both narrow:

* `self_backend_analysis.py`: resolve the per-block dict through a stable int
  bucket (`_stable_text_bucket_key`) side index -- the same technique
  `_record_definition` / `_record_use_position` / `_record_block_length`
  already use in this function -- and never look up the growing result dict.
  Equality is still checked with `text_key_names_equal`, so inconsistent
  native str hashing stays handled.
* `self_backend_ir.py`: `_text_key_index` walks the mapping and skips the first
  `cursor` keys instead of building `list(mapping)`. Same complexity, but no
  giant allocation per growth, which bounds the damage for any other caller.

### Measured, on the real 28 MB shard

```
                                        host
old formulation (differential oracle)   47.6 s   (25 s at 85k defs, 47.6 s at 118k -> superlinear)
new formulation                          2.8 s   17x
output                                  identical (differential over every function)
peak RSS (host, new)                    499 MB
```

A unit check covers `_text_key_index`'s cursor across interleaved
grow-and-probe, and the differential oracle is kept in the probe script rather
than the test, since the pre-fix formulation is what it exists to compare
against.

### Contract change surfaced by this

`jobs_for_input_sizes` no longer collapses the whole batch to one worker when
any input crosses the split threshold; it caps at `LARGE_INPUT_CONCURRENCY`.
`test_native_large_inputs_are_serial_unless_jobs_are_explicit` encoded the old
rule and was red. It is now
`test_native_large_inputs_are_capped_not_serialized`, and it asserts the cap
actually binds on a batch wide enough to want more workers, so it cannot pass
with the cap removed. The 1.27 GB figure quoted in that code comment was
measured on **stage1** and did not cover a stage2 oversized shard -- the 54.4 GB
footprint above is why the cap is kept rather than removed.

### Status

Fix landed and differentially verified on host. Outstanding: the
stage1 -> stage2 -> stage3 chain on a pcc1 rebuilt from the fixed source, with
wall time and peak RSS per stage, then the five-GC cold/warm matrix.

## Update No.20 -- two more O(n^2) scans in the emit path, found by profiling instead of guessing [CONFIRMED]

`sample` on a live worker pointed at `_dot_numeric_text_key_id`, and a
microbenchmark **refuted** optimizing it: 240000 calls take 0.03 s, and
memoizing gives only 1.6x. Recording that as a denial mattered, because a
`cProfile` run at Python level then showed the same function as the top entry
by a wide margin -- the leaf was right, the *fix* would have been wrong. What
made it hot was its callers:

```
   ncalls   tottime  cumtime  function                    (10.4 MB module, ~120 s total)
100972224     42.2s    64.2s  _dot_numeric_text_key_id
203257888     15.2s           str.startswith
 65825805     14.5s    78.1s  text_key_names_equal
   124268      6.8s    69.8s  _aarch64_increment_value_use_count   <-- caller
   200971      1.4s    12.3s  _int_mapping_get                     <-- caller
```

124268 increments produced 65.8 million comparisons: 530 per call, because a
dict miss walked the whole growing `counts` dict. That is O(distinct values^2).

### Fix 1 -- canonical key instead of a recovery scan

`text_key_names_equal(a, b)` holds exactly when the spellings are identical or
both are dot-numeric with the same id. So keying dot-numeric names on the id
itself makes an ordinary dict lookup *already* exact, and both scans in
`self_backend_target_passes.py` disappear:

```python
def _aarch64_use_count_key(value_name: str):
    numeric_id = _dot_numeric_text_key_id(value_name)
    if numeric_id >= 0:
        return numeric_id
    return value_name
```

Int and str keys cannot collide in one dict. This also makes zero-padded
spellings (`%.05` vs `%.5`) converge deterministically, where the ordered scan
resolved them only by whichever entry it reached first. Contained: the dict has
exactly one consumer (`_aarch64_collect_value_use_counts` builds it,
`_aarch64_value_use_count` queries it) and nothing iterates its keys.

### Fix 2 -- reuse the incremental index in regalloc

`_type_mapping_get` / `_int_mapping_get` / `_last_use_mapping_get` had the same
miss-then-scan shape, but over `func.value_types`, `func.value_registers`, and
`block_last_uses` -- shared structures whose key type must not change. They now
call `text_key_mapping_get`, which is O(1) amortised, keeps the mapping pinned
in its cache entry so an id() cannot be recycled under it, and covers both
recovery cases the scan covered (`by_id` for dot-numeric spellings, `by_bucket`
for plain equality after an inconsistent native hash).

`_int_mapping_set` and `_int_mapping_delete` **always** scan (no fast path at
all, and `delete` allocates `list(mapping)`). They did not appear in the top 22
so they are left alone; noted here rather than fixed silently.

### Measured, on a 10.4 MB module, byte-identical output throughout

A/B in one process, injecting the old implementation over the new one so
nothing else differs. `sha` is over the full assembly text:

```
old (both scans)              32.2s   30180438 bytes   sha=621eb0e15e2b34b5b96767bd
+ use-count canonical key     18.9s   30180438 bytes   sha=621eb0e15e2b34b5b96767bd   1.70x
+ regalloc shared index       17.0s   30180438 bytes   sha=621eb0e15e2b34b5b96767bd   1.89x
```

The old run went second, with caches warm, so 1.89x is if anything
conservative. 10.4 MB IR -> 30.2 MB / 1398007 lines of assembly.

### Retracted from Update No.19

The claim that 98.7% of emitted lines were one-value-per-line data directives
was measured on a **stale** `.s` in an old temp directory, produced by an older
pcc1 whose emitter did not pack. Current source both dedups the location table
and packs it 8 per line (`_append_packed_location_lines`), and a current emit of
a 10.4 MB module expands 3x, not 55x. The line-count analysis in No.19 does not
describe current behaviour and is withdrawn. This is the second time in one
session that an old artifact produced a confident wrong conclusion.

### Memory attribution (host, per phase, 28 MB / 59402-block shard)

```
read 29 MB text            0.07 GB
parse                      0.41 GB
collect_block_local_last_uses  0.49 GB
prepare_module_for_target  0.74 GB
build_stack_map_plans      0.84 GB
```

The host completes the analysis chain in under 1 GB where one pcc1 emit worker
held 11.5 GB RSS with a 66 GB physical footprint. So the remaining memory gap is
**pcc runtime object overhead plus an allocator that does not return pages**,
not another algorithmic blowup in the emitter. stage2's 21.4 GB peak is
`LARGE_INPUT_CONCURRENCY` (4) times the per-worker peak, not a single phase
growing.

### Status

Both scans fixed, output byte-identical, 41 focused backend tests green. A
stage2 run on a pcc1 rebuilt from this source is the outstanding end-to-end
evidence, together with the 28 MB oversized shard's time and peak RSS.

## Update No.21 -- pcc2 could not print any integer; root cause chain and where it still stops [CONFIRMED to a specific edge]

`stage2 exit=0` is not "pcc2 is correct". On an **idle** machine (an earlier
`<null>` was wrongly excused as contention with a concurrent stage3 -- the
retest disproved that excuse):

```
same input, def main(): print(7)
  pcc1 -> 7          pcc2 -> <null>

print("hi") / x="hi";print(x) / print(True)   pcc2 correct
print(1)                                       pcc2 <null>
print(1 + 2)   pcc2 "Unhandled non-exception object (null)"
```

The emitted IR differed in exactly **one** line out of 1818:

```
pcc1:  %int.lit.tagged.2.12 = inttoptr i64 3 to ptr
pcc2:  %int.lit.tagged.2.12 = inttoptr i64 0 to ptr
```

### Immediate cause, fixed

`_emit_int_literal_object` computed the tagged constant as
`((value << 1) | 1) & 0xFFFFFFFFFFFFFFFF`. A **literal** integer above the
tagged lane evaluates to **0** in pcc-compiled code, so under pcc2 the mask
became `& 0` and every int literal lowered to a NULL pointer. The mask was
redundant: for an in-lane `value`, `(value << 1) | 1` is already in
`[-2**63+1, 2**63-1]`, and for negatives its bit pattern is identical to the
masked form. Removed.

All 13 over-lane literals in `pcc/py_frontend` and `pcc/backend` were rewritten
as equivalent expressions (`((0xHI << 32) | 0xLO)`, each asserted equal to the
original), because several were latent miscompiles of their own: the FNV-1a
64-bit offset basis in `macho_parallel.py`, a stack-map mask in
`precise_stackmap.py`, an x86 mask, and the float-inf bit pattern.
`tests/python/test_selfhost_no_over_lane_int_literals.py` keeps new ones out.

**Verified end to end**: rebuilt stage1 (445 s / 6.6 GB) and stage2
(2493 s / 21.4 GB), then `pcc2` compiled and ran `print(7)` -> `7`.

### Underlying defect, narrowed but NOT fixed

Why is a big literal 0 in the first place? The parser does
`int(e.text, 0)` (`pcc/parse/py_lift.py:583`), so inside pcc1 the literal is
parsed by the **runtime port** `py_int_from_cstr`. Probed with a pcc1-built
binary:

```
                                       pcc1     host
int("9223372036854775808", 10)            0      correct
int("9223372036854775808",  0)            0      correct
int("0xFFFFFFFFFFFFFFFF",   0)            0      correct
int("FFFFFFFFFFFFFFFF",    16)            0      correct
int("9223372036854775807", 10)      correct      correct   <- in-i64 path
(1 << 63)  computed at run time     correct      correct
4611686018427387903 * 4             correct      correct
```

So only the over-i64 path is wrong. Bisected inside the port by temporary
sentinels (all removed, archive rebuilt, baseline re-confirmed):

* `_parse_bigint` **is** reached for all four failing cases (entry sentinel
  12345 came through).
* Its accumulation loop runs the right number of iterations from the right
  offset: 19 iterations / first byte `'9'` for the decimal, 16 / `'F'` for the
  hex.
* `py_int_mul` and `py_int_add` behave correctly: a tagged-bit probe reported
  `p1` tagged, and both `pm` and `pa` **non-NULL heap bignums**.
* Returning a **tagged immediate** through this path works; returning the
  **heap bignum** yields 0 at the caller.
* The frontend IR is clean -- in `py_int_from_cstr`,
  `%.172 = call ptr @user_py_int_parse__parse_bigint(...)` is immediately
  followed by `ret ptr %.172`, with no `pcc_gc_release` / `pcc_gc_unpin` /
  `py_decref` in between.
* The in-i64 path (`E`) returns a heap bignum too -- 2**63-1 is above the
  tagged lane -- through the **same** `@c_abi_export` boundary, and it works.
  The only structural difference is that it returns from the function tail
  while the bigint path returns from **inside the digit loop**.

`[DENIED]` A user-level minimal reproducer does **not** reproduce: returning a
heap `str` from inside a `while` loop in an ordinary program is correct under
pcc1 (`in_loop = [a7]`). So this is not simply "early return from a loop loses
a heap pointer" at user level; it is specific to the freestanding /
`@c_abi_export` port context.

So the loss happens **below the frontend IR** -- self-backend, link, or object
staging -- on the edge that returns a heap pointer from inside a loop in a
freestanding exported function. That is where the next session should start:
read the emitted assembly for `py_int_from_cstr` around its early-return block,
and build a freestanding `--python-library` reproducer rather than a user-level
one.

### Status

Call sites fixed and pcc2 proven to print integers again. The compiler defect
itself is open and tracked as **M5-SELFHOST-BIG-INT-LITERAL** (P0), with this
narrowing recorded in its `open_boundary`.

## Update No.22 -- the big-literal root cause, one layer fixed and one layer named [CONFIRMED]

Continuing No.21. The loss is **not** below the frontend IR after all -- the port
chain (`_parse_bigint`, `py_int_from_cstr`, `py_int_from_cstr_or_raise`) is
correct in IR and returns `ptr` everywhere. The loss is on the **consumer**
side, and the user-program IR shows it directly:

```llvm
%int.parse.12.18     = call ptr @py_int_from_cstr_or_raise(...)   ; correct bignum
%m.int_unbox.27      = call i64 @py_int_to_i64(ptr %int.parse.12.18, ptr %ov.flag.26)
%exact.int.box.19.28 = call ptr @py_int_from_i64(i64 %m.int_unbox.27)
```

`marshal_from_object` unboxes an int-typed object to i64, and the overflow slot
it allocates was documented **"caller can ignore"**. Above 2**63-1
`py_int_to_i64` yields 0, and the immediate re-box produces 0. That is also why
the failing threshold is exactly the signed i64 maximum and not the tagged lane:
`int("9223372036854775807")` was always correct.

### Fixed: the adjacent unbox/re-box pair

`py_int_from_i64(py_int_to_i64(x))` now returns `x`. This is a correctness fix,
not an optimisation -- keeping the bignum is what CPython does.

An ownership defect in the first version of this fix was caught before it
shipped: `py_int_from_i64` hands back a **new owned reference**, so returning
the borrowed original would let the caller's release drop a reference it never
took -- the over-release shape that has broken pcc1 before. The path now
retains. Verified over 20000/50000 iterations: correct total, peak RSS 13.3 MB
(a leak would balloon it), no `[BAD_INCREF]`.

`[DENIED]` Carrying the link as an attribute on the IR value (which would
remove the id()-keyed table entirely) is impossible: llvmlite `Value` uses
`__slots__` and has no `__dict__`. Reverted to a side table whose entry stores
the keyed value, so a recycled id() cannot hand back an unrelated object.

### Verified under a rebuilt pcc1 (stage1 233 s warm)

```
                                    before    after
int("9223372036854775808", 10)          0     9223372036854775808
int("9223372036854775808",  0)          0     9223372036854775808
int("0xFFFFFFFFFFFFFFFF",   0)          0     18446744073709551615
int("FFFFFFFFFFFFFFFF",    16)          0     18446744073709551615
```

### Still open, and now precisely named

A **source literal** is still 0:

```
print(9223372036854775808)   ->  0
print(0xFFFFFFFFFFFFFFFF)    ->  0
print(1)                     ->  1
```

because the parser's value has to survive a store:

```
int(e.text, 0)                       correct bignum now
  -> stored into IntLit.value / a local inferred as `int`   TRUNCATED
  -> _emit_int_literal_object(0)     emits 0
```

Confirmed minimally -- the loss happens at the **assignment**, before any field
store:

```python
big = int("9223372036854775808", 0)
print(big)          # pcc: 0        CPython: 9223372036854775808
```

An `int`-annotated local or dataclass field is an i64-backed exact-int slot and
cannot hold a bignum. Per the project north star, `int` is an
arbitrary-precision semantic type whose value projection must **deopt or
promote** on lane overflow, never wrap -- and this does not even wrap. The fix
is the admission demotion already designed as **INT-P0-PROJ**; it is not
attempted here.

### Status

Runtime `int(str)` parsing fixed and verified under pcc1. Source literals remain
0, blocked on INT-P0-PROJ. The 13 over-lane literals in the closure stay
rewritten as expressions, and the lint gate keeps new ones out, so the pcc2
integer-printing failure cannot recur through that route.

## Update No.23 -- big-literal fix attempt: verdict RETRACTED, see No.24

Continuing No.22. The remaining half was chased to a precise shape ladder, then
the fix was implemented, measured unsound in **both** directions, and reverted.
Recording it in full so the next attempt does not repeat it.

### The shape ladder (host-compiled, fast loop -- seconds per iteration)

Reproducing the parser's chain on the host instead of rebuilding pcc1 each time
was the single most useful move here; earlier rounds of this investigation paid
~5 minutes per hypothesis for no reason.

```
1 module, direct ctor arg   Lit(int(TEXT, 0))            0
2 module, via local         tmp = int(...); Lit(tmp)     correct
3 fn, direct ctor arg       return Lit(int(text, 0))     0
4 fn, via local             v = int(...); Lit(v)         correct
5 fn returns the int        return int(text, 0)          correct
6 fn, untyped param         Lit(int(text, 0))            0
```

Two distinct sites, both found:

* `_materialize_class_init_call_args` (`call_expression_lowering.py`) opened the
  staging slot from `_emit_expr(arg)`'s type. For an int-typed call that is an
  **i64**, so the slot was i64 and `store i64 %m.int_unbox, ptr
  %__pcc_ctor_arg_0_0.addr` dropped the bignum. Staging the object projection
  fixed shapes 1 and 3.
* `int(<dyn>)` (`numeric_builtin_lowering.py`) unboxes all four branches
  (str/float/bool/int) and phis them as `_I64`, so shape 6 loses any bignum.

### RETRACTION (see Update No.24)

**Every `[DENIED]` verdict below is withdrawn as unsupported.** They compared a
program that produces bignums against one that does not, and concluded "my
change leaks".  That is not the right control: the arms differ in the *size and
kind of object retained*, not in whether the change is present.  Two further
methodology errors compounded it -- a claim that the measurements were served
from a stale cache (they were not), and a baseline taken with `git show HEAD:`
while HEAD itself had moved mid-session, so several "mine vs HEAD" comparisons
were mine-vs-mine.

The correct control, run in No.24: the same loop shape producing bignums through
a path **this work never touched** (`1 << 70` computed at run time) grows
29 MB -> 80 MB from 20k to 60k iterations, i.e. the same order as the changed
path (26 MB -> 76 MB).  Bignum-producing loops in this runtime grow regardless of
this change, which is a separate pre-existing condition worth its own
investigation (~320 B per bignum retained).  Nothing here supports attributing a
leak to the fix.

### Original (withdrawn) reasoning

`[DENIED]` **Object projection alongside the i64 phi in `int(<dyn>)`.** Each
branch produced an object and a parallel `ptr` phi carried it. Objects are
created eagerly but consumed only if some later site re-boxes, and most callers
just use the i64, so the unconsumed references leak. Measured: peak RSS 17 MB at
20k iterations vs 162 MB at 200k -- linear, about 160 B per call, against a flat
1 MB baseline.

`[DENIED]` **Recovering the source object inside `marshal_to_object`.** Isolated
by swapping single files against HEAD:

```
marshal mine, others HEAD          162 MB    leaks
marshal HEAD, others mine            1 MB    no leak, and no fix either
```

With a retain it leaks one reference per call, because the release side dedups
by value identity and releases the value once. Without the retain the value is
freed early. So this site cannot be fixed by an incref at all: the recovered
value has to be **registered with the ownership lowering as that call's owned
result** (the `_note_owned_dynamic_call_value` mechanism), which is the design
the next attempt should start from.

A correction to an intermediate reading in this investigation: `total=100000`
from the 20k-iteration probe was **not** corruption -- it is the correct total
when all five values print as `"0"` (1 char x 5 x 20000). Truncation, not a
double free.

`[DENIED]` Carrying the unbox link as an attribute on the IR value instead of an
id()-keyed table: llvmlite `Value` uses `__slots__` and has no `__dict__`.

### What was kept, and why it is safe

* The redundant `& 0xFFFFFFFFFFFFFFFF` in `_emit_int_literal_object` stays
  removed. This is the fix that lets **pcc2 print integers at all**, it is
  independent of the recovery work, and ordinary ints verify clean
  (`1 / -7 / 4611686018427387903`).
* The 13 over-lane literals in `pcc/py_frontend` and `pcc/backend` stay
  rewritten as expressions, with
  `tests/python/test_selfhost_no_over_lane_int_literals.py` keeping new ones
  out. Several were latent miscompiles of their own (FNV-1a offset basis, a
  stack-map mask, an x86 mask, the float-inf bit pattern).
* `tests/python/test_int_unbox_rebox_roundtrip.py` is now
  `xfail(strict=True)` so the gap stays visible and flips to a hard failure the
  moment it is really fixed.
* Leak baseline re-confirmed flat after the revert: 1 MB at both 20k and 200k
  iterations, no `[BAD_INCREF]`, 81 focused tests green.

### Status

Source-level integer literals above 2**63-1 still lower to 0 under pcc1. The two
losing sites are identified, the ownership requirement is understood, and the
naive fix is disproved with numbers. Tracked as **M5-SELFHOST-BIG-INT-LITERAL**.

## Update No.24 -- big integer literals FIXED; four sites, one principle [CONFIRMED]

All six shapes of the ladder from No.23 are now correct, verified on
host-compiled binaries:

```
1 module, direct ctor arg   Lit(int(TEXT, 0))          9223372036854775808
2 module, via local         tmp = int(TEXT, 0)         9223372036854775808
3 fn, direct ctor arg       return Lit(int(text, 0))   9223372036854775808
4 fn, via local             v = int(...); Lit(v)       9223372036854775808
5 fn returns the int        return int(text, 0)        9223372036854775808
6 fn, untyped param         int(<dyn>)                 9223372036854775808
```

### The principle that made it work

Do not unbox and then try to recover.  Reach for a runtime call that **returns a
new owned object**, and never let the value touch i64:

* `emit_int_builtin_as_object` -- `int(<str>)` returns the
  `py_int_from_cstr_or_raise` result directly.
* `_emit_int_dyn_as_object` -- `int(<dyn>)` dispatches on the type tag and phis
  the **objects**.  Placement is the whole point: the same object phi bolted onto
  the *i64-returning* function leaked, because the objects were built eagerly and
  most callers only want the i64, so nothing consumed them.  Inside the
  object-returning variant the join is always consumed, so every branch can hand
  back an owned reference (the str branch's parse result already is one, the int
  branch retains its borrowed input, float/bool box their exact i64).
* `_maybe_emit_exact_int_object` gained an `Attr` case: `py_instance_get_field`
  when the container's class and field index are statically known (BORROWED, so
  nothing to release), otherwise `py_obj_getattr` registered with
  `_note_owned_dynamic_call_value` (NEW reference).
* `_materialize_class_init_call_args` stages an `int` argument as the object
  projection instead of opening the slot from `_emit_expr`'s i64 type -- an i64
  staging slot cannot hold a bignum.

### Leak question, answered with a real control

```
                                    20k      60k
all-small-int control               20 MB    55 MB
changed path (int(str)/int(dyn))    26 MB    76 MB
UNTOUCHED path (1 << 70 computed)   29 MB    80 MB
```

The untouched path grows the same way, so the growth is a pre-existing property
of bignum-producing loops here, not something this change introduced.  The
all-small-int control also grows, so the loop shape itself retains something --
both are separate findings, filed rather than fixed here.

### Gates

87 passed, plus one pre-existing failure unrelated to integers
(`test_multiple_legacy_starstar_kwargs_split_to_splat_dict`, a
`NotImplementedError` for multiple `**mapping` operands, confirmed still failing
with this change disabled).

### Status

Fixed on host across all six shapes. `pcc1`-rebuilt source-literal confirmation
is the outstanding evidence.

## Update No.25 -- the real gate is `boxed_world=False`, not any single emit site [CONFIRMED]

No.24's fix works for **user programs compiled by the host** (all six shapes), and
`pcc1` itself gained several genuine fixes (below).  But a `pcc1`-rebuilt source
literal stayed 0 through four more rebuilds, and instrumenting the **real**
chain -- instead of the mimic programs used up to that point -- explained why in
two lines.

Splitting the chain at both ends:

```
[biglit] lift 9223372036854775808 -> 0      <- lost at the parser's int(e.text, 0)
[biglit] emit received 0
```

and then asking why the object path never ran:

```
[biglit] assign target=idx boxed_world=False forced=True rhs=IntLit
```

`_int_exprs_are_boxed()` is **False** under `--ir-scaffold=on
--python-libpython=off`, i.e. the mode pcc1 is built in.  In that world `int`
values are native i64 by default, so `_maybe_emit_exact_int_object` -- and with
it every object projection added in No.24 -- is never reached.  Confirmed
directly: compiling `pcc/parse/py_lift.py` in that mode produces **zero**
`emit_int_builtin_as_object` calls.

That is why the mimic programs kept "fixing" the bug while pcc1 did not move:
the host compiles user programs in the boxed world and pcc1 is compiled in the
unboxed one, so the two exercise different code entirely.

### What this means for the fix

The remaining work is not another emit site.  It is the admission decision
itself: an `int` whose value range is unbounded (an `int(<str>)` result, a value
arriving from an unannotated source) must be **demoted to the object
projection** in this mode rather than staying in the i64 lane.  That is
INT-P0-PROJ, a mode-level change, and it is deliberately not attempted here.

### Landed and verified under pcc1 (stage1 green three times: 219 s / 334 s / 525 s)

```
int("...") dec/hex x base0/base10     0 -> correct
int-typed field read                  0 -> correct
field value re-wrapped through int()  0 -> correct
```

Plus, for host-compiled user programs, all six shapes of the No.24 ladder.

### Also landed

A runtime `py_obj_as_int_object(o, base)` (C + pcc-Python port mirror + ABI
registration) so the Dyn case is ONE call.  Building the four-way dispatch in
the frontend instead created basic blocks and a phi inside
`_maybe_emit_exact_int_object` -- a probe whose result callers may discard,
which orphans the blocks and parks the builder on the join.  That compiled every
small host program and **broke stage1**; isolation confirmed it (stage1 exit 0
with it disabled, exit 1 with it enabled).

### Not landed

`pcc1` still lowers a source-level integer literal above 2**63-1 to 0.  Tracked
as **M5-SELFHOST-BIG-INT-LITERAL**, whose blocker is now precisely named rather
than guessed.

All temporary instrumentation (`PCC_DEBUG_BIGLIT` in `py_lift.py`,
`literal_lowering.py`, `numeric_builtin_lowering.py`,
`assignment_statement_lowering.py`) has been removed.

## Update No.26 -- pcc1 is 5.5x slower than CPython on the same work; one point-fix measured NEGATIVE [DENIED]

Same module (`pcc/parse/py_lift.py`, 1303 lines), same flags:

```
host (CPython running pcc)   0.93 s
pcc1 (native binary)         5.22 s      5.5x
```

Consistent with the stage ratio (cold stage1 444 s vs stage2 2400 s = 5.4x), so
this is a general property, not something specific to stage2.

### A profile that had to be redone

The first attribution was wrong: it resolved offsets against a `nm` dump taken
from a **different pcc1 build** earlier in the session, and pcc1 had been rebuilt
a dozen times since. Comparing the two symbol tables, only the first **3**
entries matched. That produced confident nonsense -- `fseek` at 10% and
`pcc_errno_write_unknown_error` at 7.6% -- for a workload that makes no `seek`
calls at all. Regenerating the table from the *current* binary gave a completely
different and plausible picture:

```
16.4%  L1CodeGen entry
 9.0%  IRBuilder.call1
 7.1%  IRBuilder.call2
 5.9%  expr_dispatch_lowering
 4.4%  pcc_py_gc_minor_graph_lock
 4.0%  strlen
 4.0%  pcc_gc_managed_pointer_find_slot
 3.8%  _irbuilder_call_from_args_list / IRBuilder._emit
```

Roughly 24% in the IRBuilder call path, 12% in GC accounting.

### `[DENIED]` Caching the callee signature text

`_irbuilder_call_from_args_list` rebuilt each callee's signature text on every
emitted call -- one `str(Type)` per parameter, a join, three concatenations --
although the signature never changes and runtime helpers are called hundreds of
thousands of times per module. Caching it on the `FunctionType` (plus the
per-argument type texts and the `@name`) produced **byte-identical IR**
(sha `4f7af05f7072ac140ea0`) and is obviously less work on paper.

It made pcc1 **slower**. Single-variable isolation, all on the same module:

```
5.22 s   before the integer fixes, no cache
6.87 s   after the integer fixes, no cache
7.61 s   after the integer fixes, WITH the cache      +11%
```

`getattr(ftype, "_pcc_call_sig", None)` in pcc-compiled code goes through
`py_obj_getattr` -> MRO walk -> string hash, which costs more than the string
building it avoids. On CPython the same lookup is one C-level dict hit, which is
why it looks like a win there. Reverted.

The methodology error is the one already recorded in
`feedback_pcc1_cost_model_not_cpython`: the candidate came from a pcc1 profile,
but the fix was validated only on the host (byte-identical IR proves semantics,
not speed). **A pcc1-side measurement is mandatory before accepting any fix
aimed at pcc1.**

### Cost of the integer correctness work

The same isolation prices it honestly: **+1.65 s, +32%** on this module
(5.22 -> 6.87). That buys literals above 2**63-1 and `int(str)` no longer
silently becoming 0, which the project contract requires (`int` is
arbitrary-precision; lane overflow must promote, never wrap). The current
admission rule is conservative -- every `__init__` parameter in a raw-int
module takes the object projection, whether or not that field can actually
receive an unbounded value. Narrowing it needs cross-module call-site
information that is not available today.

### Why a point-fix cannot close a 5.5x gap

Amdahl: removing the entire 24% IRBuilder slice caps out at 1.32x. The
structural cost is the **text round trip** -- the frontend builds structures,
serializes them to LLVM IR text, and the self-backend parses that text back into
a *different* structure set (`llvm_capi.ir.Module` vs
`self_backend_ir.ParsedModule`). Evidence collected in this investigation:
parsing one 28 MB module takes 6.3 s; a module holds 682807 operand strings over
192360 distinct values; `raw_lines` held 423698 str objects per module.

A memory transport already exists for target passes
(`PCC_SELF_TARGET_PASS_TRANSPORT=memory`,
`run_self_target_memory_pass_pipeline`), but the main frontend->backend edge
still hands over text. Bypassing it is viable only for `--backend self`: the
text is load-bearing for `--emit-llvm`, the LLVM backends, the object cache key,
and the IR verifier. And the two data models differ, so "pass structures
directly" means either a converter or unifying them.

**Not attempted**, because the share of stage2 spent serializing and parsing is
still unmeasured. A `PCC_BOOTSTRAP_PROFILE_DIR` stage2 run is the prerequisite;
choosing a layer without it is what produced the negative result above.

## Update No.27 — bottleneck relocated with a trustworthy profiler [CONFIRMED]

Every earlier profile in this file resolved addresses by hand and at least two
of them were fiction. `scripts/pcc_profile.py` now makes that class of error
impossible: it reads symbols from the sampled process's own executable, derives
the slide from the image `sample` reports rather than assuming `0x100000000`,
counts only frames in that image, and treats `--binary` as a check whose
mismatch is a hard error. It also walks the pid down to the busiest leaf,
because `$!` after `gtimeout X cmd &` is gtimeout, and gtimeout's
`__sigsuspend` resolved against pcc1 as `_pcc_platform_waitpid` at 53% with no
sign of trouble.

**Shape of the load matters more than its size.** Two synthetic modules, pcc1,
`--backend self --python-libpython=off`:

```
600 small functions   7205 lines    < 30 s
1 function, 9000 branches  36005 lines   > 9 min 50 s (killed)
1 function, 2000 branches   8005 lines   > 170 s (killed)
```

Cost grows non-linearly in blocks **per function**, not in total module size.
This is the oversized-lane signature and it reproduces in about a minute, so it
replaces the stage2 chain as the fast loop for this work.

**Where the time goes** (2000-branch function, 20 s sample, 16718 samples,
1.8 GB footprint, 20.5% of samples outside pcc1):

```
14.7%  pcc_gc_managed_pointer_find_slot
 7.3%  pcc_py_gc_minor_graph_lock
 4.0%  py_decref
 3.9%  pcc_py_gc_minor_graph_unlock
 3.8%  user_py_str_accessors__byte_find
 3.6%  user_py_class__strs_eq
 3.5%  py_incref
 3.2%  user_py_gc_backend__pointer_is_managed_no_lock
 3.2%  memmove
 3.0%  pcc_gc_managed_pointer_index_contains
 2.8%  pcc_gc_pointer_is_managed
 2.7%  memset
 2.4%  pcc_gc_load_ptr
 2.2%  user_py_class__class_lookup_in_mro
 1.9%  pcc_gc_store_root
 1.9%  pcc_gc_index_py_find_slot
```

Grouped: **GC pointer bookkeeping + refcounting is ~49%** of self time, and
`managed_pointer_find_slot` alone is 14.7%. The lock/unlock pair is 11.2% in a
single-threaded compile. String/attribute lookup (`byte_find`, `strs_eq`,
`class_lookup_in_mro`) is a further ~9.6%.

This says the emit slowdown is **not** an algorithmic hot spot in a pass — it is
the per-managed-pointer tax, paid once per pointer question, multiplied by the
number of pointers a giant function creates. Consistent with
[[reference_gc_provenance_predicate_order]], where reordering the probes in this
same predicate took 87 s to 54 s.

Not yet measured: whether `find_slot` is being called redundantly (same pointer
asked repeatedly) or once per genuinely distinct pointer. That distinction
decides between a memo and an index-structure change, and is the next step.

## Status

Bottleneck located and attributed. No fix attempted in this update.

## Update No.28 — Python-frontend line information [CONFIRMED]

The C frontend has emitted `DICompileUnit`/`DISubprogram`/`DILocation` for a
long time; the Python frontend emitted none, so a pcc-compiled Python program
could only be debugged by rebuilding it with print statements. That loop is
now removable on the LLVM path.

**Design.** Locations are stamped by `IRBuilder.debug_location` at `_emit`, the
single point where instructions are appended, mirroring LLVM's
`SetCurrentDebugLocation`. Statement lowering marks a boundary once
(`_emit_stmt` calls `_di_locate`) instead of decorating hundreds of emit sites.
`_emit_user_function` opens a `DISubprogram` scope and restores the previous one
after the body.

**Four things made this silently emit nothing**, each found only by
instrumenting the real path:

1. `generation_lowering` **re-creates** `self.module` when handed a module, so
   the compile unit built in the constructor belonged to a discarded
   `ir.Module`. Debug nodes are owned per module; `_di_init` now runs again at
   the replacement site.
2. The IR pass pipeline dropped every `!dbg`: mem2reg/sroa rewrite instruction
   lines and rebuild them without the suffix. Locations pointing at
   instructions that no longer exist are worse than none, because they look
   right. A debug build now skips the owned transforms — `-g` implies `-O0`, as
   elsewhere. Carrying locations through them belongs with the real pass
   infrastructure (roadmap items 5-6).
3. LLVM rejects a `DILocation` whose scope is a `DIFile` ("location requires a
   valid scope") and discards the entire compile unit. Module-level statements
   therefore carry no location until the synthesized module-init function gets
   its own subprogram.
4. `-g` never reached the frontend: `_cli_main_impl` returns from its
   `path.endswith(".py")` branch **before** the option-normalization block where
   the flag was first wired. Every direct call to the parser returned
   `emit_debug=True` while the CLI produced nothing. The flag is now set ahead
   of that branch, and `cli_bootstrap` accepts `-g` too, since pcc1 is where
   the instrument-rebuild loop actually hurts.

**Verified.** `clang` accepts the metadata with no warning and emits a real
`__debug_line`; the table's rows map to source lines 2 and 3, which are the two
statements inside the test function. With the flag off the IR is unchanged
(0 `!dbg`, 0 `DILocation`, 0 `DISubprogram`). Regression:
`tests/python/test_py_debug_info.py`, 4 tests, one per failure above.

**Not covered.** The self-backend consumes no `!dbg` and pcc's own assembler
emits no DWARF, so `--backend self` binaries still have no line table. That
needs either a `__debug_line` encoder in the Mach-O assembler or a sidecar
line map keyed by symbol and byte offset; the metadata this update produces is
the input either one would consume.

**Pollution caught during the work:** `PCC_PY_DEBUG_INFO` leaked into the
runtime archive build, so four runtime port `.ll` files were compiled with debug
info. The archive is a build dependency, not the user's program, and must not
change when a user toggles `-g`; the build subprocess now drops the variable,
and the four polluted artifacts were removed so they regenerate clean.

## Status

Line information lands on the LLVM path, gated and regression-tested. The
self-backend line table is not implemented.

## Update No.29 — two gates red, attribution recorded

Two focused gates fail at the end of this work. Neither is caused by the debug
info feature, whose code paths are all inert with the flag off (`_stamp`
returns immediately when `debug_location is None`; every `_di_*` helper returns
early; the pass-pipeline guard only triggers when the flag is set).

1. `tests/python/test_fallback_baseline.py::test_per_module_fallbacks_under_ratchet`
   — `pcc.backend.self_backend_aarch64_darwin_regalloc: 57 vs baseline 51
   (+5.0%)`. **This one is attributable**: the earlier de-quadratic change in
   this same session rewrote `_type_mapping_get` / `_int_mapping_get` /
   `_last_use_mapping_get` to delegate to `text_key_mapping_get`, and that
   delegation costs six additional semantic fallback actions. The speedup was
   large and measured; the ratchet is doing its job in flagging the cost. Needs
   either a fallback-free formulation of the delegation or a justified baseline
   move — not a silent bump.

2. `tests/python/test_py_multi_file_compile.py::test_borrowed_object_local_rebind_keeps_gc_root`
   — expects `store ptr %pending.owned.resolve..., ptr %pending.addr`; the
   emitted IR uses `pcc_gc_store_root` around a pinned retain instead. The test
   and `ownership_lowering.py` were last touched together in HEAD
   (`977ad074`, "self-backend GC-pin fix"), and no uncommitted edit in this
   session touches GC-root or pin emission (verified by diffing each changed
   codegen file for `gc_pin`/`store_root`/`owned.resolve`). **Attribution is
   unproven**: confirming it requires running the test against a clean tree,
   which needs git operations this session is not authorized to perform.

`tests/python/test_bootstrap_gate_baseline.py` passes (2 passed), and the
llvm_capi parity gate passes (27 passed) despite the `IRBuilder` change.

## Update No.30 — host-pcc bottlenecks and pcc1 bottlenecks are not the same concept

They share a name and almost nothing else. Stage1 is CPython running the
compiler's source; stage2 is that same source as native code. A bottleneck is a
property of the *cost model*, and the two cost models disagree on the cheapest
operations in the language.

```
                     CPython (stage1 / host pcc)   pcc1 native (stage2)
attribute load       one C-level dict hit          MRO walk + string hash
str hash             cached on the object          recomputed every time
allocation           pymalloc pools + free lists   GC-tracked, index insert
refcount             inline, non-atomic            barriered store, may be atomic
pointer slot write   plain store                   pcc_gc_store_ptr barrier
"is this managed?"   n/a                           hash probe over live pointers
```

**Measured, not argued.** A callee-signature cache produced *byte-identical
IR* — so its semantics were provably unchanged — and it was a host-side win.
Under pcc1 it made the compiler **11% slower** (6.87 s -> 7.61 s), because the
cache was read through `getattr`, which CPython answers with one dict hit and
pcc-compiled code answers by walking an MRO and hashing a string. Second case:
`sort(key=)` in the pcc runtime was an insertion sort, so it paid O(n²) *key
calls*; the keyless path had a merge sort. On CPython that code was fine. Both
defects are invisible to a host profile by construction.

The tooling differs for the same reason. A host profile has Python-level caller
attribution through `cProfile`. A stripped pcc1 gives `sample` nothing but C
leaves, so attribution has to be reconstructed — resolve addresses against the
sampled binary, then fold by call path. That is what `scripts/pcc_flamegraph.py`
exists for; `scripts/pcc_profile.py` answers "what is hot", the flame graph
answers "who asked".

Operationally: **a host profile finds candidates; only a pcc1 number accepts
one.** Optimising stage2 from stage1 measurements is how the 11% regression got
written.

## Update No.31 — stage2 root cause: the hypothesis, stated so it can be refuted

What is established: emit is 81.5% of stage2; the oversized lane (a single
huge function) is 53.8% of emit; cost grows non-linearly in **blocks per
function**, not in module size (600 small functions of 7205 lines compile in
under 30 s, one 2000-branch function of 8005 lines exceeds 170 s); and the flat
self-time profile of that shape is ~49% GC pointer bookkeeping —
`pcc_gc_managed_pointer_find_slot` 14.7%, minor-graph lock/unlock 11.2%,
incref/decref 7.5%, `pointer_is_managed` + `index_contains` + `load_ptr` +
`store_root` the rest.

**Hypothesis.** The managed-pointer index is a process-wide structure whose
occupancy grows with the number of live managed objects. Compiling one giant
function holds its whole value/block/type universe live at once, so occupancy
grows with the function's size. Every "is this pointer managed?" question then
costs more as the function grows, and the number of questions also grows with
the function — O(N) work per question × O(N) questions is the observed
superlinear curve. On CPython the same code asks no such question at all, which
is why stage1 does not show the curve.

**This is a hypothesis, not a finding.** It predicts three things, each cheap to
check and each able to kill it:

1. `find_slot`'s cost per call rises with live managed-object count. If probe
   length is flat, occupancy is not the mechanism.
2. The flame graph attributes `find_slot` to a small number of callers asking
   redundantly about the *same* pointers, rather than to one question per
   distinct pointer. Redundancy means a memo fixes it; distinctness means the
   index structure has to change.
3. Splitting the giant function (roadmap item 1) reduces peak live objects, and
   the curve flattens with it. Item 1's shape effect is already measured; what
   is not measured is whether peak occupancy moved with it.

Until (1) and (2) are measured, any change here is a change made from profile
shape alone, which this file already records as repeatedly measuring zero.

## Status

Root cause not established. Hypothesis stated with its refutation conditions;
the flame graph tooling needed to test condition (2) now exists.

## Update No.32 — flame graphs land; Update No.31's hypothesis is [DENIED]

`scripts/pcc_flamegraph.py` produces both graphs from an Apple tool's call
tree, folded to `caller;callee <self weight>` and rendered to a self-contained
SVG: `cpu` from `sample`, `heap`/`peak` from `malloc_history -callTree`
(the target needs `MallocStackLogging=1`).

Two parsing defects had to be fixed first, both of which silently produce
plausible-looking nonsense:

* `sample` prints a **flat top-of-stack summary** after the call tree, indented
  about four columns. Folding it together with the tree invented shallow paths —
  the heaviest "stack" in the first run was
  `Thread;start;pcc_gc_managed_pointer_find_slot`, which cannot happen — and
  double-counted every sample. `pcc_profile.py` had the same defect.
* The busiest-descendant walk could pick a short-lived helper (`sh`, `as`) that
  exited before the sample began. It now requires the chosen pid to still be
  alive.

**Result, pcc2 compiling one 9000-branch function, 20770 samples:**

```
self time                                  caller attribution
10.8%  pcc_gc_managed_pointer_find_slot    IRBuilder.call1, _irbuilder_call_from_args_list
 8.1%  py_class__strs_eq                   StmtDispatch/ExprDispatch/OwnershipLowering mixins
 5.9%  py_class__class_lookup_in_mro       same mixins
 5.4%  py_gc_minor_graph_lock
 3.0%  strlen                              (from strs_eq)
 2.3%  py_incref     1.9% py_decref
 2.1%  py_gc_minor_graph_unlock
 1.8%  pointer_is_managed_no_lock  1.8% gc_load_ptr  1.8% memmove
```

**Update No.31 is [DENIED].** It predicted `find_slot` would be attributed to a
few callers re-asking about the *same* pointers, so a memo would fix it. The
graph shows the opposite: its callers are the IR **emission** sites
(`IRBuilder.call1`, `_irbuilder_call_from_args_list`), one question per emitted
instruction, on distinct pointers. A memo cannot help. The occupancy story is
also unsupported by this data and remains unmeasured.

**What the graph found instead — and it is the answer to "why is pcc1 slower
than pcc0".** Attribute and method lookup is **14.0%** of self time, and its
callers are the codegen dispatch mixins. `_class_lookup_in_mro`
(`pcc/py_runtime/py/py_class.py:407`) is a nested linear scan with **no cache**:
for each class in the MRO, scan *every* method of that class, comparing names
with `_strs_eq`. So one method call costs O(MRO depth x methods per class)
string comparisons. CPython answers the same question with one dict hit plus a
type-version cache.

Three previously separate observations collapse into this one mechanism:

* the callee-signature cache that produced byte-identical IR and still made
  pcc1 **11% slower** — it was read through `getattr`;
* the general rule recorded here that host-green optimisations measure zero or
  negative under pcc1;
* **the layer1 split into many mixins deepens the MRO, so refactoring the
  compiler for readability directly taxes pcc1.** The architecture is tuned for
  CPython's cost model.

`_strs_eq` is not the thing to optimise — it already short-circuits on pointer
equality and on a two-character prefix. The call *count* is the defect, so the
fix is a method-lookup cache, the way CPython does it: a global direct-mapped
`(class, name) -> method` cache with a version counter bumped on any class
mutation. Two hazards to design around, both recorded elsewhere in this repo:
`_class_lookup_in_mro` re-reads the method slot through
`pcc_gc_note_relocation_read`, so cached function pointers are invalid under
the moving backends (#3/#4) unless the cache stores the slot and re-reads
through the barrier; and adding fields to `PyClassObject` has a history of
layout drift between the C struct and its pcc-Python mirror, so a global side
table is safer than a per-class field.

**Memory graph.** pcc allocates through its own allocator, so `malloc_history`
sees bulk `pcc_allocator_refill_small` -> `mmap` refills rather than per-object
allocations; the graph answers "which path drove the allocator to grow".
Attributed to the deepest pcc frame, growth is spread rather than
concentrated: `Block_append` 5.8%, import scanning ~15% across four helpers,
`type_infer._infer_stmt` 4.4%, the IRBuilder call helpers ~10%, lexing 3.8%.
Per-object attribution would need the runtime to capture a stack per
allocation.

## Status

Both flame graph tools work and are indexed in AGENTS.md. The stage2 root cause
is now attributed, not guessed: uncached MRO method lookup (14%) plus
per-emission managed-pointer probes (10.8%). No fix implemented in this update.

## Update No.33 — the GC tax quantified, and a [DENIED] verdict whose mechanism is now gone

Both flame graphs now use the same estimator (blocked frames excluded on both
the native and host sides; the earlier pcc0-vs-pcc2 pair did not, which made
the comparison meaningless). Same input, same command:

```
                              pcc0 (host, 1987 on-CPU)   pcc2 (native, 20642)
GC barriers / pointer index          0.3%                      57.2%
attribute + method lookup             --                       14.3%
stable string-key computation       26.7%                  (swamped)
IR text parsing                     10.4%
allocation / memmove                 0.4%                       4.9%
```

**That 0.3% vs 57.2% is the answer to "why is stage2 slower than stage1".** It
also corrects Update No.32's closing claim: operand de-stringification is
pcc0's largest cost, not the prerequisite for pcc1 overtaking pcc0. pcc1's
problem is an order of magnitude larger and it is GC bookkeeping.

Two optimisations were measured and **[DENIED]** on the way here, both aimed at
making a hot leaf cheaper:

```
per-character modulo -> mask      0.98x   (byte-identical output)
memoising the _stable_*_key trio  0.99x   (reverted; unbounded caches, no gain)
```

The second denial explains the first: `_dot_numeric_text_key_id` short-circuits
before the character loop for `%.N`-shaped names, which is nearly every SSA
name, so the loop barely runs. The 10.7% attributed to
`_stable_text_bucket_key` is call overhead, not loop body. Making a leaf
cheaper cannot help; only not calling it can. That is why the two changes that
*did* work this session (`_text_list_contains`, `_record_use_position`, both
byte-identical) replaced O(n) calls with one lookup rather than speeding up the
comparison.

**Where the 57.2% actually sits**, by caller:

```
5.6%  IRBuilder.call1
4.7%  _irbuilder_call_from_args_list
3.1%  IRBuilder.call2
2.4%  Block_append
2.4%  IRBuilder._int_binop
----
18.2% of ALL on-CPU time is GC work charged to the IR emission helpers
```

`_irbuilder_call_from_args_list` rebuilds the callee signature on **every**
emitted call: two temporary lists, a `str(t)` per argument type, a join, and a
concatenation. For a fixed callee such as `py_incref` the result is identical
every time.

**This is where a recorded [DENIED] verdict becomes live again.** A callee
signature cache was tried before, produced byte-identical IR, and was rejected
because it made pcc1 **11% slower** (6.87 s -> 7.61 s) -- the recorded cause
being that the cache was read through `getattr`, and `getattr` in pcc-compiled
code walked an MRO and hashed a string where CPython does one dict hit.

That mechanism was removed this session: `_class_lookup_in_mro` now has a
method cache (green on all five GC backends). So the denial's stated cause no
longer holds, which is exactly the "new evidence that overturns the verdict"
the investigation rules require before re-trying a refuted change. It is not
proof the cache will now win -- that needs a pcc1 number, and per this file's
own rule a fix aimed at pcc1 requires a pcc1 measurement.

**Next, in order:** rebuild pcc1, re-measure the callee-signature cache under
it, and if it wins, extend the same "allocate once per callee, not once per
emitted instruction" treatment to `Block_append` and `_int_binop`. That attacks
18.2% of on-CPU time without touching a single GC barrier.

## Status

Root cause quantified and localised to specific call sites. Two symptom fixes
denied with measurements. The highest-value lead is a previously refuted change
whose refutation no longer applies.

## Update No.34 — measuring the callee signature cache under pcc1

Re-trying a change this file records as **[DENIED]**, with the evidence that
overturns the verdict: the denial's stated cause was that the cache was read
through `getattr`, which in pcc-compiled code walked an MRO and hashed a
string. `S-P0-MRO-METHOD-CACHE` removed that walk this session (method cache,
green on all five GC backends), so the mechanism behind the 11% regression no
longer exists.

The cache avoids both failure modes this repo has recorded:

* **not an attribute read** — keyed on `id(fn)` in a module-level dict, an
  int-hashed lookup with no `getattr`, which is what the old attempt paid for;
* **the entry pins its key** — `(fn, callee_ref, ret_ty, sig_text,
  expected_arg_types)`, because an `id()`-keyed memo whose entry drops its key
  lets address reuse turn a stale fingerprint into a **HIT**; that exact bug
  stopped pcc1 compiling any program containing a `def` for a day.

**Measurement protocol**, chosen after a first attempt was useless:

```
fresh salted input per run   pcc's object cache is keyed on source content;
                             reusing one input read 23.3 s against 54.1 s,
                             which is a cache hit and not a speedup
3 runs per arm, take min     two 2000-branch runs differed 206.4 s vs 269.0 s
                             (15%), which would drown a moderate win
600-branch input             keeps each run ~60 s so three fit in a budget
same binary path A/B         the baseline pcc1 is copied aside before the
                             rebuild overwrites it
```

Baseline pcc1 (built from current source without the cache):
`61.0 / 65.0 / 61.5 s`, **min 61.0 s**, spread ~6%.

Host-side byte identity with the cache applied: confirmed against the
pre-change binary (`509782244c2a3467`).

## Status

Baseline measured; cached build in progress. The verdict recorded next must be
a pcc1 number, since a host number is what made the original attempt look
acceptable.

## Update No.35 — the callee signature cache is [CONFIRMED] under pcc1

The change this file recorded as **[DENIED]** now measures a win, because the
mechanism behind the original refutation was removed this session.

```
baseline pcc1     61.0 / 65.0 / 61.5 s     min 61.0 s   (spread 6%)
signature cache   54.0 / 54.5 / 55.2 s     min 54.0 s   (spread 2%)
                                           1.13x, -11.5%
```

Correctness: both pcc1 binaries compiled the same input to a **byte-identical**
binary (`1c7c74dbce92fae3`) and the compiled programs print the same result.
Host-side output was byte-identical too. Gates: llvm_capi IR parity +
end-to-end + bootstrap gate baseline, 29 passed.

**Why it wins now and did not before.** The 2026 attempt produced identical IR
and was rejected on a pcc1 number: 6.87 s -> 7.61 s, **11% slower**, because
the cache was read back through `getattr` and `getattr` in pcc-compiled code
walked an MRO comparing method names with `_strs_eq`. `S-P0-MRO-METHOD-CACHE`
landed a method cache this session (green on GC backends 0..4), so that walk is
no longer on the path. The new cache also sidesteps it structurally: it is a
module-level dict keyed on `id(fn)` — an integer hash, no attribute access —
and each entry stores `fn` itself so address reuse cannot turn a stale
fingerprint into a hit, the failure that once stopped pcc1 compiling any
program containing a `def`.

Note the symmetry with the two denials in Update No.33: those tried to make a
hot leaf cheaper and measured 0.98x/0.99x. This one removes work entirely —
two temporary lists, a `str()` per argument type, a join and a concat, per
emitted call, for a result that is constant per callee.

**Measurement protocol** (each clause exists because a sloppier version lied):
fresh salted input per run, because pcc's object cache is keyed on source
content and reusing one input read 23.3 s against 54.1 s; three runs per arm
taking the minimum, because two 2000-branch runs differed 206.4 s vs 269.0 s;
600-branch input so three runs fit the budget; and the baseline binary copied
aside before the rebuild overwrote it.

**Next, same shape, not yet measured:** `IRBuilder._int_binop` calls
`str(lhs_ty)` per emitted instruction on interned type objects
(`IntType(64) is IntType(64)`), so a `_type_text` cache with the same id-keyed,
key-pinning shape should remove another slice of the 18.2%. Patch is written
but deliberately not stacked onto this measurement.

## Status

[CONFIRMED] 1.13x on pcc1, byte-identical output, gates green. The first
measured reduction of the 57.2% GC-bookkeeping tax, achieved without touching
any GC barrier.

## Update No.36 — why a real stage2 failure reported nothing, and the four defects behind it

A stage2 run reported `PCC_BOOTSTRAP_STAGE_RESULT stage=2 elapsed_ms=334749
output=.../pcc2` and no pcc2 existed. Reading that line as a timing gives an
8x "speedup" for a run that produced nothing. Four separate defects had to be
fixed before the actual error was even reachable:

1. **`scripts/bootstrap.sh` emitted a success-shaped result line on failure.**
   It did exit non-zero, but the line anyone greps for carried an elapsed time
   and an output path for work that never happened. Now a failed stage prints
   `PCC_BOOTSTRAP_STAGE_FAILED ... rc=N output=<none>`, a missing artifact fails
   even at rc=0 (`output=<missing:...>`), and the success line carries `rc=0`.

2. **The diagnostic read `.message`.** `str(getattr(exc, "message", ""))` has
   returned an empty string for every exception since Python 2, so the failure
   text was never printed at all. It reads `safe_exception_text(exc)` now.

3. **The worker reported an empty `PyPipelineError: `.** It now falls back to
   `repr(exc)` plus a traceback when the text is empty.

4. **`raise X from exc` does not set `__cause__` under pcc1.** Verified with a
   probe: the wrapped message survives (`str(exc)` gives `'inner detail'`) but
   `getattr(exc, "__cause__", None)` is **MISSING**. So a diagnostic that walks
   the cause chain — the obvious fix, and the one added first — cannot work in
   the self-hosted compiler at all. The type name has to travel *inside* the
   message instead, so all 22 re-raise sites in `pipeline.py` changed from
   `str(exc) or type(exc).__name__` to `_wrapped_error_text(exc)`, which always
   prefixes the original exception's type.

Item 4 is a genuine runtime gap worth its own row: exception chaining is part of
Python semantics, and losing it silently degrades every diagnostic pcc emits
about itself.

**Also recorded: a pcc-Python port authoring hazard.** Bisecting the MRO cache
by inserting `return null()` at the top of `_class_lookup_cache_block` — leaving
the original body as dead code — produced a pcc1 that could not compile
`print("hi")`. The probe was intended as a no-op and instead broke the compiled
function, which nearly produced the conclusion "the MRO cache breaks pcc1".
Restoring the source and rebuilding was clean. Disable a port function by making
its caller skip it, not by an early return over dead code.

**What is established about the stage2 failure itself**, each with a control:

```
pre-existing        baseline pcc1 (no signature cache) fails on the same input
not the MRO cache   disabled and rebuilt: still fails
not the traceback   import: that module's closure check is exit=0
not the source      host pcc compiles pcc/__main__.py successfully
pcc1 is otherwise healthy   it compiles print/2000-branch/exception probes fine
```

## Status

Diagnostics repaired; the failure is reproducible and bounded to pcc1 compiling
the full pcc source. The error text itself is still pending a run with the
repaired messages.

## Update No.37 — two more emitter caches [DENIED], and the rule that predicts it

Baseline pcc1 (module-try fix + signature cache): **53.1 s** first, and **53.9 s**
when re-measured in the same window as the arms below — so the machine drifts
about 0.8 s over a session, and the first report of these numbers was inflated by
that drift. Against the same-window baseline, all three attempts produced
**byte-identical** output:

```
remove Block._text_lines (a full duplicate of [rec.text for rec in _instrs],
  also making _replace_record_text O(1) instead of a linear scan)  55.6 s  -3.2%  DENIED
cache str(Type) in _int_binop (IntType is interned, so the same object
  was re-stringified once per emitted instruction)                 55.1 s  -2.2%  DENIED
drop the temporary list in _next (it existed only to be joined with
  an empty separator: one list allocation per emitted instruction)  54.3 s  +0.7%  NEUTRAL
```

The two caches were reverted. `_next` was kept: within noise, but strictly less
code and one allocation fewer, so there is nothing to trade away.

**Methodology note this cost:** the first pass reported 4.7% and 3.8% by
comparing against a baseline measured earlier in the session. Re-measuring the
baseline arm in the same window moved both verdicts by ~1.5 points and turned a
third "regression" into noise. An A/B whose arms are measured hours apart is not
an A/B.

**The rule these four denials share** (with the two in Update No.33 at 0.98x and
0.99x): in pcc-compiled code a cache is not free — the lookup allocates. A dict
`get` plus the tuple that pins the key costs more than `str(ty)` on an interned
type, which is a small method returning a stored string. Removing one cheap
operation and adding a dict probe is a net loss.

The signature cache won for the opposite reason: it removed **two temporary
lists, one `str()` per argument type, a join and a concatenation, per emitted
call** — many allocations replaced by one probe.

```
worth doing   removes MANY allocations per hit, adds one probe
not worth it  removes ONE cheap operation, adds a probe and a tuple
```

`_text_lines` has the same shape: incremental `append` per instruction is
amortised, while rebuilding an N-element list per block render allocates more in
total. "Fewer stored copies" is not the same as "fewer allocations", and in pcc1
only the second one matters.

## Status

Both [DENIED] with measurements and reverted. The signature cache (1.13x) stands
as the only accepted emitter change; the remaining 18.2% attributed to the
emission helpers needs a change that removes allocation in bulk, not one that
trades a small operation for a lookup.

## Update No.38 — the host-side bottleneck was a character loop, 14.7x off it [CONFIRMED]

The flame graph of a whole stage1 build (428 self-profiling processes, 9632
samples, via `scripts/pcc_flamegraph.py host --cmd`) put the largest single
consumer somewhere none of the previous work had looked:

```
before                                       after
20.6%  rename_llvm_global_refs        ->      1.4%   (rank 1 -> rank 14)
 3.1%  llvm_global_name_char          ->      3.3%
23.7%  combined                       ->      4.7%
 9632  total samples                  ->      7615   (-20.9%)
```

`rename_llvm_global_refs` inspected **every character of the whole IR text in
Python** to find `@name` references outside string literals. IR text is
overwhelmingly neither `@` nor `"`, so the fix is to jump with `str.find` and let
the scan happen below the interpreter; the Python-level loop then runs once per
interesting position instead of once per character. Measured on a real 0.9 MB
module with 332 renames: **0.040 s -> 0.007 s, 5.8x**, output identical.

Correctness: 6000 randomised inputs over an alphabet of `@ " \ . $ - _` and
newlines, compared against the original character-scan kept as an oracle — **0
mismatches**; identical output on real IR; the compiled binary is byte-identical;
60 tests pass in the multi-file and IR-parity gates.

The module has no imports beyond `__future__`, i.e. it is inside the
no-libpython closure, so `re` was not an option — `str.find` only.

**The first version of this fix measured 0.8x — SLOWER than the character loop
it replaced.** It called `text.find('"', index)` at every `@`, so each of the
hundreds of references re-scanned the same span looking for the next quote:
`find` is cheap per call but not free per character scanned, and that turned a
linear scan into a quadratic one. Carrying the quote position and refreshing it
only when the cursor passes it gives the 5.8x. **A change that looks exactly like
an optimisation was a pessimisation, and only measurement separated them.**

`llvm_global_name_char` did not improve (3.1% -> 3.3%), as expected: it is
charged per character *of the names themselves*, and the number of names did not
change. It is now the dominant cost inside this function and the next candidate.

## Update No.39 — the 16 GB is in the coordinator, not the emitters

Sampled `Physical footprint` (not RSS) every 20 s across a full stage2:

```
total peak            19.8 GB
single-process peak   16.2 GB   pcc1 coordinator
emit worker            1.3 GB   pcc1 --pcc-self-backend-emit-batch
```

The curve rises monotonically in the second half (8.8 -> 13.5 -> 19.4 GB) while
worker turnover only produces small dips, so the coordinator is accumulating —
it holds every module's IR text at once.

This reframes the earlier work: the signature cache, `_text_lines`, `str(Type)`
and `_next` all targeted the **emitter**, which accounts for 1.3 GB. The 16.2 GB
was never touched. It also corrects an earlier note in this file that blamed a
9.0 GB reading on forced-serial diagnosis: normal parallel stage2 reaches 16.2 GB
in one process on its own.

Stage2 timing, which must be reported cold-vs-warm because the two differ by
3.4x: **2056 s** on the colder run, **612 s** warm, pcc2 186 MB, `rc=0`.

## Status

[CONFIRMED] 14.7x off the top host-side consumer, byte-identical, gates green.
Coordinator memory is the next structural target; `llvm_global_name_char` the
next cheap one.

## Update No.40 — what the 6.4x is worth, stated honestly

Three stage2 runs after the module-try fix:

```
2056 s   first successful run
 612 s   warm
1152 s   with the rename_llvm_global_refs work
```

**These are not comparable.** Cache state differs between them and cache state
alone moves stage2 by 3.4x, so subtracting them would be the same error this file
records repeatedly. The attributable evidence is the pair of measurements that
control for everything else:

```
flame-graph attribution, same command / tool / estimator before and after
  rename_llvm_global_refs   20.6% -> 1.4%   (rank 1 -> rank 14)
  with its char helper      23.7% -> 4.7%
  total samples             9632  -> 7615   (-20.9%)
microbenchmark, real 0.9 MB module, 332 renames
  0.041 s -> 0.006 s = 6.4x, output identical character-for-character
```

Memory did not move: **19.0 GB total, 16.2 GB single process**, same as before.
That is the honest expected result — both changes were CPU-side, and the
coordinator holding every module's IR text was not touched.

A clean stage2 wall-clock comparison needs two runs with identical cache state
and no source change between them. That is a separate experiment and has not
been run.

## Status

Two [CONFIRMED] CPU wins with direct attribution; memory unchanged and still the
largest open problem.

## Update No.41 — coordinator IR-text retention localized in source; text alone cannot be 16.2 GB

Read-only localization of the No.39 accumulation, no run performed (another
session was actively benchmarking pcc1 on this machine, and concurrent pcc1
runs corrupt both measurements).

### The retention chain, with the copies it makes

```text
pipeline_self_backend_link.py:41-49   link_self_backend reads EVERY module .ll
                                      back into RAM: ir_texts.append(
                                      normalize_ir(stream.read())) — the fread
                                      consumer; profile phase "link_self_read_ll".
                                      normalize_ir makes a 2nd transient copy
                                      per module.
pipeline.py:2945 / compile_parallel   upstream, module_ir_texts already holds
                                      (name, ir_text) for every module during
                                      the frontend phase.
pipeline_self_backend_emit.py:373-401 emit writes every text BACK to tmp files
                                      (self_backend_module_<i>.ll) for the
                                      workers; split_large_ir_modules can copy
                                      the large ones again; `inputs` stays alive
                                      through the whole emit phase for the
                                      in-process fallback (line 756) and the
                                      threshold scans (line 838).
```

So the coordinator's life is: .ll on disk -> all texts in RAM (plus a
normalize copy) -> all texts written back to disk -> workers run on files.
The list is pure relay; nothing after the tmp-file write needs the text except
the fallback path.

### Counter-evidence against "text retention = 16.2 GB" [recorded so streaming is not oversold]

`~/.cache/pcc/python-ir-pass-cache` holds the frontend IR **for many
generations combined** in 389 MB of .ll files. One closure's single copy of
every module text is therefore on the order of a few hundred MB, not 16 GB.
Streaming/path-handoff removes the retention and the double disk round-trip,
but a 16.2 GB peak requires a multiplier the text list alone does not provide.
Candidates, unmeasured: normalize/split copies stacking, per-line string
processing fragmenting the pcc allocator (which never returns footprint to the
OS), or a different phase entirely. Update No.39's "it holds every module's IR
text at once" is a hypothesis fitted to a monotonic curve, not an attribution.

### Next probe (designed, not run)

One warm stage2 with per-phase profile JSON enabled plus a 10 s
`footprint`-sampling loop on the coordinator pid; align the footprint curve
against the phase boundaries (`link_self_read_ll`, emit dispatch, link). That
splits frontend-collect vs emit vs link ownership of the 16.2 GB for the cost
of a single ~455-612 s run. Only run it when no other pcc1/bootstrap work is
live on the machine.

Task-board row: PERF-P1-STAGE2-COORDINATOR-IR-STREAMING.

## Status

Retention chain localized to three sites; attribution of the 16.2 GB still
open — the next probe is the phase-aligned footprint curve, blocked only on
machine availability.

## Update No.42 — fixed-arity IRBuilder cache hits are [DENIED]

The candidate moved declared-callee cache hits for `IRBuilder.call0/call1/call2`
into three arity-specific renderers.  Its intended win was concrete: avoid the
temporary argument list and the generic loop for the dominant zero-, one-, and
two-argument calls.  Because pcc-native registers every managed local at
function entry, the final candidate also used a thin public dispatcher and
separate fast/slow functions; merely branching inside the fast renderer would
have charged its complete root set to the generic control arm.

Correctness was green before measurement:

* callee cache/mutation/byte-differential tests: **21 passed**;
* compiled `ir.py` root/container/source-closure gates: **7 passed**;
* self/no-libpython stage1: `rc=0`, **179.433 s**, candidate bootstrap source
  `692e526845c5fad27826c31f473c86f7065b33e2962336024a555a9b4dba4153`,
  pcc1 SHA256
  `672d4c9e77e9c6c59228a2a3ac721875c660da5a172b3728777111e57a23d19d`.

The measurement used that one pcc1 binary, explicit GC0, frontend and self
backend object caches disabled, debug-call trace disabled, and three salted
300-branch inputs.  Each arm was a fresh process; order alternated
fast/force-generic, force-generic/fast, fast/force-generic.  Every matched pair
produced a byte-identical native binary and identical runtime output.

```
                         fast                         force-generic
wall (s)           64.28  64.98  64.78          66.12  71.33  63.64
user (s)           61.30  61.39  61.18          61.59  64.79  60.79
sys (s)             1.40   2.01   2.01           1.60   2.19   2.02
instructions (B)  409.87 407.37 407.13         404.36 405.51 404.44
cycles (B)         85.74  86.04  85.26          85.25  86.49  85.13
max RSS (GB)        3.716  3.716  3.660           3.337  3.495  3.718
peak footprint (MB)400.77 400.80 400.74          408.58 408.50 408.55
```

Median result: wall **64.78 -> 66.12 s** (fast is only 2.0% faster, below the
3% acceptance line); summed user+sys is **63.19 s on both arms**; fast retires
**0.73% more instructions**, uses **0.57% more cycles**, and reports **6.3%
more max RSS**.  Its process peak footprint is 1.9% lower, but that isolated
metric cannot offset flat CPU and regressions in the other resource counters.

This is conservative against the candidate: the force-generic arm still paid
the common dispatcher and then an extra slow helper before entering the old
generic core.  The real pre-candidate wrapper called the generic core directly.
Therefore a fixed renderer that cannot beat this deliberately heavier control
on CPU/instructions/RSS cannot be accepted.  A separate older-vs-newer binary
comparison showed a much larger regression, but crossed other source changes
and is intentionally **not** used for the verdict.

The fixed renderers, slow wrappers, dispatcher, and measurement-only
`PCC_FORCE_GENERIC_IR_CALLS` switch were reverted.  Two independent changes
remain:

1. the module cache keeps an immutable tuple mutation guard at entry 4 and a
   list render view at entry 8; this fixes the pcc1 `py_list_getitem(tuple)` NULL
   regression without weakening mutation isolation;
2. generic per-argument rendering uses two named concatenations instead of a
   three-item temporary list, preserving moving-GC roots while deleting that
   allocation.

After the revert the same focused gates are green (**21 passed**, **7 passed**).
Do not retry an arity-specific renderer with several managed text locals: its
entry-hoisted root/frame cost is now measured.  A future retry needs a different
representation that reduces the generic core itself, plus an absolute
same-source baseline; another wrapper/dispatcher split is the same denied
shape.

## Status

`[DENIED]` and reverted.  Cache correctness and the generic no-three-item-list
change remain; neither is claimed as the fixed-arity performance win.

## Update No.43 — the direct-mapped MRO method cache is [DENIED]

The 1024-entry `(class address, name address) -> (MRO index, method index)`
side cache was made correctness-complete before measurement: sequence-guarded
publication, content revalidation, shared-epoch mutation invalidation, GC3/4
method relocation healing, and a GC4 class-forwarding epoch bump before old
address reuse.  Its focused pre-measurement gate passed all four nodes in
289.16 s across the C and pcc-Python runtimes.

The performance comparison then used two self/no-libpython pcc1 binaries:

* candidate pcc1 SHA256
  `cb5fc3e4b515649ba53015ac33c14b88a9892206120951476e3171789e27d66e`;
* exact no-MRO-cache pcc1 SHA256
  `52673d4781cd944c6faf334d7f82fbd1548049ed72ac97a144abbf7712b978e4`.

The second compiler differed only in the pcc-Python
`_class_lookup_in_mro` implementation: the cache helpers and cache path were
replaced by the parent relocation-safe linear walk.  Both compilers linked
their outputs against the same candidate runtime archive
(`5dfe654949694173a86aec718451c00c3030bd4df35bc6e57e3cb41525a44cf6`),
because the baseline archive independently exposed a target-link failure and
would have introduced a second variable.  The measurement used explicit GC0,
disabled frontend and self-backend object caches, one unmeasured warmup per
compiler, three salted 300-branch inputs, and alternating order
candidate/baseline, baseline/candidate, candidate/baseline.  Every matched
pair produced a byte-identical native binary and identical runtime output.

```
                         MRO cache                    linear no-cache
wall (s)           63.37  65.37  64.05          64.19  64.41  63.87
user (s)           61.00  61.59  61.44          61.22  61.68  61.33
sys (s)             1.98   2.23   1.84           1.92   2.12   2.06
instructions (B)  405.17 408.17 404.31         404.13 404.32 404.29
cycles (B)         85.40  86.44  86.91          85.45  86.58  86.25
max RSS (GB)        3.716  3.716  3.718           3.716  3.716  3.718
peak footprint (MB)407.00 407.00 407.03          407.00 406.96 406.95
```

Median result: wall **64.19 -> 64.05 s**, only **0.22% faster**; summed
user+sys **63.39 -> 63.28 s**, only **0.17% lower**.  The candidate retired
**0.22% more instructions** and used **0.22% more cycles**; RSS and footprint
were effectively unchanged.  This misses both the 3% materiality floor and
the task's requirement to materially remove the recorded lookup hotspot.

An immediate-start 12 s flame graph on the same first input explained why.
The cache candidate attributed 383 of 9952 samples (**3.848%**) to
`class_lookup_in_mro`, `_strs_eq`, and the two cache helpers; the linear
baseline attributed 374 of 10054 (**3.720%**) to lookup plus `_strs_eq`.
Only nine candidate samples landed in cache helpers, while the residual scan
cost was unchanged.  The historical 14.0% number came from a different
9000-branch pcc2 workload and was not a valid prediction for this controlled
pcc1 window.

The direct-mapped MRO cache, its C/pcc-Python side tables, and its freestanding
global were removed.  The test file was not deleted: it now locks the chosen
linear relocation-safe walk, MRO shadowing, dual-runtime GC0..4 behavior,
GC3/4 method/class relocation, and concurrent immutable-table reads.  The
post-revert gate is green: **4 passed in 292.38 s**
(`build/mro-linear-focused-20260820.log`).

Three independent correctness fixes remain because the older four-slot
instance-field cache still needs them: atomic class-cache epoch access,
successful C `py_class_delattr` invalidation, and GC4 class-forwarding
invalidation before raw-address reuse.  `py_class_add_method` also retains a
cold epoch bump because a newly installed data descriptor can shadow an
already cached instance field.  These are not claimed as MRO-cache wins.

Do not retry this direct-mapped raw-address location-cache shape.  A future
MRO-dispatch proposal must first demonstrate a high hit rate on the current
frozen pcc1 workload and must reduce retired instructions in a same-source
absolute A/B; source-level O(1) reasoning is not performance evidence here.

## Status

`[DENIED]` and reverted.  Semantics and independent field-cache correctness
fixes remain; the next performance slice must be selected from the current
profile rather than the historical 14.0% sample.

## Update No.44 — the mutation-safe per-Module signature cache is [DENIED]

After the MRO cache denial, the next task-board boundary was measured before
adding more emitter code: compare the hardened per-`Module` callee-signature
cache with an exact no-signature-cache compiler.  This is distinct from the
fixed-arity renderer denied in Update No.42.

Both pcc1 binaries were built from the same restored source except for two
cache boundaries in `pcc/llvm_capi/ir.py`: the no-cache arm omitted
`Module._callee_signature_cache` and rebuilt declared-callee signature text on
every call, while preserving the current generic per-argument concatenation,
moving-GC roots, debug behavior, and function-pointer path.

```
per-Module cache pcc1 SHA256
  8678a6a58bff4df89ce652888214f6860f58351a53f7bb28d3b8ab1cbd4fda05
exact no-cache pcc1 SHA256
  35caef0ea5bb36a6b595f24ca9a155f302475dacb139c9746506d28831d1088a
shared runtime archive SHA256
  730ca846333a7b0aab338f47874348f48f9d687de30459809e2e2ae312799f14
candidate ir.py SHA256
  54ea73c193f61eb351a62c4bd837aa36fd858ff9006425c5aa7d227719059525
no-cache ir.py SHA256
  4369f23f81bba6741756ecf3021d0d727fbeae055489c629e4696d61f20aa145
```

Protocol: explicit GC0; same runtime archive; frontend and self-backend object
caches disabled; debug-call trace disabled; one unmeasured warmup per compiler;
three salted 300-branch inputs; alternating order cache/no-cache,
no-cache/cache, cache/no-cache.  Every matched pair produced a byte-identical
native binary and the three outputs were `74856`, `84856`, `94856`.

```
                         per-Module cache             exact no-cache
wall (s)           63.87  65.85  64.28          64.95  63.40  64.68
user (s)           61.32  62.19  61.26          61.59  61.14  61.43
sys (s)             1.91   2.27   1.75           1.91   1.89   1.99
instructions (B)  405.42 404.45 403.87         405.91 405.75 406.11
cycles (B)         86.01  88.57  86.39          86.60  85.29  86.62
max RSS (GB)        3.716  3.716  3.718           3.716  3.716  3.718
peak footprint (MB)407.06 407.04 407.00          410.21 410.21 410.13
```

Medians: wall **64.68 -> 64.28 s** (**0.62% faster**), summed user+sys
**63.42 -> 63.23 s** (**0.30% lower**), instructions **0.36% lower**, cycles
**0.24% lower**, max RSS flat, footprint **0.77% lower**.  The direction is
slightly positive, but it misses the task's required 1.08x benefit by an order
of magnitude and is not material enough to justify the dict, nine-field entry,
mutation walk, and lifetime/concurrency surface.

This does not reproduce Update No.35's 1.13x global-cache result.  The current
safe version adds `fn.module` plus `_callee_signature_cache` attribute access,
a Module-owned dict lookup, and full FunctionType mutation validation; the
historical process-global cache did not.  Removing the MRO method cache also
invalidated the old claim that attribute lookup was now cheap.  These are
candidate explanations, not attribution.  The process-global dict cannot be
restored as an optimisation: pcc-native has no GIL around concurrent dict
resize, it retains completed Module/Context graphs, and its old entry did not
observe in-place FunctionType mutation.

The only allowed next proposal is the task-board fallback already stated: one
explicitly initialized cache-entry field on `Function`, with no `getattr`, no
Module dict and no process global.  It must replace, not stack on, the denied
per-Module cache; an entry stored on its owning Function does not need to pin
`fn`.  If a same-source absolute A/B still fails the 1.08x threshold, remove
signature memoization entirely and retain the simpler generic renderer.

## Status

`[DENIED]` as the production shape.  The source was restored after building
the exact no-cache compiler only to define the next single-variable proposal;
the per-Module cache is not accepted performance evidence.

## Update No.45 — exact Function-slot call rendering is [DENIED]

The final signature-render proposal removed memoization and used an exact
`Function` guard plus constant-index `name`/`ftype` field loads; subclasses and
duck values retained the dynamic path.  A receipt-bound four-pair A/B compared
that current path with the otherwise identical dynamic no-cache path.  Every
matched input produced a byte-identical native binary.

Candidate pcc1 was
`e1cfc09b14fe518a034f23f6308dcd57e959d2967a2b11565c55c7ea174a804a`;
baseline pcc1 was
`e8f4514da4875785827ddc36e0712537215b2a4b668edd2d6d3dfa0fc0d96e3c`;
both used runtime archive
`4ffd3d021d5af696cd996d40d4d588040c9509f44f07d56c3643b2eff03e3e1b`.

Median wall was **29.645 s candidate versus 29.735 s baseline**, a paired
speedup of only **1.003711x**.  Paired wall ratios were 0.9811, 1.0041,
1.00336, and 1.00915; one arm was slower.  CPU was 0.6% lower, instructions
0.1% lower, cycles 0.7% lower, and RSS effectively flat.  This misses the
pre-registered 1.08x threshold by an order of magnitude.  Stage1 build times
from different build order/windows are not used as performance evidence.

Do not continue optimizing IRBuilder call rendering from this profile.  The
real stage2 self-emit flamegraph contains no attributable IRBuilder/llvm-capi
path, while precise stack-map planning and its GC/reference traffic dominate.

## Status

`[DENIED]` as a stage performance optimization.  Correct exact-Function versus
subclass behavior may remain for semantics; it carries no speed claim.

## Update No.46 — complete current stage2 and native worker lifetime

A source-current GC0 self/no-libpython run now completed both stages on the
same frozen source:

```
source manifest  91b940cdeec1e8ec267f1b4adfcf01ae74740f61b4855f322474d2007ef83815
pcc1             aaeffa06d1a251f622aa057928644e0f41cd863cc6d2de3755a9028ef46eb4e5
pcc2             7a80a9ec42f592c5a4b5f12df984d8ba238055d4db1d826a5136a8084a83b4f3
runtime archive  4ffd3d021d5af696cd996d40d4d588040c9509f44f07d56c3643b2eff03e3e1b
```

Stage1 completed in **266.54 s** with max RSS **6.273 GB**.  Stage2 completed
in **875.10 s** with `time -lp` max RSS **9.834 GB**: stage2 is still
**3.283x** stage1.  Phase totals select two structural owners:

```
                              stage1       stage2       ratio
frontend codegen parallel     31.348 s     171.186 s    5.46x
native emit                   94.765 s     516.717 s    5.45x
pcc-owned link driver        104.113 s      99.719 s    0.96x
complete self-backend link   204.525 s     616.851 s    3.02x
```

The stage2 frontend scheduled 212 chunks for 212 modules, versus 40 chunks in
stage1.  Native emit accounted for 455 objects: seven oversized and 448 safe.

The first measured memory correction makes every native safe emit item a
fresh process while retaining four-item batches for host/source stage1.  A
real three-medium-item probe reduced max RSS from **4.823 GB** to **2.549 GB**
with wall **34.49 -> 34.47 s** and byte-identical assembly.  In the complete
stage2 all 448 safe manifests contained one item.  Periodic process-tree RSS
fell from the prior emit observation of at least **15.89 GB** to a current
observed peak **10.25 GB**; middle/late emit was generally **4.30--7.05 GB**.
The full observed reduction is at least 35.5%, but the <=8 GB task threshold
is still red.  Therefore this is retained as a partial memory improvement,
not a claim that all stage2 memory was batch accumulation.

The same real self-emit flamegraph has 11,906 samples.  Precise-stackmap
inclusive cost is 4,252 (35.71%); `build_stack_map_plans` is 3,659 (30.73%);
strict GC/refcount leaves are 61.0% of that planner; arena iterator/getitem is
784 (6.58% overall).  The next proposal is limited to a stackmap-private,
non-escaping reusable instruction cursor that removes generators *and*
transient views while preserving public iterator semantics.  A plain
`while + arena[index]` rewrite is pre-denied because it still allocates a view
per instruction and its visible Amdahl ceiling is about 1.071x.

Full receipts and measurement limits are recorded in
`docs/goal/evidence/2026-08-21-stage2-profile-and-native-worker-lifetime.md`.

## Update No.47 — the reusable precise-stackmap instruction cursor is [DENIED]

The Update No.46 profile justified one fail-first implementation: keep the
public compact-arena iterator/view semantics unchanged, but let precise
stack-map planning reuse two private mutable projections while its seven
internal scans walk dense arena storage directly.  The candidate removed both
the generator and the per-record `CompactParsedInstrView` allocation rather
than merely replacing `for` with `while + arena[index]`.

The first candidate exposed an independent compiler correctness bug before it
could be measured.  Eight local assignments used
`instr: ParsedInstr = cast(ParsedInstr, cursor)`.  `typing.cast` codegen
returned its borrowed second operand unchanged, while assignment ownership
classified the outer `Call` as owned.  Preserved stage1 IR therefore released
the sole cursor owner on the next loop iteration without any retain, and the
real oversized shard failed after 15.70 s.  The first visible
`'attr.name.err.5868'` was a corrupted-state symptom, not proof of a CFG lookup
bug.  A proposed block-name bucket workaround was removed before measurement;
it would only mask the lifetime error and add hashing/list allocation to the
hot liveness loop.  The generic `typing.cast` ownership defect is tracked
separately as `PY-P0-TYPING-CAST-OWNERSHIP-TRANSPARENCY`.

The corrected candidate contains no cast or local instruction alias: every
scan consumes the private cursor synchronously and a distinct exception cursor
protects the only nested scan.  Focused public-view/stack-map tests passed
43/43.  A frozen self/no-libpython GC0 pcc1 built successfully in 242.03 s:

```
candidate source precise-stackmaps SHA256
  ef38f93d1b4f1c1c38db74b9936aa78dcd22d00290a549443568d98614da115e
candidate source self-backend-ir SHA256
  657128f2b110853f3213969edef38f62e1b050f4ed871317a8d09e20a68583fe
candidate pcc1 SHA256
  56d8b377be02e7a2dd5c4501d55954729223d705ca0b330c6824d53abab17b32
baseline pcc1 SHA256
  aaeffa06d1a251f622aa057928644e0f41cd863cc6d2de3755a9028ef46eb4e5
runtime archive SHA256
  4ffd3d021d5af696cd996d40d4d588040c9509f44f07d56c3643b2eff03e3e1b
```

The exact 5.011 MB shard that had failed then completed in 50.11 s versus the
52.16 s baseline control, and its 16.46 MB assembly was byte-identical.  This
was only about 1.041x faster.  A second fail-first pair used the most
stackmap-dense retained input, `self_backend_split_1_shard_17.ll` (37,545
stack-map labels).  Balanced warmups were candidate 36.55 s and baseline
38.05 s.  The measured pair was:

```
                              candidate          baseline       ratio C/B
wall                              36.78 s           38.25 s        0.9616
user + sys                        35.73 s           37.98 s        0.9408
instructions                379.624 billion   407.912 billion     0.9307
cycles                      118.428 billion   126.128 billion     0.9390
max RSS                       5.240 GB          5.270 GB           0.9942
peak footprint                5.225 GB          5.255 GB           0.9942
```

Both measured assemblies have SHA256
`7de52928a1647d65f13cd992fdc42746eeb8daace2253a1e7d5db0172584c595`.
The wall speedup is **1.03997x**, consistent with the first shard but below the
pre-registered **1.08x** acceptance threshold.  Because this is a fail-first
rejection, no larger four-pair acceptance run was launched.  Lower retired
instructions are useful attribution, but they do not override the wall gate or
justify a mutable layout-cast abstraction whose visible profile ceiling was
already only about 1.071x.

## Status

`[DENIED]` and reverted.  Do not retry generator/view removal alone.  The next
precise-stackmap proposal must attack the measured 61.0% planner GC/refcount
traffic (or a larger structural owner), preserve byte-identical assembly, and
meet the same-source wall/RSS gate before any full stage2 rebuild.

## Update No.48 — exact dense-shard GC attribution rejects two more local shapes

The retained GC0 baseline pcc1 replayed the same frozen, most stackmap-dense
shard under the existing `scripts/pcc_flamegraph.py cpu` tool.  This was a
direct self-backend emit worker with frontend and object caches disabled; it
did not rebuild pcc1 or modify the shard:

```
pcc1
  aaeffa06d1a251f622aa057928644e0f41cd863cc6d2de3755a9028ef46eb4e5
input self_backend_split_1_shard_17.ll
  0cfa1b864b3b833683894ed35fb4f5e8024e9e915fd6281c24a712e0e165e498
folded samples
  build/stage2-gc-tax-profile-v1/dense-shard.folded
assembly result
  b74451d772536be79257d38191a18ea183b98b5cb0d8abf50e55052f1aa00f31
```

The new 20-second capture contains 16,595 samples.  Inclusive attribution is
3,717 samples (22.40%) below `build_stack_map_plans` and 3,643 (21.95%) below
`build_function_stack_map_plan`.  The hottest exact leaves within the latter
are:

```
pcc_gc_managed_pointer_find_slot       331   2.00% of all samples
pcc_py_gc_minor_graph_lock/unlock      301   1.81%
object_graph_lock/unlock               132   0.80%
py_incref + py_decref                  151   0.91%
pcc_gc_store_root                       81   0.49%
pcc_gc_load_ptr                         77   0.46%
```

This makes the old task-title hypothesis quantitatively false for this owner:
even deleting every planner-attributed `find_slot` sample would have an Amdahl
ceiling of only **1.02035x**, far below the pre-registered 1.08x gate.  The
cost is distributed across allocation, roots/frames, retain/release, index
queries and graph locks.  No managed-index-only source edit is justified by
this profile.

The existing flamegraph tool was also tried in `peak` and `heap` modes against
the same binary launched with `MallocStackLogging=1`.  Current macOS reported
lite stack logging with no high-water allocation history, and the
freestanding runtime's raw VM allocator produced no usable allocation stacks.
Those attempts therefore provide no allocation-type or live-set evidence.
The tool was not changed: CPU attribution remains valid, while heap ownership
must be obtained from runtime counters or a phase-aligned process-tree probe.

A second discriminator tested whether `PlannedSafepoint`-shaped object
construction itself could explain the planner tax.  One native binary selected
between an eight-field ordinary class and an eight-item tuple, constructed and
consumed 2,000,000 records, and ran balanced class/tuple pairs under GC0.  The
tuple lane measured 5.02/4.99 seconds versus 5.42/5.46 seconds for the class
lane: paired-median speedup **1.0869x**, CPU **1.0874x**, and max RSS
1.130 GB versus 1.310 GB.  This is a valid representation micro-result but not
a stage candidate.  Its wall delta is about 217.5 ns per record; the real
dense shard contains only 37,545 stack-map records, so even assigning the full
microbenchmark delta to production predicts only about **0.0082 seconds** per
shard.  The 36--38 second emit is instead dominated by repeated dataflow
set/dict/root operations.  Converting the record class to a tuple is therefore
`[DENIED]` for stage2 and was not implemented in production source.

## Status

`[DENIED]` for both a `find_slot`-only edit and a safepoint-record
class-to-tuple edit.  The next emit proposal must remove a whole measured
object-lifecycle/dataflow group with at least an 8% end-to-end sample path, or
the investigation must move to the independently measured 171.186-second
frontend/coordinator owner.  Do not write another planner-local patch from a
single hot symbol.

## Update No.49 — a real frontend worker profile localizes the next bounded owner

The retained, accepted GC0 pcc1 replayed the original stage2 singleton worker
for module index 81, `pcc.py_frontend.codegen.class_gen`.  The V4 manifest was
copied byte-for-byte and only its result and IR output paths were redirected to
`build/stage2-frontend-worker-profile-v1`; the original AST, exports, sources,
module order, sibling initializers, no-libpython mode and scaffold mode stayed
frozen.  The existing repository performance lock covered the worker and the
existing `scripts/pcc_flamegraph.py cpu` capture.

Frozen identities and result:

```
pcc1                              aaeffa06d1a251f622aa057928644e0f41cd863cc6d2de3755a9028ef46eb4e5
original worker manifest          7fa94593c0550f2ab316360e9b00528564cd36dd2113e9ff55a35d08fca95cd6
native exports                    d6a5902b6fe741cd6e80bf2312871845703932e22c9e11a569a7c2f1e1573912
module_81 AST                     02c90f74d88d452ada1e5f137ef1dfb88ad63cba2dcb0d49bacaea7800b1f4a5
replayed/original module_81 IR    19f1c3b6d0278941f30e35c9ae7ea67a21b301b3e85c7018ae0b37ffb10030ea
folded profile                    c48b17388e1d2d6c555535b67b411af3d25f4d07b2932cb677cece0b6ab16616
```

The worker returned zero, stdout was empty, and the 10,888,793-byte IR was
byte-identical to the retained stage2 artifact.  Diagnostic worker timing was:

```
wall       27.73 s
user+sys   26.82 s
AST read    0.618 s
infer       1.089 s
codegen    24.601 s
max RSS     2,519,433,216 B
footprint   2,473,674,960 B
```

The 20-second flamegraph contains 10,785 samples.  Inclusive paths are
`emit_stmt` 61.02%, `emit_expr` 39.25%, `emit_call` 23.66%, llvm-capi
`IRBuilder_call*` 18.24%, and module rendering 4.03%.  GC/refcount runtime
leaves together account for 6,421 samples (59.54%), but are distributed among
many callers; this does not make `IRBuilder.call` alone the owner.

One bounded caller is now measured precisely:

```
compute_free_names.__nested_walk   1,653 samples   15.33%
GC/ref leaves called directly
  from that walk                   1,051 samples    9.75%
```

The preserved pcc1-generated walker contains 81 frame-enter sites: 33 plain
common-entry registrations and 48 path-specific LIFO registrations.  The
latter belong largely to rare comprehension/lambda branches, but their locals
also enlarge the common function's static root set.  The next candidate is
therefore limited to a source-level control-flow split that moves those rare
branches into cold helpers and reduces the common walker from the 33 plain
registrations; it must not change closure, comprehension, nonlocal, global,
generator or lambda semantics.  This is a hypothesis, not yet an accepted
optimization.  A sampled run is diagnostic only; acceptance requires
unsampled matched A/B runs, byte-identical IR, at least 1.08x worker wall
speedup, CPU/instructions in the same direction and no RSS regression above
2%.  If the split adds mutual-closure ownership or misses that gate, it is
`[DENIED]` without a stage rebuild.

## Update No.50 — the `compute_free_names` common/cold split is `[DENIED]`

The proposed split mechanically promoted the stateless helper cluster and
moved comprehension/lambda branches into a cold same-module walker.  It did
not change AST serialization, inference, code-generation algorithms, runtime
barrier implementation/semantics, cache-key scheme, worker scheduling, or
output format.  The generated frame-registration count and compiler identity
were the measured variables.  The compiled common walker dropped from 33 to
14 plain frame registrations, strict contextual fallback remained zero, and
the focused closure/comprehension/lambda/native-shape gates passed 16/16 in
42.22 seconds.

Two immutable source snapshots produced these GC0 self/no-libpython compilers
against the same runtime archive:

```text
candidate hoist file       0be77e63e49dc181906cf90cba9b9f2648b214693e44f710979614e1bc2f7f84
baseline hoist file        4fc06cd534a109ffd0a3b7a499b7ae264a40fa803af3d65879dcf4b7bdac8e8c
candidate source manifest  51a722f1045568f38e93284c3fb5d84d4f1a69e173a827e0b294a0a7b03787aa
baseline source manifest   2078d2105c450d0bf85ccc592c223bbea99872d541f18888476b61cf81cb813e
candidate pcc1             0da3fad8a480fcf3fd719715cbb3bfe2bf13db339536b20ff14a797c6b31ecf1
baseline pcc1              b2ba3969609dd0ba2b25b5c9d99cc480b606f451af57d01517001d0afda29d47
runtime archive            b42890eeca1e1387c7282be0297a9f7daadb1042c0efb65d533cf8b94375b3d0
worker manifest            7fa94593c0550f2ab316360e9b00528564cd36dd2113e9ff55a35d08fca95cd6
expected IR                19f1c3b6d0278941f30e35c9ae7ea67a21b301b3e85c7018ae0b37ffb10030ea
```

The unsampled experiment used four balanced discarded warmups followed by
four matched pairs ordered `CB/BC/BC/CB`.  Every process returned zero with
empty stdout/stderr and reproduced the exact 10,888,793-byte IR:

```text
pair                     1          2          3          4
baseline wall        29.36 s    29.30 s    29.13 s    29.05 s
candidate wall       28.66 s    28.80 s    29.11 s    29.20 s
wall speedup          1.0244     1.0174     1.0007     0.9949

arm-median wall speedup              1.008979x
paired-median wall speedup           1.009024x
paired-median CPU ratio C/B          0.989271
paired-median instruction ratio C/B  0.977634
paired-median max-RSS ratio C/B      0.999984
paired-median footprint ratio C/B    0.999997
```

The approximately 2.24% instruction reduction is real attribution, but it did
not translate into the pre-registered 1.08x wall improvement and the fourth
pair was slightly slower.  It therefore cannot justify the extra helper/call
shape.  The candidate was removed by a forward patch before any candidate
full-stage rebuild; the malformed empty synthetic-comprehension semantic
regression remains independently useful.

## Status

`[DENIED]` and removed.  Do not retry this exact helper-hoist or common/cold
mutual-call shape without a new phase-matched profile that changes its measured
ceiling.  The independently routed stage2 coordinator live-set task remains
the highest priority; this singleton-worker result neither proves nor changes
its 9.834 GB baseline.

## Update No.51 — medium emit concurrency reduction is `[DENIED]`

The current-source phase-aligned diagnostic produced a valid self/no-libpython
pcc2 and localized its largest synchronized process-tree RSS to the native
emit lanes: 13.439 GB in medium emit and 12.057 GB in oversized emit.  The
coordinator itself peaked at 6.755 GB in the export/AST/vthread lifetime and
fell below 0.2 GB after that helper returned.  This changed the next finite
question from speculative coordinator streaming to whether the already-fresh
batch1 medium workers could run at lower concurrency without losing stage2
throughput.

A frozen replay used 32 unique medium IR items (40,553,830 input bytes) from
the retained stage2 artifacts.  Both arms used current-source pcc1
`b2ba3969609d...`, runtime archive `b42890eeca1e...`, one fresh process per
item, caches and Python IR passes off, with the same recorded selected
environment.  The experiment changed the scheduler cap from the
production-equivalent eight workers to four.  Four balanced four-item warmups
were followed by the first full pair;
the harness-recorded early-stop rule stopped if wall exceeded 1.10x or RSS
failed to fall below 0.80x.  The task-board retention boundary required wall
at most 1.03x, RSS at most 0.60x and at most 8 GB.  The raw manifest does not
preserve complete per-arm argv, harness source identity or every ambient
tool/environment receipt, so this is retained as negative focused evidence,
not a claim-grade single-variable acceptance run.

```text
metric                         four workers          eight workers      C / B
wall                               86.854 s              56.836 s       1.52814
user + system                      323.61 s              364.27 s       0.88838
instructions                       3.55584e12            3.56921e12     0.99625
cycles                             1.05499e12            1.13961e12     0.92575
synchronized process-tree RSS      10.024 GB             13.601 GB      0.73698
```

An independent post-run rehash found all 32 assembly files byte-identical
between arms and matching the retained oracle.  The candidate saved aggregate
CPU/cycles and about 26.3% RSS,
but it still exceeded 8 GB and made the lane about 52.8% slower.  The early
stop is therefore evidence discipline, not missing pairs.  The durable local
manifest is `build/stage2-medium-concurrency-ab-v2/manifest.json`, SHA256
`65cb49d6a1778e5bbae61f23cfa4a8867ba85a39b63410bd9cdf0b300874a14b`.

## Status

`[DENIED]`; production remains at eight fresh batch1 workers and no source
change was made for this candidate.  Do not retry concurrency or the already
retained batch4-to-batch1 lifetime change.  The next emit experiment is
diagnostic: profile one complete production-shaped batch1 medium worker with
the existing CPU flamegraph and synchronized RSS, then select at most one
parse/precise-stackmap/render object-lifecycle owner whose measured end-to-end
ceiling is at least 1.08 before writing code.

## Update No.52 — emit-text lifecycle DENIED from the frozen capture; IR sidecar wire pre-registered

No source change and no new sampling. Two evidence steps, then one
pre-registration.

### Step 1 — the emit-text hypothesis is DENIED by leaf mining

The direction under test was: per-instruction `list[str]`/concat/temporary
text churn as one wholesale >=8% eliminable lifecycle. Mined the existing
frozen `build/stage2-medium-worker-profile-v1/complete-v2.folded` (16,032
samples). Leaf categories per inclusive scope:

```text
scope            samples  gc/refcount   text/memops
whole worker      16,032  10,618 (66.2%)   964 (6.0%)
function emit      4,602  ~67%             ~6%
instruction emit   2,644  1,812 (68.5%)    148 (5.6%)
parse module       3,309  2,138 (64.6%)    228 (6.9%)
prepare module     6,312  find_slot 740, graph_lock 396, roots 340,
                          incref/decref 295, pin/unpin 195 — no
                          algorithmic leaf in the top 20
```

No list/str/join/format leaf appears in the worker top 20; the top leaves are
`_pcc_gc_managed_pointer_find_slot` 1,818 (11.3% of the worker),
`_pcc_py_gc_minor_graph_lock` 940 (5.9%), store_root/load_ptr, incref/decref,
pin/unpin. The text-building shape exists in source
(`_emit_function` per-instruction `list[str]`, `lines.extend` at
`self_backend_aarch64_darwin.py:266`) but is not where time goes. VERDICT:
DENIED — text restructuring cannot wholesale-remove the tax; per the
pre-registered rule no emit-text candidate was written.

### Step 2 — parse-local shapes are pre-denied by measurement, without code

Host probes on the frozen module98 IR (sha `47289e1d0517d365...`, 1,831,588
bytes, 59 functions):

```text
parse wall (host)                 0.405 s
regex calls during parse          166,083   (166,022 match)
decode_value_token calls          35,887    distinct results 6,997  (5.13x dup)
```

Caller attribution of `find_slot` in the worker: the regex engine is the #1
caller (`_py_re_engine_truth_flags_from` 279 + `fullmatch_flags` 46), ahead of
the stack-map planner (134). Two parse-local candidates were sized from the
leaves and rejected BEFORE implementation:

* replacing the instruction-shape regex chain with str-method dispatch:
  direct re-engine leaves ~90-130 samples plus induced Match-object GC traffic
  ~200-400 samples => ~1.02-1.03x worker ceiling. Below the 1.08 bar.
* interning `decode_value_token` results (28,890 duplicate allocations
  removed per module98): parse GC tax attributable to operand strings is a
  fraction of parse's 2,138 gc samples => ~1.01-1.02x parse-local, plus an
  unmeasured downstream equality effect. Below the bar alone.

This is the same lesson as No.33/No.37/No.47/No.48: single-site removals
inside one 20.64%-inclusive phase cannot clear 1.08. Only removing the whole
scan-and-rebuild group does.

### Step 3 — pre-registered proposal: self-backend IR sidecar handoff (No.52)

One proposal. The frontend worker already holds structured IR and renders it
to `.ll` text; the emit worker re-parses that text through the 3,185-line
regex parser to rebuild an equivalent structure set. Add a sidecar wire so the
emit worker consumes pre-decoded records instead:

* producer: at frontend IR finalization, when `--backend self`, walk the
  module once and emit a sidecar next to the `.ll` — one record per
  instruction with fields already decoded (opcode kind, SSA/global names,
  canonical type text), plus block/function boundaries and the module-level
  entities the parser produces.
* consumer: `self_backend_parse` gains a sidecar entry point that builds
  `ParsedModule` directly (split on a control separator per record, memoized
  canonical type text -> `TypeDesc`). No regex, no shape chain, no
  `raw_lines`, no duplicate operand strings by construction (the encoder
  emits each distinct operand once per record that needs it; the decoder does
  not re-slice text).
* the `.ll` text stays canonical and untouched: object-cache key, IR
  verifier, `--emit-llvm`, differential tooling, and the fallback. The worker
  uses the sidecar only when it exists and its recorded `.ll` SHA256 matches
  the `.ll` beside it; any mismatch, decode error, or absent sidecar falls
  back to today's `parse_self_backend_module` unchanged.
* closure: the new producer/consumer module must pass the 30-second
  no-libpython closure check before any longer build; fallback-ratchet
  baseline must be unchanged.

Expected size, stated honestly: parse is 20.64% of the worker inclusive;
scanning (regex chain, line slicing, shape chain) is ~55-65% of parse under
the pcc1 cost model, so the realistic worker win is ~1.10-1.13x and the
absolute ceiling is 1.26x. This is above the bar but thin, and it buys a
secondary RSS reduction (no per-module text materialization or duplicate
operand strings in workers). It does not by itself close the stage2 gap; it
is the largest single measured group removable from the emit worker.

### Pre-registered gates and rejection line

1. Differential correctness first, on the host: for every retained stage2
   module IR available locally (minimum 20 modules including module98 and one
   oversized shard), `decode(sidecar) == parse_self_backend_module(text)` on
   the full `ParsedModule` value equality relevant to the backend (functions,
   blocks, instruction kind/data tuples, types). Any mismatch => DENY.
2. Focused gates: existing parse/token-classifier tests, arena tests,
   precise-stackmap ABI nodes from the No.46 gate list, plus new
   sidecar-roundtrip and fallback-on-mismatch tests.
3. Candidate pcc1 closure check, then frozen module98 unsampled replay:
   paired median wall speedup >=1.08 over at least three balanced pairs,
   user+sys and instructions improving, RSS/footprint <=1.02x, rc0 and
   byte-identical assembly versus the recorded oracle.
4. DENY and remove the sidecar path entirely (producer, consumer, tests)
   before any complete stage2 rebuild if any gate fails. No stacking of a
   second change into the same measurement.

Status of this update: pre-registration recorded before implementation; no
compiler source change has been made.

## Update No.53 — sidecar producer is infeasible: frontend instructions are text-only records [DENIED BY ANALYSIS]

Implementation due diligence on No.52's producer found a structural blocker
before any code was written.

`pcc/llvm_capi/ir.py` stores every emitted instruction as an
`InstructionRecord(text, opname)` (`ir.py:975-1015`); `Block._instrs` is a list
of these text records (`ir.py:1090`) and `Block.render()` joins
`_text_lines` (`ir.py:1143-1265`). The Python frontend never holds structured
per-instruction operands — the rendered text line is the representation, and
the emit worker's parse is the only structure builder in the pipeline. This
also confirms No.12's "the IR builder returns opaque handles with no operand
access" from the value side.

Consequences, measured against the pcc1 cost model:

* A producer that walks "the module" for pre-decoded fields has nothing to
  walk. Building structured records at emission would add a tuple + field
  bookkeeping per emitted instruction to the frontend's hottest path
  (IRBuilder emission is ~24-30% inclusive of the frontend worker, No.49) —
  under an allocation-dominated cost model the added frontend allocations
  likely exceed the worker parse time saved (parse is 20.64% of the emit
  worker, and only its scanning half is removable).
* Re-parenting the parse into the frontend worker moves the identical cost
  between two pcc1 processes and saves nothing.
* The `.ll` text must stay canonical regardless (cache key, verifier,
  `--emit-llvm`, oracles).

VERDICT: No.52's sidecar proposal is DENIED BY ANALYSIS and was not
implemented. No compiler source changed. The emit-worker local route is now
exhausted at every measured level: text lifecycle (No.52 step 1), parse-local
regex/interning (No.52 step 2), planner/stackmap/cursor/concurrency shapes
(No.47/48/50/51), and the wire (this update). The remaining measured owners
are the uniform ~5.5x pcc1 execution penalty on both compute phases (No.26/
No.46) and the 66.2% GC/refcount leaf tax that is distributed across every
phase — both of which belong to the structural tracks (backend No.9,
value-lane No.7/No.8, or a pre-registered GC0 runtime-tax row), not to
another emit-local candidate.

Side note recorded for hygiene: `Block.render()` carries
`_debug_ir_render_enabled()` instrumentation with hardcoded module/block
names (`user_prog_Parser_make`, `user_pcc_parse_py_lex__is_digit_code`,
`ir.py:1146-1264`). It is gated off by default but is session-shaped debug
code inside a hot render path; it should be removed or promoted to a
deliberate tested feature in its own change.

## Update No.54 — the provenance predicate was five calls deep; fusing it is [CONFIRMED], a cached reciprocal is [DENIED]

No.53 closed the emit-local route and named the remaining owners as the uniform
pcc1 execution penalty and the distributed GC/refcount leaf tax. This update
attacks that tax where it is measurable without a stage build, and it revises a
baseline this file has quoted since the granule design landed.

### The baseline was stale, and the replacement cost more than the original

`ARCH-P0-PROVENANCE-GRANULE-MAP` quotes "index machinery 21.26% (frozen
module98 worker) / 18.98% (live 600-fn compile)". Those predate the S2
activation that has since landed in the strict port, so they describe a runtime
that no longer exists.

Re-measured on current source with a natively compiled heavy-object workload
(4,000-node lists rebuilt 40 times, six method-dispatch walks each, 4,000 dict
inserts of fresh instances per round; 5.9 s wall, 4 s `sample`, 3,195 self
samples, aggregated with `scripts/pcc_sample_aggregate.py`):

```text
gc_index category                                1.8%   (was the top leaf)
pcc_gc_managed_pointer_find_slot                 absent from the top symbols
granule_object_slot 305 + granule_find_slot 195
  + granule_span 194 + granule_stride_count 100
  + granule_is_object_start 73                 = 867/3195 = 27.1%
```

S2 did remove the per-object exact-set traffic. It also made the provenance
question *more* expensive than the hash set it replaced (27.1% vs 21.26%).
That is a sufficient explanation for the stage2 ratio not moving, and it means
no S3 bar could have been earned by this path as it stood.

### `[CONFIRMED]` The cost was call depth, not the data structure

One provenance question ran five pcc-compiled calls:
`pcc_gc_pointer_is_managed` -> `pcc_gc_granule_is_object_start` ->
`_granule_object_slot` -> `pcc_gc_granule_span` -> `_granule_hash` /
`_granule_find_slot`. Under this compiler's cost model each call pays frame and
root bookkeeping, and the leaf work is a shift, a probe and a few compares.

Two changes, in order, each measured on the same workload with two discarded
warmups per arm and alternating pairs:

```text
1. stop recomputing the carve count per query (the span descriptor is
   immortal and already caches it; registration already rejects a stride
   whose count is 0)
       10 pairs, 10 favouring candidate, paired-median C/B 0.9762  -> 1.0243x

2. fuse the whole chain into pcc_gc_granule_is_object_start as straight-line
   code, leaving the decomposed helpers exported and unchanged for their own
   callers and tests
       10 pairs, 10 favouring candidate, paired-median C/B 0.8937  -> 1.1189x

   final accepted state versus the pre-fusion arm
       12 pairs, 12 favouring candidate, paired-median C/B 0.8855  -> 1.1293x
```

Cumulative on this workload: 0.9762 x 0.8855 = 0.8645, i.e. **1.157x**. After
the fusion the predicate is a single leaf at 458/3224 = 14.2%, and the whole
provenance path is 16.1% against the 27.1% it started at.

This is the first change in this investigation to clear its own bar by
removing a *group* rather than a site, and it did so by deleting call frames,
not by improving an algorithm. Recorded as the reusable rule: on this runtime,
a decomposed helper chain on a per-operation path is itself the cost.

### `[DENIED]` Caching a multiply-by-reciprocal in the span descriptor

The fused predicate still divides once (`carve_offset // stride`). A
`ceil(2**32/stride)` reciprocal was cached in the descriptor (widened 32 -> 40
bytes) and the division replaced by one multiply and one shift. The identity is
not general, so it was first verified exhaustively over the only domain it is
used on -- all 11 strides x every carve offset in `[0, 65488)`, largest product
42 bits -- and pinned by a focused test before being measured.

It does not pay:

```text
first 10 pairs    7/10 favouring candidate   paired-median 0.9857 -> 1.0145x
next  14 pairs    5/14 favouring candidate   paired-median 1.0145 -> 0.9857x
combined          12/24                      no effect
```

The extra descriptor load plus the `magic <= 0` guard costs what the division
saves, and the division is not on the critical dependency path. Removed by a
forward patch, with a comment at the site so it is not retried; the descriptor
is back to 32 bytes and the focused reciprocal test was removed with it.

### Gates

```text
granule map + pointer provenance + layout contract      13 passed
no-libpython closure emit of freestanding_allocator.py  exit 0
five-backend finalizer/resurrection/weakref/trashcan    44 passed x 5
tests/python/test_bootstrap_gate_baseline.py            2 passed
```

One run of the granule gate reported
`test_granule_single_writer_races_real_pthread_readers_through_grow` failing;
it passes in isolation and passed on a warm re-run of the same three files. It
was archive-rebuild contention inside the batch, not a defect.

### Status

Fusion `[CONFIRMED]` and kept. Reciprocal `[DENIED]` and removed. The next
measured owner inside the predicate is the span lookup itself -- a hash plus
open-addressing probe where the design's own references (ZGC `ZPageTable`, Go
`spanOf`) do one shift and one load -- tracked as
`ARCH-P1-GRANULE-SPAN-LOOKUP-RADIX`. Everything here is a runtime-workload
measurement: no stage1, stage2, module98, fixed-point or five-GC claim is made,
and the emit worker's own share must be re-measured on a current-source pcc1
before any stage ratio is quoted.

## Update No.55 — the relocation read barrier was paying for provenance under GC0 `[CONFIRMED]`

Update No.54 fused the objecthood predicate and named the span lookup as the
next owner inside it. Re-profiling the same class of workload on current source
put a different symbol on top: the predicate's biggest *caller* was not a
container operation but `pcc_gc_note_relocation_read`, the relocation read
barrier, which was asking the provenance question on every pointer read under
every backend.

### The shape

```python
    if pcc_gc_object_is_known_no_lock(obj) != 0:      # full provenance lookup
        if (load_i32(obj, 12) & 2048) == 0:
            return obj
    _graph_lock()                                     # taken for every pointer
    resolved = _note_relocation_read_unlocked(obj)     # that is NOT a known object
    _graph_unlock()
```

Under backends 0/1/2 nothing can have moved, so every branch of that returned
`obj`. The asymmetry was already visible one file over: `pcc_gc_load_ptr`
(`py_obj.py:383`) gates on `pcc_gc_read_barrier_enabled` and the selected
backend before doing any resolution work. The barrier it guards did not. This
is the missing half of that pair, not a new mechanism.

### Why the early exit is exact

Three existing invariants, now pinned by a regression:

1. `pcc_gc_install_forwarding_unlocked` returns `-1` before doing anything
   unless the selected backend is 3 or 4.
2. `pcc_gc_set_backend` refuses a backend *change* while `_forwarding_head()`
   is non-null or `pcc_gc_forwarding_population != 0`, so "not 3/4" implies the
   forwarding list and index are both empty and `pcc_gc_forwarding_find` cannot
   match.
3. Flag `2048` is set only on the two install paths, so it cannot be set under
   a non-moving backend, and its only readers are the GC3 promotion and GC4
   selector paths.

A pre-config read of `0` is safe for the same reason as (1). Backends 3/4 reach
byte-identical code; the only skipped action is clearing a stale `2048` hint
under a backend where nothing reads it.

### Measured (backend 0, `benchmarks/python/granule_heavy_object.py`)

```text
pair  base   cand   C/B          before -> after, self samples
   1  6.064  4.297 0.7087          note_relocation_read      114 -> gone
   2  5.768  4.376 0.7586          ..._read_unlocked          77 -> gone
   3  5.772  4.315 0.7477          object_is_known_no_lock    96 -> gone
   4  5.789  4.309 0.7443          object_index_find         101 -> gone
   5  5.817  4.300 0.7392          index_py_find              68 -> gone
   6  5.913  4.281 0.7240          forwarding_index_find      54 -> gone
   7  5.887  4.415 0.7500          minor_graph_lock/unlock 68+68 -> gone
   8  5.776  4.289 0.7425          gc_index category        4.8% -> absent

base median 5.803   cand median 4.304
paired-median C/B 0.7434  =>  1.3452x    8/8 pairs favour the candidate
```

An earlier arm carrying a redundant `pcc_gc_config_initialized` load measured
1.3102x over 10 pairs (10/10). That condition was removed: it is redundant by
invariant (1), it added a second global load to the hot path, and it would have
widened the module's declared global-import closure. The final gate adds no new
global import -- `pcc_gc_backend_selected` was already in `RAW_GLOBAL_IMPORTS`.

### Gates

```text
test_freestanding_gc_forwarding_identity.py                  6 passed
test_freestanding_gc_relocation_payload.py
  + test_freestanding_gc_forwarding_retirement.py           24 passed
test_gc_granule_map.py + test_runtime_pointer_provenance.py
  + test_runtime_layout_contract.py                         13 passed
test_bootstrap_gate_baseline.py + both fallback baselines    42 passed
five-backend finalizer/resurrection/weakref/trashcan        44 passed x 5
```

Backend 1 passes all 44 here; the 2026-08-23 resurrection reclaim-count failure
is green, consistent with the backend-1 phantom-cycle fix landed since.

### Status

`[CONFIRMED]` and kept. Tracked as
`PERF-P1-RELOCATION-READ-BARRIER-NONMOVING-GATE` with evidence
`docs/goal/evidence/2026-08-24-relocation-read-barrier-non-moving-gate.md`.

This is a runtime-workload measurement under backend 0 on one machine. No
stage1, stage2, module98, fixed-point or five-GC claim follows from it, and the
barrier's share of a real pcc1 emit worker has not been measured. Note for
whoever measures next: the read barrier is a *caller* of the provenance
predicate, so its removal changes the denominator the
`ARCH-P1-GRANULE-SPAN-LOOKUP-RADIX` baseline was recorded against. Re-profile
before quoting that row's 12.2%.

## Update No.56 — compiled mem2reg was quadratic in alloca candidates `[CONFIRMED]`, 1.71x pcc1 frontend

Update No.55 gated the read barrier. With that in, a current-source pcc1 was
built and profiled on a real 5,924-line module frontend
(`pcc/py_frontend/type_infer.py`, `--emit-llvm`, GC0, 25.65s). The flat profile
named `pcc_gc_granule_is_object_start` at **18.2%**, four times the next
symbol -- and Update No.11's lesson plus `pcc_flamegraph.py`'s own docstring
both say not to optimize that leaf. Caller attribution instead:

```text
callers of pcc_gc_granule_is_object_start   (2458 attributed samples)
   790  <- compiled_default_passes._rewrite_functions
   503  <- compiled_default_passes._mem2reg_function
    94  <- llvm_capi.ir._irbuilder_call_from_args_list
    43  <- pcc_gc_free_object_memory
```

**53% of the provenance-question cost came from two functions in one file.**
The intermediate `pcc_gc_pointer_is_managed` / `_ptr_can_have_header` frames
were elided by the tail-call pass, which is why the attributed caller is the
compiled pcc source function -- the hazard already recorded in this file.

### The defect

`_mem2reg_function`'s classification loop visited every alloca candidate for
every line, asking `_contains_ssa_name` each time. On the same real module:

```text
262 functions, 252,453 lines
sum(lines x candidates) = 38,528,560   vs sum(lines) = 252,453   ->  152.6x
worst function: 41,519 lines x 407 candidates = 16,898,233 inner iterations
```

38.5M inner iterations for 252k lines, each a dict lookup plus a substring
scan -- and under pcc1 each of those object touches is a provenance-checked
barrier. The leaf was not the problem; the caller asked 152x too often.

Note how this interacts with the negative result recorded above under "Two
smaller changes in the same area": halving the *number of traversals* moved
nothing, because "the traversal was never the cost -- the GC barriers inside it
were". That is exactly right, and it is why this change works where that one
did not: cost is proportional to **objects touched**, and this removes 152 of
every 153 touches rather than one of two traversals.

### The fix and why it is exact

Tokenize each line's maximal `%name` tokens and look up only those candidates.
`_ssa_names_in` returns precisely the set for which
`_contains_ssa_name(line, name)` is True -- that helper accepts `%name` only
when the following character is outside `_SSA_NAME_CHARS`, i.e. when the token
is maximal, so `%s1` does not answer for `s` and `%s1`/`%s10`/`%s100` stay
distinct. Candidates never interact, so token order versus insertion order
cannot change the result. `name not in candidates` + subscript rather than
`dict.get`, which mis-lowers in self-compiled frontend code.

### Measured (single-variable pcc1 A/B, byte-identical IR)

```text
  1  base   25.86  cand   14.70  C/B 0.5683
  2  base   25.65  cand   14.67  C/B 0.5717
  3  base   25.38  cand   15.36  C/B 0.6054
  4  base   25.76  cand   15.84  C/B 0.6149
  5  base   25.30  cand   14.78  C/B 0.5844

base median 25.65s   cand median 14.78s
paired-median 0.5844  =>  1.7113x    5/5 pairs
emitted IR sha256 across all 10 compiles: one value, byte-identical
```

The host-side speedup of the pass alone is 1.82x (1.30s -> 0.72s over the same
262 functions). The 1.71x is a *pcc1* number for the whole frontend, and the
gap between the two is the point: on the host `str.find` and dict lookup are
C-fast, so the host measurement would have understated this by half.

Equivalence: byte-equal to the retained every-candidate oracle on 464 real
functions across three differently shaped modules (61 + 141 + 262, zero
mismatches). Two regressions added to
`tests/python/test_compiled_default_pass_tier.py`, both written before the
change and passing against the unchanged source.

Gates: bootstrap gate baseline + both fallback baselines + pass-tier tests,
57 passed / 2 deselected.

### Status and next owner

`[CONFIRMED]`, kept, tracked as `PERF-P1-MEM2REG-CANDIDATE-SCAN-QUADRATIC`.

Re-profiling the candidate on the same input:

```text
  13.7%  pcc_gc_granule_is_object_start   (was 18.2%)
   5.2%  py_class__strs_eq                (was 3.2%)
   3.8%  pcc_gc_store_root
   3.2%  py_class__class_lookup_in_mro    (was 1.9%)
   3.2%  pcc_gc_load_ptr
   2.9%  strlen                           (was 1.6%)
   2.1%  py_capi_type_runtime__is_type_object
```

MRO method lookup -- `strs_eq` + `class_lookup_in_mro` + `strlen` +
`is_type_object` -- is now **13.4%**, comparing method-name strings byte by
byte with a `strlen` per comparison. That is `S-P0-MRO-METHOD-CACHE` and is the
next target.

`_sroa_function` **does** carry the same shape, and worse -- a triple loop
`lines x candidates x candidate["geps"]`, each level asking
`_contains_ssa_name`. It is nevertheless **not worth changing**: SROA
candidates are allocas of literal structs with 2-4 fields, and a real emitted
module has **zero** of them (0 candidates in 261,214 lines of `type_infer.py`
IR), so `_sroa_function` returns at its `if not candidates` guard and costs
nothing. Recorded here so the next reader does not pay for the same
transformation twice. If a future frontend change starts emitting literal
struct allocas, this becomes the same defect and the same fix applies.

One stale hazard cleared while in this area: `dict.get()` on a **module-level**
dict no longer mis-lowers under pcc1. The 2026-06-25 failure
(`codegen[<module>]: KeyError`) does not reproduce -- a current-source pcc1
returns `None` for a module-level miss and handles the 2-arg default, int and
string keys, `.get()` through `self.attr`, and mutation of a returned dict,
all identical to CPython. Module-level containers remain a distinct lowering
case (a module-level `set` still loses its DynType), so a local-dict probe does
not cover that class of question.

Frontend-only, one module, one machine, `--emit-llvm`: the native emit and link
phases (516.717s and 99.719s of the routed 875.10s stage2) are untouched and
unmeasured here, so no complete-stage ratio follows.

## Update No.57 — class-name compare in one pass `[CONFIRMED]` 1.0200x; first-byte prefilter `[DENIED]`

Update No.56 named MRO method lookup as the next owner: `strs_eq` 5.2% +
`class_lookup_in_mro` 3.2% + `strlen` 2.9% + `is_type_object` 2.1% = 13.4% on
the candidate pcc1's `type_infer.py` frontend profile. `S-P0-MRO-METHOD-CACHE`
already holds a 2026-08-20 `[DENIED]` for a 1024-entry direct-mapped location
cache (0.22% wall), so that shape was not retried. Two different candidates
were measured instead: make each comparison cheaper, and avoid the call.

### `[CONFIRMED]` — one pass instead of three

`_strs_eq` compares raw C names, so there is no cached length to read. It ran
`strlen(a)`, then `strlen(b)`, then a bounded byte loop — a matching name was
read about three times. The C mirror in `py_class.c` never did this; it calls
`strcmp`. This was a port-vs-C **cost** divergence, not a semantic one.

Replaced with the ordinary terminator-comparing single pass, resuming at index 2
because bytes 0 and 1 are already known equal and nonzero. Equivalence is
mechanical: unequal lengths are caught when one side reaches its NUL while the
other has not; equal-length strings are decided at the first differing byte or
at the shared terminator. Done-flag loop, not `break`, per the port subset.

```text
  1  base 15.01  cand 14.48  C/B 0.9646      base median 14.98s
  2  base 14.93  cand 14.69  C/B 0.9839      cand median 14.68s
  3  base 14.93  cand 14.61  C/B 0.9784
  4  base 14.95  cand 14.69  C/B 0.9824      paired-median 0.9804
  5  base 15.02  cand 14.79  C/B 0.9846      =>  1.0200x   6/6 pairs
  6  base 15.13  cand 14.68  C/B 0.9705      IR byte-identical (12 compiles)
```

After: `strs_eq` 5.2% -> 2.5%, and `strlen` left the top 18 entirely.

### `[DENIED]` — first-byte prefilter at the walk call sites

Hoist the wanted name's first byte out of both loops and reject a candidate
with a byte compare instead of a call into `_strs_eq`, in both
`_class_lookup_in_mro` and `_lookup_field_index`. The reasoning was sound under
this compiler's cost model (a call carries frame and root bookkeeping) and the
result was still worse:

```text
single-pass + prefilter (pcc1_cand3)   1.0136x   5/5
single-pass alone       (pcc1_cand4)   1.0200x   6/6
```

The prefilter cost about **0.6%**. Its inline load and compares per candidate
outweigh the avoided call, and `class_lookup_in_mro`'s own share barely moved
(3.2% -> 3.0%) because the work simply relocated into it. Part of the field
half's explanation: `_lookup_field_index` already sits behind the one-entry
`py_inst_field_cache_name0` cache, so most field lookups never reach the walk.

Removed by forward patch. **Do not retry a first-byte prefilter at these call
sites.**

### The profile/wall gap is the lesson here

13.4% of self samples were attributed to this family, and removing 5.8 points
of it bought 2.0% of wall. Where the profile and a paired measurement disagree,
the paired measurement is what counts — and the batch-vs-alone split is what
made the prefilter's cost visible at all. Had both landed together the result
would have read as "1.0136x, accept" and shipped a change that was making
things slower.

### Status

Single pass `[CONFIRMED]` and kept as
`PERF-P1-CLASS-NAME-COMPARE-SINGLE-PASS`; prefilter `[DENIED]` and removed.
**1.0200x does not clear `S-P0-MRO-METHOD-CACHE`'s pre-registered 3% floor**,
so nothing here is a closure or partial claim against that row.

Regression: `tests/python/test_class_name_compare_prefix_families.py` compiles
a DEFAULT-mode program (not `PCC_RUNTIME_CC=cc`, which links the C `strcmp`
sources and would exercise none of this) with prefix-family method names
(`a`/`ab`/`abc`/`abcd`, `p`/`pq`/`pr`, `foo1`/`foo2`) and prefix-family fields
(`x`/`xy`/`xyz`/`xyzw`, `q`/`qr`) across a three-deep shadowing MRO. Written
and passing before the change. Focused class-lookup/dataclass/dispatch gates:
21 passed.

Remaining in this family on the current profile: `class_lookup_in_mro` 3.0% and
`is_type_object` 1.5%, both unaddressed and both now small enough that the 3%
floor is the right gate to keep them behind.

## Update No.58 — the native-emit worker profiled; inlining a hot guard `[DENIED]`

Updates No.56 and No.57 were both measured with `--emit-llvm`, which returns
before the self backend. That was the frontend, not the phase that dominates
stage2. On the same module and pcc1:

```text
full compile to a binary     86.4s wall   212.9s user   259% CPU (parallel)
frontend only (--emit-llvm)  15.0s wall    14.2s user    97% CPU (serial)
```

Native emit + link is **~93% of a full compile's CPU**, consistent with the
routed split (frontend 171.186s, emit 516.717s, link 99.719s of 875.10s).

### Two facts that cost time to learn, recorded so they do not again

* `--python-library` together with `-o <file>` fails instantly with an empty
  `PCC-PY-COMPILE-001` / `exception_type=Exception` on **both** host pcc and
  pcc1. It is a flag-combination error, not a pcc1 regression. Drop
  `--python-library` to compile through the backend.
* Sampling the **coordinator** pid reports "83% outside the image". The emit
  workers are separate `pcc1` child processes and they are short-lived — a
  worker caught during a real stage2 had `etime 00:02`, so a 40s profile of it
  outlives the process. Find a busy child with `ps` and profile that pid;
  profiling the coordinator measures almost nothing.

### Emit worker profile (current source, GC0, 650 samples, one worker)

```text
   9.7%  pcc_gc_granule_is_object_start        2.3%  frame_roots_disabled_fast
   6.9%  py_capi_type_runtime__is_type_object  1.8%  note_frame_leave_lifo
   5.8%  pcc_gc_store_root                     1.2%  note_frame_leave
   3.5%  pcc_gc_load_ptr                       1.2%  frame_enter_lifo
   2.6%  pcc_gc_unpin   2.3% pcc_gc_pin        1.1%  frame_enter
   1.7%  managed_pointer_find_slot  1.7% pointer_is_managed_no_lock
```

Frame enter/leave bookkeeping ~7.6%; provenance ~15%. Same "66.2% GC/refcount
leaf tax" this file already named, now localized to the emit worker.

`is_type_object` at 6.9% is **not** a regression of Update No.11. That fix put
the O(1) index probes ahead of the 24-way `ptr_eq` chain inside
`_pointer_is_managed_no_lock`, and the ordering is still in place. 6.9% means
the emit worker asks provenance about genuinely *unmanaged* pointers often, and
every negative answer still walks the 24-way chain after all the O(1) probes
miss. Making that negative case cheap — e.g. a conservative address-range
prefilter over the 24 statics, which is a pure over-approximation and cannot
change an answer — is **untried** and is the largest unexplored item here.

### `[DENIED]` — inlining `pcc_gc_frame_roots_disabled_fast`

All four frame entry points open with `if pcc_gc_frame_roots_disabled_fast()
!= 0: return`, so compiled code pays a call out to a three-load predicate on
every frame enter and leave. The C mirror never paid it (`static`, inlined by
the C compiler), which made this look like the same port-vs-C cost divergence
`_strs_eq` turned out to be. Inlined the three loads at all four sites:

```text
frontend A/B,  6 pairs   1.0113x   4/6 favouring   (one contended 17.63s base)
frontend A/B, 10 pairs   1.0059x   7/10 favouring  ratios 0.9584 .. 1.0087
full compile,  2 pairs   wall 0.9855 / 1.0334
                         CPU  1.0259 / 1.0237   <-- candidate burns MORE CPU
```

The frontend numbers are noise. The full-compile **CPU** figures decide it — CPU
is the low-noise metric for a 259%-CPU parallel job — and both pairs agree the
candidate costs about **2.5% more**. Reverted by forward patch; output was
identical throughout.

### The pattern this establishes

Second denial of the same shape today, both reasoned from the same correct
premise (a call here carries frame and root bookkeeping):

```text
first-byte prefilter in _class_lookup_in_mro / _lookup_field_index   -0.6%
inlining pcc_gc_frame_roots_disabled_fast at 4 frame entry points    -2.5% CPU
```

What worked in the same session removed work rather than relocating it: mem2reg
stopped visiting 152 of every 153 candidate pairs (1.71x), `_strs_eq` stopped
walking each name three times (1.0200x), and the relocation read barrier
stopped asking a provenance question it could not need (1.345x on its
workload). **Hoisting a small predicate into a hot call site is not a reliable
win under this backend and has now measured negative twice.** Do not retry that
shape without new evidence.

## Update No.59 — where this round of work stopped, and why

Session close-out for Updates No.55-58. Nothing new was measured here; this
records the stopping decision so the next reader does not have to reconstruct
it.

### Accepted (three), all with single-variable pcc1 or workload A/Bs

```text
mem2reg candidate scan inverted        pcc1 frontend  1.7113x   5/5   No.56
_strs_eq single pass                   pcc1 frontend  1.0200x   6/6   No.57
relocation read barrier gated          workload       1.3452x   8/8   No.55
```

Frontend total: 25.65s -> 14.68s, **1.75x** (1.7113 x 1.0200 = 1.746,
consistent with the two independent A/Bs). Emitted IR byte-identical
throughout. Final-source mandatory gates: 64 passed, 2 deselected, exit 0.

### Denied (two), both the same shape

```text
first-byte prefilter at the MRO/field walks             -0.6%
inlining pcc_gc_frame_roots_disabled_fast at 4 sites    -2.5% CPU
```

The three accepted changes removed work. The two denied changes relocated work
to save a call. **That is the discriminator, and it held twice.**

### Why the round stopped rather than continuing

1. The remaining profile is the per-operation GC tax — provenance lookup, root
   store, pin/unpin, frame bookkeeping — which is what this file already named
   as the residual owner after "the emit-local route exhausted at every
   measured level". The emit-worker profile in No.58 confirmed that rather
   than overturning it.
2. Every remaining candidate has the shape that measured negative twice, or is
   the granule radix.
3. The one large structural attack left, `ARCH-P1-GRANULE-SPAN-LOOKUP-RADIX`
   (9.7% of the emit worker, 12.5% of the frontend), carries publication
   ordering, transactional slab registration, real-pthread race and GC0..4
   obligations. That is a multi-session slice, not a single measure/build
   cycle, and its own row says no module98 bar can be claimed without it.
4. The 23-way type-object scan's negative path was investigated and **not**
   attempted. The premise checks out — `nm` on a real pcc1 shows all 23 tokens
   contiguous from `0x1052ddb50` to `0x1052dff10`, 0x1a0 apart, a 9152-byte
   span, so a conservative address-range prefilter is a pure over-approximation
   that cannot change an answer. It was skipped because `global_addr` needs a
   literal name, so a min/max init cannot be a loop: ~70 lines of one-time
   setup for an uncertain 2-3%, in exactly the shape that had just been denied
   twice. Recorded so the next reader inherits the verified premise and the
   cost estimate rather than re-deriving both.

### The honest ceiling on what was achieved

The frontend is 171.186s of the routed 875.10s stage2; native emit is 516.717s
and link 99.719s, and **no change landed in either**. A 1.75x frontend is worth
roughly 73s of 875s if it transfers unchanged, about 8% — an arithmetic
projection, not a measurement. No stage1/stage2 pair, module98 A/B, fixed point
or five-GC matrix was run for any of this.

## Update No.60 — current cold/hot boundary and canonical-error layout quadratic `[CONFIRMED]`

Current frozen source `eab5407f64bce65451e9a2dc2216d8d48636f886`, pcc1
`0338405442cc6da75fe743f159e694b31423c3d0d3a40b32b58e44e925735986`,
GC0/self/no-libpython. One isolated empty-cache Stage2 and one same-source hot
replay produced byte-identical pcc2
`2d66c9647d02b1df31873ad1660f0f491020e148e62e5bf889b796136f750f04`:

```text
                         cold                 hot
wall                     1356.194 s           183.429 s
frontend IR cache        0 hit / 1 miss       1 hit / 0 miss
frontend actions         0 hit / 212 miss     skipped from bundle hit
self objects             0 hit / 463 miss     463 hit / 0 miss
native emit              897.916 s            0 worker processes
frontend codegen         195.522 s            1.327 s cache-load path
link driver              139.083 s            33.730 s
cache publish/retention    2.430 s             0.243 s
```

This denies the working hypothesis that the shared 89 GB cache's retention
work caused the 21-minute stage: an isolated 1.8 GB cache reproduces the cost,
and retention is 0.18% of cold wall. Cold native emit is the owner (66.2%).

A current production-shaped pcc1 emit of the largest single-function
`pcc.cli_bootstrap` shard (4,495,046 bytes, 89,448 lines, one function) took
47.27 s / 512.66B instructions / 4.60 GB peak footprint. Its 20-second native
flamegraph again shows distributed GC/refcount tax, but host cProfile exposes
one previously unmeasured algorithmic multiplier:

```text
plan_aarch64_canonical_error_fallthroughs  4.495 s of 30.044 s host emit
text_key_names_equal                       4,507,467 calls / 3.255 s
_dot_numeric_text_key_id                   4,859,089 calls / 3.086 s
```

The pass linearly searches the remaining block list for every canonical
post-call success target, then performs `pop`/`insert`; the shard has 8,705
blocks. This is O(edges x blocks) comparisons plus list shifts. It is distinct
from all denied cache/guard/stackmap-local shapes above: the proposal removes
the whole scan-and-shift group.

### Proposal No.60 `[CONFIRMED]`

Build a stable integer bucket from block text to original block index, maintain
the current order as `prev`/`next` integer arrays, and mark nodes processed as
the pass advances. An unprocessed target is exactly the old algorithm's
"found after current" condition; detach/insert-after is O(1), and one final
walk materializes `aarch64_block_layout`. Preserve explicit text equality for
inconsistent native hashes and preserve the exact edge list/order.

Pre-registered rejection line: focused differential/reference tests and
hash-skewed labels first; no-libpython closure next; then at least three
balanced unsampled pairs on the exact frozen shard, byte-identical assembly.
Require paired-median pcc1 wall speedup >=1.08, user+sys and instructions in
the same direction, and peak footprint <=1.02x. If it misses, mark `[DENIED]`
and remove the candidate before any Stage2 rebuild. Do not stack another
change into this measurement.

## Update No.61 — Proposal No.60 result and full fixed point `[CONFIRMED]`

The deterministic 240-block reference test passed with exact layout and edge
equality.  The slow oracle performed more than 6,000 remaining-block
comparisons; the indexed pass stayed within two recognizer text comparisons
per canonical edge.  The flow module also remained inside the strict
no-libpython closure.

Three alternating pcc1 pairs on the exact frozen largest shard all produced
byte-identical assembly:

```text
pair                         1               2               3
baseline wall               41.60s          41.57s          41.45s
candidate wall              30.29s          30.26s          30.34s
median wall speedup          1.37339x
CPU speedup                  1.37450x
candidate/base instructions 0.78354
candidate/base cycles       0.72904
candidate/base footprint    0.99997
```

### An apparent Stage2 red was a build-protocol confounder

The receipt-built candidate pcc1 failed an integrated Stage2 at 206.460 s with
an empty `PCC-PY-COMPILE-001`.  That did not establish candidate causality:
the otherwise matched baseline-source pcc1 built through the same
`run_pcc_stage1_build.py` path failed at 163.404 s with the same error.  Their
source manifests differed only in the flow file.  The candidate's 463 emitted
shards and 48 original split inputs also compiled individually.

The candidate was therefore rebuilt through ordinary `bootstrap.sh`, not
accepted from the local A/B alone.  Ordinary pcc0 -> pcc1 passed in 298.027 s.
With another verified-empty isolated cache, its full cold Stage2 passed:

```text
                         baseline       candidate       delta / ratio
Stage2 wall              1356.194s      1109.920s      -246.274s / 1.2219x
native emit               897.916s       708.549s      -189.367s / 1.2673x
oversized workers         289.843s        95.553s      -194.290s / 3.0334x
safe workers              586.496s       591.968s        +5.472s / 0.9908x
frontend codegen          195.522s       201.571s        +6.049s / 0.9700x
link driver               139.083s       129.470s        -9.613s / 1.0742x
```

The cold counters match (212 frontend actions, 463 native object misses,
seven oversized objects and 48 split modules).  The result is localized to the
oversized lane rather than cache retention or a changed workload.

Stage3 then passed in 302.363 s with 463 object-cache hits.  Both pcc2 and pcc3
have SHA-256
`d5cfbb0415659d365f32afc57485a913e70854e5358ea2ec850dfd5bc2a1436f`;
the fixed point is byte-identical.  Fallback gates are green (bootstrap
baseline 2 passed / 2 deselected, IR fallback 8 passed, fallback baseline 32
passed), and the focused layout file is 3 passed.

### Status

Proposal No.60 is `[CONFIRMED]` and retained.  It removes 246.274 s (18.16%)
from the complete cold Stage2, not merely from a microbenchmark.  It does not
close `PERF-P0-STAGE2-COLD-CACHE-REGRESSION`: 1109.920 s remains above the
600 s threshold, and three alternating full cold/hot pairs are still owed.
The next measured owner is the safe-worker lane at 591.968 s; do not retry the
now-removed canonical layout scan or attribute the residual to the 1.851 s
cache-publish path.

Durable evidence:
`docs/goal/evidence/2026-08-26-stage2-cold-hot-canonical-layout.md`.

## Update No.62 — reuse block-local last-use analysis `[CONFIRMED]`

After Proposal No.60, the largest sub-2,000,000-byte safe shard is
1,973,250 bytes, 19 functions and 3,596 blocks (SHA-256
`6d52e8b9f335c050d4c58ef50e72cd32eee428ed80211f329b6ead5f6f8aff54`).
Ordinary-bootstrap candidate pcc1
`e984196bd53a5e081cdc62d5d1971e2a65069fb6e02afd823ea01a649fa3cb9d`
emits it in 18.30 s / 197.18B instructions / 2.26 GB peak footprint.

A complete 12-second native call graph (9,962 samples) attributes 43.56% to
function emission, 34.38% to precise stack-map construction, 7.98% to module
preparation, 5.17% to global emission and 4.77% to the target adjacent-memory
pass.  The 16.65% `pcc_gc_granule_is_object_start` leaf is distributed across
these owners rather than identifying a change.

Host cProfile on the same IR instead exposes one exact repeated computation:

```text
collect_block_local_last_uses   38 calls / 0.760 s cumulative
  <- assign_stack_slots         19 calls / 0.386 s
  <- allocate_aarch64_block_registers
                                19 calls / 0.374 s
```

Each function is scanned once during target-neutral stack-slot assignment and
then scanned again immediately before AArch64 emission.  The result depends
only on parsed blocks, phis, instruction kind/data and terminators; slot
assignment mutates `value_types`/slot fields but none of the analysis inputs.
Update No.19 fixed a quadratic *inside* this analysis; it did not remove this
second identical whole-function analysis.

### Proposal No.62 `[CONFIRMED]`

Add one optional `ParsedFunction.block_local_last_uses` analysis field.
`assign_stack_slots` computes and stores the mapping it already consumes.
`allocate_aarch64_block_registers` reuses that exact mapping when present and
retains the current compute path for direct callers that have not run
stackprep.  Do not add a global `id()` cache, change mapping key semantics,
skip either consumer, or cache any other analysis in this experiment.

Regression requirements: prove stackprep publishes the result; prove regalloc
does not call the collector again after stackprep; prove a directly constructed
function with no cached result still computes it; retain hash-skew and stack
layout tests.  Assembly must be byte-identical on the frozen safe shard.

Pre-registered rejection line: three balanced current-pcc1 exact-shard pairs,
median wall and CPU speedup >=1.02, instructions in the same direction, and
peak footprint <=1.02x.  A first valid pair below 1.015 may stop and deny.
Failure of no-libpython closure or any focused semantics also denies it.  No
Stage1 or full Stage2 rebuild follows a denial.

## Update No.63 — Proposal No.62 result and attribution boundary `[CONFIRMED]`

The host check kept assembly byte-identical, reduced cProfile calls from
27.99M to 25.88M, reduced total profiled time 6.342 s -> 5.867 s, and reduced
`collect_block_local_last_uses` from 38 calls to 19.  The three source modules
remained in the strict no-libpython closure, and focused self-backend/precise
stack-map tests are 355 passed.

Matched receipt-built pcc1s used the same runtime archive, host Python and
external tools; their manifests differed only in the three pre-registered
files.  Three alternating exact-safe-shard pairs all emitted byte-identical
assembly:

```text
pair                       1             2             3
baseline wall              16.01s        15.98s        16.10s
candidate wall             15.53s        15.53s        15.64s
median wall speedup         1.02941x
median CPU speedup          1.03143x
candidate/base instructions 0.97633
candidate/base cycles       0.97073
candidate/base footprint    0.97105
```

The receipt-built candidate passed the pcc1 native-function test but failed a
GC0 compile smoke with the known empty `PCC-PY-COMPILE-001` shape.  The
matched receipt-built baseline failed the exact same input in 0.51 s with the
same diagnostic.  The ordinary current-source pcc1 compiled and ran the input
correctly, and its GC0..4 matrix is 5 passed.  This is the second paired
control showing that receipt-built pcc1 is valid for direct emit A/Bs but not
for integrated bootstrap correctness.

### Full build result and the attribution limit

Ordinary-bootstrap cold Stage2 passed in 1076.793 s, versus 1109.920 s for
No.60, and Stage3 passed in 336.860 s.  pcc2 and pcc3 are byte-identical at
SHA-256
`1c86b82ddeb18872441ac691b6b6676778a9b588b411d58134fc39f59a904787`.

The full phase delta is not the simple safe-lane transfer predicted by the
exact-shard A/B:

```text
                                  No.60       No.62       delta
compiler profile                  1105.537s   1072.209s   -33.328s
frontend codegen parallel          201.571s    168.235s   -33.336s
native emit                        708.549s    711.927s    +3.378s
oversized workers                   95.553s     91.261s    -4.292s
safe workers                       591.968s    600.185s    +8.217s
link driver                        129.470s    132.223s    +2.753s
```

The source-shape change generated one additional native object (464 vs 463;
457 vs 456 safe).  Therefore the complete 33.127 s wall improvement is a valid
source-pair result but is **not** attributed entirely to last-use reuse.  The
three-pair exact-shard result proves the direct mechanism; the one cold build
shows that the candidate does not regress total wall and retains fixed-point
correctness.  It does not prove a safe-lane critical-path reduction.

Fallback gates are current-source green: bootstrap+IR fallback 10 passed / 2
deselected, fallback baseline 32 passed.  Proposal No.62 is `[CONFIRMED]` and
retained, with this bounded claim.

The next probe must identify the real longest safe batch/item with durable
per-item timing; choosing the largest safe file by bytes did not identify the
lane's wall-critical work.  Do not attribute frontend scheduling variance to
the native emitter or pay for another cold Stage2 before the exact critical
item clears its own A/B floor.

Durable evidence:
`docs/goal/evidence/2026-08-26-stage2-last-use-analysis-reuse.md`.

## Update No.64 — exact medium ranking exhausts the next emit-local route

The accepted No.62 frontend bundle was verified and split with the production
function into exactly 464 object inputs: seven oversized, 152 medium and 305
small.  New tested tool `scripts/pcc_emit_rank.py` replayed all 152 medium
inputs with the ordinary Stage2 pcc1, one fresh process per item and eight-way
concurrency.  All items returned zero with assembly receipts; elapsed wall was
254.28 s.

The top completions were 24.25 s `string_method_lowering`, 22.66 s
`self_backend_parse`, 22.42 s `self_backend_verify`, 21.90 s
`assignment_statement_lowering`, 21.71 s `cpy_call_lowering`, and 21.49 s
`precise_stackmap`.  This proves byte size alone was an unsafe critical-path
proxy.  The rank-1 item measured about 15.5 s in isolated No.62 A/Bs but 24.25
s under the production-shaped eight-worker replay, so resource competition is
part of its lane-critical cost.

A current No.62 pcc1 call graph on that exact item attributes function emit
42.42%, stack-map plans 33.26%, stack-map render 8.42%, target pass 5.08%,
globals 5.00% and regalloc 4.77%.  The last-use reuse did what it claimed:
regalloc fell from 8.78% in the preceding capture.  The largest remaining
leaf, `pcc_gc_granule_is_object_start` at 18.89%, is distributed across all of
those owners.

No new emit-local proposal is registered.  Cursor/view removal, find-slot
only, safepoint representation, parse regex/interning, sidecar wire, text
lifecycle, call rendering and concurrency have existing measured denials.
The current evidence identifies the shared provenance lookup as the remaining
structural group, owned by `ARCH-P1-GRANULE-SPAN-LOOKUP-RADIX`.  Retrying a
smaller local shape would violate the investigation's floor and the human's
"do not optimize merely to optimize" constraint.

Status: the Stage2 P0 should wait on the radix row before another cold build.
This is a routing result, not a claim that radix alone reaches 600 s.

Durable evidence:
`docs/goal/evidence/2026-08-26-stage2-medium-critical-item-ranking.md`.

## Update No.65 — disabled memory-pair pass still parses every line `[DENIED]`

After the radix fixed the distributed runtime tax, exact medium ranking moved
the critical item to `assignment_statement_lowering` item 302.  Its current
pcc1 call graph attributes 9.89% to
`pair_adjacent_aarch64_64bit_memory_ops`; host cProfile reports 0.220 s on the
same IR.

The Stage2 hidden emit worker calls AArch64 emission with `optimize=False`.
Pairing is therefore disabled, but the pass still calls `_aarch64_opcode` and
exclusive-region recognizers on every assembly line.  Its only obligation in
this mode is removing and validating the source-semantics barrier markers.

### Proposal No.65 `[DENIED]`

Add one `enabled=False` fast path that scans once, updates only barrier depth,
removes begin/end markers, preserves every ordinary line byte-for-byte, and
retains unmatched/unterminated diagnostics.  The enabled pairing path remains
unchanged.  A regression must monkeypatch `_aarch64_opcode` to fail and prove
disabled mode never calls it while still removing nested markers and rejecting
bad depth.

Pre-registered rejection line: current ordinary pcc1, exact frozen item 302,
one balanced warmup per arm and three alternating pairs; assembly must be
byte-identical, median wall and CPU speedup >=1.05, instructions improve, and
footprint <=1.02x.  Miss denies/removes before another cold Stage2.

### Result

The regression failed before implementation as intended, then the focused
target-pass file passed 28/28 with the candidate.  Every A/B assembly was
byte-identical.  The candidate was nevertheless decisively slower:

```text
median wall speedup baseline/candidate  0.75015x
median CPU speedup                      0.73306x
candidate/base instructions             1.22572x
candidate/base cycles                   1.37373x
candidate/base footprint                0.96114x
```

The pcc-compiled `for line in lines` fast path cost much more than the original
indexed `while` plus opcode checks.  The candidate and its dedicated test were
removed by forward patch; no Stage2 was run.  Do not retry a `for`-iterator
barrier-only pass or infer pcc1 cost from the host's 0.220 s cProfile result.

## Update No.66 — decouple runtime IR passes from bounded bootstrap passes `[CONFIRMED]`

The invalid 793.029 s Stage2 exposed a real configuration owner even though it
could not prove radix transfer.  That pcc1 reused a runtime archive whose
`pcc_gc_granule_is_object_start` was a 276-byte register-resident function.
After a source change forced rebuild under bootstrap
`PCC_PYTHON_IR_PASSES=off`, the same source shape became a 436-byte function
with a 112-byte stack frame.  Stage1 profiles distinguish the arms:
`ensure_runtime=1.288s` stale reuse versus `23.798s` current rebuild.

`bootstrap.sh` correctly disables passes for compiler modules, but the same
ambient variable leaks into the independently compiled pcc-Python runtime
archive.  The bounded runtime default tier is already `mem2reg,sroa`; applying
it to runtime modules does not enable passes for pcc's 212 compiler modules.

### Proposal No.66 `[CONFIRMED]`

Add `PCC_RUNTIME_PYTHON_IR_PASSES`, default `default`, to the pcc-Python
runtime make invocation only.  Preserve explicit `off` and all other supported
pass strings.  Stage compiler `PCC_BOOTSTRAP_PYTHON_IR_PASSES=off` remains
unchanged.  Runtime provenance must rebuild rather than silently reusing an
archive from another mode.

Pre-registered rejection line: focused make-command/config tests, then matched
ordinary pcc1 module98 A/B with byte-identical assembly, wall/CPU >=1.10,
instructions improving and RSS/footprint <=1.00x.  Only an accepted result may
run a fresh cold Stage2/fixed point.  No stale runtime archive is evidence.

### Result

The independent runtime policy tests pass.  Current 32 KiB radix module98 A/B
produced byte-identical assembly and measured 1.34880x wall / 1.37580x CPU,
instructions 0.79771x, cycles 0.72420x and footprint 0.99920x.

An explicit empty-cache cold Stage2 completed in 890.433 s versus the valid
1076.793 s baseline.  Native emit fell 711.927 -> 531.077 s with identical
212-module / 464-object / 7-oversized / 457-safe counts.  Stage3 completed in
254.571 s and pcc2/pcc3 are byte-identical.  Current fallback gates are green
(10 passed / 2 deselected plus 32 passed).

`[CONFIRMED]` and retained.  This is the valid replacement for the retracted
stale-archive 793.029 s claim.  Stage2 remains above 600 s.

Durable evidence:
`docs/goal/evidence/2026-08-27-runtime-ir-pass-policy-stage2.md`.

## Update No.67 — target-final label scan ordinary-instruction fast path `[pending]`

On the current optimized-runtime item 302, stack-map render owns 13.06% and
host cProfile assigns 0.227 s of 0.404 s render time to
`_aarch64_text_label_offsets`.  The scanner calls `strip()` and checks every
directive shape for each ordinary instruction even though emitter-owned
ordinary instructions always start with two spaces, labels are unindented and
directives begin with a dot.

An uncommitted host oracle on the real 15,695-label assembly produced the exact
same offset dict and reduced 100 scans 3.290 s -> 1.318 s (2.50x).

### Proposal No.67 `[pending]`

After section tracking, classify non-directive two-space lines as one 4-byte
instruction without allocating stripped text.  Preserve the existing slow
path byte-for-byte for labels, alignment, data directives, `.space`, unknown
directives and diagnostics.  Add a mixed text/directive regression.

Pre-registered rejection: current ordinary pcc1, frozen item302, balanced
warmups and three alternating pairs, byte-identical assembly, median wall/CPU
>=1.05, instructions improving, footprint <=1.02x.  Miss removes the candidate
before another cold Stage2.

## Update No.68 — disabled pair pass with indexed while `[DENIED]`

After No.67, render fell 13.06% -> 3.79% and the still-unmodified memory-pair
pass rose to 12.31%.  No.65 denied a `for line in lines` barrier-only path at
0.750x wall because pcc's iterator cost dominated.  That verdict does not test
an indexed `while`, which is the retained pass's efficient traversal shape.

Proposal: `enabled=False` uses an index/while barrier-only scan, preserving
nested-marker validation and every ordinary line.  It skips opcode/exclusive/
pair parsing.  Enabled behavior is untouched.  Reuse the fail-if-opcode-called
regression.  Same exact item302 three-pair rejection line as No.67: assembly
identical, wall/CPU >=1.05, instructions lower, footprint <=1.02x; otherwise
remove before Stage2.

Result: assembly stayed byte-identical and instructions/footprint improved, but
median wall was only 1.02279x and CPU 1.03315x (instructions 0.96025x, cycles
0.96740x, footprint 0.96206x).  This misses the 1.05 gate.  Removed by forward
patch; no Stage2 run.  No.65 and No.68 together exhaust barrier-only disabled
pair-pass rewrites under both iterator and indexed traversal.

## Update No.69 — bounded IR passes for compiler modules `[DENIED]`

After No.67, valid cold Stage2 is 686.160 s, 86.160 s above target.  Native
emit remains 373.081 s.  Runtime-only bounded mem2reg+sroa already reduced
native emit 711.927 -> 531.077 s, proving the pcc-compiled execution tax is
sensitive to these semantics-preserving passes.

The compiler's 212 modules still use
`PCC_BOOTSTRAP_PYTHON_IR_PASSES=off`.  The 2026-05-27 investigation disabled
accidental default passes to bound bootstrap time but never built matched pcc1
compilers or measured their execution speed.

Proposal No.69 is configuration-only: build one current ordinary pcc1 with
compiler modules on bounded `default` while runtime remains independently
`default`.  Do not change repository defaults yet.  Stage1 must pass its
native smoke.  Then frozen module98 needs byte-identical assembly, wall/CPU
>=1.10, improving instructions and footprint <=1.02x across three alternating
pairs.  A miss ends the experiment without Stage2; an acceptance permits one
empty-cache Stage2/fixed point before any default change.

Result: Stage1 succeeded in 332.871 s, but module98 assembly-identical A/B was
noise: wall 1.01362x, CPU 1.00514x, instructions 1.00041x, cycles 0.99998x and
footprint 1.00043x.  `[DENIED]`; no default changed and no Stage2 was run.

## Update No.70 — one semantic validation per owned-link boundary `[DENIED; raw final validator retained]`

After No.67, the valid cold Stage2 is 686.160 s and the pcc-owned link driver
is 121.440 s.  A frozen replay of the exact 464 cached Stage2 assembly inputs
plus the current same-source pcc-Python runtime archive reproduced that phase
in 122.69 s / 230.04 s user / 6.44 s system / 6.376 GB max RSS and produced an
executable compiler.  All 464 assembly-cache `.pco` payloads were published
within an 18-second window; the final image appeared another 85 seconds later.
The remaining cost is therefore coordinator validation/merge/finalization,
not assembler throughput.

The same inputs with `PCC_MACHO_INCREMENTAL_LINK_CACHE=off` took 113.20 s and
produced the exact same SHA-256.  The 1.084x difference denies disabling or
bypassing incremental state as the Stage2 fix: cold image/layout keying costs
about 9.5 seconds, not the 86.160-second complete-stage gap.

A deterministic cProfile of the coordinator on the 464 freshly produced
`.pco` inputs (assembly excluded, profiling overhead intentionally not used as
a wall claim) recorded 828,907,508 calls.  `link_executable` split almost
exactly between prepare and finalize (83.333 s / 80.168 s profiled).  The two
structural repeat groups were:

* 464 input `decode_native_object` calls: 47.313 s, including full semantic
  validation of payloads that the same worker had validated immediately before
  encoding them;
* final merged stack-map `decode_stack_map`: 70.957 s, including 15.4 million
  dataclass constructors and a 64.802-second second traversal.  The v2 table is
  intentionally shared: millions of records reference a much smaller set of
  location slices, but the validator rechecks every referenced location.

Proposal No.70 is one validation-provenance change, not permission to weaken a
boundary.  Public/disk `decode_native_object` remains fully fail-closed.  The
assembly worker may use a private encode/decode seam only for a NativeObject it
has just validated and the in-memory bytes returned by that exact worker;
later disk cache hits still use the public validated decoder.  `from_sections`
must validate the source Section model once and the converted indexed model
once, without converting it back and validating the identical source model a
second time.  The executable boundary keeps every existing stack-map semantic
check, but performs them directly on the immutable v2 payload and memoizes
location validation by `(location_index, location_count, frame_size)`; the
result is validation-only and must not materialize a decoded map.

Pre-registered gates: malformed native-object and stack-map corpus parity must
raise the same diagnostic class/message through the public path; a focused
test must prove disk cache hits retain full validation while the same-worker
transport uses the private trusted seam; all native-object, incremental-link,
Mach-O link and precise-stackmap tests pass.  Then three alternating direct
links of the frozen 464 inputs must produce byte-identical executable images,
run `--help`, and show paired median wall/CPU speedup >=1.50 with peak RSS
<=1.02x.  A miss removes the private fast path before any Stage1/Stage2 build.
An acceptance permits one ordinary empty-cache Stage2 followed by Stage3 and
fallback gates; it does not by itself satisfy the task's three-pair <=600 s
exit criterion.

### Result

The final wire-format stack-map validator is independently `[CONFIRMED]` at
its narrow boundary.  On the exact 89,480,328-byte final pcc2 stack-map table,
the existing materializing decoder took 31.585 s and 31.752 s; the raw
validator took 2.437 s, 2.433 s and 2.432 s (about 13.0x).  It keeps every
final-image semantic check and adds no acceptance path: malformed/truncated,
wrong-target, shared-slice/different-frame and raw-pointer parity are covered
by the 32-passing precise-stackmap file.  `macho_exec` now uses that validator
only for the validation-only executable-publication call; decoded-map callers
remain unchanged.

The broader private trusted-transport candidate missed its pre-registered CPU
threshold and was removed by forward patch.  Three frozen 464-input pairs,
with incremental linking disabled in both arms and alternating B/C order,
produced the same executable SHA-256
`baac72710663dfd1e77a8184df92472c66e762dde480886a9c6e0c93a25caf00`
and passed `--help`:

```text
pair                 baseline wall/CPU     candidate wall/CPU    wall / CPU
1                    107.12 / 221.18       57.82 / 153.45       1.853 / 1.441
2                    108.81 / 222.45       61.29 / 158.08       1.775 / 1.407
3                    111.00 / 222.33       57.43 / 152.40       1.933 / 1.459
paired median                                                     1.853 / 1.441
```

Median RSS ratio was 0.846 and footprint ratio 1.002, but median CPU speedup
1.441 missed the required 1.50.  The private same-worker encode/decode seam,
the `from_sections` validation shortcut, their driver/worker wiring and their
focused tests were all removed.  The post-reversion focused link/native/
incremental/stack-map gate is 83 passed.  No Stage1 or Stage2 was launched for
No.70.  The last source-current full result remains the preceding 686.160 s
Stage2 / 240.840 s Stage3 fixed point; the retained raw validator's complete-
stage transfer is intentionally unclaimed until a later ordinary build.

## Update No.71 — the emit worker re-profiled, and pcc1 was skipping a whole phase `[CONFIRMED]`

Resuming after the No.70 pause, the goal was raised: Stage2 should become at
least as fast as Stage1, not merely reach the 600 s task threshold. The
reference points are the last valid current-source Stage2 at **686.160 s** and
the Stage1 measured under No.69 at **332.871 s**, i.e. pcc1 is about **2.06x**
slower than CPython running the same compiler source.

### A fresh caller-attributed profile of the real emit worker

Profiled the exact pcc1 that produced the 686.160 s Stage2
(`f1526b0262cd17fe…`, `build/stage2-label-scan-bootstrap-candidate-v1/out/pcc1`)
on the frozen critical medium item 302 (`assignment_statement_lowering`),
solo, 7452 on-CPU samples:

```text
GC/refcount leaf tax                     44.4% of the worker
prepare_module_for_target                51.0%   (parse 24.0, stackprep 17.1, verify 9.5)
_emit_prepared_aarch64_darwin_module     49.0%
  build_stack_map_plans                  48.5%
    build_function_stack_map_plan        45.1%
      _py_dict_get                       13.0%  <- largest single direct child
      __nested_add_record                 5.4%
      _managed_live_after                 5.3%
      _block_entry_states                 4.7%
      _managed_value_origins              3.8%
proving "this pointer is NOT managed"    10.7%   (lock + index + object index
                                                  + is_type_object + forwarding)
```

Two operational notes worth keeping. `scripts/pcc_profile.py` refuses a target
launched through an `env` wrapper ("sample reported no image named 'pcc1'");
exec pcc1 directly with the variables exported instead. And item 302 takes
**11.89 s** solo versus the 21.90 s recorded in the No.64 ranking, which was
measured with eight concurrent workers — the ranking's numbers are contended
by construction and are not a solo baseline.

### The 13% dict lookup, and what reading it actually found

`build_function_stack_map_plan` calls `py_dict_get` directly for 13.0% of the
worker. Its own children are `py_obj_hash` 4.4%, `pcc_gc_managed_pointer_find_slot`
2.4%, `_mul_u32_low` 0.4% — the signature of hashing a **tuple** key. The two
direct tuple-keyed lookups in that function are `interned_locations[fingerprint]`
and `live_after.get((block_index, instruction_index), frozenset())`.

The second one is per safepoint record and allocates twice per call: the tuple
key, and the `frozenset()` default, which Python evaluates eagerly on every
call whether or not the lookup hits. `_managed_live_after` allocates a matching
tuple per instruction on the producer side. Replacing the whole
`dict[tuple[int, int], frozenset[str]]` with `list[list[frozenset[str]]]`
answers the same question by position with no allocation, no tuple hash and no
dict probe. Host output for item 302 was **byte-identical** across the change
(`c665d81361e0c0d8…` both arms), so it is output-neutral by construction.

### The measurement that stopped being about performance

Establishing the host-vs-pcc1 baseline for that A/B showed the two compilers
do **not** agree on this input:

```text
pcc1 31d6ac3b (pre-No.67)   77453861e652b6a4…   0 managed reload triples
pcc1 f1526b02 (post-No.67)  77453861e652b6a4…   0 managed reload triples
host pcc (current source)   c665d81361e0c0d8…   1200 managed reload triples
```

Two independent pcc1 generations agree with each other and disagree with the
host from the identical IR file, so this is a stable self-host divergence, not
stale source. All 10616 pcc1-only diff lines are `.long <offset>` stack-map
entries shifting because the code is shorter; there are no pcc1-only
instructions.

Root cause, confirmed by probe rather than by reasoning: **`frozenset(d)` and
`set(d)` on a dict return an EMPTY set in pcc-compiled code.** The generic
`set(iterable)` lowering walks the source with `py_obj_len` +
`py_obj_getitem(src, i)`, and for a dict that indexing is a key lookup for the
integer key `i`. So inside pcc1,
`managed_names = frozenset(managed_origins) | ambiguous_managed` lost every
name with a known origin, `_managed_live_after` tracked nothing, and
`_planned_managed_reloads` returned `()` at every safepoint. pcc1 has been
emitting **no managed-value reloads at all** — in pcc2, in pcc3, and in every
program it compiles.

Four hypotheses were killed by probe first, and each is recorded so they are
not retried: tuple-keyed `dict.get(k, default)`, single-argument
`dict.get(k)` over dataclass values, `dict.get` under a pcc1-compiled program
rather than a host-compiled one, and dict-literal tuple keys. All four behave
correctly. The fifth probe — `frozenset(dict)` — reproduced in one run.

Full write-up, repro, verdicts and the regression:
[`set-and-frozenset-of-dict-lower-to-empty.md`](set-and-frozenset-of-dict-lower-to-empty.md).
`tests/python/test_native_set_from_dict_keys.py`, 4 passed, confirmed red
before the fix.

### What this does to the performance storyline

It invalidates the comparison, in the honest direction:

* **686.160 s was measured with pcc1 skipping a real phase.** It is not a
  like-for-like predecessor of any post-fix Stage2 number, and it must not be
  quoted as one.
* **Stage2 should be expected to get slower in direction, by an unmeasured
  amount.** "Slower because it is now correct" is not a free pass: the added
  time still has to be profiled and attributed, because some of it may be an
  inefficient reload implementation rather than necessary reload work.
* **The 1200 figure is item 302 only.** `managed_names` is
  `frozenset(managed_origins) | ambiguous_managed`, so `ambiguous_managed`
  alone could still have produced reloads elsewhere. Claim exactly this: on
  this frozen item the count went 0 -> 1200 and the assembly became
  byte-identical to host pcc's. Do not extrapolate to "every module gains
  1200" or "every program had zero".
* The `live_after` list-of-lists change stays — it is host-byte-identical and
  it removes allocation from exactly the path the fix is about to make hotter
  — but it can no longer be A/B'd against a pre-fix pcc1, since the two
  compilers no longer compute the same thing. Its verdict has to come from a
  post-fix baseline.
* Part of `_managed_live_after`'s 5.3% was pcc1 computing a liveness fixpoint
  over an empty `tracked` set. That work was never free and never used.

No Stage1 or Stage2 was launched in this update. The next expensive run is a
Stage1 rebuild on the fixed source, and the gate that matters for it is the
five-GC matrix — backends #3 and #4 are exactly the collectors these reloads
exist to serve.

## Status

Active. Stage2 is paused at 686.160 s *measured with a compiler that was
omitting managed reloads*; that number is retained as historical record and
retired as a baseline. Next action is a Stage1 build on the fixed source,
then a fresh phase profile, before any further optimisation verdict.

## Update No.72 — the IR parser's anchored regexes are 8.3% of the emit worker `[pending]`

From the same No.71 profile (item 302, current-source pcc1, 7452 samples), the
pcc-Python regex engine is a top-level cost that this file has never named:

```text
_pattern_method_call inclusive      618   8.3%
  <- _decode_simple_value_token     149   2.0%
  <- _parse_instruction             129   1.7%
  <- _parse_block                   112   1.5%
  <- _call_instr_from_parts         112   1.5%
  <- _parse_terminator               63   0.8%
  <- _parse_icmp_instruction         21   0.3%
_py_re_engine_truth_flags_from      518   7.0%
```

Under CPython `re` is C; under pcc1 it is pcc-Python compiled by pcc, so the
cost divergence is the same shape Update No.30 tabulated for attribute loads
and string hashing. The hottest callers reach it through `decode_ssa_name` /
`decode_global_name`, which run on essentially every value token and use
**fully anchored** patterns that are plain character classes:

```python
_SSA_NAME_RE    = re.compile(r'^%(?:"([^"]+)"|((?:[A-Za-z_.$][\w.$-]*|\d+)))$')
_GLOBAL_NAME_RE = re.compile(r'^@(?:"([^"]+)"|([A-Za-z_.$][\w.$-]*))$')
```

### Proposal No.72 `[pending]`

Add a **confident-only** fast path in front of each, in the shape No.67
confirmed for the label scan: the fast path returns only when it is certain,
and every other input falls through to the existing regex unchanged. It is not
an over-approximation and it is not a reimplementation — the regex stays as the
authority for quoting, escapes and any non-ASCII name.

Fast path for `decode_ssa_name`, after `%`:

```text
all digits                      -> existing numeric-name cache
'.' + all digits                -> existing dot-numeric-name cache
[A-Za-z_.$] then all [A-Za-z0-9_.$-]  -> return as-is
anything else (quotes, escapes, non-ASCII, empty) -> fall through to the regex
```

`\w` is Unicode-aware in Python's `re`, which is exactly why the fast path must
restrict itself to ASCII and defer rather than decide. Same shape for
`decode_global_name` after `@`.

Pre-registered rejection, matching No.67/No.68: current ordinary pcc1, frozen
item 302, balanced unmeasured warmups, three alternating pairs, assembly
byte-identical, median wall and CPU both >= 1.05x, instructions improving,
footprint <= 1.02x. A miss removes the fast path before any Stage2.

Two prior denials bound the expectation and are why this is written as work
*removal* rather than work *relocation*: inlining
`pcc_gc_frame_roots_disabled_fast` at four sites measured -2.5% CPU (No.58) and
the first-byte prefilter in `_class_lookup_in_mro` measured -0.6% (No.57).
Hoisting a predicate into a hot call site has now measured negative twice. This
proposal does not hoist a predicate; it skips an entire engine invocation for
the overwhelmingly common token shape.

Not started, and its numbers are now stale — recorded before measuring so the
staleness cannot become a misleading claim later.

**The 8.3% figure was measured on a compiler that was skipping a phase.** The
No.71 profile was taken with a pcc1 whose `frozenset(managed_origins)` was
empty, so `build_function_stack_map_plan` planned **zero** managed reloads
(root cause:
[`pcc1-stage2-stale-managed-self-outlives-root.md`](pcc1-stage2-stale-managed-self-outlives-root.md)
— a `set`/`dict` probe-budget defect that silently dropped negative
pointer-aligned keys). With reloads restored, that function does strictly more
work, so the IR parser's *share* of the emit worker must fall even if its
absolute cost is unchanged.

Consequences, none of which invalidate the proposal but all of which change
what may be claimed about it:

* The ranking that made the parser "the next target" has to be redone against
  a profile from a correct compiler. Another item may now outrank it.
* The 1.05x pre-registered bar was chosen against the old distribution. It
  stays as written — moving a threshold after seeing new data is how a denial
  turns into an acceptance — but a miss must not be re-litigated by pointing
  at the changed baseline.
* Item 302's solo wall time (11.89 s under the old compiler) is likewise not
  a valid control for a post-fix A/B.

So the order is: finish the correctness chain, take a **fresh** caller-attributed
profile on a correct pcc1, and only then decide whether No.72 is still the
right next slice.

## Update No.73 — the restored reload planning is recomputed per safepoint `[DENIED]`

The first correct cold Stage2 completed at **892.439 s** (profile total
884.081 s), against 686.160 s from the compiler that was skipping reload
planning entirely. The increase is not spread out — it is one lane:

```text
phase                        skipping     correct      delta
frontend codegen parallel     134.700     136.638      +1.4%
native emit, oversized (7)     61.729     185.412      +200%
native emit, safe (457)       296.913     322.378      +8.6%
pcc-owned link driver         121.440     111.374      -10.1 s
total                         686.160     884.081     +197.9 s
```

**The frontend phase is an unplanned control and it validates the rest.**
Reload planning cannot touch it, and it moved 1.4%. So the reference host's
background load did not inflate these numbers uniformly; the emit increase is
real work, not noise. (The absolute total is still not a clean baseline — see
the load caveat on the task row — but the phase *structure* is trustworthy.)

Reload planning is charged per safepoint, and the oversized shards have the
highest safepoint density, which is exactly the observed shape.

### What `_planned_managed_reloads` allocates on every single safepoint

```python
reloads = []                                  # fresh list
destinations = {}                             # fresh dict
for name in sorted(live_values):              # fresh sorted list
    ...
    reload = PlannedManagedReload(...)        # one dataclass per live value
reloads.sort(key=lambda item: (               # ONE 3-TUPLE PER ELEMENT
    item.destination_offset, item.source_offset, item.derived_offset))
return tuple(reloads)                         # fresh tuple
```

The tuple-key sort is the same shape this file already removed once: the
`_location_sort_key` comment records that a tuple key cost ~4.3 million tuple
allocations for one function and was replaced by an int key. That fix never
reached this function, because until the mapping bug was fixed this function
returned `()` on its first line and none of it ran.

### Proposal No.73 `[DENIED BY MEASUREMENT — its premise is false]` — memoize, do not micro-optimize

Do **not** start by packing the sort key into an int. Frame offsets are
arbitrary-precision under pcc, and a packed three-field key risks leaving the
tagged small-int lane and allocating a bignum per element — worse than the
tuple it replaces. Attack the redundancy instead:

1. **Intern the live sets in `_managed_live_after`.** `live` only changes at a
   def or use of a tracked value, so consecutive instruction slots very often
   get equal `frozenset(live)` values. Keep the previous frozenset and reuse
   the object when the set is unchanged. This removes allocations in the
   producer *and* makes identity a sound cache key downstream.
2. **Memoize the reload tuple on `(active_version, id(live_set))`.** Both
   inputs to `_planned_managed_reloads` are then covered: `active_offsets` is
   already version-counted, and the interned live set gives identity.

The `id()`-keyed cache hazard this repository has been bitten by — a freed
object's address being reused so a stale fingerprint *hits* — does not apply
here, and for a structural reason rather than by luck: `live_after` owns every
interned frozenset for the whole duration of the plan, so no keyed object can
be freed while the cache is alive.

Pre-registered rejection, same line as No.67/No.68: current ordinary pcc1,
frozen oversized item, balanced unmeasured warmups, three alternating pairs,
**assembly byte-identical**, median wall and CPU both >= 1.05x, instructions
improving, footprint <= 1.02x. A miss removes the change before any Stage2.

The candidate item is the largest oversized shard,
`pcc.py_frontend.codegen.call_expression_lowering` (item 311, 5,108,635 bytes),
with `method_call_expression_lowering` (item 376) as the confirmation input.

Not started: Stage3 is running and it reads `pcc/*.py` as its input, so editing
compiler source now would corrupt that stage's input rather than merely
contend with it.

### Result — measured before implementing, and the premise did not survive

Counted on the real largest oversized shard (item 311,
`call_expression_lowering`, 5,108,635 bytes) by wrapping
`_planned_managed_reloads` in-process on the host. Call counts are
contention-independent, so they stand despite the loaded reference machine;
only the timing is contaminated, and it is reported as such.

```text
_planned_managed_reloads calls   18,592
  of which live_values is empty  13,754   (74% return () on the first line)
distinct inputs                   1,117   input redundancy  94.0%
distinct outputs                    309   output redundancy 98.3%
planner wall (host)                0.02 s  for the entire function
```

The redundancy is real and high, **but the work being repeated is nearly
free**: 0.02 s on the host for all 18,592 calls. Memoizing that cannot recover
124 seconds. The proposal is denied on its premise, before a line of it was
written, and the interning half is denied with it — there is no point interning
live sets to enable a cache that has nothing to cache.

### So where did oversized emit 61.729 s -> 185.412 s actually go?

**Unknown, and deliberately not guessed.** Two candidate explanations are
already ruled out by measurement:

* **Not the planning.** 0.02 s host for the whole function, above.
* **Not the volume of emitted code.** On item 343 the reload-carrying assembly
  was 226,958 lines against 222,282 without — **+2.1%**, nowhere near +200%.

One suspect is introduced by the fix itself and must be measured rather than
assumed away: with the probe budget corrected, `set`/`dict` now **retain
elements they previously dropped in silence**, so containers are larger,
collision chains are longer, and every lookup does more work. That is a
legitimate cost of correctness, but "correct, therefore the slowdown is
justified" is not a finding — the size of the cost has to be measured before
any of it is accepted.

Next, in this order, per the review:

1. Let Stage3 finish and establish the pcc2/pcc3 fixed point. Until then
   892.439 s is a **candidate** cold Stage2, not a baseline.
2. Attribute the oversized lane **under pcc1**, not on the host. The host
   number above is exactly why: 0.02 s there says nothing about the
   allocation-dominated cost model that governs pcc1.
3. Only then choose an optimization, against a profile from a correct
   compiler.

## Update No.74 — the oversized-lane attribution in Update No.73 was WRONG `[retracted]`

Update No.73 wrote that the oversized emit lane grew "because reload planning
is charged per safepoint, and the oversized shards have the highest safepoint
density, which is exactly the observed shape." That reasoning was plausible and
it is **wrong**. Retracted here rather than left standing.

### Measured: the correct compiler is FASTER per item, not slower

Same frozen oversized input, same machine, back to back:

```text
                                    wall     user     assembly lines
pcc1 31d6ac3b  (reloads OFF)       81.12 s  77.85 s   529,592
pcc1 6615a64f  (reloads ON, fixed) 55.50 s  51.37 s   550,296   (+3.9%)
```

**1.46x faster while emitting 3.9% more assembly.** Whatever the Stage2
oversized lane's 61.729 s -> 185.412 s is, it is not the cost of the restored
reload work.

### Caller-attributed profile on the correct compiler agrees

Item 311 under pcc1 6615a64f, 18,478 on-CPU samples:

```text
emit prepared module                98.4%
  build_stack_map_plans             91.0%
    build_function_stack_map_plan   85.4%
      _planned_managed_reloads       0.2%   <-- the restored work
      _block_entry_states           11.7%
      _managed_live_after            9.1%
      _managed_value_origins         4.4%
prepare (parse + verify + stackprep) 1.6%
```

Reload planning is **0.2%** under pcc1, matching the 0.02 s host measurement
from No.73. The earlier worry that "the host cost model does not apply here"
was itself unnecessary: this work is simply cheap on both.

About 60% of the worker sits in `build_function_stack_map_plan`'s own
per-safepoint loop, outside the four named sub-analyses. That is the real
target, and it is *not* new work — it was there before the fix too.

### An arithmetic contradiction that invalidates a comparison

The Stage2 profile says the old run finished **7 oversized objects in
61.729 s**. But item 311 alone takes **81.12 s** under that same old compiler.
Both cannot be true, so the frozen `build/stage2-current-object-inputs-no62-v1`
items are **not** the workload either Stage2 run's oversized lane actually
processed — they come from the earlier No.62 frontend bundle, and splitting
depends on IR size, which the fix changed.

They remain a perfectly good **A/B input** (identical bytes to both arms). They
are not a reconstruction of a lane.

### Honest position on 892.439 s vs 686.160 s

**Unattributed.** The only thing measured is that per-item emit got *faster*.
Candidate explanations, none tested: different oversized/safe split between the
two runs, different scheduling or concurrency in the lane, and the reference
host's background load during the 892 s run. Attributing it needs per-item
timings from both runs, and the old run did not record them.

What must NOT be said: that the increase is "the cost of correctness". That was
this file's own claim one update ago and the measurement does not support it.

## Update No.75 — the location fingerprint costs more than the merge it avoids `[CONFIRMED]`

With the reload red herring cleared (No.74), the oversized worker's real
distribution on the correct pcc1 (item 311, 18,478 samples):

```text
build_function_stack_map_plan            85.4%
  _py_dict_get (tail-called subscript)   27.5%   <-- largest single item
    -> py_obj_hash                        8.1%
    -> pcc_gc_managed_pointer_find_slot   4.1%
    -> calloc / malloc / free             2.3%
  _block_entry_states                    11.7%
  __nested_add_record                     9.2%
  _managed_live_after                     9.1%
  _managed_value_origins                  4.4%
  _planned_managed_reloads                0.2%
```

The intermediate frames are elided by the tail-call pass, so the 27.5% is
attributed to the planner rather than to `py_dict_getitem` and `add_record`.
The `calloc`/`malloc` underneath say this dict is **growing**, not merely being
read, and `managed_pointer_find_slot` says fresh objects are entering the GC
index on the way in. That is the `interned_locations` fingerprint:

```python
fingerprint_parts = []
for group in active.values():
    fingerprint_parts.append(id(group))
fingerprint_parts.sort()
fingerprint = tuple(fingerprint_parts)
if fingerprint in interned_locations:
    entry = interned_locations[fingerprint]
else:
    entry = (_locations(active), tuple(active.values()))
    interned_locations[fingerprint] = entry
```

Per version change that is a list, a sort, a tuple, a tuple hash (element-wise
multiplies, visible as `_mul_u32_low`), a dict probe and sometimes a table
growth — to avoid one `_locations(active)` flatten-and-sort. The memo is doing
its job (the existing comment records 12186 merges collapsing to 2465 distinct
answers) but its *key* now costs more than the merge.

### Proposal No.75 `[CONFIRMED on the pre-registered line]`

Replace the id-tuple fingerprint with an **XOR of the group ids**. XOR is
order-independent and self-inverse, which is exactly set semantics, so the
sort disappears with it. The key becomes one integer in the tagged lane: no
list, no tuple, no element-wise hash, no allocation, and a dict probe on an int
instead of on a tuple.

XOR admits collisions, so correctness cannot rest on the key alone. The entry
already stores `tuple(active.values())` — the groups it was keyed on — for the
`id()`-liveness rule this repository learned the hard way. That stored tuple
becomes load-bearing: on a hit, verify it against the current `active.values()`
by identity before using the cached locations, and treat a mismatch as a miss.
The verification is O(number of active groups), which the same profile shows is
single digits, and it makes a collision a slow path rather than a wrong answer.

Pre-registered rejection, same line as No.67/No.68: current ordinary pcc1
(baseline `6615a64f`), frozen item 311, balanced unmeasured warmups, three
alternating pairs, **assembly byte-identical**, median wall and CPU both
>= 1.05x, instructions improving, footprint <= 1.02x. A miss removes the change
before any Stage2.

Note the baseline for this A/B is 55.50 s on item 311, measured on the correct
compiler — not the 81.12 s the pre-fix compiler took, and not anything derived
from the retired 686.160 s Stage2.

### Result — all four pre-registered gates pass

Implementation: XOR of the active groups' ids replaces the sorted id-tuple as
the memo key; the entry's stored `tuple(active.values())` — already kept for
the id()-liveness rule — becomes load-bearing and is verified by identity on
every hit, so an XOR collision is a slow path, never a wrong answer.

Semantic checks before any timing: strict no-libpython closure rc=0; host emit
of item 343 byte-identical to the pre-change reference (`ae5db2c6…`); the
candidate Stage1 built clean (345.693 s, rc=0, pcc1 `e9762f9e`).

Three alternating pairs on frozen item 311, `/usr/bin/time -lp` parenting pcc1
directly (a first attempt wrapped `gtimeout` between them, which attributes
the per-process counters to gtimeout — footprint read 999,736 bytes against
pcc1's real 8.7 GB; discarded, order fixed, re-run):

```text
pair   wall B/C            user B/C            ins C/B   footprint C/B
1      55.72/53.97 1.032   52.36/49.13 1.066   0.9099    0.8020
2      54.20/46.58 1.164   51.33/44.61 1.151   0.9105    0.8035
3      55.20/52.23 1.057   51.65/48.19 1.072   0.9104    0.8025

median wall 1.057 (>=1.05)   median CPU 1.072 (>=1.05)
instructions 0.910 (improving)   footprint 0.803 (<=1.02)
assembly: all six runs byte-identical (ff943e10afe802c4…)
```

Honest weighting: the machine carried heavy unrelated load (load average 13+,
a chat client at 342% CPU), so the wall median clearing 1.05 by 0.007 is the
weakest of the four numbers. The instruction count is load-independent and
stable to 0.06% across pairs at **-9.0%**, and CPU at +7.2% median agrees;
those two carry the acceptance. The footprint drop was not predicted:
**8.68 GB -> 6.96 GB (-20%)**, consistent with millions of fingerprint tuples
(and their managed-pointer index entries) no longer being allocated on an
input this size.

Focused gates after acceptance: 52 passed, 2 deselected (precise-stackmap ABI,
mapping-family, probe-coverage). Per protocol the acceptance buys one
empty-cache Stage2 + Stage3, now running under `gtimeout 3000s` with a
verified-empty object cache (pcc1 `e9762f9e`, `build/bootstrap-no75-v1`).
No claim about the complete stage until it reports.

## Update No.76 — per-block `_state()` re-sorts unchanged root state `[IMPLEMENTED — batched at the user's direction]`

Thought ahead while the GC3 probe occupies the machine; no source is touched
until it reports. Ranking caveat: the o311 profile predates No.75, so the
percentages below are upper bounds on the post-No.75 shares — but the absolute
work of these functions is untouched by No.75, so the code analysis stands.

`_block_entry_states` (11.7% of the oversized worker, 5.7% inside `_state`)
runs per block:

```python
outgoing = _state(active)     # tuple(active[key] for key in sorted(active))
```

That is a **string-key sort plus a fresh tuple per block edge**, even when the
block contained no frame enter/leave at all — and in the huge shard functions,
frame-protocol instructions cluster at entry/exit while thousands of interior
blocks never touch `active`. Under pcc1 a string compare walks bytes and a
tuple is a GC allocation, so the common case pays the full price for nothing.

### Proposal No.76 `[pending]` — reuse the entry tuple when the block did not touch `active`

Track one flag through the block walk:

```python
touched = False
for instr in block.instructions:
    if _apply_frame_protocol(func, globals_by_name, aliases, active, instr):
        touched = True
outgoing = _state(active) if touched else entry_state
```

Conservative on purpose: `_apply_frame_protocol` also returns True on its
escape paths (global/heap/caller-owned slots) *without* mutating `active`;
those blocks rebuild unnecessarily, which is rare and merely misses the
optimization — it can never produce a wrong tuple. When `touched` is False,
`active` is exactly the mapping `entry_state` was built from, so the reused
tuple is **value-identical by construction**; reuse also makes the join's
`previous != outgoing` comparison hit the identity fast path.

Do NOT replace the flag with a `len(active)` before/after compare: a block
that enters one root and leaves another keeps the length while changing the
content.

Pre-registered rejection, unchanged line: baseline pcc1 `e9762f9e` (No.75),
frozen item 311, balanced warmups, three alternating pairs, assembly
byte-identical, median wall and CPU >= 1.05x, instructions improving,
footprint <= 1.02x. A miss removes the change before any Stage2.

Second candidate if the fresh profile ranks it higher: the live-set interning
half of the denied No.73, re-justified on its own terms (one frozenset
allocation per unchanged instruction slot, allocation count being pcc1's
dominant cost) rather than on the dead memo premise.

Structural observation deferred, not proposed: `build_function_stack_map_plan`
re-runs the same `_apply_frame_protocol` walk per instruction that
`_block_entry_states` already ran — a whole duplicated pass. Removing it is a
refactor of two functions' contract, not a bounded slice; it needs its own
design and its own risk budget.

Order of operations once GC3 reports: fresh caller-attributed profile on the
No.75 pcc1 (the o311 ranking is stale), then implement whichever of the two
candidates the fresh profile ranks first, then the A/B.

## Update No.77 — protocol change by user direction, and the review's constraints on it

The user explicitly waived per-change A/B ceremony ("不要ab测试了。直接优化
stage2"), so Nos. 76, the live-set interning, and the managed-refcount ABI
were **batched**: each individually proven output-neutral (byte-identical
item343/item311 emits, closure, unit gates), attribution deliberately traded
away, the running batch chain (`bootstrap-batch79-v1`) standing as the
stage-level validation for the stack as a whole. Single-pulse (non-ceremonial)
item311 walls: 46 -> 45 -> 42.85 -> 42.23 s across the stack.

An external review then flagged, correctly:

* the managed-refcount ABI (`py_incref_managed`/`py_decref_managed`) skips the
  raw-pointer provenance walk and derefs the header — a wrong
  managed-by-construction proof at any call site turns a safe no-op into an
  invalid read, and it had **no tests**. Now gated by
  `tests/python/test_native_refcount_managed_variants.py`: dict/list/tuple/
  instance/compare/set paths under GC0/GC3/GC4 with forced collects, a
  finalizer round-trip that surfaces refcount drift in either direction, a
  C-mirror arm, and a static fence asserting the FRONTEND never emits these
  symbols (the contract is only provable inside the runtime).
* the redundant post-definition forward decl of `pcc_incref_prepare_ex` in
  py_obj.c — queued for removal with the next source window.
* wave 2 of call-site conversion (instance_get_field + compare paths) is
  **frozen in the scratchpad** until the batch chain is green and the new
  tests pass.

## Update No.78 — the planner recomputes every frame-protocol transition `[DENIED]`

After closing the linker-local round, the No.75 complete profile restores the
correct phase priority: Stage2 is 977.866 s, IR-to-assembly native emit is
583.303 s (safe workers 383.496 s, oversized workers 186.176 s), and the final
owned assembler/linker is 118.087 s.  Further linker micro-candidates stop.

The current source already contains No.76's unchanged-block `_state()` reuse
and live-set interning.  A read-only sizing run on the real frozen oversized
item311 (`call_expression_lowering`, 5.1 MB IR) instrumented the actual prepared
planner path:

```text
functions                         1
blocks                         9474
instructions                  59984
safepoint records             20004
_apply_frame_protocol entry   59984 calls / 27633 true
_apply_frame_protocol emit    59984 calls / 27633 true
```

The second 59,984-call walk is not hypothetical: `build_function_stack_map_plan`
first calls `_block_entry_states`, which resolves every frame enter/leave and
builds the active group, then its safepoint loop invokes the same function on
the same instruction stream and repeats `_direct_call`, `_root_group`, pointer
resolution and frame-map decoding.  The second walk is needed only for the
transition result, not for CFG join discovery.

### Proposal No.78 `[DENIED]` — compute transitions once, replay them in emit

Split transition decoding from active-state mutation.  `_block_entry_states`
computes each instruction's transition while doing the authoritative CFG walk,
applies it, and retains only one compact reference per instruction: `None`, an
existing `_RootGroup`, a leave-key string, or one escape singleton.  Return
that table with the block entries.  The emit loop applies the precomputed
transition and never reruns `_root_group`/pointer resolution.

This retains active-state validation in both consumers: duplicate enter,
leave-without-enter and join mismatch still fail closed.  It does not store
full active states per instruction.  On item311 the table is 59,984 references,
while it removes 59,984 transition decodes and 27,633 duplicate true-event
constructions.

Per the user's request to stop paying an A/B ceremony for every tiny edit,
this is one structural emit-owner slice, not a micro-candidate series.  Gates
before any expensive measurement: focused transition-count regression proves
one decode per instruction, precise-stackmap ABI/GC-root tests pass, strict
closure passes, and item311 assembly is byte-identical.  Batch any other
seconds-level output-neutral checks first, then run one worker-level A/B for
the whole source-stable emit batch.  No Stage2 until that batch has a verdict.

### Result

The focused transition-count/root-state tests passed and the candidate emitted
the exact control assembly SHA-256 `ff943e10afe802c44faff43146a67b56735cd74bb6f1d79db1d8251cfe8f7251`.
One receipt-bound item311 pulse was sufficient to reject it before any paired
ceremony:

```text
                         control       candidate      candidate/control
wall                     54.00 s        85.16 s       1.577
CPU                      45.47 s        56.28 s       1.238
instructions            637.13 B      627.47 B       0.9848
cycles                  148.35 B      154.71 B       1.0429
peak footprint            6.874 GB       6.857 GB      0.9975
assembly                 ff943e10...   ff943e10...    identical
```

The machine was more loaded during the candidate, so wall/CPU alone are not a
clean attribution.  The load-independent result is still too small for the
complexity: retaining a full transition table removed only 1.52% of
instructions and 0.25% of footprint.  It is not the 583-second absolute owner.
The source change and regression were removed by forward patch; the sizing
counts remain evidence that a duplicated scan exists, not evidence that it is
worth caching.

## Update No.78 (2026-08-27): oversized-lane per-item receipts and the width-2 pairing schedule [MEASURED, not implemented]

`pcc_emit_rank --lane oversized --jobs 1` on batch77 pcc1 + the frozen no62
manifest (performance lock held) put real numbers on the serial oversized lane:
7 items, 164.8 s serial; walls 46.2 / 35.7 / 28.9 / 13.2-13.7 s; peak
footprints 6.87 / 4.82 / 4.39 / 2.29-2.38 GB
(`build/oversized-lane-baseline-v1/manifest.json`).

Schedule computed from the receipts, 8 GB budget: the giant
(`call_expression_lowering`, 6.87 GB) must run alone; `method_call_...`
(4.82 GB) and `cli_bootstrap` (4.39 GB) must not overlap each other (9.21 GB)
but each pairs with any small (<= 7.2 GB). Two lanes after the giant ->
110.8 s wall, ~54 s saved, peak <= 7.2 GB. One-knob admission: width 2 with a
concurrent input-bytes-sum cap (~7 MB) calibrated from the measured pairs (the
linear GB/MB model over-predicts pair sums; do not use it for admission).

Deliberately NOT implemented: `pipeline_self_backend_emit.py` is adjacent to
the in-flight Indexed Function Kernel restructure, and that lane targets
exactly the planner dominating these walls — receipts must be re-taken on the
kernel compiler before quoting savings. Row:
`PERF-P2-OVERSIZED-LANE-PAIRING` (depends on the kernel row); evidence:
`docs/goal/evidence/2026-08-27-oversized-lane-pairing-schedule-design.md`.

## Update No.79 (2026-08-27): oversized admission waves LANDED — lane 177.6 s -> 119.4 s [CONFIRMED]

No.78's schedule is implemented as the default: `pack_admission_waves`
(first-fit-decreasing, width 2, 7 MB concurrent-input-byte cap) in
`pipeline_self_backend_emit.run_emit_worker_pool`, weights taken from each
command's OWN batch contents because `_pack_batches` LPT-reorders and the
caller's `item_bytes` list misaligns with command order (using it directly
would have co-scheduled the two giants — caught while wiring, pinned by the
pool-level unit test that re-derives wave bytes from the written manifests).

Gate: snapshot chain (HEAD + the two scheduler files), GC0 —
stage1 239.3 s, stage2 667.8 s, stage3 248.1 s, **pcc2 == pcc3
byte-identical**; then one cold profiled stage2:

```text
total                                    664.2 s   (batch77 cold: 867.8)
link_self_native_emit_oversized_workers  119.37 s  (batch77: 177.64  -33%)
link_self_native_emit_safe_workers       241.75 s  (batch77: 309.65)
oversized pool processes                 7 — all ran, no cache shortcut
```

Attribution discipline: the LANE counter is the claim (-58.3 s, matching the
No.78 wave math). The total's -203 s is machine-load epoch on top — the safe
lane and frontend shrank with zero code changes. The cache-warmth hypothesis
for the fast first pass was tested and DENIED (cold rerun within 4 s).
Stage2 cold is under the task's 686.160 anchor for the first time; the
remaining mass is worker compute (Indexed Function Kernel lane) plus the
241.8 s safe lane. Boundaries: five-GC ride-along pending; re-receipt the
per-item walls after the kernel lane lands (the byte cap stays valid —
footprints track input bytes, not speed).

## Update No.80 (2026-08-27): wide-cap single wave DENIED on a loaded machine; the env-existence hypothesis DENIED by its own control [both DENIED]

Attempt: scale the admission cap to physical memory in bootstrap.sh
(hw.memsize/2700 -> ~38 MB on this 96 GB Mac -> the whole oversized lane in
one 7-wide wave; a manual 7-wide worker run of the frozen no62 items was
green with zero stderr).  Three stage2 arms then failed at 224-309 s with
the empty-text PyPipelineError, dying in the FRONTEND phase (codegen 194 s
vs the green run's 110 s — degraded, then dead) before the admission code
even runs at link time.

Hypotheses killed in order:
- "the pcc1-side env parsing (char loop) crashes": the construct compiles
  and runs correctly under the self backend, and the GREEN chain had
  already compiled and executed the same code.  DENIED.
- "an env allowlist rejects the unknown PCC_SELF_BACKEND_* name": no such
  scan exists.  DENIED.
- "the env's existence is the trigger" (2/2 green unset vs 3/3 red set):
  the control arm with the KNOWN-GREEN pairs value (cap=7000000 explicit)
  failed identically -> the correlation was TIME ordering, not causation.
  DENIED.
- Actual cause: the machine had drifted into memory exhaustion between the
  green runs and the arms — vm.swapusage 23.7/24 GB used, fseventsd at
  16 GB RSS after a day of snapshot/build churn, ~55 GB active+inactive.
  Workers were killed silently (nothing on stderr; the sh wait swallows
  signal deaths) and the coordinator surfaced the empty PyPipelineError.

What stands and what was reverted:
- STANDS: the in-compiler 7 MB pairs default and its receipts (lane 177.6
  -> 119.4 s, fixed point green) — all recorded before the pressure built.
- REVERTED: the bootstrap.sh hw.memsize-derived cap.  Physical RAM is not
  available RAM; a launch-time cap must derive from AVAILABLE memory or
  not exist.  The env stays an explicit-user knob.
- Tooling lesson: my stage wait pattern matched only
  PCC_BOOTSTRAP_STAGE_RESULT and slept through STAGE_FAILED for 660 s —
  wait patterns must match both markers.
- The tiny-compile probe arm was orthogonal noise: bootstrap-built pcc1
  fails ANY user compile from the snapshot cwd (batch77's pcc1 too),
  before env even matters.  Do not diagnose stage behavior with it.

## Update No.81 — fresh correct-compiler profile reactivates No.72 `[pending]`

CPython 3.15 Tachyon first profiled the complete host Stage1 across 227
processes.  The accepted summary/native-data-plane pcc1 then replayed exact
item311 in 24.35s / 292.602B instructions / 1.725GB and emitted the retained
`ff943e10...` assembly.  A 15-second native caller flamegraph captured 11,378
on-CPU samples from that same pcc1 binary.

The post-V104 distribution is now:

```text
prepare_module_for_target                 6,936  60.96%
parse_self_backend_module                 6,048  53.16%
  _parse_call_instruction                 2,988  26.26%
    _call_instr_from_parts                2,925  25.71%
build_stack_map_plans                     1,320  11.60%
emit_function                             2,205  19.38%
pair_adjacent_aarch64_64bit_memory_ops      861   7.57%
```

The old stable-key and scheduler hypotheses stay denied: stable-key work is
only 38 samples, and current medium item423 already fell from the historical
24.25s to 5.76s.  The new absolute parser owner is the pcc-Python regex engine:

```text
_pattern_method_call inclusive            1,325  11.65%
_py_re_engine_truth_flags_from             1,169  10.27%
  caller: _call_instr_from_parts             353   3.10%
  caller: _decode_simple_value_token          349   3.07%
  caller: _parse_block_instructions           348   3.06%
  caller: _instruction_destination_from_line  153   1.34%
```

This is the fresh correct-compiler attribution No.72 required before it could
start.  Implement exactly its already-registered confident-only ASCII fast
path in `decode_ssa_name` and `decode_global_name`: numeric/dot-numeric SSA and
plain `[A-Za-z_.$][A-Za-z0-9_.$-]*` names return without regex; quoted,
escaped, Unicode or invalid inputs fall through to the unchanged anchored
patterns and diagnostics.

Before a Stage1 rebuild, compile one ordinary driver with the same accepted
pcc1/runtime against baseline and candidate source roots.  Require identical
output and at least 1.20x runtime improvement.  Final acceptance remains
No.72's original bar: source-frozen candidate pcc1, exact item311 assembly,
three alternating item311 pairs, median wall and CPU >=1.05x, improved
instructions and footprint <=1.02x.  A miss removes only the fast path by
forward patch; no Stage2 runs first.

## Update No.82 — No.72 ASCII name fast path `[CONFIRMED]`

The first prefilter driver was invalid and is excluded: pcc1 compiled it with
rc=0, but the produced binary failed with
`ImportError: pcc.backend.self_backend_parse`. A replacement pair embedded the
exact baseline/candidate helper bodies, ran under the same accepted pcc1 and
frozen runtime, and produced identical `11900000` output. It measured 15.62s /
230.663B instructions / 325.7MB footprint for the regex baseline and 2.73s /
38.056B / 254.2MB for the fast path, clearing the registered 1.20x rebuild
prefilter.

Two CPython 3.15.0rc1 Stage1 builds then froze 1,137-file control/candidate
source trees. Machine comparison proves that only
`pcc/backend/self_backend_parse.py` differs. Both pcc1 artifacts are GC0,
self/no-libpython, use the identical `624e1de9...` runtime, and link only
libSystem.

After balanced warmups, frozen item311 ran in B/C, C/B, B/C order:

```text
pair   wall B/C   CPU B/C   instructions C/B   footprint C/B   assembly
1       1.0945     1.0940         0.90069          0.98720      ff943e10...
2       1.1413     1.1388         0.90085          0.98721      ff943e10...
3       0.9443     1.0397         0.90103          0.98715      ff943e10...
median  1.0945     1.0940         0.90085          0.98720      identical
```

Pair 3's candidate wall contains about 1.6s off-CPU delay and remains in the
record rather than being discarded. The pre-registered median wall/CPU
>=1.05x, improved-instructions, footprint <=1.02x, and exact-assembly boundary
passes. Focused name-decoder tests pass 2/2, and the changed parser module
passes strict self/no-libpython closure emission. The fast path stays.

This result accepts one parser optimization, not whole Stage2, provenance,
parallel emit, or GC1--4. Full receipts:
`docs/goal/evidence/PERF-P0-PROVEN-NATIVE-DATA-PLANE-PARALLEL-EMIT/001-ascii-ir-name-fast-path.md`.

## Update No.83 — promote the structural call fallback `[DENIED]`

The accepted No.72 profile still attributed 5.17% to the regex engine and
24.91% to `_parse_call_instruction`. The existing non-regex call fallback was
first compared against `_CALL_RE` across all 416 call-bearing frozen shards:
2,678,736 calls, every regex matching, zero fallback errors and zero parsed
result mismatches. An ASCII callee scanner plus quoted/Unicode regex fallback
then passed the same complete differential and focused/closure gates.

Source-frozen CPython 3.15.0rc1 pcc1 arms differed only in the parser. The
candidate emitted exact `ff943e10...` item311 assembly, but its balanced
warmup missed the registered early line:

```text
                         control        candidate       C/B
wall                       15.64s          15.23s       0.9738
CPU                        15.53s          15.16s       0.9762
instructions             207.258B        209.318B       1.00994
peak footprint             1.703GB         1.604GB      0.94209
```

The caller profile explains the denial. Regex falls 5.17% -> 2.38%, but
`_extract_leading_type_token` grows 4.61% -> 8.08%, `_parse_ir_type_prefix`
4.48% -> 7.27%, and the whole call parser 24.91% -> 34.34%. Promoting the
fallback reparses every return type and moves rather than removes work.

No paired ceremony or Stage2 ran. The proposal and its temporary tests were
removed by forward patch; the parser is byte-identical to accepted No.72 SHA
`809341af...`. A future call fast path must obtain the already-canonical return
type without this prefix-parser pass; another full fallback promotion is
denied. Full receipt:
`docs/goal/evidence/PERF-P0-PROVEN-NATIVE-DATA-PLANE-PARALLEL-EMIT/002-structural-call-parser-denied.md`.

## Update No.84 — confident scalar-call parser `[DENIED]`

No.83 showed that promoting the full structural fallback moved regex cost into
type-prefix parsing. A narrower candidate avoided that mechanism: only simple
scalar returns, explicit non-nested signatures, ASCII names and
non-parenthesized arguments bypassed `_CALL_RE`; all other shapes fell through.
It covered 2,678,616 of 2,678,736 frozen Stage2 calls (99.9955%) with zero
normalized field mismatch. Its pcc1 microbenchmark was 3.76x faster with
74.4% fewer instructions, so one source-frozen build was authorized.

The real item311 result is a stable improvement but misses the stated line:

```text
pair   wall B/C   CPU B/C   instructions C/B   footprint C/B   assembly
1       1.04155    1.04164        0.96464          0.86805      exact
2       1.03247    1.03329        0.96533          0.86806      exact
3       1.04218    1.04227        0.96491          0.86801      exact
median  1.04155    1.04164        0.96491          0.86805      ff943e10...
```

Caller attribution confirms real deletion: regex pattern work 5.17% -> 1.07%
and the whole parser 54.08% -> 45.65%, with no No.83-style type-prefix growth.
But the 100-line candidate does not clear the explicitly retained 1.05x median
wall/CPU criterion. The threshold is not changed after observation. No Stage2
ran; source and temporary tests were forward-removed, restoring accepted No.72
SHA `809341af...` byte-for-byte.

Full receipt:
`docs/goal/evidence/PERF-P0-PROVEN-NATIVE-DATA-PLANE-PARALLEL-EMIT/003-scalar-call-fast-path-denied.md`.

## Update No.85 — provenance/raw-span/batch-intrinsic sizing `[DENIED]`

After restoring No.72, the self-time distribution is almost entirely generic
GC/refcount/type/container leaves. Three source-free discriminators prevent
turning that observation into unsafe or low-ceiling code.

First, 500,000 GC0 retain/release pairs retire 828.8M instructions through the
checked path versus 623.1M through direct refcount primitives (1.33x isolated).
The direct arm is only an impossible upper bound: it omits moving-GC,
finalizer, underflow, immortality and logging semantics. The whole granule
predicate is 7.99% of item311, so this cannot supply the global factor; the old
container-wide managed ABI remains denied.

Second, an equal-output 4.44MB text scan measured:

```text
native splitlines object path            0.02s / 0.330B instructions
semantic-int raw byte loop               0.39s / 5.639B
freestanding pcc.i64 raw byte kernel     0.29s / 4.342B
```

The freestanding result is a host-pcc0/self-backend oracle, not pcc1. It shows
that raw ownership lowers memory but a per-byte Python-authored scan is 13x
more instruction-heavy than the existing bulk runtime helper. Connecting an
intermediate span arena back to the semantic parser would repeat V22/V25's
denied second decode/copy boundary. No parser source changed.

Third, the current flamegraph puts every `CompilerIntArena` method at only
1.50% inclusive; converge/or/zero/copy and liveness state-word batch helpers
are each 0.01--0.23%. Intrinsicizing them alone has no 5% ceiling. V85's
per-word raw plane remains denied; any root-state proposal must fuse the full
state/transition/location lifecycle, not one method.

No production source changed in this update. Full sizing receipt:
`docs/goal/evidence/PERF-P0-PROVEN-NATIVE-DATA-PLANE-PARALLEL-EMIT/004-provenance-raw-span-sizing-denials.md`.

## Update No.86 — source-scoped import-scan cache `[pending]`

The accepted No.72 whole-stage transfer completes GC0 Stage2 in 566.528s
against same-source Stage1 274.56s (2.063x). Stage2 CPU is 2722.99s versus
1078.93s. Phase comparison identifies frontend worker execution (+80.765s)
and native emit (+121.943s) as the two large deltas; the 218-versus-40 frontend
chunk difference is intentional one-module native-worker isolation retained by
No.46, not a scheduler bug.

The current Stage2 process tree peaks at 18.532GB. Root pcc1 itself reaches
6.991GB and is already 6.5--6.8GB before the codegen worker wave. A
frontend-only MallocStackLogging capture has 6.3GB physical footprint: 4.4GB
of early VM regions lack stacks, while the corrected byte-weight call tree
attributes 1.863GiB. The dominant overlapping paths are source import/closure
scans: `_without_type_checking_imports` 44.4%, relative/multi closure 29.1%,
native-extension provider expansion 24.2%, and import-spec scanners about
21%. The flamegraph tool first had to be fixed separately: it had labelled
allocation counts as bytes.

Direct call-count instrumentation on the real 218-module closure is
deterministic and confirms repeated work:

```text
_without_type_checking_imports calls       1510
total source bytes scanned            50,044,882
distinct source texts                        221
unique source bytes                     7,216,809
byte amplification                          6.93x
callers: source-absolute 654 / extension 430 / package-target 426
```

### Proposal No.86

Create one local path-keyed scan cache inside
`_prepare_multi_source_compile_closure`. Retain raw source, ordinary
TYPE_CHECKING-masked source, and the package-only AttributeError-import masked
variant under separate short path keys. Thread that explicit cache through the
multi-closure, required provider, recursive stdlib and native-extension
passes. Prepared-source scanners must not mask a second time. The cache dies
when closure preparation returns; a later top-level compile rereads files.
There is no global, id-keyed, mtime-only or cross-build cache.

The fail-first two-module test observed four masks per source. It now observes
exactly two and proves a second closure call sees modified file contents.
Current real-closure sizing is 433 masks / 14.328MB, reducing calls 71.3% and
byte amplification 6.93x -> 1.98x while retaining exactly 218 modules.
Import/closure/AST-reuse gates pass 46 with one explicitly frozen-baseline-red
`textwrap` policy test deselected; the No.72 source fails that node identically
because policy now says textwrap has a compiled provider while the stale test
still expects exclusion.

Before a full Stage2, build one source-frozen pcc1 differing only in
`pipeline_dependency_closure.py`, then run frontend-only `--emit-llvm` with
caches off. Require rc0, the same 218 modules / 1090 actions / summary graph,
combined `collect_multi_source_relative_closure + expand_recursive_stdlib`
<=12.5s versus 20.922s, largest process <=5GB versus 6.991GB and tree peak
<=14GB versus 18.532GB. Any changed module set, fallback, stale-read test,
pcc1 closure error, or missed resource line denies/removes before Stage2.

### No.86 result `[DENIED; removed]`

The candidate reduced the real closure's type-mask calls 1510 -> 433, scanned
bytes 50.0MB -> 14.3MB and amplification 6.93x -> 1.98x with the exact same
218-module closure. Focused gates pass 46; one `textwrap` node is explicitly
excluded because frozen No.72 fails it identically and current policy now
admits a compiled provider.

Source-frozen Stage1 differs only in dependency closure and is supporting
positive signal: wall 274.56 -> 264.09s, CPU 1078.93 -> 1049.58s and
instructions 177.341B -> 167.262B. It is one construction per arm, not paired
performance evidence.

The required frontend-only pcc1 run breached both resource lines and was
stopped before an output/profile could be published:

```text
                             baseline      candidate
largest process                6.991GB        5.715GB
process tree                  18.532GB       16.640GB
registered line                 5.0GB         14.0GB tree
```

Root improves 18.2% and tree 10.2%, but post-observation threshold movement is
not allowed. The interrupted run has returncode -15, no compiler.ll/profile,
and no surviving child; no Stage2 ran. Source and the candidate test were
forward-removed, restoring dependency-closure SHA `19dd5751...` exactly.

The result confirms the mechanism and its limit: fewer allocations do not
unmap the long-lived coordinator's allocator high water. The next proposal
must run closure/import scanning in a short-lived native pcc1 worker and return
only a deterministic path/module manifest; process exit is the safe reclaim
boundary. Full receipt:
`docs/goal/evidence/PERF-P0-PROVEN-NATIVE-DATA-PLANE-PARALLEL-EMIT/005-import-scan-cache-denied.md`.

## Update No.87 — short-lived native closure worker `[pending]`

No.86 proves that reducing repeated scans is real but cannot reclaim the
long-lived coordinator's already-mapped allocator slabs. The next proposal
uses the same process-lifetime mechanism that accepted summary workers use.

Reuse the existing V4 frontend worker manifest and hidden
`--pcc-python-multi-codegen-worker` entry. Two explicit job kinds,
`closure-recursive` and `closure-shallow`, carry the existing seed paths,
module names, libpython mode and scaffold mode. The worker executes
`_prepare_multi_source_compile_closure` with a local scan cache, performs the
existing package-site no-libpython validation, writes one deterministic
`CLOSURE/ITEM` path-module wire, and exits. Parent parsing rejects ERR, wrong
count/order/index, duplicate module, non-absolute/missing path and malformed
records. Worker failure is fail-closed; there is no in-process retry after a
native worker starts.

Only a one-element native executable prefix enables the path. Host/source
Python keeps the historical in-process closure. Thus Stage1 is not
handicapped and pcc1 remains the execution owner; no host helper is introduced.
The scan cache is optional and enabled only inside the short-lived worker, so
the denied long-lived parent shape is not reintroduced.

Fail-first worker-owner/parent-wire tests are green 4/4; native-only routing,
source freshness and explicit cache tests are green. The worker/closure/
pipeline packet is 116 passed. The complete real multi-file semantic gate is
41/41 in 99.25s. Standalone closure failures in parallel/pipeline are frozen-
baseline-equal (missing CalledProcessError provider / multi-source library
shape) and are therefore covered by the multi-file gates rather than relabelled
as new failures.

Before a full frontend or Stage2, build one source-frozen pcc1 differing only
in dependency closure, pipeline, frontend-parallel and worker-execution. A
four-module/two-level re-export executable canary must run with
`PCC_HOST_PYTHON=/bin/false`, record `multi_frontend_closure_worker=1`, print
`42`, link only libSystem and leave no child. Then run the same frontend-only
full compiler gate as No.86. Require rc0, 218 modules / 1090 actions / exact
summary counts, root <=4.5GB, tree <=14GB, and closure-worker phase <=25s.
Only that result may authorize Stage2.

### No.87 result `[DENIED: scan cache; isolation mechanism retained for No.88]`

The frozen candidate contains 1,137 files and differs from accepted No.72 in
exactly the four declared closure-worker files.  Its pcc1 is
`4ec83a1839a858983ebe8915d6ece6d9f9bb4484d9c296eb4413afbe30a6d9d9`,
uses CPython 3.15.0rc1 as the pcc0 host, reuses runtime archive `624e1de9...`,
and links only libSystem.

The four-module/two-level function re-export canary proves the new execution
boundary.  With `PCC_HOST_PYTHON=/usr/bin/false` and the final link explicitly
labelled as the system-cc oracle, pcc1 records
`multi_frontend_closure_worker=1`, compiles four summaries / twenty actions,
and produces a libSystem-only program that prints `42`.  The pcc-owned Mach-O
driver cannot be combined with the false host variable because that transition
driver is itself a Python script; the first failed harness receipts are retained
and are not attributed to the worker.

The required cache-off frontend-only gate completes, preserves the exact
218-module / 1,090-action / 218-summary graph (4,738 nodes / 7,801 edges), and
cuts resource peaks below both lines:

```text
return code / status             0 / COMPLETE
outer wall                       179.753s
largest process                  3.082GB       (line <=4.5GB)
process tree                    11.648GB       (line <=14GB)
closure-worker phase             32.897s       (line <=25s; FAIL)
leftover children                       none
```

The output IR is expected to differ because it compiles the four changed
compiler source modules; closure membership, action count and summary graph are
the registered semantic comparison and remain exact.  The cache is therefore
denied in the environment it was meant to optimize: CPython-host sizing and
Stage1 construction had been positive, but pcc1's generic dict/string data
plane makes the cached worker 57% slower than the retained 20.922s in-process
closure reference.  No threshold is moved and no Stage2 is authorized.

Full receipt:
`docs/goal/evidence/PERF-P0-PROVEN-NATIVE-DATA-PLANE-PARALLEL-EMIT/006-closure-worker-cache-denied.md`.

## Update No.88 — isolation-only native closure worker `[pending]`

Keep the proven short-lived native worker, deterministic manifest validation,
native-only routing and fail-closed error propagation.  Remove the scan-cache
API and execute the unchanged accepted dependency-closure implementation inside
the worker.  This isolates allocator high water without paying pcc1's expensive
dict/string cache operations.

Before another Stage2, restore `pipeline_dependency_closure.py` byte-for-byte
to accepted No.72, rebuild one frozen pcc1 differing in exactly pipeline,
frontend-parallel and worker-execution, then rerun the same canary and complete
frontend-only gate.  The unchanged registered lines are: exact 218 modules /
1,090 actions / summary graph, largest process <=4.5GB, tree <=14GB and closure
worker <=25s.  Any miss denies/removes the worker before Stage2.

### No.88 result `[DENIED; worker removed]`

The dependency-closure implementation was restored byte-for-byte to accepted
No.72 SHA `19dd5751...`; the new frozen pcc1 therefore differs in exactly the
three isolation/worker files.  Its SHA is `708bc1c7...`, its Stage1 receipt is
263.63s / 1,053.04 CPU-s / 177.500B instructions, and it remains CPython
3.15.0rc1, GC0, self/no-libpython and libSystem-only.

Focused owner/dependency/pipeline tests pass 102/102, the complete multi-file
semantic gate passes 41/41, and the no-host system-cc-oracle canary again
records one closure worker, four summaries / twenty actions, prints `42` and
links only libSystem.

The complete frontend-only result has no compiler error but fails three
registered conditions:

```text
return code / status             0 / COMPLETE
outer wall                       157.763s
modules / actions                218 / 1,090
summary nodes / edges            4,737 / 7,797  (registered 4,738 / 7,801)
largest process                  5.227GB         (line <=4.5GB; FAIL)
process tree                    13.837GB         (line <=14GB; pass)
closure-worker phase             30.800s         (line <=25s; FAIL)
leftover children                       none
```

Together No.87 and No.88 bound this design: caching gives 3.082GB process /
11.648GB tree but 32.897s closure; removing the cache gives back only 2.097s
while process high water rises to 5.227GB.  Neither point satisfies the
predeclared correctness/performance/resource envelope.  No Stage2 ran, and the
closure-worker production/test surface is forward-removed before selecting a
new proposal.

Full receipt:
`docs/goal/evidence/PERF-P0-PROVEN-NATIVE-DATA-PLANE-PARALLEL-EMIT/007-isolation-only-closure-worker-denied.md`.

## Update No.89 — packed call/argument span lane `[pending]`

The receipt-bound No.72 item311 flame graph is a current-binary profile, not a
stale symbol attribution: manifest compiler SHA `ebde05bb...` equals the
accepted pcc1, the folded sample is newer than that binary, and the input SHA
is fixed.  Of 9,240 on-CPU samples, `parse_self_backend_module` owns 4,997
(54.08%), `_parse_call_instruction` 2,302 (24.91%), and
`_parse_call_args_into_plane` 906 (9.81%).  The whole machine-code emit side is
3,352 (36.28%).  This is a material owner.

Two historical constraints rule out superficially similar work.  No.13 proves
`py_func_call_kwargs` is an ancestor hidden by tail-call elimination rather
than a call-parser child.  No.83/No.84 prove that promoting the structural
fallback or adding another substring-based scalar prefilter merely relocates
work; No.84's real 1.0416x win was removed on its registered 1.05 line.  No.83
explicitly permits a future lane only if it obtains canonical return/call data
without the type-prefix reparse.

No.89 is that distinct lane.  For the compiler's confident canonical call
shape, scan the original already-stripped instruction using integer start/end
positions and `ord(text[index])`, which the Python frontend lowers directly to
`py_str_ord_at_i64` without allocating one-character strings.  Do not create a
sequence of `fast_rest` substrings and do not build a split list for call
arguments.  Materialize only the final destination/callee/type/value texts
that the authoritative `IndexedCallPlane` retains, and append its packed
records directly.  Quoted/Unicode names, aggregate returns, nested constant
expressions, call attributes, malformed input and the diagnostic object
projection all retain the current regex/structural fallback unchanged.

Pre-registered gates before any full Stage2:

1. Compare the span lane against `_CALL_RE` plus the current parser across
   every call-bearing frozen Stage2 shard (currently 2,678,736 calls): exact
   acceptance/fallback and exact normalized packed call/argument fields.
2. Add focused simple/indirect/vararg/metadata plus quoted, aggregate, nested
   and malformed fallback regressions; run parser, call-flag, indexed-kernel,
   verifier and precise-stackmap focused gates plus strict parser closure.
3. A self-contained accepted-pcc1 driver must produce identical output and
   improve wall/CPU/instructions by at least 1.20x before one frozen build.
4. A source-frozen candidate may remain only if three alternating item311
   pairs emit byte-identical `ff943e10...`, median wall and CPU are >=1.05x,
   instructions improve and footprint is <=1.02x.  Any miss forward-removes
   the span lane before Stage2; no threshold moves after observation.

### No.89 result `[CONFIRMED]`

The final host differential covers all 416 call-bearing frozen items and all
2,678,736 regex-accepted calls.  The span lane accepts 2,624,882 (98.0%),
explicitly leaves 53,854 to the old parser, and has zero mismatch across
destination, return type, callee, indirect bit, argument types/values,
fixed-count, vararg and alignment.  Focused parser/type/call-flag/kernel/
unreachable/stackmap gates pass 150/150; the host item311 assembly remains
`ff943e10...`.

An accepted-pcc1 self-contained driver first exposed an unsupported
`startswith(prefix, start)` bridge; replacing it with the generic
`ord(text[index])` literal-span comparison removed the stub.  The corrected
driver produces identical `8950000` output and improves 4.65 -> 3.23s wall,
4.63 -> 3.17s CPU and 69.643B -> 43.036B instructions.  Its 873MB footprint
versus 115MB baseline was retained as a rejection risk rather than hidden.

The source-frozen candidate contains 1,137 files and differs only in
`self_backend_parse.py`.  Candidate pcc1 `b0c6844f...` uses CPython 3.15.0rc1,
GC0, runtime `624e1de9...`, self/no-libpython and libSystem only.  After
balanced warmups, the three alternating pairs are:

```text
pair   wall B/C   CPU B/C   instructions C/B   footprint C/B   assembly
1       1.07629    1.07802       0.936046          0.728948      exact
2       1.07721    1.07814       0.936471          0.728936      exact
3       1.07485    1.07502       0.936380          0.728988      exact
median  1.07629    1.07802       0.936380          0.728948      ff943e10...
```

Every registered line passes.  The real workload resolves the micro memory
warning in the favourable direction: footprint falls 27.1%, not grows.  The
span lane stays.  This accepts one parser/data-plane slice; it does not yet
claim whole Stage2, fixed point or GC1--4.  Full receipt:
`docs/goal/evidence/PERF-P0-PROVEN-NATIVE-DATA-PLANE-PARALLEL-EMIT/008-packed-call-span-confirmed.md`.

## Update No.90 — make packed spans physically raw i64 `[pending]`

The fresh No.89 profile is correctly binary-bound (`b0c6844f...`) and exact-
assembly, but its immediate 6-second window is phase-specific: it covers early
parse rather than a whole-item percentage.  Within that actual parse window,
`_parse_indexed_scalar_call_span` is 1,732 / 4,540 samples and
`_append_simple_call_arg_spans` 326.  More decisive than the percentage, the
accepted-pcc1 auto IR contains repeated `int.tag.*`, object add/compare and GC
root paths for `position`, `index`, bounds and counts.  No.89 created an integer
data model but left its physical projection as semantic Python heap ints.

No.90 changes only that projection.  Import the explicit annotation marker
`pcc.i64` and type the private span helper parameters, returns and local
positions/counts as raw i64.  Their provenance is statically bounded: every
value is a string length, a `find`/`rfind` result checked before use, a subspan,
or a count bounded by that length.  Such a value cannot exceed signed i64 on a
machine whose address space holds the string.  User-visible Python `int`
semantics, parser fields, retained strings/types, diagnostics and fallback
remain unchanged; under host CPython the annotations still resolve to `int`.

Pre-registered gates before another full Stage2:

1. Focused span/parser/kernel/verify/stackmap gates and the complete 2,678,736-
   call differential remain exact; host item311 assembly stays `ff943e10...`.
2. Emitted IR for every new span helper has raw i64 parameters/locals and no
   `int.tag.*`, `py_int_from_i64`, `py_int_to_i64` or object arithmetic on the
   span loop.  Conversion at the final semantic/container boundary is allowed
   and must be named.
3. An accepted-No.89-pcc1 driver comparing semantic-int spans with raw-i64
   spans must produce identical output, improve wall/CPU/instructions by at
   least 1.10x and keep footprint <=1.02x.  This explicitly prevents accepting
   No.89's synthetic 7.6x footprint warning in another form.
4. Only then build one frozen candidate differing from No.89 in the parser.
   Three alternating item311 pairs require exact assembly, median wall and CPU
   >=1.05x, improving instructions and footprint <=1.02x.  A miss removes only
   the raw annotations before Stage2; thresholds do not move after observation.

### No.90 implementation prerequisite and pre-build evidence

The first raw annotation exposed a real generic projection gap rather than a
parser-local workaround: default boxed-int mode overrode explicit `pcc.i64` in
local storage, ABI parameters/returns, coercion, binary dispatch and the
forced-exact control-flow planner.  The implementation therefore touches the
parser plus the seven narrow owners of that projection.  Ordinary Python
`int` remains boxed/tagged and arbitrary precision; only exact `pcc.i64/u64`
names bypass those object paths.  Type inference admits dynamic values into a
raw annotation only for range-proven `len`, `ord(str)` and exact-str
`find/rfind`; an arbitrary `int`-returning function is still rejected.

After source-hash invalidation, all eight span helpers have raw i64 ABI/bounds
and zero `int.tag.*`, `py_int_from_i64`, `py_int_to_i64`, object arithmetic or
exact-int roots in their complete emitted bodies.  The final focused packet is
152/152, the complete frozen-call differential is unchanged at 2,624,882 hits
/ 53,854 fallback / zero mismatch, and host item311 remains `ff943e10...`.
Typed-int coverage is 48/49: 31 nodes before the first failure plus 8/8 and 9/9
remaining shards pass.  The excluded node is source-only and baseline-equal:
all current/No.89/No.72 `py_list.py` files have SHA `18dd98e...`; its regex
accidentally includes the following ordinary function and sees that function's
`_list_is_sane` call.

The originally registered accepted-pcc1 micro cannot activate a compiler
codegen change until that compiler is rebuilt.  A host-generated replacement
was also not a valid discriminator: its semantic arm is automatically unboxed,
while a standalone type-marker import is not a runtime package closure.  These
are harness limitations, not observed performance misses.  The pre-build
authorization is therefore the stronger actual-compiler IR-shape proof above;
the unchanged three-pair item311 line remains the first performance and memory
verdict after one build.

The OFF standalone fallback gate reaches one pre-existing failure after its
multi-file compile and aggregate fallback tests pass:
`pipeline_context` reports 481 actions versus baseline 441.  An independent
No.89 source/compiler control also reports 481 with identical action-site
counts, so No.90 did not create the +40.  The baseline is not raised and the
candidate snapshot starts from No.89, excluding unrelated worktree source.

### No.90 result `[DENIED; raw projection removed]`

The first frozen build failed before producing pcc1 because a runtime
`from pcc import i64` in the parser enlarged the bootstrap closure from 218 to
219 modules and exposed cascading unavailable-module diagnostics. This was a
candidate defect, not a reason to change the closure. Replacing that import
with a `TYPE_CHECKING`-only annotation restored the exact 218-module closure
while preserving the registered raw helper IR shape.

The repaired frozen candidate contains 1,137 files and differs from accepted
No.89 in exactly the parser plus the seven declared projection-owner files.
Candidate pcc1 `06df6da6...` was produced by CPython 3.15.0rc1 with GC0 and
runtime archive `624e1de9...`; it is self/no-libpython and links only
libSystem. Its Stage1 receipt is 275.13s wall, 1,135.88 CPU-s, 177.596B
instructions and 1.317GB peak footprint.

After balanced warmups, the three alternating item311 pairs against accepted
No.89 pcc1 `b0c6844f...` are:

```text
pair   wall B/C   CPU B/C   instructions C/B   footprint C/B   assembly
1       1.04056    1.03906       0.967777          0.999974      exact
2       1.03583    1.03591       0.968484          1.000000      exact
3       1.04059    1.04147       0.966892          0.999947      exact
median  1.04056    1.03906       0.967777          0.999974      ff943e10...
```

The raw projection is a real but sub-threshold win: instructions fall a
stable 3.2%, footprint does not grow, and all six assemblies equal
`ff943e10...`, but median wall and CPU miss the pre-registered 1.05 line. The
threshold is not moved after observation. All eight production files were
forward-restored byte-for-byte to accepted No.89 and the two candidate-only
typed-int tests were removed; the retained packed-call regression suite passes
7/7. No Stage2 or GC transfer was authorized.

Full receipt:
`docs/goal/evidence/PERF-P0-PROVEN-NATIVE-DATA-PLANE-PARALLEL-EMIT/009-raw-i64-span-projection-denied.md`.

## Update No.91 — deterministic block-range parallel AArch64 emission `[pending]`

The complete accepted-No.89 item311 capture starts 67ms after pcc1 launch,
contains 10,627 on-CPU samples, is bound to compiler `b0c6844f...`, completes
normally in 13.98s and emits exact `ff943e10...`. Its mutually exclusive
top-level split is 5,336 samples in prepare and 5,209 in prepared emission.
Inclusive owners are parser 4,463 (42.0%), dense function emission 2,218
(20.9%), stack-map planning 1,318 (12.4%), and the final pair pass 895 (8.4%).

The record inventory rules out function-level parallelism on this critical
input: it is one function containing 9,474 blocks and 59,984 instructions.
Existing Stage2 parallelism is already per object/module, so another function
worker would execute exactly one task. The parallel grain must be the frozen
block layout inside that one function.

### Proposal No.91

After stack-map planning, target planning and register allocation have frozen
all shared function state, split the dense AArch64 block layout into stable
contiguous ranges weighted by instruction count. Reuse the already-tested
`ordered_parallel_map`: every worker owns a local line list; results are joined
and flattened in range order; the lowest input-index failure remains the
diagnostic owner. Module symbols, kernel columns and packed stack-map records
are read-only during the worker phase. Global peepholes, target-final label
offsets, stack-map rendering and compact unwind remain serial after the exact
text order is restored.

The native emit pool must publish its actual concurrent process width through
`PCC_OUTER_PARALLELISM`, multiplied by any inherited outer width. Automatic
block jobs divide the CPU budget by that value, cap at four, and remain serial
for small functions. `PCC_SELF_BACKEND_BLOCK_EMIT_JOBS=off` is the exact
rollback; a positive value is a bounded override. This prevents eight safe
emit processes from each silently starting four inner threads, while allowing
the one/two-process oversized waves to use idle cores.

Pre-registered gates before any Stage2:

1. range planning covers every frozen block exactly once, uses no empty or
   overlapping range and is deterministic under skewed weights;
2. serial/off/auto/explicit multi-thread emission repeatedly produce exact
   assembly, with a test proving more than one thread actually owns a range;
3. native-pool commands publish the correct combined outer width for ordinary
   pools and each memory-admission wave; invalid configuration fails closed;
4. focused AArch64, indexed-kernel, stack-map, pool and strict self/no-libpython
   closure gates pass;
5. one source-frozen CPython3.15rc1 candidate differs only in the declared
   emitter/pool files. Against accepted No.89 item311, three alternating pairs
   require exact `ff943e10...`, median wall speedup at least 1.07x, candidate
   CPU and instructions no more than 1.10x control, and footprint no more than
   1.05x. The candidate's forced-serial arm must remain within 1.01x
   instructions of control. A miss forward-removes No.91 before Stage2.

This proposal does not parallelize parsing or stack-map dataflow and cannot by
itself close the Stage2/Stage1 ratio. It is selected because it covers the
14.8% dense-block subtree with an existing proven thread primitive, whereas
the previously denied pair-pass and transition-cache rewrites covered smaller
or non-scaling work.

### No.91 v1 prerequisite failure — eager optional mmap import

The first exact-two-file frozen candidate completed its CPython3.15rc1 main
build in 287.48s and produced libSystem-only pcc1 `d07e6e11...`, but the
mandatory `pcc1 --help` smoke failed before CLI dispatch. Direct execution
gave the complete first boundary:

```text
Traceback ... pcc/backend/macho_parallel.py, line 23
    import mmap
ImportError: No module named 'mmap'
```

The new AArch64 import made all of `macho_parallel` eager at compiler startup;
its deterministic thread map needs only `os`/`threading`, while `mmap` is used
only by the optional host/file-backed `write_mmap_output`. The correction is
not an mmap fallback and does not duplicate the parallel primitive: move that
one import into its owning function. The pcc1 native bytearray linker remains
unchanged. A source regression proves importing the ordered map no longer
requires a module-level mmap dependency; the focused parallel/emitter/pool
packet passes 22/22.

The failed binary, manifest and source snapshot remain retained. A new v2
snapshot must start again from accepted No.89 and differ in exactly AArch64
emission, native worker-pool propagation and the lazy optional mmap owner.
The performance and memory thresholds above do not move.

### No.91 result `[DENIED; fully removed]`

The exact-three-file v2 candidate built successfully under CPython 3.15.0rc1.
Pcc1 `5d10244a...` is self/no-libpython, reuses GC0 runtime `624e1de9...`,
links only libSystem and passes `--help`. Stage1 was 311.75s wall / 1,124.16
CPU-s / 178.251B instructions / 1.302GB footprint; versus No.89, instructions
rise only 0.37% and CPU falls 1.0%, so the slower one-shot wall is not
attributed to source.

The required pcc1 worker gate fails before producing assembly when block jobs
are `auto`:

```text
self backend emit worker failed: failed to start a Mach-O link worker
```

Source inspection closes the performance question without another build.
The shipped `pcc/py_stdlib/threading.py` contract says the default runtime runs
Thread targets synchronously, and the pcc-Python runtime implementation of
`py_threading_thread_start` directly invokes the callable on the current
thread; it has no pthread dispatch. Thus fixing the host-only `name=` Thread
constructor shape could make the path execute, but cannot parallelize it.
Enabling/implementing a truly threaded pcc-Python runtime is a distinct
runtime/GC project, not a local emitter fix.

The forced-serial arm independently misses its gate. It emits exact
`ff943e10...`, but the up-front weight/range scan raises instructions from the
adjacent No.89 warmup's 193.917B to 204.101B (`1.0525x`, required `<=1.01x`)
and footprint from 1.241GB to 1.257GB. No formal pairs or Stage2 are justified.

All three production files were forward-restored byte-for-byte to accepted
No.89; the original test files are restored, and a retained generic multi-block
determinism test plus the call-plane suite pass 8/8. The failed v1, successful
v2 and worker receipts remain evidence. Full receipt:
`docs/goal/evidence/PERF-P0-PROVEN-NATIVE-DATA-PLANE-PARALLEL-EMIT/010-block-parallel-denied.md`.

## Update No.92 — transfer function body lines without join/resplit `[pending]`

The complete No.89 profile puts `parse_self_backend_module` at 42.0% of the
worker. One representation boundary is unnecessary by inspection:
`_iter_function_defs` already owns every function body's `list[str]`, joins it
into one multi-megabyte string for its return tuple, and `_parse_blocks`
immediately calls `splitlines()` to recreate the list before doing any work.
No consumer observes the intermediate body string.

A source-free CPython3.15 discriminator used the exact frozen item311 bytes and
performed the same per-line strip/consume in both arms. Three alternating
pairs over 20 repetitions produced identical totals:

```text
arm                    pair1       pair2       pair3       alloc peak
join + splitlines      0.240488s   0.237605s   0.236700s   14,356,117 B
direct retained lines  0.108495s   0.107647s   0.107532s          432 B
input                  5,108,635 bytes / 97,286 lines
```

This is a host structural discriminator, not a pcc1 performance claim. The
accepted-pcc1 `-c` harness hit the known `__init__` wrapper boundary, and a
PATH compile exceeded the 90-second cheap-probe budget and was terminated with
no leftover process; neither failed harness is timing evidence.

### Proposal No.92

Change the private handoff only: `_iter_function_defs` returns
`(header_text, body_lines)` and transfers ownership of the already-created
line list; `_parse_blocks` consumes that list directly. Header joining,
function order, empty/comment filtering, diagnostics, reachability, ID order,
packed publication and downstream passes remain unchanged. Reset
`body_lines = []` after publication so later function lines cannot mutate an
earlier tuple.

Pre-registered gates before Stage2:

1. focused tests preserve multiline headers, empty/comment lines, multiple
   functions, unterminated diagnostics and exact parser/assembly output;
2. source shape contains no function-body `join` followed by block
   `splitlines`; parser/kernel/call-plane/stackmap focused gates pass;
3. host item311 remains exact `ff943e10...` and the complete call differential
   remains unchanged if exercised;
4. one source-frozen CPython3.15rc1 candidate differs from accepted No.89 only
   in `self_backend_parse.py`. Three alternating pcc1 item311 pairs require
   exact assembly, median wall and CPU at least 1.05x, improving instructions,
   and footprint no more than 1.02x. A miss forward-restores No.89 before any
   Stage2 or GC run.

### No.92 result `[DENIED; fully removed]`

The implementation changed only the private body handoff and passed 56 focused
parser/kernel/call/stackmap nodes, strict parser self/no-libpython closure and
exact host item311 assembly. Source-frozen pcc1 `104261e9...` was built by
CPython 3.15.0rc1 with the same GC0 runtime, self/no-libpython and libSystem
only. Its one-shot Stage1 improved 275.13 -> 265.07s and 1,135.88 -> 1,060.72
CPU-s, but instructions were essentially unchanged at 177.612B versus
177.596B; that construction is supporting signal, not paired evidence.

The receipt-bound pcc1 worker result does not transfer the host micro win:

```text
pair   wall B/C   CPU B/C   instructions C/B   footprint C/B   assembly
1       0.99925    0.99925       0.998758          0.983065      exact
2       1.00377    1.00454       0.998703          0.983079      exact
3       0.99775    0.99850       0.999175          0.983065      exact
median  0.99925    0.99925       0.998758          0.983065      ff943e10...
```

The candidate reliably lowers footprint 1.7% but does not improve wall, CPU or
instructions materially and misses the pre-registered 1.05 line. The pcc
runtime's split/join representation cost therefore differs from CPython's
14.3MB host allocation result. The threshold is not moved. The parser was
forward-restored byte-for-byte to No.89; retained generic multiline-body,
multi-block and call-plane tests pass 12/12. No Stage2 or GC run occurred.
Full receipt:
`docs/goal/evidence/PERF-P0-PROVEN-NATIVE-DATA-PLANE-PARALLEL-EMIT/011-body-line-transfer-denied.md`.

## Update No.93 — connect the pcc-Python pthread kernel, then parallelize frozen blocks `[pending]`

The full native stack-map subtree is not one fusible owner. Its mutually
exclusive pcc1 shares are root-state 3.26%, backward liveness 3.09%, reload
planning 1.31%, record publication 1.20%, origins/aliases 0.59% and scattered
runtime leaves. Root-state and liveness have opposite dataflow directions;
No.78 already proved transition replay removes only 1.52% instructions, and
the arena word-helper route is denied by No.85. No new stack-map cache/fusion
proposal is justified.

No.91 exposed a larger prerequisite and initially overgeneralized it. The
default pcc-Python archive intentionally selects a single-thread stub, but the
repository already contains and tests
`freestanding_thread_kernel_pthread.py`. An isolated current-source
`PCC_WITH_THREADS=1` pcc-Python archive owns `pcc_thread_start/join/STW`
without `pcc_threads.o`, and its raw pthread start/join/STW gate passes in
109.40s. The missing link is explicitly documented in `py_threading.py`:
high-level `Thread.start()` still invokes its callable synchronously even when
`pcc_threads_enabled()==1`.

### Proposal No.93

Port only the already-working C `PyThreadObject` bridge into its pcc-Python
mirror: conditional synchronous fallback, an owned start-handoff reference,
one C-ABI thread-main function, raw handle publication, safepoint-aware join,
is-alive state and detach-on-dealloc. The existing pthread kernel remains the
machine owner. Add `name=None` at the end of the minimal stdlib Thread shim so
the shared deterministic mapper's diagnostic keyword is accepted without
changing existing positional arguments.

Once the bridge is green, reapply No.91 with its serial overhead fixed: absent
or `off` returns directly without scanning block weights; only `auto`/positive
jobs build deterministic contiguous ranges. The native pool publishes actual
outer process width. All global plans/IDs/symbols/order stay frozen before
threads and all target-final passes stay serial after ordered merge.

Pre-registered gates before Stage2:

1. the isolated threaded pcc-Python archive retains raw pthread ownership and
   start/join/STW gates; default archive remains synchronous;
2. a high-level pcc-Python runtime program uses two Events so synchronous
   `Thread.start` deadlocks but real pthread start completes, then four Threads
   plus Lock produce the exact shared count; dropped user Thread references
   retain the start-handoff lifetime;
3. focused threading dispatch/TLS/root/update gates pass under GC0, plus block
   range coverage/failure/order and repeated exact item311 assembly;
4. one source-frozen CPython3.15rc1 pcc1 links an isolated threaded
   pcc-Python runtime and remains self/no-libpython/libSystem-only. Candidate
   `off` must emit exact assembly with instructions <=1.01x its same-source
   unthreaded-wrapper shape. Candidate `auto` must beat candidate `off` by at
   least 1.08x wall and accepted No.89 by at least 1.05x wall before formal
   pairs;
5. three alternating No.89-versus-threaded-auto item311 pairs require exact
   assembly, median wall >=1.05x, candidate CPU/instructions <=1.20x and
   footprint <=1.10x. Any correctness or performance miss removes the bridge,
   block emitter and pool changes before Stage2. GC1--4 remain deferred by the
   human's ordered gate.

Runtime investigation:
`docs/investigations/pcc-python-thread-object-pthread-bridge.md`.

### No.93 focused implementation evidence

The Thread-object bridge and deterministic block emitter are source-complete.
Final focused results are: runtime ownership/raw/high-level 3 passed in
113.54s; threaded/default high-level behavior 2/2; GC0 two-worker explicit
collection 2/2; exception TLS 2/2; block/pool/link/kernel 33/33; bootstrap
worker argv 12/12; both changed thread modules strict self/no-libpython closure;
and current host item311 off/4-thread exact `ff943e10...`.

One attempted compatibility bundle found `from threading import Thread,
Condition, Semaphore` produced no output with the pcc-Python default archive.
The identical flushed probe is also rc0/empty under accepted No.89 runtime
`624e1de9...`, so that is a pre-existing pcc-Python entry gap, not No.93. The
Thread shim was restored to its original signature and the mapper dropped its
non-semantic `name=` keyword.

The next allowed expensive action is one exact-four-file source-frozen pcc1
build using the final isolated threaded pcc-Python archive and
`PCC_WITH_THREADS=1`. No more source changes or runtime rebuilds may be stacked
before its off/auto worker verdict.

### No.93 v1 build receipt is invalid for threaded execution

The exact-four-file v1 build itself completed and smoked: pcc1 `d065eed1...`,
267.89s wall / 1,069.70 CPU-s / 178.213B instructions, final threaded runtime
`fdbabe79...`, self/no-libpython and libSystem-only. The receipt nevertheless
proves the build tool dropped the requested mode: its normalized environment
contains no `PCC_WITH_THREADS` key. Running that binary with multiple compiler
threads would combine a threaded runtime with compiler code emitted under
single-thread frontend rules, so no worker timing is permitted.

`run_pcc_stage1_build.py` now owns an explicit
`--with-threads {0,1}` parameter, defaults to zero, writes the chosen value into
the build/smoke environment and therefore into the signed receipt. Ambient
thread state remains excluded. Focused tool tests pass 2/2. The v1 binary and
receipt remain retained as invalid evidence; v2 must use the exact same
source/runtime and differ only in this explicit build-mode input.

### No.93 result `[DENIED; compiler/runtime candidate removed]`

The explicit-mode v2 receipt is valid. Source-frozen pcc1
`90298ddc...` contains exactly four production diffs, records
`PCC_WITH_THREADS=1`, links threaded pcc-Python runtime `fdbabe79...`, is
self/no-libpython/libSystem-only and passes `--help`. Stage1 is 277.42s wall /
1,140.65 CPU-s / 188.031B instructions / 1.489GB footprint.

The pre-registered worker discriminator decisively denies the transfer:

```text
arm                       wall       CPU       instructions     footprint   asm
No.89 baseline            13.39s    13.36s      193.782B        1.241GB    exact
threaded candidate off    21.76s    21.72s      314.119B        1.257GB    exact
candidate/base             1.625x    1.626x       1.621x        1.0127x
threaded candidate auto   >30s then rc1: all() argument is not iterable
```

Even perfect elimination of the profiled 14.8% dense-block subtree cannot
repay a 62% atomic/safepoint instruction tax. Auto also fails before assembly
on pcc1's `all(completed)` iterable boundary. Per the early rejection rule,
no formal pairs, Stage2 or GC1--4 ran and the `all()` gap was not patched merely
to continue a mathematically failed performance candidate.

The Thread bridge itself passed every focused runtime gate and remains a
ready, documented capability patch, but the combined proposal explicitly tied
its landing to compiler transfer. The bridge, emitter, pool and lazy-mmap
production changes were therefore forward-restored byte-for-byte to No.89.
The standalone investigation retains the patch design and green artifacts for
a later runtime-capability task. The claim-grade Stage1 tool's explicit
`--with-threads` mode is independent and retained.

Full receipt:
`docs/goal/evidence/PERF-P0-PROVEN-NATIVE-DATA-PLANE-PARALLEL-EMIT/012-threaded-block-parallel-denied.md`.

## Update No.94 — fork/COW block workers are not an owned pcc-Python boundary `[DENIED by capability audit]`

After shared-heap pthreads failed on their 1.62x atomic/safepoint tax, the only
parallel shape that could preserve the fast non-atomic runtime would fork a
single-threaded emit worker after parse/plan freeze and let children consume
copy-on-write kernel pages.

The current execution surface cannot make that claim:

- `pcc/py_stdlib/multiprocessing.py` explicitly says pcc ships no fork/spawn
  runtime yet;
- Darwin's owned process ABI is `posix_spawn`/pipe/waitpid. It starts a fresh
  executable and cannot inherit the parsed heap;
- the Linux unsafe spawn-pipe lowering contains an internal raw fork branch,
  but only to complete spawn/exec and does not expose continued Python
  execution in the child;
- there is no `pthread_atfork`, after-fork allocator/GC index reset, root/frame
  registry reset, lock-state repair or child-publication ABI;
- direct `fork()` occurrences are C test probes, not a pcc-Python capability.

Calling raw fork and continuing the inherited managed heap would therefore be
an unowned GC/allocator experiment. Using the supported exec boundary would
require serializing or reparsing the 5.1MB function kernel in every child,
reintroducing the dominant 42% parser and the denied object/wire adapters.
No code or process experiment is justified. This parallel route is `[DENIED]`
until a separate runtime task provides a specified after-fork contract.

Full receipt:
`docs/goal/evidence/PERF-P0-PROVEN-NATIVE-DATA-PLANE-PARALLEL-EMIT/013-fork-cow-capability-denied.md`.

## Update No.95 — accepted No.89 full Stage2 baseline and phase owner

After No.90--94 were removed, production pcc source is byte-identical to the
accepted No.89 snapshot. One source-frozen, cache-off, GC0 Stage2 ran under the
performance lock with 1-second process-tree sampling, 10 frontend jobs, 8
self/link jobs, explicit no-host runtime builder and no Stage3/GC matrix.

The run is complete and valid:

```text
stage wall / compile wall        598.629s / 583.515s
compiler profile                 583.088s
aggregate CPU                    2690.892s
process-tree peak                13,033,111,552 B
peak processes                   27
pcc2                             b23b322a... / runnable / libSystem-only
publish barrier                  15.081s / rc0
```

Against same-source No.89 Stage1 275.13s, Stage2 is 2.1758x wall (compile-only
is 2.1208x). The target remains far open.

The adjacent pre-No.89 summary-worker Stage2 was 578.301s stage wall and
563.141s compiler profile. Phase comparison prevents a false regression
attribution:

```text
phase                              summary control   No.89 current   delta
frontend codegen parallel             159.028s        180.518s     +21.490
  export/summary                        58.265s         69.924s     +11.659
  codegen workers                       91.696s        109.684s     +17.988
ensure runtime                          12.687s         19.404s      +6.717
native emit oversized                   28.140s         25.640s      -2.500
native emit safe                       210.530s        206.406s      -4.124
owned link driver                       85.332s         76.493s      -8.839
self backend IR-to-image               337.953s        331.753s      -6.200
```

No.89 improves the backend boundary it changes. The total wall increase is in
frontend/runtime phases that No.89 does not touch and is not evidence to remove
it. Current absolute owners are native object emission 254.675s, frontend
codegen 180.518s and owned link 76.493s. Even deleting the whole frontend would
leave roughly 402s, so no single phase can close the ratio.

The next cheap discriminator is a caller-attributed profile of one real frozen
frontend codegen worker from this completed run. Item311 already exhausted
emit-local routes; the frontend profile must identify a shared runtime/data
plane owner rather than another isolated helper.

Full receipt:
`docs/goal/evidence/PERF-P0-PROVEN-NATIVE-DATA-PLANE-PARALLEL-EMIT/014-no89-stage2-profile.md`.

## Update No.96 — exact frontend worker profile selects AST dispatch projection

The completed Stage2 retained all frontend artifacts. Worker 0 is the largest
real codegen item: assigned module 1 `pcc.cli_bootstrap`, 14,287,979-byte AST
wire and 19,279,474-byte IR. A private manifest changed only result/IR output;
accepted No.89 pcc1 replayed it in 15.376s at 2.322GB peak RSS and emitted IR
byte-identical to Stage2 SHA `065100ba...`.

An immediately attached, binary-bound flamegraph covers 12,217 on-CPU samples
from the exact pcc1. Worker structure:

```text
L1CodeGen.generate                         10,628   87.0%
  _generate_impl                          10,015   82.0%
    emit_user_function                     6,614   54.1%
      emit_stmts / emit_stmt_impl           7,7xx   63.0% inclusive
    module top init                         1,439   11.8%
    vthread analyses                          894    7.3%
    hoist nested functions                    505    4.1%
AST wire decode                               681    5.6%
native exports decode                         570    4.7%
type inference                                197    1.6%
LLVM-C Module string/render                   607    5.0%
```

Leaf aggregation makes the cross-phase and frontend-only costs distinct:

```text
leaf family                         frontend worker      item311 emit
granule_is_object_start                  9.97%               8.05%
GC load + store                          6.21%               9.98%
strs_eq + class_lookup_in_mro             9.30%               not top-level
```

The generic GC/provenance tax is shared but its global unchecked bypass is
already unsafe and denied. The new structural owner is compiler-internal AST
dispatch: `StmtDispatchLoweringMixin._emit_stmt_impl` and
`ExprDispatchLoweringMixin._emit_expr_impl` repeatedly classify a closed-world
dataclass/ADT node through generic Python class/MRO semantics.

Before code, audit the AST hierarchy and wire schema. A candidate is allowed
only if compiler AST classes are a closed, non-user-subclassed set and the wire
decoder can publish one stable dense kind ID per node. Ordinary `isinstance`,
user classes and diagnostic projection must remain unchanged. Measure the
real module-1 node/kind/check counts and pre-register a whole stmt+expr dispatch
slice; replacing one `isinstance` branch or adding an id-keyed cache is denied.

Full receipt:
`docs/goal/evidence/PERF-P0-PROVEN-NATIVE-DATA-PLANE-PARALLEL-EMIT/015-frontend-worker-profile.md`.

## Update No.97 — dense compiler-AST kind projection across stmt+expr dispatch `[pending]`

The AST audit satisfies the structural prerequisites. `py_ast.py` defines one
closed internal frozen-dataclass hierarchy; repository search found no runtime
subclasses of its Expr/Stmt concrete classes. The stable JSON wire already
carries exact `__pcc_py_ast_v1__` kind tags and its decoder is the authoritative
constructor. Foreign/duck objects remain supported by dispatch slow paths.

Frozen `pcc.cli_bootstrap` contains 32,718 Expr and 7,159 Stmt nodes: 39,877
core dispatch objects. The two dispatch modules contain 52 static
`isinstance` sites. Current code repeatedly pays class/MRO work even though the
wire already classified each node.

The physical field mechanism was tested before source edits. Host dataclasses
store a base `field(default=0, kw_only=True, compare=False, repr=False)` in the
instance without changing positional constructor ABI. Subclass field override
is rejected by pcc (`duplicate value for argument`), but a single base field
plus inherited `__post_init__`/subclass constant compiles and runs under the
self backend, printing the expected ID 7. Explicit pcc keyword override is
fail-closed; host `__post_init__` overwrites it.

### Proposal No.97

Add one private dense kind field to Expr and Stmt only. Each concrete subclass
publishes a unique integer class constant; the inherited post-init writes that
constant into the frozen instance. The field is kw-only with a default, is
excluded from repr/equality and is omitted from the wire schema, so existing
constructors and serialized bytes remain stable.

`_emit_stmt_impl`, `_stmt_kind_name` and `_emit_expr_impl` read the base field
once. For nonzero valid IDs they dispatch through integer comparisons and do
not call generic class/shape classification. ID zero or any object missing the
field follows the current `isinstance`/duck path byte-for-byte. Ordinary Python
classes and `isinstance` semantics are untouched; this is a compiler-private
projection on the typed AST.

Pre-registered gates before Stage2:

1. all concrete Expr/Stmt classes have stable unique nonzero IDs; instances
   store the field, equality/repr/wire bytes remain unchanged, replace/type
   inference preserves IDs, subclass inheritance stays on the matching semantic
   branch, and explicit spoofing cannot survive construction;
2. exact-node stmt/expr tests prove the fallback classifiers are not called;
   duck/unknown tests prove the old slow path still works;
3. focused AST wire/type-infer/codegen/class-schema gates and strict closures
   pass. On the frozen worker input, current and No.89 CPython3.15rc1 host
   replays must be byte-identical (`23c7fa96...` in the pre-build control);
   the candidate pcc1 replay must stay byte-identical to the accepted No.89
   pcc1 IR `065100ba...`, and item311 assembly stays `ff943e10...`;
4. one CPython3.15rc1 source-frozen pcc1 differs from No.89 in exactly nine
   files: `py_ast.py`, stmt dispatch, expr dispatch, `layer1_support.py`, the
   physical field contract, and the wire codec whose separate field table
   keeps `kind_id` out of serialized bytes, plus `pipeline_context.py` to keep
   unannotated dataclass class constants out of exported field/constructor
   schemas, plus the generic dataclass lowering fix that makes a derived
   dataclass call an inherited `__post_init__`, plus removal of the py_ast-only
   constructor bypass that bound positional arguments against physical slots.
   Three
   alternating largest-frontend-worker pairs require exact IR, median wall and
   CPU >=1.08x, improving instructions and footprint <=1.05x. Candidate
   item311 must remain exact. A miss forward-restores No.89 before Stage2.

### Implementation gate progress

The field-layout/wire split is now explicit: physical Expr/Stmt contracts add
the inherited `kind_id: int` slot, while the independent wire field table
omits it. The focused AST/wire/type-infer/dispatch packet passes 70/70. Direct
strict library closures pass for `py_ast.py`, `py_ast_contract.py`, and
`pipeline_ast_wire.py`; the direct leaf-mixin form is inapplicable because the
CLI auto-collects more than one package source, and the exact same command
fails identically on frozen No.89. The candidate Stage1 build is therefore the
strict full-closure gate for the three mixins.

The originally written host hash was a mode-label error: `065100ba...` is the
accepted native pcc1 worker IR, not host CPython IR. A source-frozen No.89 host
control and current candidate host replay both emit 26,635,233-byte
`23c7fa96d2dfb84fd8e40040313e855f0845599b1e4f246da8f060b578b4d911`
exactly. This corrects the oracle before the candidate pcc1 is built; it does
not relax the native pcc1 exact-output gate.

The first exact6 Stage1 attempt stopped after 89 seconds with
`missing required argument 'kind_id'`. The real export schema proves why:
declaring `kind_id = dataclasses.field(...)` makes it a semantic constructor
field, while the native-export wire cannot represent that `field(...)` call as
a default and correctly turns `has_default` off. It also exposes a second
correctness issue: the context builder currently mistakes unannotated
dataclass class constants such as `_pcc_kind_id` for instance fields.

The corrected exact7 design makes `kind_id` a compiler-private physical slot,
not a semantic dataclass field. Host instances receive it from inherited
`__post_init__` in their normal `__dict__`; the pcc physical class contract
allocates the indexed slot; repr/equality/wire and constructor ABI remain the
old semantic fields. `pipeline_context.py` will exclude unannotated class
constants from dataclass fields while preserving valueclass handling. A
focused export-schema regression must prove both `field_names` and generated
`__init__.call_sig` omit the constant before another Stage1 attempt.

The exact7 compiler built successfully in 267.11 seconds and linked only
libSystem, but its first native frontend worker failed before inference. The
smaller `pcc1 -c 'print(1)'` probe reported the lift boundary, and current-
binary disassembly established the layout rather than guessing: generated
`ExprStmt.__init__` writes `span` to slot 0 and `expr` to slot 2, but never
calls inherited `Stmt.__post_init__`, leaving physical `kind_id` slot 1 null.
This is a generic pcc dataclass inheritance defect: CPython-generated derived
dataclass initializers call an inherited `__post_init__`, while pcc currently
checks only methods declared directly in the derived body. Exact8 repairs that
generic lowering, with a compile-and-run inherited-post-init regression, before
building another pcc1.

Exact8 compiled in 342.49 seconds but the same `pcc1 -c 'print(1)'` gate still
failed. Disassembly and the host four-module lift stack identify the second
stacked defect exactly. `_extern_class_decl_plan` receives both the correct
physical `ExprStmt` fields `(span, kind_id, expr)` and the correct semantic
`__init__(self, span, expr)` export. `native_modules.py` nevertheless has a
py_ast-only `force=True` branch that bypasses `__init__` and binds positional
arguments directly against `ClassInfo.field_names`. It therefore writes the
lifted expression into `kind_id` and leaves `expr` null. Exact9 removes that
forced no-init path. The ordinary exported-init path preserves semantic
argument binding, and the exact8 generic fix makes the generated init populate
the physical dense slot through inherited post-init. A focused lift-stack IR
gate must show `_s_Expr` calling `ExprStmt.__init__` and no direct expression-
to-`kind_id` store before another pcc1 build.

## Update No.97 result — dense compiler-AST kind projection `[DENIED]`

The final exact9 candidate is correct on the scoped gates and emits the exact
accepted native worker IR, but it is slower. After balanced warmups, three
alternating largest-frontend-worker pairs measured:

```text
pair/order  wall B/C  CPU B/C  instructions C/B  footprint C/B  tree RSS C/B
1 B/C       0.98264   0.98259  1.00950           0.99951        1.05030
2 C/B       0.98785   0.98782  1.00910           0.99953        1.03759
3 B/C       0.98919   0.98789  1.00976           0.99950        1.01807
median      0.98785   0.98782  1.00950           0.99951        1.03759
```

All six outputs are the exact 19,279,474-byte `065100ba...` oracle. The
candidate misses the 1.08 line and regresses both CPU and instructions, while
footprint is neutral. It is denied before Stage2. The dense-kind path is
forward-removed. Two generic correctness fixes discovered during the slice —
derived dataclasses calling inherited post-init and unannotated dataclass class
constants staying out of exported instance schemas — remain independently
covered. A frozen candidate caller profile is the next diagnostic boundary;
do not propose another class-dispatch change until it explains this negative.

Full receipt:
`docs/goal/evidence/PERF-P0-PROVEN-NATIVE-DATA-PLANE-PARALLEL-EMIT/016-dense-ast-kind-projection-denied.md`.

## Update No.98 — shared post-hoist vthread analysis kernel `[pending]`

The No.97 postmortem proves the kind slot was populated but still paid generic
object-field/GC/unbox cost; class/MRO and AST-decode samples did not fall. Two
adjacent proposals are denied by existing/current evidence: runtime
`py_isinstance` already has exact-class pointer early return, and a per-Value
provenance flag would initialize 468,643 Values to serve only 58,297 checks / 
658 hits.

The next non-overlapping measured duplication is vthread effect analysis.
Compute owns 3.72% and immediate boundary classification 3.59%; both rebuild
the same post-hoist callable scope/binding/call/class evidence. Frozen host
instrumentation measures 92.4ms + 88.9ms, sees 11 real hoists, and produces
zero effects/rejects. No.98 shares the exact post-hoist evidence rather than
skipping analysis. The optional cache is identity-bound to the module and
native-export table; independent public callers remain unchanged, and
generator proofs reuse the same proof cache.

The registered pcc1 line is three alternating shared-versus-force-unshared
pairs with exact output, median wall/CPU >=1.025x, instructions <=0.98x and
footprint <=1.02x. Focused equality and once-per-callable counters precede the
single build. No Stage2 runs first.

Full proposal:
`docs/goal/evidence/PERF-P0-PROVEN-NATIVE-DATA-PLANE-PARALLEL-EMIT/017-no97-postmortem-no98-shared-vthread-analysis.md`.

### No.98 first-build correction

The first candidate pcc1 never reached measurement: its new cross-module
combined-analysis call caused strict lowering to replace
`GenerationLoweringMixin._generate_impl` with an unavailable stub. Breadcrumbs
and `PCC_DEBUG_STRICT_NOLIB_STUB` identified the first fallback as
`py_cpy_from_pcc_obj(self)`. No A/B or Stage2 ran.

The corrected source restores `generation_lowering.py` exactly to No.89.
Existing `compute_vthread_may_park_callables` publishes a one-shot cache inside
the vthread module, and existing `classify_vthread_park_boundaries` consumes
and clears it on exact module/export identity; the force-unshared control does
not publish. The vthread packet is 29/29, both changed strict functions have
real bodies, shared/unshared/No.89 host IR is exact, and item311 remains exact.
One corrected exact3 build is therefore the next allowed action.

## Update No.98 result — shared vthread analysis `[DENIED]`

The corrected v2 pcc1 ran both shared and force-unshared worker canaries with
exact `065100ba...` output. Three alternating same-binary pairs then measured:

```text
pair/order  wall U/S  CPU U/S  instructions S/U  footprint S/U  tree RSS S/U
1 U/S       0.78589   0.93873  1.00091           0.99999        1.00786
2 S/U       1.01119   1.00563  1.00000           1.00000        0.98353
3 U/S       1.51387   1.07975  0.99874           1.00001        1.02081
median      1.01119   1.00563  1.0000009         1.00000        1.00786
```

Opposite off-CPU delays dominate pair 1 and pair 3, but the clean pair 2 and
median CPU/instructions agree: no work was removed in practice. It misses the
registered 1.025/0.98 lines and is denied before Stage2. The vthread handoff is
forward-removed.

This is the second consecutive frontend metadata projection whose logical
work reduction was erased by generic object/cache cost. The next proposal must
start from a whole-Stage2 owner of at least 25%; do not schedule another
3--7% pass-local cache or metadata field.

Full receipt:
`docs/goal/evidence/PERF-P0-PROVEN-NATIVE-DATA-PLANE-PARALLEL-EMIT/018-shared-vthread-analysis-denied.md`.

## Update No.99 — direct `.pco` publication inherited the wrong concurrency owner `[CONFIRMED root cause; fix under gate]`

The 2026-08-31 kernel panic is now tied to one exact retained Stage2 tree, not
to independent benchmarks.  The surviving frontend manifest directory writes
native objects under `pcc2.pcc-pco.28786`; PID 28786 is the older pcc1 process
in `JetsamEvent-2026-08-31-060006.ips`, and all eleven pcc1 processes in that
report belong to coalition 1327.  The other ten were spawned in one second and
are the first frontend codegen wave.  All 225 codegen manifests exist and zero
worker result files exist, so no worker completed before the swap storm.

The V4 manifests preserve deterministic launch order.  Matching that order to
the monotonically increasing worker PIDs and the Darwin 16 KiB page count gives
the exact first wave:

```text
worker  module                                              PID    footprint
0       pcc.cli_bootstrap                                  35774    2.538 GiB
1       pcc.py_frontend.codegen.class_gen                  35779    2.220 GiB
2       pcc.py_frontend.codegen.unsafe_lowering            35784    8.747 GiB
3       pcc.py_frontend.type_infer                         35789    3.619 GiB
4       pcc.backend.self_backend_precise_stackmaps         35794   14.182 GiB
5       pcc.backend.self_backend_parse                     35799    6.220 GiB
6       pcc.cli_bootstrap_array_core                       35804    5.100 GiB
7       pcc.py_frontend.codegen.native_modules             35809   28.360 GiB
8       pcc.llvm_capi.ir                                   35813   26.699 GiB
9       pcc.py_frontend.pipeline                           35820   36.035 GiB
```

This identifies the regression introduced by the direct-native-object slice.
Before that slice, frontend workers stopped after frontend publication and the
later self-backend oversized/huge lanes applied their own width-one/two memory
policy.  Direct `.pco` publication moved indexed self-backend emit, assembly,
Section/Relocation authoring and native-object encoding into each frontend
worker, but retained `PCC_PY_FRONTEND_JOBS=10`.  The explicit numeric value
also disabled `auto_source_lanes`, so the three 27--36 GiB owners bypassed the
large-module serial lane entirely.  The format is not established as faulty;
the execution owner changed without transferring its memory scheduler.

The current correction keeps direct `.pco` ownership but changes the complete
boundary:

- compiled-native export and codegen classify source/AST sidecar size, run
  oversized modules serially and cap the residual lane at two;
- compiled-native self-backend auto width is two, independently of the
  Stage1-only eight-worker measurement, and automatic Mach-O link width is two;
- direct `.pco` workers no longer stringify/write LLVM IR that no consumer
  reads, and release frontend, assembly and authoring graphs before the next
  representation is allocated;
- ordinary bootstrap stages receive a Darwin resource preflight plus an 8 GiB
  aggregate/600-second external circuit breaker; safety-mode process-table
  failure kills the tree after a one-second bound rather than waiting through
  the old 5+20-second telemetry retry;
- receipts retain full argv and manifest paths for the largest process, so a
  future cap trip names the module without another diagnostic Stage2.

Focused scheduling, direct-pco execution and contextual zero-fallback gates are
green.  This update is not Stage2 memory proof: current source still needs one
hard-capped run.  A `MEMORY_LIMIT` result keeps this investigation active on
the named single worker; the cap and timeout do not move.

## Update No.100 — first capped Stage2 names the remaining owner; oversized assembler handoff is pcc1-green

The first source-frozen run after No.99 used pcc1 `ca8863b6...`, frontend
`auto`, self/link width two, cache-off GC0 and the fixed 8 GiB/600-second
guard.  It did not complete, and it failed safely rather than entering swap:

```text
status                         MEMORY_LIMIT
elapsed                        about 218 s
sampled tree peak              8,692,858,880 B
largest worker                 8,036,073,472 B
manifest                       worker_0.manifest
assigned module                pcc.cli_bootstrap
surviving compiler children    none
pcc2                           not produced
```

The sampler's 250 ms interval accounts for the bounded overshoot above the
8 GiB threshold.  Full argv and the manifest path were present in the terminal
diagnostic, proving the new attribution boundary works.  This is not a Stage2
correctness or performance result.

Existing controlled measurements explain the remaining single-process high
water: the same module's frontend-only worker is about 2.3 GiB and its
self-backend emit is about 4.6 GiB.  Freeing those graphs does not unmap the
compiled allocator's slabs, so constructing assembler Sections/Relocations and
encoded `.pco` in that same address space crosses the cap.  The follow-up keeps
direct indexed emit but transfers oversized assembly by path; process exit
reclaims the frontend/emitter heap before the link driver's short-lived
assembler produces `.pco`.  Safe modules still publish `.pco` directly.  A
versioned ordered input manifest preserves original module order across mixed
`ASM`/`PCO` inputs, and the driver reconstructs its object list by that global
index rather than grouping all assembly ahead of native objects.

Current frozen source `87e4bb10...` builds pcc1 `47621eac...` in 350.18 s;
the Stage1 process tree peaks at 4,847,337,472 bytes, the strong function canary
passes, and linkage is libSystem-only.  A pcc1-compiled two-module canary with
an oversized entry and safe sibling proves the actual mixed route:

```text
output                                      42
multi_frontend_codegen_oversized_assembly_handoff >= 1
multi_direct_assembly_modules               >= 1
multi_direct_native_object_modules          >= 1
multi_direct_ir_text_bytes                    0
process-tree peak                   392,855,552 B
link command                         --internal-input-manifest
```

The canary, real mixed driver executable, 60-node multi-file packet, 131
focused scheduler/link/sampler tests and 224-module contextual zero-fallback
closure are green.  A second full Stage2 has deliberately not started: the
first-run contract permits no automatic retry after `MEMORY_LIMIT`.  The next
authority transition is exactly one run of pcc1 `47621eac...` under the same
8 GiB/600-second limits; no threshold or concurrency changes are requested.

## Update No.101 — containment succeeds, throughput and Stage1 policy fail `[human-paused]`

The v10 source-frozen build makes two separate verdicts unavoidable.

First, containment is working.  Current pcc1 `b8fc70aa...` ran its Stage2 for
450.426 seconds, stayed below the hard limit at a 7,838,253,056-byte sampled
tree peak, and an explicit human interrupt persisted `INTERRUPTED`, killed the
whole process group and left no compiler child.  The checkpoint coordinator
completed its frontend in 138.888 seconds and exited before deferred workers,
so allocator high water no longer overlaps every worker wave.

Second, the performance design is not acceptable.  The retained lane plan is
1 serial, 6 paired-oversized, 8 heavy, 13 medium and 196 small modules, but
only 13 of 224 result files existed at the interrupt.  Repeating the unchanged
cold run is not a justified experiment: its critical path must be predicted
from the retained per-module inputs/results first.

The prerequisite Stage1 also exposed a policy regression that should have
stopped the work one build earlier.  Applying frontend jobs two to host
CPython produced only eight codegen chunks for 224 modules;
`multi_frontend_codegen_parallel` took 303.517 seconds of the 369.13-second
Stage1.  A Stage2 memory rule was incorrectly generalized to a different
execution owner.  The historical 130-second number was a projection, not a
measurement, while historical 199--212-second measurements used a different
parallel/cache contract.  Mixing those three categories delayed recognition
of a real current regression.

Status: human-paused.  No more compiler run is authorized from this exact
state.  The next source proposal must separate host Stage1 scheduling from
compiled Stage2 risk admission and must use the retained v10 manifests to
show a <=600-second, <=8-GiB schedule before another cold verification.

## Update 2026-09-05 — process-split scheduler and packed instruction plane `[CONFIRMED; investigation remains open]`

Later task-board slices supersede the paused v10 execution shape without
weakening its 8 GiB circuit breaker.  A fresh-process PIDX boundary prevents
frontend and emitter high-water overlap, one unified exact-memory scheduler
replaces serial lane waves, and independent Mach-O link width eight is proven
under the same aggregate cap.  The source-frozen v51 Stage2 completes in
595.457s / 2143.782 tree CPU seconds / 7.679GB sampled tree RSS; Stage1 is
163.05s.  Exact receipts are evidence 044--046 under
`PERF-P0-PROVEN-NATIVE-DATA-PLANE-PARALLEL-EMIT`.

The finite AArch64 instruction vocabulary is now carried as scalar records:
the complete retained 226-sidecar inventory has 21,124,702 structured and zero
fallback instructions.  Word emission is batched, two internal relocation-
record copies are removed by one-shot in-place index remapping, and the
native-object writer packs each section's relocations through one compiler
arena rather than one `struct.pack`/`bytes` object per relocation.  Six
representative PCOs remain byte-identical.  On frozen `py_ast`, the accepted
relocation slice changes pcc1 worker CPU 19.59->17.99s, instructions
296.459B->274.576B and tree RSS 1.817->1.681GB.  Full evidence and gates are in
evidence 047.

The next owner is measured, not inferred.  v54 phase counters show RSS at
decode/transport/assembly/encode of 155/1173/1329/1682MB, while final live
allocator requests are only 768MB against 1649MB mapped capacity.  Therefore
the investigation remains open on emitter object projection and phase overlap;
no scheduler-only change or whole Stage2 retry is justified until the same
representative worker loses materially more RSS/CPU.

### Per-function post-scan result `[DENIED]`

A bounded attempt converted instruction strings to scalar records after each
function rather than after the complete module.  Six representative PCOs and
101 focused tests were exact, but the adjacent pcc1 `py_ast` result regressed:
CPU 17.77->18.49s, instructions +2.44%, and tree RSS 1.681->1.859GB.  Transport
RSS itself grew 1173->1357MB because repeated per-function label/record scans
increase allocator churn; early `del` sites recovered at most 9MB RSS.  The
whole experiment was forward-removed, restoring pcc sources byte-for-byte to
accepted v54.  Full receipt: evidence 048.  Do not retry this post-scan shape.

### Quiescent decoded-module collection `[DENIED]`

The same v56 pcc1 compared an explicit GC-off control with releasing the
decoded module/transport shell plus one GC0 collection before assembly.  PCOs
were exact, but CPU regressed 17.99->18.32s and RSS was flat at 1.68GB.  More
decisively, allocator live bytes (555.214MB) and mapped capacity (1143.210MB)
did not change across collection.  This is not cyclic garbage waiting for a
phase collection.  The candidate was forward-removed; full receipt is evidence
049.  Next work must eliminate the producer's construction, not retry del/GC.

### Packed stack-map record arena and v58 Stage2 `[CONFIRMED; target open]`

`py_ast` carries 46,625 final 32-byte stack-map rows.  Replacing one
`Struct.pack`/`bytes` object per row with one native scalar arena and one blob
per function improves its pcc1 worker 18.01->17.13s CPU and
1.682->1.585GB RSS.  All 195 PCO items were replayed and byte-checked; rebinding
the admission floor from that complete population reduces the full PCO phase
160.355->105.146s.  Source-frozen v58 Stage2 completes in 544.963s / 1999.275
tree CPU seconds / 7.731GB, versus Stage1 164.88s.  It is a real 8.5% Stage2
wall improvement over v51, but the 3.305x goal gap remains.  Full receipt:
evidence 050.

### Existing summary override under 8 GiB `[DENIED]`

A same-v58 checkpoint requested summary width seven, but the existing API
correctly clamps it to `max_parallel=3`.  Export+summary moved only
74.577->73.422s (1.5%); checkpoint total was 121.302s versus 117.593s and peak
was 7.689GB.  No source change was made.  The proven full width-seven policy
remains 16-GiB-only.  Full receipt: evidence 051.

### Hybrid ASM plus packed stack-map sidecar `[DENIED]`

For `cli_bootstrap`, moving stackmaps out of ASM shrank text 61.1->39.9MB and
2.82M->1.34M lines, but same-v59 emit CPU rose 32.39->37.12s; host assembly
saved only 1.75 CPU seconds.  Combined wall was 43.68->44.05s and CPU
40.12->43.10s for only about 2% lower peak RSS.  The artifact mode and its
undefined-only object relaxation were forward-removed.  Full receipt: evidence
052.  Do not build the pair-publication protocol for this codec shape.

### Mixed ASM/PCO scheduler `[DENIED after Stage2 transfer]`

A stable floor-descending mixed replay completed all 227 frozen artifacts in
208.642s versus 227.190s for sequential ASM+PCO phases, with exact bytes and a
6.17GB peak.  Source-frozen transfer disproved that local result: v60 mixed
emit took 227.076s, and complete Stage2 was 560.184s / 2005.342 CPU seconds /
7.733GB versus accepted v58 544.963s / 1999.275 CPU / 7.731GB.  Contention
increased ASM worker duration enough to erase overlap.  The mixed scheduler
was forward-removed.  Full receipt: evidence 053.

This is the third consecutive adjacent miss (summary-width under 8 GiB,
hybrid stackmap, mixed phases).  Per the convergence guardrail, further
scheduler/codec variants stop here.  The next proposal must own a complete
cross-phase pcc1 object/value or GC-protocol family with a >=25% ceiling.

### Lazy verifier diagnostic projection `[DENIED]`

The architectural zoom-out's raw module-1 sample attributed 49.6% inclusive
time to indexed verification, so a complete candidate moved successful-path
SSA names and per-instruction diagnostic strings behind their exact malformed-
IR branches.  Verifier/diagnostic/record-inventory gates, strict closure and
the 227-module contextual gate passed; a source-frozen pcc1 was libSystem-only
and stayed below the 8 GiB breaker.  On the same retained 14.9MB `py_ast`
sidecar, however, v58 control versus candidate was 17.23s versus 17.20s CPU,
262.097B versus 261.464B instructions (-0.24%), and identical 1.575GB tree
RSS.  PCOs were byte-identical.  The apparent verifier owner is therefore
runtime/GC work below the Python frames, not diagnostic projection self cost.
The candidate was forward-removed, restoring production and test source to
accepted v58.  Full receipt: evidence 054.  Do not retry this spelling-laziness
shape; select the measured 50.4% GC/refcount protocol family instead.

### Handwritten packed verifier `[DENIED; mechanism confirmed]`

A raw-arena verifier prototype reduced the retained module-1 verifier caller
share from 43.6% to 3.9%.  Exact ASM improved 29.25->26.51s wall,
424.137B->381.963B instructions and about 6% tree RSS; exact `py_ast` improved
only about 2.4% instructions.  The physical implementation is denied: it grew
to 1,553 production lines duplicating the 1,522-line ordinary verifier, added
505KB to pcc1, and the non-adjacent Stage1 warning moved 164.88->171.53s /
673.69->693.17 CPU seconds.

Two code-converge rounds repeatedly found missing cross-plane rules (kind vs
metadata, payload vs use facts, text IDs, post-stackprep state, terminator
operands, PHI stamps and bounded switch work).  Fixing individual cases did
not solve the semantic-locality defect: every future verifier rule would have
two handwritten owners.  No Stage2 was run; the prototype was forward-removed.
Full receipt: evidence 055.  The measured owner remains valid, but the next
proposal must mechanically share one canonical rule source between packed
execution and ordinary diagnostic projection, with a registered raw-span lease
and compact failure-code replay.

### Borrowed integer spans across verifier helpers `[DENIED]`

A follow-up kept the ordinary verifier as the sole semantic owner and replaced
its packed-arena reads with borrowed three-word `CompilerIntSpan` aggregates.
It exposed and fixed a real independent correctness hole: cross-module
valueclass instance-method declarations hard-coded `self` as `ptr`, even when
the defining method used an aggregate receiver.  An aliased-import regression,
the complete export/valueclass focus set, the 227-module contextual gate and
self emission are green.  ABI-only v65 built a libSystem-only pcc1 in 163.24s /
669.65 tree CPU seconds.

The span transfer itself is denied.  On the same retained module-1 sidecar,
v58 versus v64 produced byte-identical 61,075,757-byte ASM, but wall regressed
29.43->31.55s, instructions 424.381B->454.622B (+7.1%), and sampled tree peak
4.536->4.666GB.  Moving nine multi-word spans through verifier helper calls
cost more than it saved at arena reads.  All span/verifier/inventory/test code
was forward-removed; the independent ABI fix remains.  No Stage2 ran.  Full
receipt: evidence 056.  Do not retry aggregate span transport; the next shape
must use a kernel-owned scalar lease or one-owner execution without duplicating
the verifier rules.

### Packed verifier CFG/dominator scratch `[DENIED]`

A second in-place rewrite removed the verifier's CFG dict, predecessor and
successor list-of-lists, dominator lists, PHI sets and switch sets using one
module-reused set of `CompilerIntArena` columns.  It kept one verifier rule
owner and passed the dense/100k-block dominator oracle, malformed diagnostics,
30 verifier/inventory tests, all 17 direct-indexed tests, the 227-module
contextual gate and self emission.  Source-frozen v66 was libSystem-only.

The representative pcc1 result is nevertheless negative.  On the same
module-1 sidecar, ABI-only v65 versus packed v66 produced exact ASM, but wall
rose 29.39->30.76s and instructions 424.123B->453.187B (+6.85%); sampled tree
peak fell only 4.536->4.494GB (-0.9%).  The arena method protocol cost exceeds
the removed container cost.  The experiment was forward-removed and no Stage2
ran.  Full receipt: evidence 057.  Do not retry arena-only container rewrites;
eliminate out-of-line scalar getter/setter calls generically first.

## Update 2026-09-06 — tuple guard transfer, complete-path owners, struct native decode `[CONFIRMED; investigation remains open]`

Two accepted runtime/provider slices moved the source-frozen Stage2 from
v80 566.617s to v82 474.406s with unchanged 8.03GB peak; Stage2/Stage1 is
3.05 -> 2.78 (Stage1 185.70s/187.21s/170.76s). Both landed under the
convergence guardrail after a fresh complete-path profile rather than as
adjacent helpers.

`RUNTIME-P0-TUPLE-PUBLICATION-NOOP-SCAN` (v81): both `py_tuple_set_item`
mirrors scanned the populated prefix after every store although
`pcc_gc_publish_initialized` is a no-op on GC0..3. Native `tuple([1]*N)`
was quadratic (1.224/4.825/19.238B instructions at 10k/20k/40k); after the
GC0..3 early return it is linear (397.7M/785.0M/1544.2M at 1M/2M/4M). Same-
compiler pre-fix control built through `cached_pcc_python_runtime(runtime_source=)`;
receipt-bound `py_ast` PCO replay 15.05 -> 12.00s user (-25.1%
instructions, exact PCO); Stage2 PCO phase 126.7 -> 98.5s. Evidence 002/003
under that row.

Complete-path profile on v81 pcc1 (`pcc_profile.py` + `pcc_flamegraph.py`,
`build/tuple-noop-scan-profile-v81/`): the GC protocol family is 60% of PCO
worker self time and 61% of the ASM worker; `_pcc_gc_granule_is_object_start`
is the top leaf (9.9% / 12.5%) because both refcount mirrors run
`_ptr_can_have_header` = `pcc_gc_pointer_is_managed` before every
incref/decref. By nearest compiler caller the PCO worker is 25.7% inside
`struct.Struct.unpack_from` (`native_object._SpanReader.unpack` 18.0% +
`precise_stackmap._take` 7.7%: the emitter validating its own output) and
9.3% inside the stack-map heapsort through `CompilerIntArena`
`get_unchecked`/`set_unchecked` method calls.

`PERF-P0-STAGE2-COMPLETE-PATH-OWNER-SLICE` slice 1 (v82): the compiled
`pcc/py_stdlib/struct.py` provider resolves each format into integer plan
rows and, only when pcc lowered `pcc.unsafe`, reads little-endian integer
fields in place from the `bytes` payload at
`abi_constant("object.bytes.data_offset")`. Importing the same offset from
the runtime port module made the cursor dynamic and produced a
`strict.nolib.stub` via `py_cpy_from_pcc_obj`; the closure check caught it.
Host and pcc1 parity with CPython are exact (74-line program, GC0..4).
`py_ast` PCO worker 11.73 -> 9.04s user, instructions -20.7%, RSS
1.053 -> 0.806GB, exact; nine PCO workers 59.19 -> 48.71s; ASM unchanged.
Stage2 v82: coordinator 124.1s, frontend 94.5s, ASM 116.5s, PCO 65.2s,
link 63.5s; compile CPU 1650.6 -> 1437.6s. Evidence 001 under that row.

Open: the provenance probe inside every refcount op is the measured
cross-phase owner (15-20% of each pcc1 phase) but is a deliberate fail-closed
net in both mirrors; removing or narrowing it is a design decision, not a
helper. The arena heapsort is the next mechanism-level PCO slice. The
coordinator (124s) and frontend-worker (94.5s) phases still need direct
pcc1 profiles. Stage3 is deferred by the human.

### Singleton-first provenance probe `[DENIED]` and native stack-map heapsort `[CONFIRMED]` (2026-09-06, v83/v84)

Hoisting the four immortal-singleton compares ahead of the granule walk in
`pcc_gc_pointer_is_managed` (both mirrors) was exact and gate-green but
measured negative on the receipt-bound `cli_bootstrap` ASM worker:
395.11B -> 407.70B instructions (+3.2%), 27.11 -> 27.44s user. Singleton
refcount traffic is too rare to pay four compares on every probe; the probe's
call count is the owner, not its per-call cost. Forward-removed; do not retry.

The stack-map heapsort now runs on raw arena words when
`CompilerIntArena.native_address()` is nonzero. The first kernel passed the
raw pointer through unannotated parameters and produced rc=0 but a non-exact
py_ast PCO: every changed `safepoint_id` was `control | 2**38` because the
frontend pins unannotated parameters and `pcc_gc_pin` writes the PINNED flag
into byte 12 of whatever it is handed (byte 12 of a 32-byte record is bit 38
of record zero's id word). Passing the address as an exact `int` and
converting inside each intrinsic argument fixed it. v82 -> v84 replays:
py_ast PCO 9.33/9.35 -> 8.51/8.50s user (-8.9% instructions), exact; ASM
unchanged. Source-frozen v84 Stage2 464.379s / 1368.4s compile CPU / 8.024GB
versus v82 474.406s / 1437.6s; PCO phase 65.2 -> 56.9s. Evidence 002 under
`PERF-P0-STAGE2-COMPLETE-PATH-OWNER-SLICE`.

The kernel's emitted IR also shows the deeper cross-cutting owner: `int`
locals live in GC slots, are reloaded through `pcc_gc_load_borrowed_ptr` and
bracketed with `pcc_gc_pin`/`pcc_gc_unpin` as if they were objects, so even a
pure integer loop pays the object protocol on pcc1. Together with the
provenance probe (about 11% of every phase by the diagnostic ceiling in
evidence 001) this is where the remaining Stage2/Stage1 gap lives.

Bounding note (same day): `py_int_*` runtime leaves are 4.3% / 2.0% / 2.2%
of self time on the PCO / ASM / frontend workers, so boxed-int arithmetic is
not a >=10% owner by itself; the int-specific share of the GC slot/root/pin
protocol is unmeasured and must be counted before a value-projection slice is
selected. The provenance probe (10-13% by diagnostic ceiling) remains the
largest measured owner and is a human design decision.

### Architectural owner: raw pointers are `TYPE_DYN`, so provenance is probed on every refcount `[CONFIRMED; vertical slice named]`

Complete-path attribution on v81/v82 pcc1 workers puts the provenance family
(`pcc_gc_granule_is_object_start` plus `pcc_gc_pointer_is_managed` and its
locked slow chain) at 17.6% (py_ast PCO), 21.5% (cli_bootstrap ASM) and
19.0% (frontend) of self time; the fused radix walk alone is 9.9-12.5% and is
hit-dominated (the locked slow chain is 4-6%). Its per-call cost is at the
floor (fused walk; reciprocal division denied earlier), so the owner is the
call count: `_py_incref_prepare`/`_py_decref_prepare` and `_ptr_is_instance`/
`_ptr_is_class` probe every operand because the frontend types every raw
pointer intrinsic result (`malloc`, `ptr_add`, `int_to_ptr`, `load_ptr`) as
`TYPE_DYN`. A raw C pointer is statically indistinguishable from an object,
so the runtime cannot trust any dyn operand; the denied
`py_incref_managed` ABI (2026-08-27, corrupted free list) and today's
stack-map kernel defect (a raw pointer through an unannotated parameter was
`pcc_gc_pin`'d, setting bit 38 of every safepoint id) are both this defect.

Measured ceiling of removing the probe from refcount ops: -10.2% CPU on the
py_ast PCO worker and -11.1% on the cli_bootstrap ASM worker (diagnostic
pcc1 `0dc59fdb...`, exact outputs). The cheap variant (singleton compares
first) measured +3.2% instructions and is denied.

Vertical slice (fail-closed, contract-preserving): give the frontend a static
raw-pointer type. Intrinsic results, `c_ptr` extern returns and explicitly
annotated raw parameters carry it; a raw value flowing into an object/dyn
slot, parameter, return, container or comparison is a compile-time
capability diagnostic unless converted through `ptr_to_int`; raw locals live
in plain (non-GC) slots with no pin/incref/store_root protocol. Once the
compiled closure and the compiled stdlib providers pass that rule, the
refcount prepare paths in both mirrors can drop `_ptr_can_have_header`
exactly, keeping `pcc_gc_pointer_is_managed` for the extension/C-API boundary
where foreign pointers legitimately enter. Adjacent probe micro-slices
(radix cache, power-of-two shift) are not selected: two consecutive
sub-threshold candidates already stand and the guardrail requires the owner.


## Update 2026-09-06 (evening): raw-pointer static typing landed; probe removal DENIED

Phase A (raw addresses typed `int` in application modules, `c_obj`/`c_rawptr`
extern markers, `__pcc_runtime_port__` pointer-lane directive) landed with
green focused/GC0..4/fallback gates; Stage1 v85 CPU 751.5 s vs 747.6 s (flat).

Phase B (skip `pcc_gc_pointer_is_managed` in incref/decref and class checks on
GC0..2) is **[DENIED]**: pcc1 crashed in Stage2 (`pcc_allocator_take_small_object`
on a corrupted free list). A C-runtime diagnostic pcc1 with `PCC_DEBUG_RUNTIME=1`
counted, per tiny compile, 213 refcount operations on non-managed pointers:
22 on the `py_set_dummy` tombstone (1-byte static) via `pcc_gc_store_ptr`, and
about 190 `pcc_gc_release` calls from the compiler's own ownership-cleanup code
on libmalloc addresses with `malloc_size == 0` (stale releases the probe
absorbs). The probe therefore masks real over-releases in pcc1's compiled code;
removing it requires immortal-header sentinels and fixing those releases first.
Both mirrors keep the probe. The original September 6 session handoff is preserved
in the external recovery archive; see [current status](../status.md#evidence-and-recovery).
