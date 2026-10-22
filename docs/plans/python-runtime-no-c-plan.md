# Python Runtime No-C Plan

**Status:** active follow-on plan; pcc-Python no-C runtime archive gate
closed for the current runtime module set
**Start condition:** begins after the no-libpython bootstrap path is
stable enough that runtime source migration is no longer blocked on
basic Python frontend bring-up  
**Problem statement:** pcc-Python runtime modules are increasingly
ported to `pcc/runtime/py/`. The current migration gate is to keep
the normal no-libpython archive free of hand-written runtime C, while
leaving C sources available for cc-C / pcc-C comparison archives and the
optional compatibility fallback.

## Goal

Make the Python runtime source tree primarily authored in pcc-Python,
with no hand-written C required for the normal no-libpython bootstrap
path.

In this plan, "no C" means:

- no CPython or `libpython` dependency in the bootstrap-safe path
- no hand-written C implementation for runtime-high object semantics
- a minimal, audited low-level ABI surface that pcc lowers directly to
  LLVM IR or platform calls

It does not mean:

- no libc or platform ABI calls
- no LLVM/object/linker toolchain
- no unsafe operations in the runtime
- no optional CPython fallback for compatibility builds

## Current State

The runtime already has three distinct source modes:

- `libpy_runtime.a`: runtime modules compiled from C by the host C
  compiler
- `libpy_runtime_pcc.a`: the same C runtime modules compiled by `pcc`
- `libpy_runtime_pcc_py.a`: starts from the pcc-C archive, then
  replaces the runtime modules with pcc-Python ports, including the
  substrate object
- `libpy_runtime_pcc_py_libpython.a`: starts from
  `libpy_runtime_pcc_py.a` and adds only `py_libpython.o` for current
  compiler/bootstrap sources that still emit `py_cpy_*` compatibility
  calls

The current Python-port archive no longer keeps a hand-written C
runtime island. `py_substrate.py` defines the stable singleton,
sentinel, exception-table, cache, and helper symbols through
`pcc.unsafe` compile-time global-definition intrinsics and direct
platform/LLVM intrinsics. `py_substrate.c` remains in the source tree for
the cc-C and pcc-C runtime archive variants.

`py_libpython.c` is now only compiled into compatibility archives when
CPython fallback is requested. It is not part of the default
no-libpython cc-C, pcc-C, or pcc-Python runtime archives; bootstrap
compatibility builds use the pcc-Python runtime archive plus the single
`py_libpython.o` bridge instead of falling back to the all-C runtime.

This plan treats `py_libpython.c` differently from the others. It should
be removed from the pure bootstrap path by policy, not ported.

Current Phase 4c checkpoint:

- Python-port runtime archive builds with the current pcc-Python
  replacements.
- Phase 2 corpus passes in both default runtime mode and
  `PCC_RUNTIME_HIGH=py` mode: `53/53`.
- Phase 3 corpus passes in both default runtime mode and
  `PCC_RUNTIME_HIGH=py` mode: `49/49`.
- Phase 4 corpus passes under `PCC_RUNTIME_HIGH=py`: `39/39`.
- Multi-file/bootstrap shim gates pass with the focused no-libpython
  checks: `61/61`.
- LLVM C API parity/end-to-end gates pass: `17/17`.
- The pcc-Python runtime archive now replaces the small/cold C islands,
  the `py_int` boundary/dispatch/bignum helper slices, and
  `py_substrate.o`. Archive checks assert that
  `libpy_runtime_pcc_py.a` contains pcc-Python replacement objects such
  as `py_process.o`, `py_int.o`, and `py_substrate.o`; rejects the old
  hand-written C islands for this archive; and does not contain
  `py_libpython.o`.
- The Python pipeline defaults to the pcc-Python runtime archive for
  no-libpython programs. If a current bootstrap source still needs
  CPython fallback, it selects `libpy_runtime_pcc_py_libpython.a` so
  runtime modules remain pcc-Python-authored and only the compatibility
  wall is C.

## Design Principles

### 1. Remove fallback before removing C

The first architectural gate is no `libpython`, no `py_cpy_*`, and a
hard failure under `--python-libpython=off` when unsupported behavior
would otherwise fall through to CPython.

Do not spend effort porting `py_libpython.c` to pcc-Python. It is the
compatibility wall by definition.

### 2. Replace substrate helpers with intrinsics

`py_substrate.c` could not be mechanically ported while pcc-Python
relied on it for pointer arithmetic, raw load/store, singleton access,
TLS-shaped storage, and system calls.

The replacement is a small unsafe intrinsic layer, `pcc.unsafe`, whose
operations lower directly to LLVM IR or platform calls:

- `malloc`, `calloc`, `realloc`, `free`
- `memcpy`, `memmove`, `memset`
- typed raw loads and stores
- pointer add and pointer comparison
- external/global address access
- stable runtime storage definitions
- `write`, `getenv`, `setenv`, `unsetenv`, `access`

Because these are compiler-recognized operations, the substrate can be
authored in pcc-Python without creating a circular dependency. The
current exception slot is pcc-defined runtime storage for the existing
single-threaded gates; true platform thread-local isolation should be a
separate unsafe intrinsic if multi-threaded Python runtime execution
becomes a supported target.

### 3. Keep unsafe code explicit

Runtime Python files that use raw memory should import an explicit
unsafe module or intrinsic namespace. Ordinary user Python should not
gain silent pointer access.

The runtime may be unsafe. User Python should remain safe by default.

### 4. Prefer direct lowering over helper explosion

Adding many C helpers is a temporary migration tactic, not the final
shape. For hot operations such as refcounting, deallocation, sequence
walking, and dict/set probing, pcc should eventually emit direct loads,
stores, pointer arithmetic, and branches.

## Phases

### Phase A: Lock The Pure Path

Purpose: make the no-libpython boundary enforceable before porting more
runtime code.

Current status: closed for the current bootstrap gates. The pipeline
rejects generated `py_cpy_*` call sites when `--python-libpython=off` is
requested, and generated no-libpython `main` functions return `0`
directly instead of depending on the old `py_cpy_main_exitcode` stub.
Representative bootstrap tests assert the property directly so fallback
regressions are visible at the smallest gate.

Tasks:

- keep `--python-libpython=off` as the purity gate
- add or preserve tests that fail when generated IR contains `py_cpy_*`
- ensure bootstrap-oriented tests check linked binaries for no
  `libpython`
- document every remaining fallback as a blocker, not an invisible
  assumption

Exit criteria:

- representative self-host/bootstrap commands run with
  `--python-libpython=off`
- unsupported imports or dynamic behavior fail loudly instead of
  relinking `libpython`

### Phase B: Introduce `pcc.unsafe`

Purpose: create the low-level vocabulary needed to replace
`py_substrate.c`.

Current status: complete for the current pcc-Python runtime archive
replacement. The frontend recognizes `pcc.unsafe` imports as compiler
intrinsics and lowers raw allocation, free, pointer add, pointer
comparison/null/tag checks, typed load/store, `calloc`, `realloc`,
`memset`, `memcpy`, `memmove`, and `write` plus C-string literals,
`strlen`, `getenv`, `setenv`, `unsetenv`, and `access`; external pointer
global address/load/store operations; and compile-time global
definitions for storage symbols directly to LLVM IR or platform symbols
without emitting `py_mem_*` or `py_cpy_*` calls. New unsafe operations
should keep that direct-lowering shape rather than adding C helper
growth.

Tasks:

- define an internal `pcc.unsafe` API for raw memory and platform ABI
  operations
- teach type inference and codegen that these calls are intrinsics, not
  normal Python runtime calls
- lower each intrinsic directly to LLVM IR or a declared platform
  symbol
- add IR-level tests for each intrinsic
- keep the API unavailable or unsupported for normal user code until the
  safety boundary is deliberate

Exit criteria:

- a tiny pcc-Python file can allocate memory, store/load fields by
  offset, compare pointers, write to stdout, and access runtime storage
  without linking the C substrate object

### Phase C: Pythonize The Substrate Shape

Purpose: move the stable runtime globals and helper semantics out of C
source.

Current status: complete for the pcc-Python archive. `py_substrate.py`
owns the stable storage and exported substrate ABI symbols in
`libpy_runtime_pcc_py.a`; `py_substrate.c` remains only for the C
runtime archive variants.

Completed:

- rewrote singleton accessors in pcc-Python using external/global
  address support
- rewrote tagged-int pointer checks and null/pointer equality helpers
- rewrote exception class tables and caches in pcc-Python
- rewrote set tombstone storage and accessors
- rewrote environment and file-descriptor helpers through unsafe
  intrinsics
- added archive checks that `py_substrate.o` in
  `libpy_runtime_pcc_py.a` is the pcc-Python object defining the stable
  symbols

Exit criteria:

- the Python-port runtime archive no longer needs `py_substrate.c` for
  no-libpython programs
- object identity for `None`, `True`, `False`, and set tombstones
  remains stable across replaced runtime modules

### Phase D: Port Cold C Islands

Purpose: remove the remaining small C modules whose only blockers are
raw memory or formatting convenience.

Current status: complete for the cold C islands listed in this phase.
`py_obj_dealloc.c`, `py_os_path.c`, `py_exc_traceback.c`,
`py_print_fmt.c`, and the remaining `py_str.c` constructor are now
replaced by pcc-Python modules in the pcc-Python runtime archive; the C
versions remain for cc-C / pcc-C runtime archive variants.

Tasks:

- keep `py_obj_dealloc.py` covered by the active-runtime no
  `py_mem_*` / `py_subs_*` grep gate
- keep `py_os_path.py` covered by focused `os.path` corpus tests
- keep `py_exc_traceback.py` covered by a tiny C harness linked against
  `libpy_runtime_pcc_py.a`
- keep `py_print_fmt.py` covered by mixed native-scalar/object print
  corpus tests; it writes object-path output through `write` and flushes
  stdio first so codegen-emitted `printf` output stays ordered
- keep float formatting behind a clearly named intrinsic or a native
  pcc-Python dtoa implementation; native float print still bypasses
  `py_print` through codegen-emitted `printf`

Exit criteria:

- `libpy_runtime_pcc_py.a` replaces these modules with pcc-Python
  objects
- existing runtime oracle tests pass across cc-C, pcc-C, and pcc-Py
  runtime modes

### Phase E: Port Large Runtime Modules

Purpose: finish the runtime-high migration beyond the currently small
ported set.

Current status: complete for the current no-C pcc-Python archive module
set. The large `py_str` and `py_int` ports are split into Python modules
and replace the C objects in `libpy_runtime_pcc_py.a`; C sources remain
for the cc-C and pcc-C archive variants.

Completed:

- ported `py_int.c` in slices, starting with tagged-int fast paths and
  fixed-width operations before bignum slow paths
- ported `py_str.c` in slices, starting with accessors, concat, slice,
  search, and case helpers
- ported the remaining container and object-operation modules that were
  still built from C
- kept differential oracle tests for the ported module set

Exit criteria:

- the normal no-libpython runtime archive is built from pcc-Python
  sources plus compiler intrinsics, with no hand-written runtime C
  object files

### Phase F: Remove C From The Normal Archive

Purpose: turn the migration into an enforceable invariant.

Current status: complete for the runtime archive invariant. The
pcc-Python no-libpython runtime archive has no hand-written C runtime
object islands, and the Python pipeline now defaults to it for
no-libpython programs. Bootstrap builds that still need CPython fallback
use the pcc-Python runtime archive plus only `py_libpython.o`.

Completed:

- added archive checks that reject the known hand-written C islands from
  `libpy_runtime_pcc_py.a`
- kept a separate compatibility archive for CPython fallback builds
- added a pcc-Python-runtime compatibility archive that appends only
  `py_libpython.o`
- made the pcc-Python runtime archive the default runtime selection for
  bootstrap-safe Python programs
- updated this plan so "no C" for the normal pcc-Python runtime archive
  means no libpython and no hand-written runtime C object in that path

Tasks:

- continue removing `py_cpy_*` from the compiler/bootstrap source
  closure until full `pcc2` / `pcc3` can run with
  `--python-libpython=off`

Exit criteria:

- bootstrap-safe binaries link `libpy_runtime_pcc_py.a` by default
- compatibility bootstrap binaries link
  `libpy_runtime_pcc_py_libpython.a`, not the all-C runtime archive
- full `pcc2` / `pcc3 --python-libpython=off` remains the next
  frontend/source-closure purity gate, not a runtime archive blocker

### Phase G: Decide Integer Representation

Purpose: keep `py_int.c` migration from turning into dead code. This is
a parallel design track, not a prerequisite for Phase B, but it blocks
meaningful ports of the bignum arithmetic core.

Current status: implemented for ordinary Python modules. Plain Python
`: int` locals, module globals, user-function parameters, return values,
and class method parameters/returns are object-shaped by default:
small integers use the existing tagged-immediate `PyObject*`
representation, and overflow or large results fall back to heap bignums.
Modules that import low-level scaffolding such as `pcc.unsafe` or
`pcc.extern` remain on the raw fixed-width path so runtime ports and C ABI
bridges can still express machine integers deliberately. Internal
compiler/bootstrap modules under `pcc.*` and `bootstrap.*` also stay raw
until an explicit source-level raw integer type exists; this keeps the
self-host compiler path stable while user modules move to Python-int
semantics.

The codegen fast path now inlines tagged-small-int `+`, `-`, `&`, `|`,
and `^`. For `+` and `-`, generated IR checks both operands are tagged,
computes the untagged result, verifies the signed 63-bit tagged range,
and only calls `py_int_*` on the slow path. Range loops keep their native
`i64` induction variable and refresh the user-visible boxed target each
iteration, preserving hot-loop shape without changing Python-visible
semantics.

Remaining performance work is incremental rather than a representation
blocker: add inline fast paths for more operators when the backend has
the right overflow/range intrinsics, and consider explicit raw integer
types for compiler/runtime hot paths that need machine arithmetic by
construction.

Tasks:

- keep the long-term representation as tagged-object Python `int` by
  default, with scaffold imports and internal compiler/bootstrap modules
  as the current raw-int opt-out
- keep range-loop induction native while storing boxed values into the
  Python-visible loop target
- extend inline tagged fast paths beyond `+`, `-`, `&`, `|`, and `^`
  only when the generated guards preserve Python semantics
- add corpus cases that observe bignum results through locals, function
  returns, comparisons, containers, and non-print call boundaries. Current
  coverage exists in `phase2/bignum_local_compare`,
  `phase2/bignum_tagged_add_overflow`,
  `phase2/bignum_object_boundaries`, and
  `phase2/bignum_user_function_boundaries`.
- keep the optimized Knuth-style divmod rewrite as a separate
  performance task; the current Python implementation favors no-C
  archive purity and correctness over low-level quotient estimation

Exit criteria:

- a bignum-producing expression remains semantically correct when stored
  in a local, returned from a function, compared, inserted into a
  container, and passed through a user function call
- tagged-add overflow at the 63-bit boundary falls back to bignum instead
  of wrapping or producing an invalid tagged pointer
- the chosen strategy has a documented and implemented fast path for
  common small integer arithmetic

## Acceptance Gates

Minimum gates:

- no `libpython` dependency in bootstrap-safe binaries
- no generated `py_cpy_*` calls under `--python-libpython=off`
- runtime oracle parity across cc-C, pcc-C, and pcc-Py modes while the
  migration is in progress
- no hand-written C object files in the final no-libpython runtime
  archive

Stricter gates:

- stage 2 and stage 3 bootstrap outputs are byte-identical or
  structurally identical after stripping nondeterministic metadata
- the self-hosted compiler can compile representative C programs
- runtime performance remains within an agreed factor of the C runtime
  on object allocation, list/dict operations, string operations, and
  exception handling

## Risks

- The substrate replacement now depends on `pcc.unsafe` compile-time
  global-definition intrinsics. Regressions there can break stable
  symbol addresses across the whole runtime archive.
- The current exception slot is runtime-global storage, matching the
  existing single-threaded validation gates. Real multi-threaded Python
  runtime execution still needs a deliberate platform TLS intrinsic.
- Exposing unsafe pointer operations to normal user Python can lock in a
  bad public API.
- Porting cold formatting paths before the pure path is locked can
  consume time without improving self-host purity.
- Float formatting can become a disproportionate project if `%g`
  compatibility is required immediately.
- Removing C source does not remove the need for a platform ABI. The
  plan should not hide libc, linker, or LLVM dependencies.

## Recommended Order

1. Closed: no-libpython gate for representative runtime binaries.
2. Closed: `pcc.unsafe` intrinsics needed by the runtime ports.
3. Closed: substrate semantics rebuilt in pcc-Python on top of
   intrinsics.
4. Closed: small cold C islands replaced in the pcc-Python archive.
5. Closed for the current archive: large runtime modules ported in
   slices.
6. Closed for runtime selection: the no-C runtime archive is the
   bootstrap-safe default, and compatibility bootstrap builds keep it as
   the base with only `py_libpython.o` added.

## Execution Queue

This queue is the concrete task ordering from the current tree. Keep
each task paired with a no-libpython test that scans generated IR for
`py_cpy_*` calls and rejects accidental `py_mem_*` helper growth when
the purpose is direct lowering.

### B1: Unsafe Raw Memory And Platform ABI

Status: complete for the current runtime ports.

Completed:

- raw allocation/free: `malloc`, `calloc`, `realloc`, `free`
- raw C-string literals: `cstr("...")`
- raw memory operations: `memset`, `memcpy`, `memmove`
- typed offset load/store: i8/i32/i64/f64/pointer
- pointer operations: add, equality, null check, tagged-int check,
  tagged-int encode/decode
- platform calls: `write`, `strlen`, `getenv`, `setenv`, `unsetenv`,
  `access`
- external pointer globals: address, load pointer, store pointer
- compile-time global definitions: scalar storage, pointer globals,
  C strings, pointer arrays, null pointer arrays, and i32 arrays

Remaining:

- direct platform TLS operations if multi-threaded runtime execution
  becomes a supported target
- optional typed unsigned loads/stores if runtime ports need them
- policy gate so `pcc.unsafe` stays internal/runtime-only

### C1: Substrate Globals In Python

Status: complete for the pcc-Python archive.

Completed:

- rewrote singleton pointer access (`py_None`, `py_True`, `py_False`)
  through `pcc.unsafe.global_load_ptr`
- rewrote tombstone/sentinel and exception-table storage through
  pcc-Python-defined globals
- kept legacy substrate accessor exports in `py_substrate.py` so older
  call sites still link without bringing back the C object
- added archive-symbol coverage for the stable substrate globals

### C2: Environment And Path Probe Helpers

Status: environment helpers moved; path probes remain.

Done:

- `py_os_env.py` now uses direct `pcc.unsafe.getenv`, `setenv`,
  `unsetenv`, `strlen`, null/tag checks, and external `py_None` access
- default runtime and `PCC_RUNTIME_HIGH=py` both pass the native
  `os.getenv` / `os.putenv` / `os.unsetenv` startup test

Tasks:

- split path probe operations (`exists`, basename primitives) away
  from path string assembly so `access`-based pieces can move first
- keep `py_os_path_join` as a later buffer/string-builder task

### C3: Traceback/Print Cold Output

Status: first print syscall slice moved.

Done:

- `py_print_sys.py` now writes bytes through direct `pcc.unsafe.write`
  and reads `py_None` via external pointer globals
- default runtime and `PCC_RUNTIME_HIGH=py` both pass the native
  `sys.stdout.write` / `sys.stderr.write` startup test

Tasks:

- replace fixed traceback header/file-line output with `write`
- add a tiny unsafe-backed byte-buffer writer in pcc-Python when
  traceback or formatter ports need assembled output
- keep float/object formatting behind existing runtime helpers until
  dtoa/format policy is explicit

### C4: Existing Python Runtime Modules Off `py_mem_*`

Status: complete for active `pcc/runtime/py` modules.

Done:

- every active Python runtime module in `PY_MODULES` now uses
  `pcc.unsafe` instead of `py_mem_*` for raw allocation, typed
  object-field loads/stores, pointer arithmetic, null/tag checks, and
  byte copying
- `py_obj_gc.py`, `py_int_parse.py`, `py_exc_tls.py`,
  `py_obj_stubs.py`, `py_exc_objects.py`, `py_exc_match.py`,
  `py_tuple.py`, `py_dict.py`, `py_list.py`, `py_set.py`,
  `py_obj.py`, `py_obj_ops_dispatch.py`, `py_obj_ops_compare.py`,
  `py_str_accessors.py`, `py_int_convert.py`,
  `py_int_bigint_convert.py`, and the memory-access portions of
  `py_exc_table.py` / `py_class.py` have moved to `pcc.unsafe`
- `py_exc_match.py`, `py_exc_table.py`, and `py_class.py` now use
  `pcc.unsafe` for their runtime object/table memory access
- `py_set.py` and `py_obj_ops_compare.py` load the set tombstone via
  `global_load_ptr("py_set_dummy")` instead of `py_subs_set_dummy`
- `libpy_runtime_pcc_py.a` rebuilds after the batch
- phase2 and phase3 corpus both pass under `PCC_RUNTIME_HIGH=py`

Tasks:

- keep a regression grep that `pcc/runtime/py` has no `py_mem_*`
  users outside historical probes
- keep a regression grep that active `pcc/runtime/py` modules have
  no `py_subs_*` helper calls

### C5: Sentinel And Exception Table Globals

Status: complete for active `pcc/runtime/py` modules.

Done:

- `py_set.py` and `py_obj_ops_compare.py` read the set tombstone through
  `global_load_ptr("py_set_dummy")`
- `py_exc_table.py` reads `PY_EXC_BUILTIN_NAMES` and `PY_EXC_PARENT`
  through `global_addr`, uses literal `PY_EXC_N_BUILTIN`, and reads /
  writes `py_exc_classes` directly
- `py_class.py` allocates user tags through `py_next_user_tag` and
  lazily builds/stores the root `object` class through
  `py_object_root_cache`
- `py_substrate.py` now hosts the stable storage symbols and retains
  legacy accessor exports for older runtime call sites, so the
  pcc-Python archive no longer needs the C substrate object

Tasks:

- add a CI-style grep that fails if active `pcc/runtime/py` files
  reintroduce `py_mem_*` or `py_subs_*`
- keep the archive-symbol test verifying that `py_substrate.o` in
  `libpy_runtime_pcc_py.a` defines the stable symbols from
  pcc-Python output

## Immediate Phase 4c Task Queue

This queue records the practical runtime-high migration that closed the
current pcc-Python no-C archive gate, plus the representation work that
continues beyond the archive-porting milestone.

### Slice 1: Low-risk `py_str_accessors`

Status: complete for the listed functions.

Moved or targeted functions:

- `py_str_byte_len`
- `py_str_utf8`
- `py_str_eq`
- `py_str_startswith`
- `py_str_endswith`
- `py_str_isdigit`
- `py_str_isalpha`
- `py_str_isspace`
- `py_str_isalnum`
- `py_str_strip`
- `py_str_lstrip`
- `py_str_rstrip`
- `py_str_strip_chars`
- `py_str_lstrip_chars`
- `py_str_rstrip_chars`
- `py_str_count`
- `py_str_concat`
- `py_str_upper`
- `py_str_lower`
- `py_str_len`
- `py_chr_from_i64`
- `py_str_contains`
- `py_str_find`

Validation gates:

- `make -C pcc/runtime libpy_runtime.a`
- `make -C pcc/runtime PCC='env -u LC_ALL uv run pcc' libpy_runtime_pcc_py.a`
- `env -u LC_ALL uv run pytest tests/test_py_runtime_pcc_emit.py -q -n0`
- `env -u LC_ALL uv run pytest tests/test_runtime_oracle_diff.py -q -n0`
- targeted `tests/py_corpus/run_pcc.py` filters for string behavior

Resolved follow-up:

- list/tuple formatting now prints string elements through repr-style
  formatting, so list-of-string corpus cases no longer need a special
  mismatch note.

### Slice 2: Remaining standalone string helpers

Status: complete.

Port before touching list-producing functions:

- `py_str_hash`
- `py_str_repeat`

Design notes:

- `py_str_hash` uses signed-i64 FNV-1a constants and relies on the
  existing pcc-Python i64 wraparound behavior.
- `py_str_repeat` uses the accessor-local `int_or_default` equivalent
  and no new C substrate calls.

### Slice 3: Codepoint indexing and slicing

Status: complete.

Port together because they share UTF-8 offset helpers:

- `py_str_index`
- `py_str_slice`

Design notes:

- positive `step == 1` slicing is a contiguous byte-range copy
- positive stepped slicing streams selected codepoints into a bounded
  output buffer
- negative stepped slicing builds a temporary codepoint-offset table
  with `py_mem_alloc` / `py_mem_free`

Validation additions:

- `tests/py_corpus/phase2/str_slice_steps` covers Unicode indexing,
  positive stepped slicing, reverse slicing, explicit negative-step
  bounds, and negative positive-step bounds under the pcc-py runtime
  archive.

### Slice 4: List-producing and variable-output string helpers

Status: complete.

Port after string-builder/list append helpers are settled:

- `py_str_split`
- `py_str_join`
- `py_str_replace`
- `py_str_splitlines`
- `py_str_splitlines_keepends`

Design notes:

- these functions mix exact output sizing, list construction, temporary
  string ownership, and `py_decref`
- keep each port paired with an oracle/corpus case that forces the
  pcc-py archive to provide the symbol

Validation additions:

- `tests/py_corpus/phase2/str_split_iter` covers whitespace splitting
  and explicit separators without depending on list repr formatting.
- `tests/py_corpus/phase2/str_splitlines` covers `\n`, `\r\n`, `\r`,
  trailing separators, and `keepends=True`.
- `tests/py_corpus/phase2/str_replace_edges` covers non-overlapping
  replacement, no-match replacement, and shrinking/same-length output.

### Slice 5: `py_int.c` design track

Status: complete for the no-C runtime archive. The pcc-py archive now
replaces all object boundary, decimal, shift, bitwise, add/sub,
multiply, divmod, exponentiation dispatch, and public `py_int_*`
operation dispatch slices. The source tree still keeps the C files for
the cc-C and pcc-C runtime archives, but `libpy_runtime_pcc_py.a`
replaces `py_int.o` with the pcc-Python implementation.

Do not start as a mechanical port. The object-boundary conversion
helpers are still useful, but the arithmetic core should wait for
Phase G's representation decision so the ported helpers are reachable
from normal Python source. Split into:

- tagged-int public fast paths
- bignum allocation, copy, normalize, and collapse-to-tagged
- add/sub/mul/cmp
- divmod/floordiv/mod/truediv
- pow, shifts, bitwise negative semantics
- decimal parse/format helpers

Each slice should have a differential oracle case before expanding the
next one.

Completed low-risk sub-slices:

- `py_int_from_cstr` moved to `py_int_parse.c` / `py_int_parse.py`.
  This keeps the arithmetic core in C but removes the parser-facing
  `strtoll` dependency from the pcc-py runtime archive.
- `py_int_to_i64` moved to `py_int_convert.c` / `py_int_convert.py`.
  The pcc-Python implementation reads heap-int layout directly and
  still delegates tagged-int decoding to `py_int_value_i64`.
- `py_bigint_to_i64` moved to `py_int_bigint_convert.c` /
  `py_int_bigint_convert.py`, covering heap bignums that fit in int64
  and setting overflow for larger values.
- `py_int_core.c` / `py_int_core.py` now owns tagged-int encode/decode,
  heap bignum allocation, `py_int_from_i64`, `py_int_value_i64`,
  `py_bigint_from_any`, `py_bigint_to_pyobject`,
  `py_bigint_to_double`, and compare/negation helpers.
- `py_int_decimal.c` / `py_int_decimal.py` now owns decimal
  `py_bigint_to_cstr` / `py_bigint_from_cstr`.
- `py_int_shift.c` / `py_int_shift.py` now owns bignum left/right
  shifts, including negative right-shift floor semantics.
- `py_int_bitwise.c` / `py_int_bitwise.py` now owns bignum
  `and` / `or` / `xor` two's-complement semantics.
- `py_int_addsub.c` / `py_int_addsub.py` now owns bignum add/sub.
- `py_int_bigint_pow.c` / `py_int_bigint_pow.py` now owns bignum
  square-and-multiply control flow.
- `py_int_mul.c` / `py_int_mul.py` now owns bignum schoolbook
  multiplication. The pcc-Python port decomposes each `uint32 * uint32`
  product into 16-bit limbs so it never relies on overflowing signed
  i64 intermediates.
- `py_int.c` / `py_int.py` now owns bignum floor divmod. The
  pcc-Python port uses shift/subtract magnitude division and existing
  bignum helpers, avoiding the unsigned `qhat * digit` intermediates in
  the C Knuth-style implementation.
- `py_int_ops.c` / `py_int_ops.py` now owns public
  `py_int_add/sub/mul/neg/floordiv/mod/truediv/pow/and/or/xor/shl/shr`;
  small tagged add/sub/neg/bitwise and bounded multiply/shift keep fast
  paths, while wide cases go through bignum helpers.

Validation additions:

- `tests/py_corpus/phase2/int_parse_bases` covers signed decimal,
  explicit bases, base-0 binary/octal prefixes, and base-36 digits.
- Existing `phase2/int_builtin`, `phase2/bignum_factorial_20`, and the
  `int_basics` runtime oracle cover the converted int64 paths under the
  pcc-py archive.

Known remaining boundary:

- The current `phase2/bignum_*` corpus passes for the observable printed
  expression path because codegen boxes printed compound int arithmetic
  and calls the runtime bignum helpers.
- The broader representation issue remains: ordinary `IntType` locals
  and non-print expression results are still native i64 unless they
  explicitly flow through object helpers. Container literals now exact-box
  bignum-producing int expressions, subscript print/object use can keep
  the boxed value, exact locals preserve bignum operations, and ordinary
  user-function int parameters/returns use an object ABI. The final
  tagged-object representation decision is still needed before this is a
  universal Python `int` model.
- The Python divmod port is intentionally not the old Knuth algorithm.
  Reintroducing that optimization should wait for explicit
  unsigned/wide-digit intrinsics or a dedicated 16-bit quotient
  estimation design.

### Slice 6: Frontend purity blockers exposed by self-compile

Status: in progress, with the current type-infer stack gate and
no-libpython archive gate closed.

Completed:

- `pcc.frontends.python.type_infer` no longer imports top-level `TYPE_*`
  singleton constants from `types.py`; it builds local AST type values
  instead, avoiding CPython-backed module globals in the no-libpython
  stack compile.
- The type-infer source avoids fallback-triggering constructs in the
  pure path: the fixed-arity tuple assignability check no longer uses
  `zip`, `ImportFrom` resolution reads `stmt.span.file` directly, and
  prepopulation writes `ctx.globals.symbols[...]` instead of calling a
  method through a dynamic attribute chain.
- `dataclasses.replace` aliases (`replace` / `_replace`) are treated as
  native helper calls during closure conversion and codegen.
- `dataclasses.replace(obj, **mapping)` lowers to
  `py_dataclass_replace_from_dict`, implemented in both C runtime and
  pcc-Python runtime.
- Dyn-typed list/dict/set method calls for the curated native method
  set route to pcc runtime helpers before the generic CPython fallback.
- Heterogeneous native dict literals no longer force CPython fallback
  solely because key/value static type classes differ.
- The default pcc-C and pcc-Python runtime archives no longer include
  `py_libpython.o`; generated no-libpython mains no longer call
  `py_cpy_main_exitcode`.
- The pcc-Python runtime archive now replaces `py_process.c` with
  `py_process.py`; command-line argc/argv storage is now exported as
  pcc-Python-defined globals so the hybrid compatibility archive can add
  only `py_libpython.o`.

Validation:

- `env -u LC_ALL timeout 360s uv run pytest 'tests/test_py_multi_file_bootstrap_shim.py::MultiFileBootstrapShimTests::test_real_python_frontend_core_self_compile_still_emits_llvm' -q -n0`
- `env -u LC_ALL timeout 360s uv run pytest 'tests/test_py_multi_file_bootstrap_shim.py::MultiFileBootstrapShimTests::test_native_type_infer_stack_compiles_without_libpython' -q -n0`
- `env -u LC_ALL timeout 900s uv run pytest tests/test_runtime_substrate_spike.py tests/test_py_multi_file_compile.py tests/test_py_multi_file_bootstrap_shim.py -q -n0`
- `tests/py_corpus/phase3/dataclass_replace_kwargs` covers both
  explicit keyword replacement and `replace(obj, **mapping)` under the
  default and pcc-Python runtime archives.

Next tasks:

- Continue scanning bootstrap-generated IR for any `py_cpy_*` calls
  under `--python-libpython=off`.
- Replace remaining source patterns that only need pcc-native container
  or dataclass operations before adding new CPython bridge paths.
- Add native helpers only when the operation is on the bootstrap-safe
  path and cannot be expressed cleanly with existing runtime objects.
