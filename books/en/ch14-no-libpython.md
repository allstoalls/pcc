# Chapter 14: No-libpython and Zero-libc — Making the Runtime pcc-Python

No-libpython excludes the CPython runtime. Zero-libc goes further by excluding the C standard library and its dynamic-link closure. “The production runtime is authored in pcc-Python” is a third statement, about implementation ownership. The three are related but not interchangeable. This chapter uses the August 2026 source and evidence state. It explains why pcc no longer treats a permanent hand-written C kernel as the destination: allocation, threads, safepoints, all five collectors, platform wrappers, C-API entry points, and the libc-like substrate are moving into a strict freestanding pcc-Python subset. The compiler retains only machine intrinsics for raw memory, atomics, system calls, and ABI operations; C and vendored-libc sources become differential oracles. On Linux, the target is a supported static closure with no production C/libc objects, `PT_INTERP`, `DT_NEEDED`, or undefined symbols. Darwin deliberately enters the operating system through named libSystem ABI calls and must never be labeled zero-libc.

## 14.1 The Problem and Design Space: Four Non-Interchangeable Claims

A native Python artifact can remove bytecode interpretation while still linking libpython. It can avoid libpython while depending on libc, the system dynamic loader, and a collection of hand-written C runtime objects. It can even close a static tracer without carrying the complete object model, extension ABI, and five collectors into that closure. The phrase “standalone binary” erases all of these boundaries.

This book uses four claim levels:

| Claim | Exact meaning | Does not imply |
|---|---|---|
| no-libpython | The artifact neither links nor loads CPython, and strict lowering has no `py_cpy_*` escape | No libc or hand-written C |
| pcc-Python-owned runtime | Production archive members are objects compiled by pcc from `pcc/runtime/py/*.py` | No platform dynamic dependency in the final executable |
| Linux zero-libc tracer | The named Linux x86_64 tracer is static, with no interpreter, dynamic dependency, or undefined symbol | The full Python runtime is zero-libc |
| Linux production zero-libc | The supported complete static closure has no production C/libc object, `PT_INTERP`, `DT_NEEDED`, or undefined symbol | The same boundary applies to Darwin |

These levels form an evidence ladder, not a vocabulary list:

```text
strict lowering
     |
     v
no py_cpy_* / no libpython
     |
     v
pcc-Python-owned runtime archive
     |
     +---- Darwin: named libSystem ABI (not zero-libc)
     |
     +---- Linux tracer: raw syscalls + static ELF (proven slice)
                    |
                    v
          full production zero-libc closure (final gate open)
```

The easier alternative is to stop at a small permanent C kernel: C owns allocation, threads, GC, and ABI machinery, while pcc-Python owns only containers and dunder semantics. That design is simpler, but it cannot satisfy the stronger execution-ownership thesis. The compiler still needs a second implementation language to produce its own runtime; collector policy retains two potential owners across C and Python; and the Linux zero-libc claim fails before archive composition is even considered. The present direction is therefore not “minimize the C kernel.” It is **remove production C implementation while retaining compiler-owned machine intrinsics and explicit operating-system ABI boundaries**.

## 14.2 The New Runtime Layers: Implementation Knowledge, Not File Extensions

The current contract has four layers, each with a different verb:

```text
semantic pcc-Python
  list / dict / str / dunder / exception / import / C-API semantics
                          |
                          v
freestanding pcc-Python
  allocator / threads / safepoints / GC / libc-like substrate / ABI shims
                          |
                          v
compiler intrinsics
  raw memory / atomics / syscall / host ABI / machine operations
                          |
                          v
OS boundary
  Linux raw syscalls             Darwin named libSystem entries

C and vendored-libc sources: differential oracle only; not production input
```

“Freestanding” does not merely mean “C rewritten with Python syntax.” These modules are implementing the heap, error substrate, threads, or collector; they cannot call back into ordinary Python objects, boxing, allocating exception paths, or the collector they are bootstrapping. `__pcc_freestanding__ = True` marks the closure for the build and validator. `pcc.unsafe` supplies raw pointers, fixed-width loads and stores, atomics, and system calls. Export decorators from `pcc.extern` assign stable C ABI names to the generated object.

The `memcpy` implementation in `pcc/runtime/py/freestanding_mem_str.py` shows the subset. It creates no `bytes` object and does not call the host `memcpy`:

```python
# pcc/runtime/py/freestanding_mem_str.py
@c_abi_export("memcpy")
def pcc_memcpy(dst, src, size: int) -> c_ptr:
    i: int = 0
    while i < size:
        store_i8(dst, i, load_i8(src, i))
        i = i + 1
    return dst
```

The compiler-intrinsic boundary is drawn by knowledge, not by how inconvenient a function is to write in Python. `page_alloc` may be intrinsic because it denotes a machine-level page mapping. Size classes, free lists, metrics, and locking are allocator policy and therefore belong to `freestanding_allocator.py`. Likewise, raw `syscall` encoding belongs in the backend, while cross-platform `open`/`read`/`write` behavior and errno publication belong in freestanding modules. This division prevents every difficult semantic operation from becoming a new compiler special case—and thus a second, unauditable runtime.

## 14.3 The Production Archive: Python Source Becomes the Low-Level Object

The `libpy_runtime_pcc_py.a` production target takes two sets of Python-born objects: semantic `PY_MODULES` and strict `FREESTANDING_PY_MODULES`. The current Makefile archives only `PCC_PY_OBJECTS` and preserves a provenance receipt for each member. C rules remain for the host-C oracle, differential testing, and other explicitly labeled modes; they are not member sources for this production archive.

```makefile
# pcc/runtime/Makefile
$(LIB_PCC_PY): $(PCC_PY_OBJECTS) $(PCC_PY_RECEIPTS)
	@set -eu; \
	rm -f "$@.tmp"; \
	rm -f "$@.capi_syms.nm.tmp"; \
	rm -f "$@.capi_syms.tmp"; \
	rm -f "$@.provenance.json.tmp"; \
	$(AR) rcs $@.tmp $(PCC_PY_OBJECTS); \
	$(RANLIB) "$@.tmp"; \
```

This rule is stronger than “a same-named `.py` file exists.” Source ownership, archive membership, and provenance must close together. Renaming `py_capi_shim.o` to `py_capi_compat.o` cannot turn a C object into Python output. The archive-source ratchet asks whether every member maps to `pcc/runtime/py/<stem>.py`, rather than maintaining a blacklist that an object rename can evade.

The five collectors are the migration's most consequential test. Production collector policy is now split across `freestanding_gc_*` modules: root and frame registration, object-slot access, common marking, incremental and concurrent scheduling, promotion, forwarding indexes, and ZPage lifecycle each have named owners. Even hash indexes once treated as permanent C-kernel material have moved into `freestanding_gc_index_table.py`; its module documentation identifies `src/py_gc_index_table.c` as a differential oracle.

```python
# pcc/runtime/py/freestanding_gc_index_table.py
@c_abi_export("pcc_gc_index_py_next_pow2")
def pcc_gc_index_py_next_pow2(value: int) -> int:
    if value < 8:
        return 8
    power: int = 1
    while power < value:
        power = power * 2
    return power
```

This changes the equality contract described in Chapter 10. The five backends no longer consume “one C collector plus one Python mirror.” They consume **one production pcc-Python slot/root contract**; the C implementation answers only whether identical inputs produce identical behavior in oracle tests. Completing a migration does not mean deleting the C file. Deletion would destroy the independent oracle. The correct move is to remove the C implementation from production linking while retaining its provenance and differential entry point.

## 14.4 Linux and Darwin: One Source, Different Machine Boundaries

Zero-libc must name a target. The Linux x86_64 self backend can lower supported system operations to raw syscalls, provide `_start`, and link statically. Darwin enters the kernel and platform frameworks through stable named libSystem ABI calls, so its Mach-O artifact retains a system dynamic boundary. This is not an incomplete Linux route; it is a different platform contract.

`freestanding_platform_io.py` keeps one pcc-Python API across both targets:

```python
# pcc/runtime/py/freestanding_platform_io.py
@c_abi_export("pcc_platform_read")
def pcc_platform_read(fd: int, buffer, size: int) -> int:
    return read(fd, buffer, size)


@c_abi_export("pcc_platform_write")
def pcc_platform_write(fd: int, buffer, size: int) -> int:
    return write(fd, buffer, size)
```

Here `read` and `write` are compiler-recognized machine boundaries. Linux lowering emits raw syscalls; Darwin lowering emits named ABI calls. Higher file, stdio, socket, and process modules should neither duplicate platform branches nor quietly fall back to glibc on Linux.

The Linux tracer connects this route to process entry. `freestanding_linux_start.py` decodes the initial stack supplied to `_start`, writes a fixed message, and terminates via `exit_group`, with no C or assembly startup object:

```python
# pcc/runtime/py/freestanding_linux_start.py
@c_abi_export("_start")
def pcc_linux_start(initial_stack: c_ptr) -> None:
    argc: int = load_i64(initial_stack, 0)
    argv0 = load_ptr(initial_stack, 8)
    status: int = 0
    if argc < 1 or ptr_is_null(argv0):
        status = 64

    message = cstr("pcc zero-libc ok\n")
    if write(1, message, 17) != 17:
        status = 74
    process_exit(status)
```

The [2026-08-03 Linux zero-libc tracer evidence](../../docs/goal/evidence/2026-08-03-linux-zero-libc-python-start.md) is mode-labeled host-pcc0 Python frontend, x86_64 Linux, self backend, no-libpython. Its ELF is static; `readelf -l` has no `PT_INTERP`, `readelf -d` has no `DT_NEEDED`, `nm -u` is empty, and the link map contains only the object generated from that Python source. The evidence deliberately does not claim the full runtime, complete C frontend, five-GC closure, or pcc1 cross-target execution.

## 14.5 Landed Ownership and the Still-Open Final Claim

Several bounded slices have `DONE_STRONG` evidence at this snapshot:

- Fifteen memory/string ABI functions are uniquely owned by `freestanding_mem_str.o`; vendored musl is oracle-only.
- The allocator is owned by `freestanding_allocator.py`, with Linux raw-syscall execution, a Darwin import ratchet, and five-GC long-run measurements.
- IO, filesystem, environment, process, time, socket, RSS, and errno wrappers are freestanding pcc-Python.
- GC0 through GC4 production collector policy is entirely freestanding pcc-Python; the production link map has no C collector definition, and all five fixed points have recorded evidence.
- The C and Python frontends have a supported shared link route through the freestanding pcc-Python libc.
- The current production-archive recipe accepts Python-born objects and provenance only.

Yet `LIBC-P3-FREESTANDING-RUNTIME-CLOSURE` remains `TODO_READY`. The reason is no longer a stated intent to preserve a permanent C kernel. The acceptance surface is larger: freeze current source identity, audit the complete production link, run default, integration, five-GC, and pcc1→pcc2→pcc3 gates; for Linux, combine `file`, `readelf`, `nm`, and link-map evidence; for Darwin, enumerate the residual libSystem ABI. A source tree that says “all members are from Python” is not itself a release proof.

The book therefore uses these status labels:

```text
source ownership       present: production archive recipe is Python-only
bounded subsystems     proven: mem/str, allocator, wrappers, five GC, tracer
full Linux runtime     acceptance pending: final broad/link closure gates
Darwin zero-libc       inapplicable: named libSystem boundary is intentional
```

## 14.6 The No-libpython Fallback Ratchet Still Matters

Removing C and libc does not remove CPython fallback automatically. `--python-libpython=off` constrains the frontend and link closure: any lowering that needs a `py_cpy_*` symbol must fail before publishing an artifact. `tests/fallback_baseline.json` and `tests/python/test_fallback_baseline.py` make that boundary a one-way ratchet; `fallback_routes.py` and `fallback_explainer.py` give each fallback a stable phase, reason, and suggestion.

The two dimensions must be measured independently:

```text
frontend closure:  Python source -> no py_cpy_* -> no libpython
runtime closure:   runtime archive -> pcc-Python owners -> OS boundary
```

If the first is green and the second red, the artifact is no-libpython but still C/libc-dependent. If the second is green and the first red, pcc owns its runtime but still requires the CPython bridge. Only when both close does the complete Linux zero-libc/no-libpython claim hold.

## 14.7 History and Lessons

### 14.7.1 The False Confidence of `PCC_RUNTIME_CC=cc` (2026-05-30)

Early no-libpython work maintained both C implementations and pcc-Python ports. Nine idiom slices appeared green under regressions and bootstrap, but a default-mode `bin(5)` probe failed to link. Four slices had modified only C files while their tests forced `PCC_RUNTIME_CC=cc`; default production mode linked the Python ports. The C change was therefore either invisible or left the old wrong behavior in place. Bootstrap did not call `bin` or set symmetric difference, so a green bootstrap did not cover those paths.

The rule left at the time was “update both mirrors.” The present migration advances it: production has one pcc-Python owner and C is an independent oracle. That removes ambiguity about which semantics default mode links, but it does not remove differential testing. The lesson remains: a green result proves only its mode, and an oracle cannot stand in for the product path.

### 14.7.2 Renaming `py_capi_shim.o` to `py_capi_compat.o` Produced a False Closure (2026-08-08)

The terminal no-C ratchet once asserted only that no archive member was named `py_capi_shim.o`. After the object was renamed to `py_capi_compat.o`, the assertion passed even though production still contained hand-written C. The supposed closed symbol set had also drifted to nineteen globals. Expanding the allowlist would have “completed” the task without changing ownership.

The investigation rejected that route and changed the predicate to source ownership: every production member must correspond to `pcc/runtime/py/<stem>.py`. The C-API families were subsequently split among `py_capi_*_runtime.py` owners, and the current production recipe no longer adds a compat object. The invariant is general: **a terminal test must assert the desired property, not a historical filename.** A zero-libc gate likewise cannot merely search for the string `libc.so`; it must inspect the interpreter segment, dynamic dependencies, undefined symbols, and complete link map.

## 14.8 Summary

pcc's runtime direction has moved from “retain and minimize a permanent C kernel” to “author the production runtime in pcc-Python and express the machine boundary through compiler intrinsics.” Both semantic and freestanding pcc-Python grow. C and vendored libc remain valuable differential oracles but leave the production dependency. No-libpython, Python-owned runtime, and zero-libc are separate claim axes. Linux has a proven raw-syscall `_start` tracer, and memory/string, allocation, platform wrappers, and all five collectors have bounded ownership evidence. The final full-runtime Linux link and gate acceptance remains open. Darwin's correct label is always “named libSystem ABI,” never zero-libc.

## Exercises

1. Read [pcc/runtime/Makefile](../../pcc/runtime/Makefile) and trace `PCC_PY_OBJECTS`, `PY_MODULES`, `FREESTANDING_PY_MODULES`, and `LIB_PCC_PY` into an archive-membership diagram. Explain why retained `src/*.c` rules do not imply membership in the production archive.
2. Read [freestanding_linux_start.py](../../pcc/runtime/py/freestanding_linux_start.py) and the [Linux tracer evidence](../../docs/goal/evidence/2026-08-03-linux-zero-libc-python-start.md). Build a checklist of everything a complete-runtime zero-libc claim needs beyond the tracer.
3. Compare [freestanding_allocator.py](../../pcc/runtime/py/freestanding_allocator.py) with `pcc.unsafe.page_alloc/page_free`. Argue why size-class policy belongs to the freestanding runtime while page mapping belongs to the machine boundary.
4. Design an archive-provenance ratchet that an object rename cannot evade. Include source ownership, member order, the C-API inventory, and atomic publication.
5. Write a mode-labeled Darwin release claim that enumerates its allowed libSystem boundary, and explain why calling it zero-libc would weaken the later Linux acceptance.
