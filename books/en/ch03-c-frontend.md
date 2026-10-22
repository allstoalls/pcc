# Chapter 3: The C Frontend — Parsing, fake-libc, and the Evaluator

The C frontend is the most mature subsystem in the pcc repository: it has compiled and run real projects at the scale of Lua, SQLite, PostgreSQL `libpq`, zlib, lz4, zstd, PCRE, and OpenSSL. This chapter covers how a C source file — or a directory of them — becomes parseable translation units (TUs) and flows through the evaluator pipeline. Concretely: the parser's dual-track structure from PLY to a native LR driver, the two preprocessing paths, the declaration-only fake libc under [utils/fake_libc_include/](../../utils/fake_libc_include), the preprocess→parse→IR→optimize→execute pipeline in [pcc/frontends/c/evaluator/c_evaluator.py](../../pcc/frontends/c/evaluator/c_evaluator.py), and source collection plus `--sources-from-make` in [pcc/driver/project.py](../../pcc/driver/project.py). How expressions are lowered to LLVM IR and how signedness is tracked belongs to Chapter 4; this chapter stops at the line where the AST enters the code generator.

## Chapter Overview: Read the C Frontend as Four Gates

You do not need to start with C grammar details. Start with four gates: preprocessing narrows host-header problems, fake-libc provides controlled declarations, the parser turns tokens into an AST, and the evaluator connects that AST to LLVM and real-project tests.

- When a C project fails, first separate source discovery, headers/declarations, parsing, and semantic lowering.
- fake-libc is not a libc implementation; it is the declaration boundary shown to the compiler.
- Real-project tests matter because many bugs appear only when preprocessing, parsing, lowering, and linking are combined.

## 3.1 The Problem and the Design Space

The question the C frontend has to answer is not "how do you parse C" — textbooks settled that long ago — but "how do you parse C **as it actually ships**." The gap between those two questions is the entire design space of this chapter:

1. **Real C is only C after preprocessing.** Expanding `#include <stdio.h>` against host system headers yields tens of thousands of lines saturated with compiler extensions: `__attribute__`, nested `__builtin_*` calls, platform conditionals. A frontend that does not intend to reimplement GCC must decide at which layer this material gets stopped.
2. **Declarations and implementations can be separated.** Parsing and type checking need the prototype of `printf`, not its implementation — the implementation arrives at link time from the real libc. That observation is the root of the fake-libc design.
3. **Real projects do not come with a list of source files.** They come with Makefiles, configure scripts, amalgamations, and TUs that conditional compilation moves in and out of the build. The frontend's entry point is not `parse(file)`; it is "recover the set of participating `.c` files and preprocessor flags from a directory and its build system."

For the parser proper, the design space had three candidates: write a full C frontend from scratch, bind clang's AST, or reuse pycparser. pcc chose the third: the file header of [pcc/frontends/c/parse/c_parser.py](../../pcc/frontends/c/parse/c_parser.py) still carries pycparser's copyright notice (Eli Bendersky, BSD), and the grammar comments state plainly that it implements the BNF of K&R2 appendix A.13. The reasoning is pragmatic: pycparser's grammar and AST have been hardened by more than a decade of real-world code, while binding clang would turn the "frontend" into a wrapper around an enormous external C++ dependency — in direct conflict with the self-hosting goal laid out in Chapter 1. But reuse was a starting point, not a destination: pycparser depends on PLY, and PLY builds its parse tables dynamically at runtime using Python reflection, which is a liability for the bootstrap track where pcc must compile itself. So the C parser evolved along a dual track — the PLY version retained as a reference, the default path replaced by a PLY-free native LR driver (Section 3.2).

Preprocessing is likewise dual-track: a pure-Python built-in preprocessor ([pcc/frontends/c/preprocessor.py](../../pcc/frontends/c/preprocessor.py)) for environments without a system compiler, and a main path that borrows the system `cc -E` while substituting fake libc headers for the real system headers. Both tracks share one stance: **text-level reshaping is legitimate at the preprocessing boundary, and illegitimate at the IR layer.** The repository's IR Fix Policy permits exactly one remaining text-level IR rewrite, the `va_arg` path (see Chapter 12); by contrast, this chapter is full of regexes and character scanners — because the preprocessing layer's job is precisely to reshape the host's world into a C subset the parser accepts. That is the boundary layer doing its job, not a fig leaf over hacks.

## 3.2 The Parser: from PLY to a Native LR Driver

### 3.2.1 The pycparser Inheritance and the Lexer Hack

C is not context-free. Whether `A * b;` is a declaration or a multiplication depends on whether `A` was previously typedef'd — the classic lexer hack: the lexer must consult the parser's symbol table to decide between returning `TYPEID` and `ID`. `CParser` implements this with a scope stack: `_scope_stack` is a stack of dictionaries, `_add_typedef_name()` marks a name as a type in the current scope, `_add_identifier()` marks it as an object, and `_is_type_in_scope()` searches outward. The lexer receives the callback `_lex_type_lookup_func()`, and pushes/pops scopes at `{`/`}` via `_lex_on_lbrace_func`/`_lex_on_rbrace_func`.

Two timing subtleties hide in this machinery, both documented as real traps in the source comments:

- **The declaration rule is split into `decl_body SEMI`.** If `declaration` were a single rule, the LALR parser would request the lookahead token *past* the `SEMI` before reducing — and that token might be the very type name typedef'd on the current line, read before the symbol table is updated. With the split, `decl_body` reduces at the `SEMI` and writes the symbol table first, so the lexer sees the new type before the next line is tokenized.
- **Function-definition parameters must enter scope the instant `{` is seen.** The comment in `p_direct_declarator_6` gives the example: `typedef char TT; void foo(int TT) { TT = 10; }` — inside the body, `TT` is a parameter, not a type. Waiting for a yacc rule to fire is too late (the lookahead token has already been mislexed), so the parser probes the lexer's `last_token` via `_get_yacc_lookahead_token()` and registers the parameter names immediately when it is `LBRACE`.

On top of the pycparser baseline, pcc's grammar carries extensions that real projects forced in (all findable as productions in `c_parser.py`): the `INT128` and `_FLOAT16` type specifiers, computed goto (`goto *expr;` → `c_ast.ComputedGoto`), GNU statement expressions (`({ ... })` → `c_ast.StmtExpr`), two call shapes for `BUILTIN_VA_ARG`, `_Static_assert`, `_Generic`, `OFFSETOF`, `nullptr`, `_Alignas`/`_Alignof`, `_Thread_local`, and GNU range designators `[0 ... 5] =` (→ `c_ast.RangeDesignator`).

### 3.2.2 PLY Table Caching and the Version-Number Discipline

PLY constructs its LALR tables on first run, at noticeable cost, so `CParser.__init__` persists them to disk. The cache module names are versioned constants:

```python
_DEFAULT_PLY_LEXTAB = "pcc_frontends_c_lextab_v14"
_DEFAULT_PLY_YACCTAB = "pcc_yacctab_v19"
```

The cache directory defaults to `pcc-ply-cache` under `tempfile.gettempdir()`, overridable via `PCC_PLY_CACHE_DIR`; the directory is inserted into `sys.path` so PLY can load tables as imports. Concurrent builds are guarded by `_ply_table_build_lock` — a context manager built on an exclusive `fcntl.flock` (deliberately written as an explicit class rather than `@contextmanager`, the comment explains, so the self-host audit does not flag the enclosing `yield`).

The version numbers are a manual discipline: **change the grammar or lexer, bump the version** — otherwise PLY sees the old table file and silently reuses it, and the new rules never take effect. This is the origin of the "stale parser caches" entry in [AGENTS.md](../../AGENTS.md)'s Common Pitfalls — the symptom is "I changed the grammar and nothing changed," and the root cause is a previous-generation `pcc_yacctab_v19.py` sitting on disk. Encoding the version in the module name turns cache invalidation from a runtime check into a naming convention: simple, but dependent on a human remembering. That is one motivation for the content hash that replaces it on the native path.

### 3.2.3 The Native LR Driver: Moving PLY out of the Closure

The factory `make_c_parser()` in [pcc/frontends/c/parse/__init__.py](../../pcc/frontends/c/parse/__init__.py) is now the only correct entry point:

```python
def make_c_parser():
    if os.environ.get("PCC_USE_PLY_C_PARSER") == "1":
        from pcc.frontends.c.parse.c_parser import CParser
        return CParser()
    from pcc.frontends.c.parse.c_parse_driver import CParseDriver
    return CParseDriver()
```

The default is the native driver; the PLY version is demoted to an environment-gated reference. The native path is a three-piece architecture:

```text
source text ──► c_lex.CLexer (native lexer) ──► CParseDriver ──► c_ast.FileAST
                                                     │
                                                     ├── ACTION/GOTO tables (c_parsetab, frozen data)
                                                     └── grammar actions (c_parser_actions)
```

- [pcc/frontends/c/parse/c_parsetab.py](../../pcc/frontends/c/parse/c_parsetab.py) holds **frozen** LR tables: pure-data Python literals generated offline from the PLY grammar by [scripts/freeze_c_parser_tables.py](../../scripts/freeze_c_parser_tables.py), importing no PLY at load time. The file header carries `GRAMMAR_SHA256` — a SHA-256 over the concatenated sources of every `p_*` method in `c_parser.py` — which CI cross-checks against the live grammar to detect "changed the grammar, forgot to re-freeze." This mechanizes the manual version-number discipline of Section 3.2.2: from "a human remembers to bump" to "a hash mismatch raises an alarm."
- [pcc/frontends/c/parse/c_parse_driver.py](../../pcc/frontends/c/parse/c_parse_driver.py) is a ~250-line standard shift/reduce state machine. Its `_PSlot` class reproduces PLY's minimal action-side interface (`p[i]`, `p.lineno(i)`, `p.slice`), so both drivers share a single set of grammar-action semantics.
- [pcc/frontends/c/parse/c_lex.py](../../pcc/frontends/c/parse/c_lex.py) is a hand-written character-at-a-time scanner — no regexes on the hot path (regex remains only for the inherently multi-character patterns: integer suffixes, float exponents) — with a constructor signature and token names fully compatible with `pcc.frontends.c.lex.c_lexer.CLexer`.

Behavioral equivalence between the two tracks is held by the gate [tests/c/test_c_parse_driver_parity.py](../../tests/c/test_c_parse_driver_parity.py). One honest boundary statement: the docstring of `c_parse_driver.py` itself notes that while the driver, actions, and tables are PLY-free at the source level, the overall pcc package still loads PLY transitively via [pcc/__init__.py](../../pcc/__init__.py) — an unfinished surface clean-up, not an achieved "zero PLY."

Why go to this trouble? Because the parser is inside the bootstrap closure. pcc1 — the first native compiler binary produced by pcc, see Chapter 15 — must be able to parse the C runtime and itself; a parser framework that builds its tables by runtime reflection is far harder for the typed-Python frontend to compile natively than "frozen tables + pure-function actions + a hand-written scanner." Nativization is not a performance fetish; it is prerequisite engineering for the self-hosted fixed point.

## 3.3 Preprocessing: Two Paths, One Boundary

### 3.3.1 The Built-in Preprocessor

The `Preprocessor` in [pcc/frontends/c/preprocessor.py](../../pcc/frontends/c/preprocessor.py) is pure Python. Its module docstring lists the supported surface: `#include "..."` (read and inlined), `#include <...>` (**silently ignored**), object-like/function-like/flag macros, `#undef`, full `#ifdef`/`#ifndef`/`#if`/`#elif`/`#else`/`#endif` with `defined()` evaluation, `##` token pasting, `__VA_ARGS__`. With system headers ignored, common types are injected from `TYPE_PREAMBLE` (typedef text for `size_t`, `va_list`, `FILE`, and friends) and common macros preloaded from `BUILTIN_DEFINES` (`NULL`, `INT_MAX`, `__STDC_VERSION__`, ...).

The most instructive piece is the `#if` expression evaluator. The intuitive implementation would feed the macro-expanded expression to Python's `eval()` — but `eval` is on the banned-builtin list of the self-host audit ([scripts/audit_selfhost.py](../../scripts/audit_selfhost.py)), and the source comment says so explicitly. So `_eval_cpp_expr()` comes with a full recursive-descent parser, `_CppExprParser`, producing a tagged-tuple tree that `_eval_tree()` evaluates with **C semantics**: `&&`/`||` and the untaken `?:` branch short-circuit (a dead `1/0` on the other side does not raise — matching C), integer division truncates toward zero (`int(l / r) if (l < 0) ^ (r < 0) else l // r`), and `!0 == 1`. Failures raise `_CppExprError`; the caller `_eval_condition()` warns and treats the condition as false. This is a microcosm: **bootstrap constraints leak all the way into apparently unrelated utility code.**

Macro expansion is a per-line fixed-point iteration: `_expand_line()` calls `_expand_once()` until the line stabilizes, capped at 30 rounds to break self-referential macros. `_expand_once` extracts the line's identifier set via `IDENTIFIER_RE`, intersects it with the macro table, substitutes longest-name-first, and replaces object-like macros with a precompiled `\b...\b` pattern using a callback replacement (the callback exists so the regex engine does not interpret C escapes like `\xNN` or `\n` inside macro bodies). Function-macro argument collection (`_find_macro_args`) balances parentheses and skips string/character literals. This is not a token-level standard preprocessor, and `##` is handled by simply deleting surrounding whitespace; its role is a fallback for controlled scenarios and `cc`-less environments, not a conformance implementation — a limitation that must be stated as plainly as the features.

### 3.3.2 The System-cpp Path: Borrowed Power, Kept on a Leash

The main path is `CEvaluator._system_cpp()` in [pcc/frontends/c/evaluator/c_evaluator.py](../../pcc/frontends/c/evaluator/c_evaluator.py): when a system compiler exists (`_has_system_cpp()` probes for `cc`/`gcc`), preprocessing is delegated to the real thing, with three mechanisms keeping the output inside pcc's digestible subset:

```text
cc -E -P -nostdinc -isystem utils/fake_libc_include  -I <user dirs>...  <many -D>  file.c
```

1. **`-nostdinc` + `-isystem fake_libc`**: cut off the host system headers and substitute the fake libc (Section 3.4).
2. **The `compat_defs` table**: macros the fake headers deliberately omit are supplied on the command line — the full `limits.h`/`stdint.h` value set (LP64 model, `INT64_MAX=...L`), the complete `PRI*` format-macro family from `inttypes.h`, a pointer-arithmetic definition of `offsetof`, and a battery of "define the extension into harmlessness" macros: `__attribute__(x)=`, `asm(...)=`, `__extension__=`, `__builtin_memcpy=memcpy`, and `_Static_assert(x,...)=` (host headers can embed unparseable builtins like `__builtin_types_compatible_p` inside static assertions, so they are erased on this path; user-code `_Static_assert` remains supported through the built-in preprocessor path — the grammar has `p_static_assert`).
3. **Platform defines**: on Darwin, the stdio global remaps `stdin=__stdinp` etc., and `-U__ARM_NEON` to keep vector intrinsics out.

If that command fails, there is a second stage: rerun against the **real host headers**, adding `system_header_compat_defs` — most importantly a rewrite of the host-shaped `__builtin_va_arg(ap, type)` into the `(*((t*)__builtin_va_arg(&(ap),sizeof(t))))` shape pcc already supports, plus OpenSSL's non-atomics fallback macros. For include directories matching zstd, OpenSSL, or PostgreSQL, the order is inverted and host headers are preferred outright (`prefer_system_headers`), with PostgreSQL output additionally narrowed from `__int128` to `long long` (the comment is explicit: libpq's frontend sources do not rely on 128-bit semantics here — a scoped concession, not a general rule).

The preprocessed output still passes through one normalization stage before the parser (`_preprocess_translation_unit_source` → `_normalize_preprocessed_source`): `_VA_TYPEDEF_NORMALIZE` flattens `typedef __builtin_va_list X;` chains into `typedef char * X;`; `_SELF_TYPEDEF` deletes self-referential typedefs; `_strip_gnu_asm_statements` uses a character scanner (not a regex — it needs parenthesis balancing and string skipping) to replace statement-position `asm(...)` with `;`; `_CPP11_ATTRIBUTE` strips `[[...]]`; `_expand_simple_gnu_range_designators` expands `{ [0 ... N] = v }` into explicit lists of at most 4096 elements; `_inject_system_cpp_keyword_compat` injects fallback typedefs/enums when `bool`/`wchar_t`/`true`/`false` are used but undefined. Every one of these corresponds to a parse failure some real project once produced. They live at the preprocessing boundary precisely so the parser grammar and the IR layer can stay clean.

## 3.4 fake-libc: Declarations as the Interface

[utils/fake_libc_include/](../../utils/fake_libc_include) follows pycparser's fake-libc idea and extends it substantially; it currently holds 83 entries (including the `sys/`, `arpa/`, `netinet/`, `linux/`, `openssl/`, and `numpy/` subdirectories). The design fits in one sentence: **compilation needs declarations and layouts; implementations belong to link time.**

Structurally, everything converges on two root files. Most headers are two lines:

```c
#include "_fake_defines.h"
#include "_fake_typedefs.h"
```

`_fake_defines.h` provides macro constants (`NULL`, `EOF`, `SEEK_*`, the limit values) and the va_arg macro family — which rewrites `va_start`/`va_arg`/`va_end`/`va_copy` into an address-taking shape:

```c
#define va_arg(_ap, _type) (*((_type *)__builtin_va_arg(&(_ap), sizeof(_type))))
```

This is a **contract** between the frontend and the code generator: the `BUILTIN_VA_ARG` productions in the grammar recognize exactly these shapes, and the single remaining text-level IR lowering exemption (Chapter 12) is built on this shape. Here the fake header is not merely a stand-in for declarations; it is a converter that regularizes the ABI deep water of variadic arguments into a compiler-agreed form.

`_fake_typedefs.h`, meanwhile, is an **explicit list of assertions about the host ABI**: `size_t` = `unsigned long`, `va_list` = `char *`, `int64_t` = `long` (LP64), `time_t`/`clock_t` branched on `__LP64__`, `FILE` = `char` (it only needs to be pointable-at), and every `pthread_*` type = `int` (sufficient for type checking; real layouts live in the linked implementation, on the assumption that these types only cross pcc-compiled code as pointers or opaque values). The [AGENTS.md](../../AGENTS.md) repository map annotates this directory as "host ABI / decl mismatches surface here" — when pcc-compiled code disagrees with the real libc about a struct layout or integer width, the first suspect is a typedef line in this file. One mismatch already exists: `mode_t` is `unsigned short` in `_fake_typedefs.h` but `unsigned int` in the built-in preprocessor's `TYPE_PREAMBLE` — the two preprocessing paths assert different things about the same type; no real project has yet stepped on it.

A few headers carry real content. `stdio.h` declares `__stdinp`/`__stdoutp`/`__stderrp` under `__APPLE__` and macro-maps `stdin` and friends (complementing the `-Dstdin=__stdinp` defines of Section 3.3.2), then adds POSIX functions like `fileno` and `popen`; `stdlib.h` adds `getenv`/`mkdtemp`/`system`. Each is an increment some real project forced — zlib's `gz*.c` once required `read`/`write`/`open` and the `O_CREAT` flag family ([docs/investigations/zlib-integration-static-local-arrays-and-layout.md](../../docs/investigations/zlib-integration-static-local-arrays-and-layout.md)). The largest fake header is the 1256-line `Python.h`: pcc's fake CPython header for compiling C extensions, defining how `struct PyObject` corresponds to pcc's 16-byte object header and faking `PY_VERSION_HEX` so numpy's compat shims skip their back-fills. That story belongs to Chapter 17; the point here is that the fake-libc idea was reused as the carrier for the C-API shim.

## 3.5 The Evaluator Pipeline

`CEvaluator` in [pcc/frontends/c/evaluator/c_evaluator.py](../../pcc/frontends/c/evaluator/c_evaluator.py) is the engine room of the C path. The core pipeline lives in the module-level functions `_compile_translation_unit_artifact_job` and `_compile_preprocessed_translation_unit_artifact`, shaped as:

```text
TranslationUnit(name, path, source)
  → _preprocess_translation_unit_source        # one of the two paths of 3.3, plus normalization
  → make_c_parser().parse(codestr)             # the dual-track parser of 3.2
  → PassPipeline.run_high_tier(ast, ctx)       # AST analysis passes populate PassContext
  → CCodeGenerator(...).generate_code(ast)  # semantic lowering (Chapter 4)
  → postprocess_ir_text(str(module))           # IR text post-processing (va_arg exemption only, Ch. 12)
  → PassPipeline.run_low_tier(ir_text, ctx)    # IR-level passes
  → artifact dict                              # ir_text / return_type / external_defs /
                                               # func_return_types / pass stats and report
```

The artifact is deliberately a JSON-serializable dict rather than an object: return types pass through `_serialize_ir_type()` and become tuples like `("int", 32)`. This serves two purposes: process-pool parallelism (`_compile_translation_units` fans multi-TU compiles out via `ProcessPoolExecutor`, and dicts cross process boundaries trivially) and on-disk caching.

**Caching is three layers deep, each amortizing a different slice of cold-start cost.** The outermost layer is the compiled-artifact JSON cache (default `~/.cache/pcc/compile-cache`, `XDG_CACHE_HOME`-aware, overridable via `PCC_COMPILE_CACHE_DIR`, disabled via `PCC_DISABLE_COMPILE_CACHE`): the key, computed by `_compile_cache_key()`, mixes `_COMPILE_CACHE_VERSION` (currently `"v4"`), the **compiler's own fingerprint**, the pass-selection signature, the backend signature, the target triple, and the preprocessed source text. The fingerprint, `_compiler_cache_fingerprint()`, hashes mtime/size of `c_codegen.py`, `c_parser.py`, `c_lexer.py`, `preprocessor.py`, and every `.py` file under the `passes`/`ssa`/`llvm_capi`/`backend` packages, plus the Python version and platform — **editing the compiler automatically invalidates the cache**, solving the stale-cache class of Section 3.2.2 with a mechanism rather than a discipline at this layer. The middle layer is a native `.so` cache: `_build_native_cache()` compiles optimized IR through `emit_object` plus `cc -dynamiclib`/`-shared` into a shared library on disk; on the next run `_load_native_cache()` grabs the function pointer with `ctypes.CDLL`, skipping preprocessing, parsing, codegen, and LLVM entirely. The innermost layer is the in-process `_jit_cache`, keyed on (source SHA-256, entry name, optimization signature, pass signature, backend signature), whose values keep the execution engine itself alive — the comment is explicit: the engine is retained so the function pointer stays valid.

**Execution has four exits.** (1) In-process MCJIT in `evaluate()`: `llvm.parse_assembly` → optimization → `create_mcjit_compiler` → `get_function_address` → a `CFUNCTYPE` call, constructing argc/argv when `prog_args` is given. (2) Multi-TU linked execution: `_prepare_linked_llvm_module()` merges modules with `link_in`, and `_raise_if_duplicate_external_definitions()` rejects cross-TU duplicate definitions beforehand using each TU's reported `external_defs`. (3) System linking: `run_compiled_translation_units_with_system_cc()` emits a native object per TU directly via `target_machine.emit_object()`, links with the system `cc`, and runs the binary in a subprocess — note that this path **never hands IR text to the system compiler**, which is the IR Fix Policy in action; on Linux it appends `-no-pie` (pcc emits non-PIC object code; the `_platform_link_flags` comment explains the `__stack_chk_guard` absolute-relocation issue). (4) The self backend: `_run_compiled_translation_units_self_backend()` produces assembly through `emit_self_asm`, then assembles and links with `cc` (Chapter 13).

The execution side also carries a Darwin-specific lifecycle defense. Some large multi-TU programs run **correctly to completion** under MCJIT and then crash the hosting Python process during llvmlite/LLVM teardown. The evaluator's countermeasures are twofold: `_detach_execution_engine()` detaches the engine/module/target-machine wrappers and parks them in the process-global list `_DETACHED_MCJIT_WRAPPERS`, so they are only dropped at interpreter shutdown; for heavier cases, `_run_linked_mcjit_worker()` isolates the entire execution in a subprocess that writes its result to a JSON file and exits via `os._exit()` — never running destructors at all. Both came out of real crashes during the SQLite integration (Section 3.7).

One debugging hook worth memorizing: when IR parsing fails, setting `PCC_DUMP_BAD_IR=<dir>` dumps the offending IR to a content-hashed file, making it possible to identify which TU and function produced it.

## 3.6 Project Collection and --sources-from-make

[pcc/driver/project.py](../../pcc/driver/project.py) turns "a path" into "a list of `TranslationUnit`s." Chapter 2 surveyed the four compile modes; here we cover mechanism and limits.

**Directories default to merged mode.** `_collect_directory()` collects `*.c` non-recursively (`os.listdir` + sort), places the file containing `main()` last, and concatenates everything into one large TU with `// --- filename ---` marker lines. The `main` test, `_has_main()`, is two-phase: a coarse regex `\b(?:int|void)\s+main\s*\([^;{}]*\)\s*\{`, and on a hit, a **real preprocessing run** (`CEvaluator._system_cpp`) followed by re-matching against the preprocessed output — so a `main` excluded by `#if` conditionals does not cause a false positive; if preprocessing fails, it falls back to the regex with a warning. `--separate-tus` mode (`_collect_directory_units`) collects the same files as independent TUs and requires exactly one `main`; dependency inputs under `--depends-on` must define none at all.

The "every `.c` in the directory counts" semantics has a well-known trap, recorded in [AGENTS.md](../../AGENTS.md)'s environment rules and Common Pitfalls: **a probe `.c` file casually dropped into a project directory during debugging gets silently conscripted into the build.** The symptoms vary wildly (duplicate symbols, behavior drift); the root cause is just directory collection faithfully doing its job. The rule: probes go in `/tmp` or a test artifact directory, never in a real project tree.

**`--sources-from-make` recovers the source list from make dry runs.** The attempt sequence in `_scan_make_goal()` encodes hard-won experience with real build systems:

```python
attempts = [
    (("-n",), ()),          # cheapest: plain dry run
    (("-n",), ("clean",)),  # an up-to-date goal emits no compile commands under -n;
                            # a dry-run "clean" deletes nothing but forces make to
                            # print the full rebuild commands
    (("-nB",), ()),         # last: -B forces every prerequisite and can trigger
                            # expensive or brittle reconfiguration rules
]
```

Each makefile candidate (besides the top-level Makefile, `Makefile.in` is also tried — zlib's top-level Makefile is a stub that says "run ./configure first," with the real rules in `Makefile.in`; this fallback was added during the zlib integration) runs through that sequence, and stdout is scanned line by line for two kinds of extraction. `_extract_c_sources_from_make_line()` tokenizes with `shlex`, claims `.c` tokens that exist on disk, and uses `realpath`/`commonpath` to decide whether they belong to the project tree; `.o`/`.lo` tokens go through `_infer_c_source_from_object_token()` to infer the same-stem `.c` (it also understands libtool's `xxx_la-` prefix). `_extract_cpp_args_from_make_line()` recovers only CPP-level flags — `-D/-U/-I/-include/-isystem/-iquote/-idirafter` in both two-token and prefix forms, with path arguments normalized to absolute. These flags are then fed back into the `_system_cpp` of Section 3.3.2, and also into `main` detection, so detection runs under the same macro environment as the real compile (`_main_detection_cpp_args`).

When all dry runs fail there is a further layer: a **pure-text Makefile parser fallback**. `_parse_makefile_tree()` handles line continuations, `include`/`-include`, the `ifdef`/`ifeq` condition stack, and the differing semantics of `=`/`:=`/`+=`/`?=`; `_make_expand()` performs `$(...)` variable expansion (supporting `firstword`/`dir`/`strip`, expanding `wildcard` to empty, with self-reference protection). `_fallback_goal_sources()` walks the rule graph from the goal down to `.c` prerequisites, falling back to scanning the `OBJS`/`OBJECTS`/`SRCS`/`SOURCES` variables. Autoconf placeholders in unconfigured trees (`@CFLAGS@` and the like) are filtered by `_AUTOCONF_PLACEHOLDER_RE`, and `PTHREAD_CFLAGS` even gets a dedicated inference that walks up to the configure script. And when a compile line carries no flags at all, `_probe_make_cpp_arg_groups()` runs `make -n -W <src> <obj>.o` — pretending the source is newer — to force make to print that one compile command.

The limit must be stated plainly (as [AGENTS.md](../../AGENTS.md) Compile Modes also stresses): this machinery **can only recover flags the build system actually emits.** Configuration macros that live only in header comments or documentation, passed by humans as ad-hoc `-DXXX`, are invisible to any dry run. zlib's configure-injected `HAVE_UNISTD_H` family was ultimately supplied at the preprocessing layer by project recognition — not something make scanning could ever solve.

## 3.7 History and Lessons

This section draws on three C-side investigation reports under [docs/investigations/](../../docs/investigations). They share one narrative skeleton: a real project fails → the first instinct is "scale / linker / MCJIT" → the instinct is wrong → staged reduction reveals a compiler semantics bug. The skeleton itself is the lesson.

### 3.7.1 SQLite: Forward-Declared Bitfield Structs Split the Type Graph

(Source: [docs/investigations/sqlite-forward-declared-bitfield-struct-tags.md](../../docs/investigations/sqlite-forward-declared-bitfield-struct-tags.md))

The symptom was far from the root cause: `sqlite3_step()` returned `0` instead of `SQLITE_DONE` for an `INSERT`, and `db->errMask` became `0`, masking every later result code. The usual suspect — MCJIT scalability — was blamed first. The decisive reduction was a minimal helper using **real SQLite types**:

```c
static int same_db(Vdbe *p, sqlite3 *db) { return p->db == db; }
```

True under native compilers, false under pcc. If a first-field pointer read on `Vdbe *` is wrong, the problem is type lowering, not SQLite logic.

The structural root cause: SQLite's `sqlite3` and `Vdbe` reference each other recursively, and `Vdbe` contains bitfields, sending it down pcc's custom-layout path. Two bugs stacked there. First, **standalone tag definitions** like `struct Vdbe { ... };` (no declared object) fell through `codegen_Decl()`'s ordinary object-declaration path instead of being registered as pure type definitions. Second, a forward-declared named bitfield struct could, at definition time, get a **fresh** `layout_*` LLVM identified type instead of reusing the existing forward-declared `struct_*` tag type. The result was a split type graph: some code referenced an opaque `struct_Vdbe` (the IR showed a bare `type opaque` line), other code used a separate layout-backed type — fatal in a recursive graph. The fix left two invariants behind: **the same source-level struct tag must map to the same LLVM identified type; any split is a compiler bug**; and standalone tag definitions look like declarations in the AST but declare no storage, and must be special-cased. The regression lives in [tests/c/test_bitfields.py](../../tests/c/test_bitfields.py).

### 3.7.2 PCRE: A Hanging Loop Whose Disease Was a Zero-Length Global

(Source: [docs/investigations/pcre-op-lengths-incomplete-array-binding.md](../../docs/investigations/pcre-op-lengths-incomplete-array-binding.md))

PCRE compiled fine, then hung forever inside `pcre_compile("hello", ...)` at runtime. The work at the time happened to be adding a system-link path for separate TUs, so the first theory was again "22 modules is too many for MCJIT." The falsification was clean: switching to system-`cc` linking left the hang intact — **the linker experiment eliminated the linker, pointing the finger at the compiled program itself.**

What followed was a textbook reduction chain. Attaching LLDB showed the live stack parked in `auto_possessify()`. Instrumentation inside the loop printed `c = 131` (`OP_BRA`) with `PRIV(OP_lengths)[c]` permanently `0`, so `code += OP_lengths[c]` never advanced. A tiny program that did nothing but print `_pcre_OP_lengths[129..133]` produced `0 0 0 0 0` — and the hang became a constant-data problem. The preprocessed source was perfectly correct; the decisive evidence was in the IR:

```llvm
@"_pcre_OP_lengths" = global [0 x i8] zeroinitializer
@"_pcre_OP_lengths.1" = global [162 x i8] [i8 1, i8 1, ...]
```

The real data existed — under a renamed symbol; every external reference resolved to the zero-length placeholder. Two root causes: the old array-declaration path created the file-scope global **before** inferring the real length from the initializer, after which it could only mint a new `.1`-suffixed symbol; and `extern const pcre_uint8 _pcre_OP_lengths[];` (an incomplete array declaration) was never unified with the later complete definition as the C standard requires. The fix matched: infer the length before creating any symbol, eliminating the `[0 x T]` placeholder path entirely; and treat "incomplete array declaration + complete definition with the same element type" as compatible at file scope. The report's debugging maxims deserve permanent residence in engineering memory: **when a table-driven loop hangs, print the table entry before suspecting control flow; correct preprocessed source does not imply correct symbol binding — read the IR.**

### 3.7.3 One Integration, Three Bug Classes: SQLite VFS and zlib in Concert

(Sources: [docs/investigations/sqlite-integration-vfs-init-and-mcjit-lifecycle.md](../../docs/investigations/sqlite-integration-vfs-init-and-mcjit-lifecycle.md), [docs/investigations/zlib-integration-static-local-arrays-and-layout.md](../../docs/investigations/zlib-integration-static-local-arrays-and-layout.md))

The SQLite integration report's title is its conclusion: it was **not one bug**. Casted function-pointer constants like `(sqlite3_syscall_ptr)lstat` were not unwrapped through `c_ast.Cast` on the constant-pointer path, lowering SQLite's entire Unix VFS syscall table to null pointers. Function-scope aggregate initializers like `struct sqlite3 db = {0};` did not actually zero the local. And finally, the program printed `OK` and then the hosting Python process took a SIGSEGV — the genuine Darwin MCJIT teardown problem, whose countermeasures (the detach list and the `os._exit()` subprocess isolation of Section 3.5) are this story's artifacts. The zlib report likewise exposed three layers at once: `enum` lowered as 64-bit integers corrupting the inflate state layout, block-scope `static` arrays lowered like automatic locals, and `static const char my_version[] = "1.3.1";` not inferring its length from the initializer and becoming a zero-length global (the same family as PCRE's bug; the version check returned `Z_VERSION_ERROR` outright). The lesson is methodological: **stacked failures must be split into separate bugs with separate evidence chains.** When a fix moves the symptom, the new symptom gets a new case file — it does not get merged into the same "root cause" narrative. That discipline was later codified as step 3 of the bootstrap regression procedure in [AGENTS.md](../../AGENTS.md).

## 3.8 Summary

Every layer of the C frontend projects the same judgment: **start by reusing mature components, but evolve toward "compilable by pcc itself, and auditable."** The parser began as pycparser/PLY and progressively replaced runtime magic with frozen tables, a native driver, and a hand-written lexer, with behavioral equivalence pinned by a parity gate. Preprocessing borrows the system `cc -E` but fences the host world out with `-nostdinc`, the fake libc, and a large compat-macro table — text reshaping explicitly confined to this layer. The fake libc buys machine-reproducible parser input with "declarations as the interface," at the price of a typedef assertion list that must stay aligned with the host ABI. The evaluator amortizes cold starts with three cache layers (compiler-fingerprint invalidation, `.so` reuse, in-process JIT) and covers the spectrum from interactive evaluation to the self backend with four execution exits. Source collection performs dry-run archaeology on make and states its boundary honestly: flags the build system never utters cannot be recovered by anyone. And the three stories of Section 3.7 keep confirming one piece of experience: a real project's failure is rarely the problem it appears to be — reducing to a minimal reproducer that uses real types, and reading the IR instead of guessing, is how this frontend grew into its present shape.

## Exercises

1. **Verify in source.** Find the comment explaining the `p_declaration`/`p_decl_body` split in [pcc/frontends/c/parse/c_parser.py](../../pcc/frontends/c/parse/c_parser.py). Explain: if they were a single rule, why would `typedef int T; T x;` on consecutive lines fail to parse? What role does yacc's lookahead token play?
2. **Cache archaeology.** Compare the manual version number in `_DEFAULT_PLY_YACCTAB` with the `GRAMMAR_SHA256` mechanism in [pcc/frontends/c/parse/c_parsetab.py](../../pcc/frontends/c/parse/c_parsetab.py): for each, give one staleness scenario it catches and one it cannot. Then read `_compiler_cache_fingerprint()` and explain why the compiled-artifact cache needs no manual version number at all (hint: `_COMPILE_CACHE_VERSION` still exists — what class of change does it guard against?).
3. **A fake-libc mismatch on paper.** `_fake_typedefs.h` asserts `mode_t` is `unsigned short`; the built-in preprocessor's `TYPE_PREAMBLE` asserts `unsigned int`. Construct a minimal C program whose `sizeof` behavior differs between the two preprocessing paths. Then argue: what kind of real libc call would turn this mismatch into a runtime error?
4. **Design trade-off.** The built-in preprocessor implements `_CppExprParser` for `#if` evaluation instead of calling `eval()`. Beyond the self-host audit's ban, give at least two reasons independent of bootstrapping (hint: C semantics vs. Python semantics; attack surface). Then argue the opposite direction: if pcc were never going to self-host, would `eval()` have been the right engineering choice?
5. **Replay the case study.** Using only the information in Section 3.7.2, write down your first four actions upon receiving the report "PCRE hangs in `pcre_compile`," annotating each with the hypothesis it is meant to falsify. Then compare against the actual sequence in [docs/investigations/pcre-op-lengths-incomplete-array-binding.md](../../docs/investigations/pcre-op-lengths-incomplete-array-binding.md) and identify the most expensive redundant step in your plan.
