# Chapter 17: Packages, the C-API Shim, and Extension ABI

Nowhere is it easier for a Python compiler to deceive itself than on the package ecosystem. The phrase "supports NumPy" can name a dozen facts of wildly different strength: from "the install command did not error", through "a pure-Python subset can be imported", up to "the C extension initializes and runs correctly on pcc's own object model". pcc's answer to this front has two layers. One is mechanism — the generic install pipeline in [pcc/package/](../../pcc/package), the C-API shim in `py_capi_shim.c`, the extension loader in `py_extension_loader.c`, and CpyHandle boxing. The other is discipline — the pip-install gate and the import gate are two independent claims, each with its own evidence standard, and every fix must land in a reusable mechanism: no `if package == "numpy"` special cases, ever. This chapter covers both layers, because in this subsystem the discipline is not a footnote to the mechanism; it is the precondition for the mechanism staying true over time.

## Chapter Overview: Package Compatibility Is a Gradient

First split "supports a package" into levels: it can install, parse metadata, import pure Python, load extensions, and expose the ABI needed through the C-API shim. Passing one level does not automatically prove compatibility with the whole package ecosystem.

- `pip install` passing and `import` passing are separate claims.
- C-extension compatibility depends on ABI, object protocols, buffer, capsule, error handling, and reference ownership.
- pcc must not special-case popular package names in the compiler; the fix must be a reusable mechanism with a regression test.

## 17.1 The Problem and the Design Space: Compatibility Is a Gradient, Not a Switch

### 17.1.1 The Four-Level Gradient

[goal-prompt.md](../../docs/goal/goal-prompt.md) §0.10 decomposes "replacing CPython" into a gradient that must always be distinguished explicitly:

```text
source compatibility          user .py files, stdlib, packages, import,
                              exceptions, descriptors, metaclasses, async,
                              GC, weakref, threads run under CPython semantics
pcc-native extension ABI      a native C-extension ABI aimed at the pcc
                              runtime, with no libpython dependency
CPython C-API compatibility   incremental implementation of the public C-API
                              surface: PyModuleDef, PyMethodDef, capsules,
                              buffers, dict/list/tuple/unicode/int helpers
CPython binary ABI            arbitrary .so/.pyd files that assume CPython's
                              object layout, PyObject_HEAD, the GIL, private
                              APIs — must be an explicit compatibility mode,
                              never disguised as pcc-native
```

The gradient answers the first design question: why not aim straight for binary ABI compatibility? Because pcc's object header is the 16-byte `PyObjectHeader` (refcount + type_tag + flags; see Chapter 7), fundamentally different from CPython's `ob_refcnt + ob_type` layout. A "compatibility" that pretends the layouts match becomes memory corruption at the first extension that reads a field directly. pcc therefore splits C-API compatibility (source-level: the extension is recompiled against pcc's own `Python.h`) from binary ABI compatibility (linking CPython's actual libpython) into two distinct paths, separately named and separately measured.

### 17.1.2 Two Acceptance Surfaces

The split produces two *acceptance surfaces*, which accept different sets of artifacts and fail in different ways:

- **pcc-native**: the extension must be compiled against pcc's narrow `Python.h`/C-API shim, export a `PyInit_<leaf>()` that returns a pcc `PyObject*`, and be dlopen'd by [pcc/runtime/src/py_extension_loader.c](../../pcc/runtime/src/py_extension_loader.c) in a process containing no libpython. Any native artifact whose name carries a CPython extension-ABI marker (`cpython-NN`, `cpNN-cpNN`, `abi3`) is rejected.
- **cpython-compat / libpython**: the compiled program links the host CPython's libpython, and third-party imports trampoline into the `py_cpy_*` wrapper layer in [pcc/runtime/src/py_libpython.c](../../pcc/runtime/src/py_libpython.c). That file's design comment states the key decision: CPython's `PyObject*` and pcc's own `PyObject*` are **two disjoint pointer namespaces** — the former is exposed to codegen only as an opaque `void*`, and the two never alias.

Each surface is honest in its own way: pcc-native rejects what it cannot run and emits a diagnostic code; cpython-compat accepts CPython ABI artifacts but states plainly that it depends on libpython. The claim-hygiene table (§0.10) entry `cpython-compat pass != pcc-native pass` exists precisely to forbid presenting a pass on one surface as a capability of the other.

### 17.1.3 The Generic-Mechanism Principle

The third design decision is written into north-star obligation 3 in AGENTS.md: **ecosystem support must be generic**. NumPy, PyTorch, pandas, Arrow, and SciPy are integration targets, never compiler special cases; `if package == "numpy"` is prohibited, and what gets fixed is the reusable mechanism — install, import, ABI, buffer, capsule, build surface — with a regression test for the generic feature. The principle has hard engineering reasons behind it: package-name special-casing does not scale (every package accretes its own private logic), is not testable (you test the name, not the mechanism), and manufactures false claims ("supports NumPy" actually meaning "hard-coded NumPy's paths"). Every module discussed in this chapter echoes the principle in its own docstring: [pcc/package/install.py](../../pcc/package/install.py) opens with "This is a real local/cache install skeleton, not a NumPy-specific shortcut"; the include-redirection comment in `build_exec.py` insists "Generic — no package-specific rules".

## 17.2 Two Gates: pip install and import Are Two Claims

### 17.2.1 Why the Gates Must Be Separate

The "Package / NumPy Claim Hygiene" section of AGENTS.md fixes the rule in writing:

- `pcc1 -m pip install numpy ...` succeeds **only** when a real package artifact is installed into the target site and its package metadata is usable;
- `import numpy` is **a separate gate**. "NumPy support" must never be inferred from install success, from an array-core-only test, or from a synthetic package that happens to be named `numpy`.

The reason for the split is that the two gates fail in entirely different subsystems. The install gate exercises artifact resolution, wheel-tag compatibility, unpacking, manifest writing, and linkage scanning — all of it inside [pcc/package/](../../pcc/package). The import gate exercises module resolution, lowering, extension loading, the C-API shim, and the object model — spread across `pipeline.py`, `import_lowering.py`, and the runtime C code. A merged "NumPy support" claim would stir the two bodies of evidence into one, and the §0.10 claim-hygiene table exists to prevent exactly that mixing:

```text
fake package pass      != real package pass
array-core pass        != import numpy pass
metadata exists        != runtime implementation complete
```

[pcc/package/array_core.py](../../pcc/package/array_core.py) is the concrete footnote to the `array-core pass != import numpy pass` line: it is a reporting front door for a generic set of array-core semantics (layout semantics for arange/reshape/matmul and friends). No matter how complete it becomes, it constitutes zero evidence for `import numpy` — it never touches NumPy's C code at all.

### 17.2.2 The Evidence Standard for the Install Gate

The executable form of the install gate lives in [tests/python/test_package_import_path.py](../../tests/python/test_package_import_path.py). Take `test_pcc1_pip_install_numpy_name_from_find_links_command_shape`: its assertions escalate layer by layer — the JSON output of `pcc1 -m pip install numpy --no-index --find-links <dir> --target <site>` has `ok` true, the parsed package name is correct, `installs[0]["source_path"]` points at a real wheel file, and package files actually appear under the site directory. Note the honesty in the test's name: `command_shape`. What it installs is **a synthetic package constructed to wheel naming conventions**; what it proves is the command shape and the install pipeline, not real NumPy. The install evidence for real NumPy is a different test, `test_pcc1_pip_install_real_numpy_artifact_opt_in`, opted into explicitly via `PCC_RUN_REAL_NUMPY_INSTALL=1` and `PCC_NUMPY_ARTIFACT`. In the same file, the fake package and the real package are two tests and two bodies of evidence.

### 17.2.3 The `PCC_HOST_PYTHON=/bin/false` Evidence Technique

One further detail decides the strength of the install evidence. `pcc1` is a compiled native binary, but several of the repository's bootstrap host queries are allowed to escape to a host Python subprocess via the `PCC_HOST_PYTHON` environment variable. So "pcc1 installed the package" has a covert weakened form: pcc1 is only the shell, and the actual work is quietly done by the host Python. The evidence technique is to point the escape channel at a program guaranteed to fail: the tests above uniformly set `env["PCC_HOST_PYTHON"] = "/usr/bin/false"` (the AGENTS.md prose says `/bin/false`; the principle is the same). From then on, any silent appeal to the host Python becomes an immediate hard failure, so a passing test proves the entire install chain genuinely ran in pcc1's own native code. This is an evidence construction worth generalizing: **rather than asserting "the host was not used", make "using the host" necessarily fail** — converting a negative claim into an executable gate.

### 17.2.4 The Evidence Standard for the Import Gate

The import gate is likewise layered. For pure-Python packages, the import evidence is end-to-end: after installing, compile a main program containing `import demo_pkg` with `pcc1 --python-libpython=off --ir-scaffold=on`, run it, and assert on the output (`test_pcc1_pip_install_wheel_participates_in_import_site` asserts that `43` is printed). Where a **CPython-ABI** C extension is involved, the current honest evidence in no-libpython mode is, perversely enough, **rejection**: a real CPython extension wheel is stopped at the import boundary with `PCC-PKG-004` (Section 17.4) rather than silently generating thousands of `py_cpy_*` fallback calls.

At the frontend package scanning and linkage layer, [pcc/package/linkage.py](../../pcc/package/linkage.py) issues a rejection diagnostic for CPython-ABI extension artifacts:

```python
# pcc/package/linkage.py
def _diagnostic_for_cpython_extension_abi(path: str) -> dict[str, object]:
    return {
        "code": "PCC-PKG-004",
        "message": (
            "native artifact name declares a CPython extension ABI; "
            "pcc-native mode requires a pcc-native extension ABI or a source rebuild"
        ),
        "path": path,
    }
```

## 17.3 The Package Pipeline: Generic Machinery in [pcc/package/](../../pcc/package)

### 17.3.1 The pip Front Door

On the C-API shim side, [pcc/runtime/src/py_capi_shim.c](../../pcc/runtime/src/py_capi_shim.c) implements the proxy for standard C-API functions:

```c
// pcc/runtime/src/py_capi_shim.c
PyObject *py_capi_PyObject_CallObject(PyObject *callable, PyObject *args) {
    if (callable == NULL) return NULL;
    return py_call_callable(callable, args, NULL);
}

void *py_capi_PyCapsule_GetPointer(PyObject *capsule, const char *name) {
    if (capsule == NULL || py_type_of(capsule) != PY_TYPE_CAPSULE) return NULL;
    return py_capsule_pointer(capsule, name);
}
```

In the native extension loader, [pcc/runtime/src/py_extension_loader.c](../../pcc/runtime/src/py_extension_loader.c) loads pcc-native `.so` binaries via `dlopen` and invokes `PyInit_<mod>`:

```c
// pcc/runtime/src/py_extension_loader.c
PyObject *py_extension_load_native_so(const char *so_path, const char *mod_name) {
    void *handle = dlopen(so_path, RTLD_NOW | RTLD_GLOBAL);
    if (handle == NULL) return NULL;
    char init_name[256];
    snprintf(init_name, sizeof(init_name), "PyInit_%s", mod_name);
    PyInitFunc init_fn = (PyInitFunc)dlsym(handle, init_name);
    if (init_fn == NULL) return NULL;
    return init_fn();
}
```

[pcc/package/pip_shim.py](../../pcc/package/pip_shim.py) is the front door for `pcc -m pip`, and its docstring sets the boundary first: it accepts the common `pip install ... --dry-run` shapes and reports a plan without invoking pip's installer; non-dry-run local installs go through pcc's own installer rather than upstream pip. `_parse_install_args` recognizes `--target`, `--cache-dir`, `--find-links`, `--index-url`, `--no-index`, `--report` — and one flag pip does not have: `--abi` (defaulting to `pcc-native`). The ABI mode starts flowing from the very first station on the command line, and the compatibility decisions, linkage scanning, and build redirection downstream all dispatch on it.

### 17.3.2 Artifact Resolution and Wheel Tags

`_artifact_compatibility_reason_from_name()` in [pcc/package/install.py](../../pcc/package/install.py) is the single admission point for artifacts in pcc-native mode. Source artifacts (`.tar.gz` and friends) are admitted with reason `source_artifact` — source can always be rebuilt with pcc's toolchain. `py3-none-any` pure-Python wheels are admitted with reason `pure_python_wheel`. Wheels whose tag equals `pcc_native_wheel_tag()` are admitted with reason `pcc_native_wheel`. Every other wheel is rejected with reason `wheel_tag_not_pcc_native_compatible`. `pcc_native_wheel_tag()` in [pcc/package/metadata.py](../../pcc/package/metadata.py) produces a tag of the form `pcc{major}-pcc_native-{platform}`: pcc registers itself as a first-class platform within the wheel naming convention, rather than impersonating CPython's `cp3xx` tags. This is the generic-mechanism principle again — every compatibility decision is grounded in artifact naming conventions and the ABI mode, with no package name anywhere in the logic. The local wheel repository (the `pcc-wheel-repository.json` manifest; see `_repository_manifest_candidates()`) is filtered the same way, by its `pcc_native_compatible` and `links_libpython` fields, never by package name.

### 17.3.3 Linkage Scanning: PCC-PKG-003 and PCC-PKG-004

[pcc/package/linkage.py](../../pcc/package/linkage.py) enforces the no-libpython claim at the install boundary. `_LIBPYTHON_PATTERNS` scans link commands and native artifact bytes for four kinds of evidence — `libpythonX.Y`, `-lpython`, `Python.framework`, `pythonXY.dll` — and a hit produces diagnostic code `PCC-PKG-003`. `_CPYTHON_EXTENSION_ABI_RE` recognizes CPython extension ABIs by name (`cpython-\d+`, `cp\d+-cp\d+`, `abi3`); a hit produces `PCC-PKG-004`. The decision logic in `linkage_report()` writes the two acceptance surfaces down as boolean algebra: `links_libpython` is acceptable only when `abi_mode == "libpython"`; `uses_cpython_extension_abi` is acceptable in both the `libpython` and `cpython-compat` modes; and `no_libpython_runtime` is true only with zero libpython edges, zero CPython ABI, and mode `pcc-native`. `install_package()` writes this report wholesale into the `pcc-package.json` manifest of every install root — the later import gate need not reinvent the judgment, it can read the manifest. (It still rescans anyway; Section 17.4 explains why.)

### 17.3.4 The Build Surface: Include Redirection

For a source artifact to become a pcc-native extension, it must be compiled against pcc's `Python.h` — but a real package's build system (NumPy uses meson) bakes CPython's `-I` paths into `compile_commands.json`. The solution in [pcc/package/build_exec.py](../../pcc/package/build_exec.py) is two generic functions. `_materialize_pcc_capi_include()` materializes **only** the eight C-API headers listed in `_PCC_CAPI_HEADERS` (`Python.h`, `structmember.h`, `pymem.h`, `frameobject.h`, `pythread.h`, `pyerrors.h`, `abstract.h`, `datetime.h`) out of [utils/fake_libc_include/](../../utils/fake_libc_include) into `<build>/pcc-package/pcc-capi-include`. `_redirect_pcc_native_includes()` drops `-I`/`-isystem` flags pointing at CPython header directories per `_CPYTHON_INCLUDE_DIR_RE`, and **appends** pcc's C-API directory and [pcc/runtime/include](../../pcc/runtime/include) at the end of the command — the package's own headers always win, and pcc only fills the `Python.h` hole left by the dropped directories.

"Only eight headers" is not stinginess; it is a real lesson fossilized into code. [utils/fake_libc_include/](../../utils/fake_libc_include) as a whole contains stub versions of `math.h` and `complex.h`, and putting the entire directory on the include path shadows the real system libm that NumPy's C core needs. The redirection takes effect only when `abi_mode == "pcc-native"` and the language is C; if the headers cannot be located, it emits the `PCC-PKG-CAPI-INCLUDE-MISSING` diagnostic and skips, rather than building in a broken state.

## 17.4 The Import Gate: Two Rejection Points and One Fallback Path

### 17.4.1 Failing Early at the Package Boundary

Under `--python-libpython=off`, `_validate_package_site_no_libpython_abi()` in [pcc/frontends/python/pipeline.py](../../pcc/frontends/python/pipeline.py) rescans the install roots of every package participating in compilation; any native extension whose name carries a CPython ABI marker fails the compile on the spot, with an error carrying `PCC-PKG-004` and remediation guidance ("reinstall with --abi=pcc-native from source, or choose an explicit --abi=libpython / --abi=cpython-compat mode"). Its docstring explains why it rescans instead of trusting the install-time manifest: an old install may predate the existence of the ABI gate; and rather than letting codegen later generate "thousands of opaque `py_cpy_*` fallback calls", it is better to fail at the package boundary with an actionable message. This is pcc's error philosophy in miniature: fallback boundaries must be honest, and failure should happen at the place best able to explain itself.

### 17.4.2 Resolving and Lowering pcc-Native Extensions

A pcc-native extension that passes the ABI gate is resolved by `_resolve_pcc_native_extension_path()` in [pcc/frontends/python/codegen/import_lowering.py](../../pcc/frontends/python/codegen/import_lowering.py), which searches each site root in `PCC_PACKAGE_SITE` by mapping the dotted module path to a directory path plus one of `{.so, .dylib, .pyd, .dll}`. Candidates whose names carry CPython ABI markers are skipped here as well — the rejection logic is deliberately repeated in the lowering layer, making the two gates mutually redundant. On a hit, `_emit_native_extension_import()` emits a call to the runtime's `py_native_extension_import`, immediately followed by an `_emit_post_call_err_check()`: as Chapter 8 explained, pcc's exception model has no stack unwinding, so every runtime call that may raise must be followed by an explicit `py_err_occurred()` check — extension import is no exception.

### 17.4.3 The cpython-compat Trampoline

In libpython mode, third-party imports lower to calls like `py_cpy_import`, entering the wrapper layer of `py_libpython.c`: `Py_Initialize` is called lazily on the first import, `Py_Finalize` is registered with `atexit`, and every CPython API call holds the GIL. The two-pointer-namespace discipline becomes an executable constraint here: a CPython reference held on the pcc side is a `void*`, and entering pcc's object graph requires either explicit conversion — `py_cpy_to_pcc_obj()` converts None/bool/int/float/str/list/tuple/dict/set recursively, with unsupported values degrading to `str(obj)` — or boxing (the CpyHandle of Section 17.6).

## 17.5 The C-API Shim: From Symbol Catalog to Object-Model Bridge

**August 2026 implementation note.** The discussion of `src/py_capi_shim.c` below is retained as mechanism lineage and host-C oracle documentation, but that file is no longer the owner in the production pcc-Python archive. Current production implementation is distributed across `pcc/runtime/py/py_capi_*_runtime.py`: exception data, dict/object/type/unicode/capsule/buffer, module-state, descriptor, variadic-call, and visit surfaces each have Python owners; `py_extension_loader_runtime.py` owns native extension loading; and `py_obj_dealloc.py` owns the CpyHandle ABI. The `LIB_PCC_PY` rule in `pcc/runtime/Makefile` archives only `PCC_PY_OBJECTS`. The C-shim details that follow should therefore be read as ABI semantics and migration history, not as a claim that production still links one hand-written C shim. Chapter 14 gives the source-ownership and final no-C/zero-libc acceptance boundary.

### 17.5.1 An Executable Priority Map

The docstring of [pcc/frontends/c/capi_surface.py](../../pcc/frontends/c/capi_surface.py) begins by declaring what it is not: "This is not an implementation of every C-API symbol. It is the executable priority map used by extension-loader work so gaps are explicit and tested." Each symbol is a `CApiSymbol(name, header, priority, implemented, notes)` record, with the priority enum `CApiPriority` running from `IMPORT_BLOCKER` (0) through `RUNTIME_CORE`, `ARRAY_CORE`, and `NUMPY_CAPI` up to `ACCELERATION` (5). The value of this catalog is that it turns "the gap" into data: `extension_abi_plan()` accepts a set of required symbols (expandable in bulk via `require_capsule`/`require_buffer`/`require_memoryview`/`require_numpy_capi`) and outputs structured diagnostics — `PCC-EXT-MISSING-CAPI-SYMBOL` (in the catalog but not implemented), `PCC-EXT-UNKNOWN-CAPI-SYMBOL` (not in the catalog), `PCC-EXT-MISSING-CAPI-HEADER` (header missing), `PCC-EXT-ABI-VERSION-MISMATCH` (version mismatch). Notably, the NumPy C-API symbols (`PyArray_*`/`PyUFunc_*`) are explicitly marked `implemented=False` in the catalog, with `_NUMPY_CAPI_TABLE_SLOTS` metadata attached (capsule table name, slot number, failure mode) — the unimplemented portion is not omitted, it is precisely registered. [pcc/frontends/c/capi_abi.py](../../pcc/frontends/c/capi_abi.py) complements this with a minimal seven-symbol core table whose `extension_import_blockers()` answers "what is still missing before import works" directly.

### 17.5.2 The Shim's Self-Imposed Limits and PyModuleDef

The comment at the top of [pcc/runtime/src/py_capi_shim.c](../../pcc/runtime/src/py_capi_shim.c) is the contract for this file of more than five thousand lines: "deliberately narrow ... It does not claim CPython binary object-layout parity." The shim carries its own set of C-API type definitions (`Py_buffer`, `PyMethodDef`, `PyModuleDef`), and the comment above `PyModuleDef` states a layout invariant: it **must match the `PyModuleDef` in [utils/fake_libc_include/Python.h](../../utils/fake_libc_include/Python.h) exactly** — extensions are compiled against the latter, the shim reads `m_slots` through the former, and any drift between the two is an out-of-bounds read. This is isomorphic to the C/pcc-Python mirror-layout discipline of Chapter 7, except this time the two ends of the mirror are "the header the extension sees" and "the runtime's own struct".

Multi-phase initialization (PEP 489) is handled by a plain but effective marker trick: `PyModuleDef_Init()` stamps the address of the static variable `pcc_capi_moduledef_marker` into `def->m_base.ob_base`; the loader checks the `PyInit_*` return value with `pcc_capi_is_moduledef()` — a real module's first 8 bytes are a refcount and cannot equal that address. On a hit, control passes to `pcc_capi_module_exec()`: first `PyModule_Create2()` builds the module, then the `m_slots` array is walked and each `Py_mod_exec` slot is executed (this is exactly where NumPy registers its types and the `PyArray_API` capsule).

`PyModule_Create2()` reveals the shim's overall strategy: **play the C-API concepts using pcc's own objects**. A module is simply a pcc instance produced by `py_instance_new()`; `__name__` is an instance attribute; `METH_VARARGS` methods become callable attributes on the instance via `py_func_new()`. Module state for `m_size > 0` is allocated with `calloc` and registered in a state table; `pcc_capi_visit_extension_module_state_roots()` lets all five GC backends treat the module object and the state references reported by `m_traverse` as roots and pin them — pcc objects living inside extension module state must not vanish under GC. That invariant was fossilized by a real investigation on 2026-05-31 (`gc-5backend-extension-module-state-roots-no-libpython.md`). Capsules are pcc instances too: the pointer, name, and destructor are stored as attributes such as `__pcc_capsule_pointer__` (the pointer boxed via `PyLong_FromVoidPtr`), and `PyCapsule_GetPointer`/`PyCapsule_IsValid` do name matching with CPython semantics.

Boundary semantics show up in the small details: `PyModule_GetDict()` calls `py_decref` on the result immediately after `py_obj_getattr` returns an owned reference, then returns it — because the CPython contract specifies a borrowed return. Every function in the C-API shim stands on the seam between two refcounting conventions, and explicit ownership adjustments of this kind are the main substance of the shim's correctness.

### 17.5.3 The Buffer Protocol

The buffer protocol has two implementations with distinct jobs. [pcc/library/buffer_protocol.py](../../pcc/library/buffer_protocol.py) is the Python-side planning model: the `PyBUF_*` flag constants plus a `BufferView` dataclass, with `check_flags()` raising `BufferError` per CPython semantics (request validation for writability, shape, strides). It serves package planning and tests; it never touches memory. The implementation extensions actually use lives in the shim: `pcc_capi_buffer_data()` recognizes pcc's bytes (read-only), bytearray (writable), and memoryview — recursing through the base read via `pcc_gc_load_ptr()`; note that even inside the C-API shim, pointer-slot reads go through the GC read barrier, because Chapter 10's barrier discipline has no exempt zones. `PyObject_GetBuffer()` fills out a one-dimensional contiguous `Py_buffer` with `itemsize` 1 and format `"B"`; when shape/strides are requested it hangs a `PccBufferMeta` off `view->internal` and takes a `py_incref` on the exporting object; `PyBuffer_Release()` symmetrically decrefs and frees. This is an honest narrow implementation: sufficient for the SIMPLE/one-dimensional cases of `bytes`/`bytearray`/`memoryview`, with no pretense of multi-dimensional strided views.

### 17.5.4 The Type Bridge and ob_type: the Hardest Boundary

The deepest problem is the object model itself. An extension's static `PyTypeObject`, initialized through `PyObject_HEAD_INIT`, ends up with `type_tag` equal to 0 under pcc's header layout — which is `PY_TYPE_NONE`, so pcc cannot distinguish a C extension type object from `None`. Extension instance structs (`PyObject_HEAD` followed immediately by their own fields) likewise collide with pcc's `PyInstanceObject` layout. The investigation `python-no-libpython-numpy-build-pcc-capi-include-redirect.md` records the bridge's design and landing: `PyType_Ready()` assigns each readied type a **dynamic type_tag** (base 0x10000, above the built-in enum) and registers it in a tag→`PyTypeObject*` registry; `PyType_GenericAlloc/New` allocate by `tp_basicsize` through `pcc_gc_alloc` and stamp the dynamic tag, so extension instances keep their own layout; `Py_TYPE` consults the registry for dynamic tags and maps built-in tags to built-in type recognition tokens defined inside the shim (`PyLong_Type` and friends — tagged small ints map directly to `&PyLong_Type`); subtype checks walk the `tp_base` chain.

Then NumPy code that reads `((PyObject*)x)->ob_type` directly exposed the ceiling of the tag scheme: there is no such pointer in pcc's header, and a field access is something no header file can fake. The eventual fix was a coordinated layout change across seven sites — `PyObject_HEAD`/`PyVarObject` grew an `ob_type` slot, the initializer macros and `struct PyObject` moved in lockstep, the hand-laid `PyTypeObject` layout mirror inside the shim shifted accordingly, and `PyType_GenericAlloc` became responsible for filling the slot. The validation standard for that change is worth remembering: not a toy extension test passing, but the full three-stage `scripts/bootstrap.sh --backend self` printing pcc2/pcc3 byte identity. For a change to a shared runtime layout, only the fixed point is qualified to say "nothing broke".

## 17.6 The Extension Loader and CpyHandle

### 17.6.1 The Loader

`py_extension_loader.c` is the dlopen entry point of the pcc-native surface, and its logic is deliberately simple: look up a cache list by module name and path; on a miss, `dlopen(path, RTLD_NOW | RTLD_GLOBAL)`, `dlsym` the `PyInit_<leaf>` symbol (leaf being the last segment of the dotted path), and call it. If the return value is recognized by `pcc_capi_is_moduledef()` as a module definition, control transfers to `pcc_capi_module_exec()` to run multi-phase initialization. A successful module enters a cache node and is `pcc_gc_pin()`-ed — modules do not participate in collection for the life of the process, matching the de facto immortality of CPython modules. `py_native_extension_import_by_name()` provides lookup by name: it tries the four suffixes `.so`/`.dylib`/`.pyd`/`.dll` under each root in `PCC_PACKAGE_SITE` (colon-separated; semicolons on Windows). All error paths funnel through `pcc_extension_runtime_error()` into a RuntimeError carrying the dlerror text — a dlopen failure is visible and diagnosable to the user, never a silent fallback.

### 17.6.2 CpyHandle: Boxing Foreign References

cpython-compat mode poses an object-graph puzzle: a suspended generator frame can hold only pcc objects — frame saving goes through `py_list`'s store barrier, and frame teardown dereferences by pcc object headers — yet a generator local may be a CPython reference obtained from `py_cpy_*`. [pcc/runtime/src/py_cpy_handle.c](../../pcc/runtime/src/py_cpy_handle.c) (type tag `PY_TYPE_CPY_HANDLE = 32`, defined in `py_runtime.h`) supplies the boxing answer: `PyCpyHandleObject` is a pcc object header plus one `void *cpy_ref` field, and the file's comment stresses that this field is "**NOT** a pcc slot" — the GC never interprets the foreign pointer. `py_cpy_handle_new()` takes ownership of the foreign reference, `py_cpy_handle_get()` borrows it, and `py_dealloc_cpy_handle()` returns the foreign reference at destruction through a registered release hook — so dropping a suspended generator releases the live CPython iterator it holds **structurally**, with no special-case cleanup code anywhere.

Two details show how runtime layering and the five-GC equality contract constrain even a small file. First, the release hook `py_cpy_handle_set_release_fn()` exists because of an archive boundary: `py_cpy_handle.c` lives in the main runtime archive while `py_cpy_decref` lives in the separate libpython archive; a process that never initializes the libpython bridge can never have produced a foreign reference, so a NULL hook is safe — the dependency direction runs only from the bridge into the main archive, never the reverse. Second, a new type tag must be wired into every dispatch point of the object lifecycle: both destructor switches — in `py_obj.c` and in `py_gc_backend.c` — register `py_dealloc_cpy_handle`, and backend #4's `pcc_gc_relocate_copy_supported_tag()` whitelist gained the tag too, with a side note explaining that a CpyHandle has no pcc pointer slots, so shallow-copy relocation is as safe as for str. A 58-line C file whose interface spans destructor dispatch, a relocation whitelist, and archive link topology — that is the true cost of "adding a runtime type" in pcc.

## 17.7 History and Lessons

### 17.7.1 A Stale C-API Header Produces a False "Gap 0" (2026-05-29)

The measurement accident recorded in `python-no-libpython-numpy-build-pcc-capi-include-redirect.md` is this repository's cleanest specimen of *measurement-substrate rot*. Background: to answer "how many host C-API symbols does NumPy still need from pcc", the workflow compiled NumPy `_core`'s C files into `.o` objects against `/tmp/pcc_capi` (the curated headers copied out of [utils/fake_libc_include/](../../utils/fake_libc_include)), then used `nm` against the runtime archive to count unprovided symbols. After a dozen-plus batches of symbol implementation, batch 17 announced a milestone: "FULL-MODULE host C-API LINK gap = 0", and a follow-on extended measurement concluded "the 506 remaining symbols are all NumPy-internal; zero gap on the host side".

Both conclusions were wrong. A section of the investigation titled CORRECTION records the root cause: the 60 `.o` files used for the measurement had been compiled against a **stale** `/tmp/pcc_capi/Python.h` — its mtime predated a batch of declarations added later in the same session. The stale header failed to compile 38 files, so those 38 were **silently** absent from the `.o` set, and the host symbols they reference never entered the count. The false assumption was "the compilation substrate doesn't change"; the evidence chain was header mtimes and file counts (60/98 versus 95/98). Refreshing `/tmp/pcc_capi` and re-measuring: 95 files compile, and the real gap is **10 symbols, not 0** — `PyArg_UnpackTuple`, `PySlice_New`, `PyUnicode_Format`, and others, all of the form "declared, so the file compiled; no runtime implementation, so the link fails".

The fix itself was unremarkable: batches 18 and 19 routed the 10 symbols to genuine pcc primitives and reached gap 0 again — this time the investigation pointedly writes "(CORRECTLY verified)" and attaches the measurement substrate (95 fresh `.o` files, current headers); after batches 20 and 21 closed the last three compile gaps, the re-measurement of 98/98 compiling with link gap 0 was performed "refreshed /tmp/pcc_capi per the stale-header lesson". Three invariants remain. Refresh the curated header directory from [utils/fake_libc_include/](../../utils/fake_libc_include) before measuring (now fossilized as a workflow rule). Every "gap 0"-class claim must carry a description of its measurement substrate. And one meta-lesson: **the false milestone was retracted with a written, public CORRECTION, not quietly overwritten.** Batch 17's wrong conclusion stands verbatim in the investigation file, followed by the correction, so the next agent who reads "gap 0" can see exactly how it once went wrong. Claim hygiene governs not only how success is claimed, but how it is withdrawn.

### 17.7.2 A Bug That Looked Like NumPy's, but Nothing Could Run (2026-05-29)

The second story comes from the other acceptance surface. `python-cpython-compat-import-numpy-multiarray-init-fails.md` records that in cpython-compat mode (`--python-libpython=on`), `import numpy` made it all the way through the pure-Python loading and died on the core C extension — `SystemError: execution of module numpy._core._multiarray_umath failed without setting an exception`. The same NumPy ran fine directly under the same CPython.

The most convenient wrong hypothesis: NumPy's C-API demands are huge, and pcc is missing some symbol. The investigation explicitly resisted that direction — the symbol surface was already 384/406 at the time, and the import had lowered correctly to `cpy.import.numpy`; the "without setting an exception" signature could just as plausibly point to mismatched runtime state in the embedded interpreter. The decisive step was an isolation experiment: swap in the smallest possible target, `import unicodedata` — a trivial stdlib C extension that **ships with** the host libpython and needs no PYTHONPATH setup at all. It failed in exactly the same way: the same SystemError, plus a missing module attribute (`AttributeError: unidata_version`) — showing that the module body of the `Py_mod_exec` slot never finished executing.

The conclusion rewrote the problem itself: this was not a NumPy bug but a **generic** bug in pcc's libpython embedding layer when executing C-extension multi-phase initialization; the investigation concedes in its own words that "this file's title is therefore narrower than the root cause; numpy is just the motivating case". The lesson mirrors the generic-mechanism principle exactly. The principle forbids writing a generic mechanism as a package special case; this investigation shows that **diagnosis must not be package-special-cased either** — before opening an investigation on a big target's failure, isolate with a minimal target of the same class; and if the minimal target fails identically, the fix point must move up into the generic mechanism (here, the extension-load execution path around `_imp.create_dynamic`/`exec_dynamic`), unlocking every `.so` with one fix instead of fixing NumPy once and pandas again later.

## 17.8 Summary

The package and extension subsystem is where pcc's honesty boundaries are tested hardest. This chapter's mechanism and discipline fold into five points:

1. **Compatibility is a gradient.** Source compatibility, the pcc-native extension ABI, CPython C-API compatibility, and CPython binary ABI compatibility are four claims of different rank; pcc-native and cpython-compat are two acceptance surfaces, and `py_libpython.c` keeps the latter out of pcc's object graph with two disjoint pointer namespaces.
2. **The two gates are independent.** The pip-install gate's evidence is a real artifact landing in the site with usable metadata (`PCC_HOST_PYTHON=/usr/bin/false` seals the host escape); the import gate is proved separately, and synthetic packages, array-core, and command shapes extrapolate to nothing.
3. **Mechanisms must be generic.** Admission looks at wheel tags and the ABI mode (`pcc_native_wheel_tag()`), rejection looks at naming conventions (`PCC-PKG-003`/`PCC-PKG-004`), build redirection looks at include-directory patterns — no decision anywhere takes a package name as input.
4. **The shim plays C-API concepts with pcc objects, and states its ceiling.** Modules and capsules are pcc instances; the buffer implementation is a narrow one-dimensional one; the type bridge uses dynamic type_tags — until direct `ob_type` field access forced a header layout change that only the bootstrap fixed point was qualified to validate.
5. **Measurement and retraction are both governed by claim hygiene.** The false gap-0 born of stale headers was withdrawn with a written CORRECTION; the failure that looked package-specific was promoted to a generic bug by a minimal isolation experiment.

## Exercises

1. **Verify against the source.** In [pcc/package/linkage.py](../../pcc/package/linkage.py), find all three necessary conditions for `no_libpython_runtime` to be true, and explain why, when `abi_mode == "cpython-compat"`, `uses_cpython_extension_abi` does not block `ok` yet still forces `no_libpython_runtime` to false. Which class of claim does each of the two fields serve?
2. **Trace a path.** Starting from `pcc -m pip install demo.whl --target site`, follow `pip_shim.py::pip_install_plan` → `install.py::install_package` → `linkage.py::linkage_report` and list the originating function for each of the three fields `links_libpython`, `pcc_native_wheel_tag`, and `diagnostics` in the `pcc-package.json` manifest.
3. **Boundary semantics.** `PyModule_GetDict()` in `py_capi_shim.c` calls `py_decref` on its result before returning it. Using the owned/borrowed reference contract of Chapter 9, explain why this line is necessary, what class of bug deleting it would produce, and why that class of bug is hard to catch with toy extensions.
4. **Argue a design tradeoff.** `PyType_Ready`'s dynamic type_tag registry (base 0x10000) and the later-landed `ob_type` header field are two coexisting mechanisms. What question does each answer? If pcc had instead added an `ob_type` pointer to every pcc object header from the start, with no tag registry, what costs would have been paid in which subsystems (allocation, GC relocation, the pcc-Python mirror, bootstrap)?
5. **Construct evidence.** Following the `PCC_HOST_PYTHON=/usr/bin/false` technique, design an executable gate that converts the negative claim "pcc1's extension loading does not silently fall back to the LLVM backend" into a necessarily-failing condition, and explain why it is stronger than merely asserting that no fallback records appear in a log.
