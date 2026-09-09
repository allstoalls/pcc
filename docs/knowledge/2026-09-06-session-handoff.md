# Session handoff — 2026-09-06 (repo split, out-of-tree packages, governance)

> Historical session snapshot, not current task instructions. HEAD, dirty state,
> blockers and artifact paths below describe the recorded session. Check current
> source, `git status` and execution before reusing them; later user instructions
> take precedence. Search relevant sections instead of loading this whole file.

Everything below is uncommitted in the core working tree unless it says
otherwise; the human owns commits. The two extracted repositories are pushed.

## Repositories

- `jiamo/pcc` transferred to https://github.com/allstoalls/pcc (public, history
  and stars kept). Local `origin` points at it; local `master` is one commit
  ahead of `origin/master` and was not pushed.
- https://github.com/allstoalls/pcc-gui (public, `~/my/pcc-gui`): package
  `pcc_gui/` (17 modules), `examples/{closure_probe,mac_diff_app,harness}`,
  `tests/`, `docs/`.
- https://github.com/allstoalls/pcc-gateway (public, `~/my/pcc-gateway`):
  package `pcc_gateway/` + `pcc_gateway/web/`, `local_http_app.py`, `tests/`,
  `docs/`.
- The core no longer contains the GUI or the gateway. The generic substrate
  they used stays: `pcc/py_runtime/py/freestanding_gateway_control.py` and
  `pcc/kernel_ir/metal_render_surface.py`.

## The usage model that was required, and now holds

An external package is an ordinary pcc package. No prebuilt library, no
`--system-link`, no host linker, no flags, no environment variable:

```bash
pcc1 app.py -o app     # app.py does `import pcc_gui`
./app
```

- `pcc1` already defaults to `--backend self --python-libpython off
  --ir-scaffold on`, so those options never need to be typed.
- `--ir-scaffold` has three effects and all of them are about compiling pcc's
  *own* IR-builder code: scaffolding `from pcc.llvm_capi.compat import ir` out
  of the link (`pipeline_dependency_closure._filter_ir_scaffold_closure`), the
  matching libpython decision (`pipeline_libpython.py:58`), and native lowering
  of `ir.*` builder calls (`codegen/ir_scaffold_lowering.py`). `auto` resolves
  to `on`. For an application it changes nothing.
- Package discovery needs no configuration: resolution starts at the entry
  file's directory and walks up to the first directory holding a project-root
  marker (`pcc-package.json`, `pyproject.toml`, `setup.py`, `.git`), and stops
  there. `PCC_PACKAGE_SITE` and the installed package site remain for packages
  outside the project. Implemented in
  `pipeline_packages.project_root_search_dirs`; tested by
  `tests/python/test_project_root_package_discovery.py`.

Verified: `pcc examples/closure_probe/probe.py -o probe` from the `pcc-gui`
checkout with no environment variable prints `live nodes 2 root 0 child 1`, and
`pcc local_http_app.py -o local_http_app` from `pcc-gateway` compiles, links and
runs.

## Compiler defects fixed while making the two packages compile

All were latent in the core (the gateway's pcc1 path was already red at HEAD).

1. `stack_alloc(SIZE)` folds a module-scope integer constant and constant
   arithmetic over such constants (`codegen/unsafe_lowering.py`;
   `tests/python/test_unsafe_module_int_constant_sizes.py`).
2. Builtin-typed receivers (`", ".join(...)`) are no longer treated as
   open-world methods that might park (`codegen/vthread_effect_analysis.py`).
3. The delegation-slot planner visits `except` handler bodies
   (`codegen/generator_lowering.py`).
4. The `finally` exception root and a named handler's release re-derive their
   frame pointer per block (`codegen/exception_lowering.py`): a may_park state
   machine splits a function at every park, so a cleanup block need not be
   dominated by the block that computed the pointer.
5. Cross-module integer globals bridge the raw-`i64` and boxed representations
   on load (`codegen/native_modules.py`).
6. A signed integer literal (`FLOOR = -7`) exports as a constant, like `7` did.
   Previously it became an untyped module global: a raw-int provider stored
   `i64` where the importer declared `PyObject*`, read the bits back as a
   tagged small int, and `-7` arrived as `-4` (`-7 >> 1`). Fixed in
   `pipeline_exports.py` + `pipeline_context.py`; the bug reproduced at HEAD.
7. A sibling module's ABI slot type wins over the caller's int policy, so a
   boxed `-1` reaches an `i64` parameter (`codegen/class_gen.py`, both
   instantiation sites). Test:
   `tests/python/test_cross_module_int_abi_slots.py`.
8. Values loaded from module-level globals are re-derived at pin/unpin and call
   sites a park boundary made unreachable from their definition
   (`_note_global_backed_value` / `_value_available_at_insertion_point` in
   `codegen/ownership_lowering.py`, applied in `_call_user` and every direct
   unpin emission). The side table stores the value with its source so the
   `id()` key can never be reused.
9. Module resolution is case-exact (`pipeline_paths.path_component_matches_case`).
   On macOS `from pkg import App` resolved `pkg.App` to `pkg/app.py` and
   compiled it twice under two module names; the link then failed with
   undefined `__pcc_py_module_top_pkg_App`.

Also added: `PCC_DEBUG_VTHREAD_REJECTS=1` dumps every rejected park boundary,
the spawn error names the receiver type, and the missing-slot error lists the
planned delegation slots. These made the may_park chain diagnosable in minutes
instead of by bisection.

## Raw-pointer static typing

- **Phase A landed.** Pointer intrinsics and `c_rawptr` extern results type as
  `int` in application modules; `c_ptr`/`c_str` extern *returns* are rejected
  there (declare `c_obj` or `c_rawptr`). Runtime ports opt into the pointer lane
  with `__pcc_runtime_port__ = True` (135 files; the ABI-constants generator
  emits it). Codegen derives the flag from the module AST
  (`layer1_init._module_declares_runtime_port`) so closure siblings match
  standalone compiles. 95 extern declarations migrated. Tests:
  `test_raw_addresses_are_ints.py` (12). Gates green: focused extern/unsafe/gc
  granule (85), GC0..4 production contract (169 each, three known reds
  deselected), fallback ratchets (45), bootstrap gate baseline. Stage1 v85 CPU
  751.5 s vs v84 747.6 s (flat).
- **Phase B denied and reverted.** Dropping the refcount provenance probe on
  GC0..2 made pcc1 crash in Stage2. A C-runtime diagnostic build counted, per
  tiny compile, 213 refcount operations on non-managed pointers: 22 on the
  `py_set_dummy` tombstone (a 1-byte static reached through
  `pcc_gc_store_ptr`) and about 190 `pcc_gc_release` calls from the compiler's
  own compiled ownership-cleanup code on libmalloc addresses with
  `malloc_size == 0`. The probe is masking real over-releases; removing it
  requires immortal-header sentinels and fixing those releases first. Recorded
  in `docs/investigations/pcc1-stage2-emit-throughput-and-memory.md`.

## Governance: board and evidence retired

- The 241 unfinished task-board rows became GitHub issues with `priority:*`,
  `status:*` and `task-board` labels, routed by track: 179 core, 50 pcc-gui, 12
  pcc-gateway. Mapping: `docs/task-board-migration-2026-09-06.md`.
- `docs/goal/task-board.yaml` and 731 uncited evidence files were deleted.
  The 198 evidence files that surviving documents actually cite were kept: a
  citation is a load-bearing reference, not a running log. `docs/goal/evidence`
  went from 5.2M/924 files to 1.4M/198 files, and no documentation link dangles.
- `docs/goal/goal-prompt.md` and `docs/current-goal-state.md` moved to
  `docs/archive/goal/`. `scripts/goal_state.py`,
  `scripts/compact_goal_startup_docs.py` and their two tests were removed with
  the board they served; `tests/python/test_harness_migration_ledger.py` went
  with the harness. `docs/goal/README.md` records what moved where.
- `AGENTS.md` now opens on `docs/knowledge/denied-experiments.md` and takes
  work from issues. Its "Working agreement" section replaces the goal-loop
  protocol and states where each kind of information belongs.
- `scripts/distill_investigations.py` generates three decision pages from the
  520+ investigations: 617 denied verdicts, 1493 confirmed root causes, and a
  symptom-routing index. `tests/test_knowledge_pages_are_current.py` fails when
  they are stale. The raw investigations are unchanged; they are the evidence
  the pages summarise.

## Still open

- **Virtual threads do not run a body spawned from a plain `def`.**
  `virtual_thread.result(thread)` returns `None`, so the gateway probe exits 0
  without printing `PCC1_GATEWAY_HTTP1_LOCAL_OK`, and the core's asyncio and
  threading tests print nothing. Pre-existing; issue
  `RT-P1-MAY-PARK-CALLER-SILENT-DEF-RED`.
- **`pcc1 app.py` with no `-o` (run mode) hangs** (300 s timeout). Unresolved;
  shebang-style usage depends on it.
- **Layering audit not started**: whether other product code sits in the
  runtime or the standard library, and whether the stdlib/runtime boundary is
  stated anywhere.
- Pre-existing reds, all with issues, none caused by this session:
  `test_unsafe_atomic_global_store.py` (freestanding `-> int` versus
  `pcc.i64`), asyncio/threading silent `def`, gateway server/tls tests,
  valueclass relocation [4], waitset [auto-2], extension-state anchors, raw
  type tags, and `test_test_infrastructure_efficiency.py`'s nested-pytest audit
  (two test files a concurrent session added were never registered).
