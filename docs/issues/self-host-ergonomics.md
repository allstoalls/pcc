# Self-host ergonomics — pcc-Python should support full Python idioms

**Status:** open. Surfaced 2026-04-28 during Phase 9.2 (Issue 1 work).

## The problem

The current self-host story is **structurally fragile**: writing pcc
in pcc-Python requires avoiding most of the Python language and
standard library, even though "pcc is written in Python" is the whole
point.

Concrete symptoms hit while trying to clean up `pcc/ir/ir.py`:

1. **`import struct` / `import math` / `import dataclasses` etc. trigger
   libpython linkage.** The frontend treats any non-whitelisted import
   as "this module needs CPython at runtime", and the link step pulls
   `-lpython3.X` accordingly.

2. **Builtin names (`int`, `float`, `str`, `len`, `range`, `dir`, ...)
   resolve through `py_cpy_import("builtins") + py_cpy_getattr`.**
   Writing `int(x, 16)` or `float("inf")` in supposedly-typed code
   still triggers a CPython trampoline call.

3. **Replacing one stdlib usage with a hand-rolled equivalent often
   makes things WORSE.** Phase 9.2 attempted to replace
   `struct.pack/unpack` with pure-arithmetic helpers. The hand-rolled
   version was correctly written (52 tests, byte-identical to struct
   for normals/inf/nan), but routing through it produced **133
   py_cpy_\* vs the struct baseline's 111** — because the helpers'
   bodies use `int(x, 16)`, `float("inf")`, `str(x)` and those
   builtin lookups go through CPython.

4. **The result: pcc-Python ends up looking like a heavily restricted
   DSL, not Python.** Library code has to be written with a tiny
   subset of operators, no f-strings around objects, no list
   comprehensions on dynamic-typed iterables, no generic decorators.
   Every stdlib feature is a multi-week refactor.

This is not what "self-hosting" should feel like.

## Why it's structural, not a bug

pcc treats imports in three buckets:

| bucket | examples | behaviour |
|---|---|---|
| **scaffold** | `pcc.extern`, `pcc.unsafe`, `pcc.ir` | compile-time only, no runtime IR |
| **compile-time-only whitelist** | `__future__`, `typing`, `click` | folded by AST, no runtime IR |
| **everything else** | `dataclasses`, `re`, `struct`, `math`, third-party | routed through `py_cpy_import` → CPython |

The third bucket is **everything Python developers actually use**.
There's no native path for "`import X` where X is just regular
Python code that we could compile too".

Same story for builtins: there's no fast path for `int()` /
`float("inf")` / `str()` etc. when the args have known types — they
all route through `py_cpy_import("builtins") + py_cpy_getattr`.

## What "good" would look like

When the user writes pcc in pcc-Python, they should be able to:

1. **`import dataclasses` and have it work natively.** dataclasses is
   itself written in Python (with a small C accelerator that has a
   pure-Python fallback). pcc could compile dataclasses' Python
   source as part of the closure. Same for `enum`, `collections`,
   `functools`, most of the higher-level stdlib.

2. **Use builtins without ceremony.** `int(x)`, `float("inf")`,
   `str(x)`, `len(seq)`, `range(n)`, `print(...)` should lower to
   native code paths when arguments have known types — no
   `py_cpy_import("builtins")` round-trip.

3. **Use f-strings, list/dict/set comprehensions, lambdas, decorators
   on typed objects** without each one being a separate codegen
   project.

4. **Replace `struct.pack/unpack` with hand-rolled arithmetic** and
   actually see fewer py_cpy_\*, not more.

The recursive-import path in particular: if pcc can compile its own
frontend, it can compile any Python module. Therefore `import X`
should just mean "add X.py to the closure and compile it" — exactly
how `import` works in CPython.

## What blocks this today

- **No recursive-package compile**: imports outside the explicit
  multi-file set fall through to CPython. Adding a "discover and
  compile transitively" mode would make most stdlib trivially work.

- **No native builtin dispatch**: pcc has typed paths for
  `list.append`, `dict.get`, `str.split` etc. (when receiver types
  are known), but **builtin functions** like `int(x)` are not the
  same code path. Adding a `_native_builtin_call(name, args)` codegen
  fast path would handle all the common cases (int/float/str/len/
  range/etc.).

- **Type tracking through builtin returns**: even when `int(x)`
  lowers to a native call, the result type needs to flow through
  subsequent expressions. Currently DynType propagates through
  builtin returns.

- **No "this Python code is a runtime port" registry**: pcc has
  `py_runtime/py/*.py` (pcc-Python ports of `dict`, `list`, `int`,
  `str`, `obj`, ...). These prove pcc-Python can host complex code.
  The same machinery could host `dataclasses.py`, `re.py`, parts of
  `os`, etc. — but currently there's no registry-based "compile this
  pure-Python stdlib module too" path.

## How this affects the current Issue 1 work

Phase 9 (getting ir.py to 0 py_cpy_\* in ON mode) is technically
feasible but **expensive in this regime**. Every fix is a yak shave:
remove one stdlib usage, find that the replacement uses three
builtins, replace those with their own hand-rolled equivalents, find
each of those uses string ops, etc.

Issue 1 will close eventually with this approach — the math works —
but the pcc-Python source we end up writing is unrecognisable as
Python. That's fragile and unmotivating.

A better sequencing for Issue 1 closure may be:

1. **First** add native builtin dispatch (`int`, `float`, `str`,
   `len`, basic ones). Single, contained codegen change.
2. **Then** add transitively-discovered native stdlib compilation.
   Bigger change but unlocks everything.
3. **Then** Phase 9 reductions become natural — write Python like
   Python, codegen lowers it natively.

vs. the current path: hand-roll everything in pcc-Python's tiny
subset until ir.py is clean.

## Concrete asks

**For pcc 1.x (post-Issue-1):**

- Add `_native_builtin_dispatch` covering at least `int`, `float`,
  `str`, `len`, `range`, `print`, `bool`, `abs`, `min`, `max`,
  `isinstance`, `type`. With type-aware fast paths.

- Add `_register_pcc_python_stdlib(module_name, source_path)` so
  modules like `dataclasses.py` can be compiled as part of the
  closure without any of their imports triggering libpython.

- Make `compile_python_multi` recursive: if file A imports B and B
  is compilable Python, pull B in automatically.

**For Phase 9 specifically:**

- Park Phase 9.2 (struct removal) until builtin dispatch lands. The
  bitwise helpers exist but trying to land them now is actively
  worse.

- Continue Phase 9.3+ (type annotations on hot vars, f-string
  refactor) as smaller fixes that don't depend on builtin dispatch.

## Bottom line

The current "self-host" is structurally **a Python-shaped
interpreter, not a Python compiler**. The user is right to flag this
— writing pcc in pcc-Python should feel like writing Python. The
work to get there is real but bounded, and it should ideally come
**before** the long tail of Phase 9 reductions (rather than after).

## 2026-04-28 update — what landed, what we learned

The three concrete asks from the original draft (native builtin
dispatch, pcc/ir/support/ port registry, recursive multi-file compile)
all shipped under Issue 11. A follow-on dispatch wave that same day
took the stage1 closure ON-mode total from ~10941 (per-module sum)
down to 9906 by adding native lowerings for `os.environ.get`,
`os.path.{dirname,isfile,isdir,getmtime,abspath}`, `sys.platform`,
`print(file=sys.stderr|stdout)`, `os.getcwd`, `os.access`,
`os.{F,R,W,X}_OK`, `os.path.join` with splat args, and unambiguous
IR-method dispatch on non-builder receivers.

The original asks closing didn't make pcc-Python "feel like
writing Python" yet. Four structural insights from the wave:

### Insight 1 — Three runtime tiers, no auto-fallback between them

The session shook out a tier model for new runtime helpers:

| tier | location | when |
|---|---|---|
| **A** pure pcc-Python | `pcc/runtime/py/*.py` | helper is value/string math; no syscalls |
| **B** C-only substrate | `pcc/runtime/src/py_os_substrate.c` | helper needs platform-specific struct layout (`struct stat`) or syscall headers (`unistd.h`) — pcc-Python can't express |
| **C** dual-write | `src/X.c` + `py/X.py` | both flavours useful; the pcc-Python port replaces the cc one in `libpy_runtime_pcc_py.a` |

There's no "compiler picks tier automatically" — each new helper is
a manual tier choice. That's fine for the engineer-author but means
"add a new os.X" is still a 4-5 file change (header + tier B/C
source(s) + ABI sig + codegen dispatch + test). For the
`os.path.X` family this added up: 8 new helpers landed, but each
required the same shape of plumbing.

The structural improvement would be a small framework where tier
selection is declarative (a single decorator + dispatch table)
rather than 5 file edits per helper.

### Insight 2 — pcc parser is the next wall, not codegen

`os.listdir` was the obvious next dispatch target (16 + 5 callsites
in `_runtime_archive_stale`, plus chained downstream effects). It
failed because **pcc's own C parser cannot eat `<dirent.h>`**:

```
Error: <input>:702: before 'EQUALS' ('=')
```

The blocker is in pcc's preprocessor / parser — likely an inline
assignment inside a libc macro expansion. Even rewriting the helper
without `while ((entry = readdir())…)` style didn't get past the
header itself.

Same wall hits any C helper that needs `<sys/stat.h>` directly,
inline-assign-style libc patterns, or modern POSIX headers with
heavy macro magic. Today the workaround is to hide everything
through `py_os_substrate.c` since cc compiles that path; but the
moment pcc's runtime archive needs to include a syscall helper that
can't go through substrate, we hit this wall again.

This is a **distinct project from Issue 1**: pcc's C front-end has
a strict subset of C it can parse, and that subset doesn't yet
cover real libc surface area. Until it does, the "self-host runtime
written in pcc-Python" path can only wrap C primitives that
substrate can hide.

### Insight 3 — `isinstance` narrowing is correct but downstream-bound

Initial hypothesis was that narrowing fails because imported
`ClassType` carries empty `fields=()`. Verified during the same
session: that hypothesis is **wrong** — `pipeline.py` lines
1790-1801 already populate `field_types` from every `name:
annotation` declaration, and `type_infer.py` lines 899-906 read
them into `ClassType.fields`. Probed `IntLit`, `BinOp`, `FuncDef`,
`Assign`: all have full field type tables.

Narrowing was re-attempted with the marshaller in place
(commit 39bda5f0 hardened the ClassType → CPython bridge first).
Result: still **+9 fallback** in `type_infer.py`, **+5** in the
multi-file ON closure. Per-site, the typed dispatch did fire
correctly — `py_obj_getattr` count went up by +30, `py_cpy_getattr`
count went down by ~the same amount. Net regression came from
**downstream marshal cost**: a narrowed `ClassType` value passed
into a CPython-bound call site (e.g. an unrelated dispatch that
declined and fell back to `_emit_cpy_method_call_src`) goes
through `py_cpy_from_pcc_obj` once per use, where a `DynType`
pointer would have been routed identically — but the narrowed
control flow visits *more* such boundaries because the narrowed
type forces some code paths that previously dropped to a faster
DynType-specific path.

The structural takeaway: **type narrowing alone isn't a win
without flow-aware dispatch**. The narrowed value needs to either
(a) flow only into typed sinks (no cpy fall-back ever) or
(b) the cpy fall-back paths need to recognise the narrowed type
and stay native rather than marshalling. Today's codegen has
neither property uniformly. Adding narrowing without one of these
trades 1 typed dispatch for 1 marshal call — break-even at best.

The actual unblock is **Insight 4 below**: kill the cpy boundary,
not narrow what crosses it.

**Follow-up later 2026-04-28:** the first successful narrowing landed
only after adding the missing native sink. The change did three things
together:

- exported class schemas are now resolved across modules when a class
  name is unique;
- method `self` is typed as the enclosing class, and simple
  `isinstance(name, ClassType) and ...` guards narrow the positive
  branch;
- class-typed receivers can register the matching external class and
  call its method natively.

That combination is different from the earlier failed attempt. It does
not merely narrow values before sending them through CPython; it gives
the narrowed value a native dispatch destination. Result: tight closure
OFF multi `19743 → 18597`, ON multi `2648 → 1493`. Independent
per-module mode still pays a small layer1.py cost for the registration
helper, so only the multi-file totals were recaptured.

The next follow-up added native `str.split(sep, maxsplit)` in both the
C runtime and pcc-Python runtime port. This removed another common
Python idiom from the CPython path (`split(".", 1)`, `split("=", 1)`,
`split(None, 1)`), moving the same closure to OFF multi `18469` and
ON multi `1319`.

The next reduction applied Insight 4 directly: when a CPython-returned
value is used only as the receiver for an already-native `str` method,
bridge that receiver with `py_cpy_to_pcc_obj` and keep the rest of the
method chain on `py_str_*`. This covers shapes such as
`platform.machine().lower().split("-")` and
`for name in os.listdir(...): name.endswith(".c")` without rewriting
source code around those APIs. Result: OFF multi `18469 → 18439`,
ON multi `1319 → 1269`.

The next step moved a standard-library boundary instead of editing
call sites: `open(path, mode?, encoding=...)` now lowers to a native
runtime file object, and `with open(...) as f: f.read()/f.write(...)`
uses `py_file_open`, `py_file_read_all`, `py_file_write`, and
`py_file_close`. This keeps the normal Python `with open(...)` idiom
and removes the CPython `open` / `__enter__` / `read` / `write` /
`__exit__` chain from the pipeline's source and artifact I/O. Result:
OFF multi `18439 → 18283`, ON multi `1269 → 1039`.

### Insight 4 — Chain-of-CPython is a cost amplifier

A single un-dispatched dynamic helper poisons every downstream
operation in its scope. Concrete: `os.listdir(d)` not native means
the loop variable `name` is a CPython value, which means
`os.path.join(d, name)` is forced through `_emit_cpython_list_ops`
(any list literal containing a CPython value goes CPython-managed),
which means another CPython getattr/call per use, which means…

In `_runtime_archive_stale`, the 16 `cpy.import.os` calls inside
that one function come from 17 chained `os.path.X` calls in tight
loops, *all* poisoned by two unboxed `os.listdir` calls. Conceptually
each native helper in the inner loops is "fully native" — but the
outer cpython value pulls them all back to cpy paths.

This means **the 9906 fallback total is misleading**: a small
number of stubborn dynamic ops at the head of a chain accounts for
disproportionate fallback. Killing one strategic helper (e.g.
`os.listdir`, or general `cpy → pcc` marshaller for str-typed
returns) likely drops the total by hundreds, not tens.

**Follow-up later 2026-04-28:** the next reduction wave validated the
amplifier model but also exposed a measurement trap. ON multi-file
fallbacks dropped from `1039 → 505` by:

- recognizing `parent.builder.X`, `self.parent.builder.X`, and locals
  assigned from `ir.IRBuilder(...)` as IR scaffold receivers;
- replacing the remaining `dataclasses.is_dataclass/fields` reflection
  in `layer1.py` with direct dataclass metadata access;
- treating `pcc.ir.compat` as a compile-time scaffold only in
  `--ir-scaffold=on`, while preserving OFF-mode runtime imports;
- lowering value-position `zip(...)` and `next(<genexpr>, default)`
  natively;
- bridging CPython scalar equality into native `py_obj_eq`.

The important caveat: **a bridge is still a libpython dependency**.
`py_cpy_to_pcc_obj` and `py_cpy_to_pcc_str` reduce the number of
downstream `py_cpy_getattr/call` sites, but their implementations still
call CPython APIs. They are a transition tool, not Issue 1 closure.
The baseline now tracks ON mode as `505 total = 18 bridge + 487
non-bridge`, with separate ratchets for bridge and non-bridge calls.
Future work cannot claim progress by increasing bridge calls while
shrinking the original dynamic-CPython surface.

**Follow-up same day:** the next cleanup wave targeted internal
compiler/runtime idioms rather than source-subset restrictions. It
added native `py_str_ord` in both runtime tiers and lowered
`ord(str)` through that helper, fixing Unicode codepoint semantics at
the same time. It also removed CPython-only internals from codegen and
pipeline hot paths: `str.encode()` for emitted UTF-8 globals, regex in
IR libpython detection, `reverse/reversed/delattr` in small helpers,
and `import builtins; dir(...)` for the builtin-name table.

Result: ON multi `505 → 411`, with bridge calls unchanged at `18` and
non-bridge `py_cpy_*` calls `487 → 393`. That is the healthier shape:
progress came from deleting real CPython dependencies, not from hiding
them behind more bridge calls.

The next small step applied the same rule to `platform.machine()`.
That call is used only as a string source for `.lower()`, `.split()`,
or equality checks in the bootstrap path, so routing through CPython
and then bridging back was pure overhead. `platform` is now a native
builtin module for import analysis, and `platform.machine()` lowers to
`py_platform_machine_str` in the C-only OS substrate shared by both
runtime archives. Result: ON multi `411 → 400`, bridge calls
`18 → 16`, non-bridge calls `393 → 384`.

The next reduction did the same for
`subprocess.check_output(argv, text=True)`. The helper lives in a
C-only process substrate object rather than `py_process.c`, because
the pcc-Python runtime archive replaces `py_process.o`. The native
path returns a pcc string directly, so `.strip()` / `.split()` chains
stay native. Unsupported bytes/encoding forms still fall back to
CPython. Result: ON multi `400 → 383`, bridge calls `16 → 13`,
non-bridge calls `384 → 370`.

The same substrate now handles
`with tempfile.TemporaryDirectory(prefix=...) as tmp:` through
`py_tempdir_new` / `py_tempdir_cleanup`. That removes the CPython
`TemporaryDirectory` object plus `__enter__` / `__exit__` calls from
the bootstrap temp-artifact paths while keeping the source idiom.
Result: ON multi `383 → 337`, bridge calls `13 → 10`, non-bridge
calls `370 → 327`.

`subprocess.run(argv, check=True, ...)` is now lowered only when it is
used as an expression statement, matching the pipeline's make/clang
side-effect calls where the `CompletedProcess` value is ignored.
Nonzero exits raise a pcc `RuntimeError`; `capture_output=True`
redirects child output. Result: ON multi `337 → 322`, bridge calls
unchanged at `10`, non-bridge calls `327 → 312`.

The next follow-up stayed on the same standard-library boundary
instead of rewriting source around it. `shutil.which(name)` now uses a
native `$PATH` scanner; `shlex.split(text)` uses a small native
POSIX-ish splitter for link-flag strings; statement-only
`subprocess.run` accepts typed bool expressions such as
`capture_output=not verbose`; and `sys.executable` lowers to the
program argv0 so `os.path.dirname(sys.executable)` can stay on the
native `os.path` path. Result: ON multi `322 → 286`, bridge calls
`10 → 9`, non-bridge calls `312 → 277`. The same wave also treats
`pcc.ir.compat` as a scaffold import for `runtime_abi.py` in
OFF mode, because that module only needs IR type constructors; OFF
multi drops `18283 → 17135` instead of raising the ABI-table ratchet.

### Updated sequencing recommendation

In order of expected return-on-effort:

1. **Extend `export_meta.py` field_types coverage to all py_ast
   dataclasses + add isinstance narrowing.** Probably 2 days,
   biggest single drop in remaining fallback (~70 direct + chained
   savings via narrower-typed AST walks).
2. **Generic `cpy → pcc` marshaller for known-shape returns.** Lets
   `_emit_cpython_list_ops`-style code paths construct pcc-native
   lists holding marshalled-back CPython values, breaking the
   chain-of-cpython amplifier without needing a new helper per
   dynamic call.
3. **Either fix pcc's C parser (so syscall headers compile) OR add
   a "this C file goes through cc only" Makefile slot.** Unblocks
   `os.listdir`, `subprocess.run`, etc. — large surface.
4. **Helper-tier framework: single decorator + ABI table + dispatch
   case generated together.** Removes the 5-file-edit cost per new
   helper, lowering the ergonomic floor for self-host work going
   forward.

Items 1 and 2 are codegen-only and bounded. Item 3 is a pcc
internal change. Item 4 is mechanical refactor.

## Issue 1 execution plan from the current closure

The Issue 1 target is stricter than "few fallback calls": the
bootstrap binary must link without libpython. The useful headline
metric is therefore the `py_cpy_*` closure split into:

- **bridge calls**: `py_cpy_to_pcc_obj` / `py_cpy_to_pcc_str`.
  These reduce downstream CPython chains but still require libpython.
- **non-bridge calls**: the real remaining dynamic CPython surface.

As of the 2026-04-29 zero-fallback wave, the working baseline is:

| metric | value |
|---|---:|
| stage1 closure ON multi | 0 |
| bridge calls | 0 |
| non-bridge `py_cpy_*` | 0 |
| stage1 closure OFF multi | 16987 |
| `ir.py` self-compile ON | 68 |

This plan deliberately avoids the "restricted Python subset" path.
The route is to delete libpython boundaries, make standard Python
idioms lower natively, and then use a link-without-libpython gate as
the real Issue 1 close criterion.

The final drop to zero came from three structural changes:

1. **Compile-time literal parsing without host Python.**
   `float("...")` folding no longer calls CPython's `float()` while
   compiling the compiler.

2. **Comprehension lowering without CPython callbacks.**
   `_emit_comprehension` no longer builds nested Python callables and
   calls them through `py_cpy_wrap_pcc_*` / `py_cpy_call_noargs`; it
   uses explicit method recursion over the generator context.

3. **Self-backend as a host-tool boundary, not an in-process
   libpython boundary.** `_link_with_self_backend` no longer imports
   and calls `pcc.backend.*` inside the compiled bootstrap binary. It
   writes the IR to a temp file and invokes the host Python command as
   a subprocess to produce assembly. This keeps the stage1 binary free
   of libpython while preserving the self-backend feature path. The
   long-term self-host target is still to compile the backend modules
   natively; the host-tool boundary is a link-clean intermediate step.

### Phase A — done: ON-mode `py_cpy_*` closure is zero

The ratchet now locks all three multi-file ON counters at zero:
total, bridge, and non-bridge. Any new `py_cpy_*` call in the stage1
multi-file ON closure is an Issue 1 regression unless it is paired
with an intentional redesign of the close criterion.

### Phase B — add the linker gate

Counting IR calls is necessary but no longer sufficient. The next gate
must actually link the stage1 bootstrap binary with libpython disabled
and fail on unresolved `Py*` / `py_cpy_*` symbols.

The close criterion for Issue 1 is:

1. ON-mode stage1 closure has zero `py_cpy_*` calls. **Done.**
2. The stage1 binary links without `-lpython3.X`.
3. Stage1 / stage2 / stage3 still produce the expected byte-stable
   bootstrap artifacts under the supported macOS arm64 environment.

Only after that should `--ir-scaffold=auto` flip to the ON behaviour
by default.

### Phase C — cross-platform reproducibility

After Issue 1 closes on macOS arm64, add Linux x86_64 as a first-class
bootstrap target. Mach-O normalization and ELF normalization are
different enough that they need separate gates. The desired CI shape is
macOS arm64 plus Linux x86_64, both enforcing stage2 == stage3 and the
fallback ratchets.

### Phase D — remove clang from the bootstrap path

This is downstream of Issue 1:

- either teach pcc's C parser enough real POSIX headers to compile the
  syscall substrate, or keep a small cc-only substrate slot explicit;
- mature the Python self-backend until it emits Mach-O / ELF object
  files directly, then replace the current host-tool subprocess
  boundary with native compiled backend modules;
- make pcc assemble the platform linker command itself instead of
  going through clang as the driver.

### Phase E — keep improving self-host ergonomics

Issue 1 should not leave future pcc development trapped in a
Python-shaped DSL. Continue investing in:

- declarative helper registration (`@dispatch` plus generated ABI and
  codegen tables);
- natural typed-object support for f-strings, comprehensions,
  decorators, and common builtins;
- recursive stdlib closure compilation for ordinary Python modules.

This is the long-term answer to "the pcc compiler can't stay a restricted subset forever": the
compiler must make Python idioms native, not force maintainers to
rewrite Python into a private subset.
