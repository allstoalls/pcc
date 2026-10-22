# Real ARM c_ast: dense-domain and duplicated-emission readback

The worklist experiment is closed as HOLD. Exact object, live-state, decoded
stackmap and CFG comparisons passed, but dense-row work fell only 0.244146%,
far below the fixed 25% mechanism threshold. No timing round followed.
The sealed result is preserved in commit
`a2c5b5e73e482b52a26f43a26289267964bdf190`.

This note is a read-only interpretation of those retained results and exact
source. It runs no compiler, test, profile or native code and proposes no
production patch. Full identities and derived counts are in
`representation-readback.json`, SHA256
`0a64d58cac8691839ae7e0af71513a44a2469f46df1756dd5aae9048df843e9b`.

## The failed repetition hypothesis

The correctly generated `arm64-apple-darwin23.6.0` module has 231 functions,
48,161 blocks and 219,614 indexed instruction records. Among the 109 functions
with positive tracked-word width, 108 converge in one sweep; only `Node_show`
requires four. The mandatory first sweep accounts for 15,970,220 of the
baseline's 16,010,084 dense-word units. Even perfect removal of all repeat
work could save only 0.248993% of that proxy on this module.

The two generated module entrypoints dominate the observed representation:

| Function suffix | B | E | T | W | K | Indexed instruction records | Dense-word units |
|---|---:|---:|---:|---:|---:|---:|---:|
| module_top_pcc_frontends_c_ast_c_ast | 20,507 | 29,934 | 3,370 | 113 | 1 | 91,901 | 8,017,124 |
| module_init_pcc_frontends_c_ast_c_ast | 20,435 | 29,826 | 3,359 | 112 | 1 | 90,944 | 7,917,952 |

Together they account for 99.5315% of the dense-row-work proxy and 83.2574%
of indexed instruction records. These percentages are not CPU-time,
whole-codegen or machine-instruction shares. Separate PHI/terminator records
are not included in the indexed-instruction column.

Both functions report zero changed rows. In the exact baseline,
`_native_managed_liveness` initializes live-in to zero and the row helper
reports any changed live-in/out word. Thus the result supports the inference
that no managed live-in bits grew at block boundaries in these two functions.
It does not prove their PHI live-out seeds, registered roots or intra-block
live states are empty.

## Actual call-state sparsity

The retained complete stream covers every non-frame-protocol call, including
calls later skipped by stackmap emission. Protocol calls retain a distinct
root-state meaning and were compared separately.

- Top: 3,370 tracked SSA values, but only 114 distinct values appear in any
  nonprotocol live-after set. Of 35,981 nonprotocol call sites, 35,527 are
  empty and 454 contain exactly one value. There are also 7,259 protocol calls.
- Init: 3,359 tracked SSA values, but only 112 appear in any such set. Of
  35,779 nonprotocol sites, 35,331 are empty and 448 are singleton. There are
  also 7,070 protocol calls.
- Neither function uses liveness overflow storage. Their retained state
  arenas have 115 and 113 records respectively, including the empty state.
- Top's singleton sites are 452 `py_tuple_set_item`, one `py_incref` and one
  `pcc_gc_pin`. Init's 448 singleton sites are all `py_tuple_set_item`.

The signature-fill sites use values named `func.signature.current.*`.
`user_function_lowering.py:2854–2895` reloads a rooted signature result after
acquiring its foreign lease and uses it across the tuple-field writes. Those
are genuine current liveness requirements. Top's two other sites share one
`function.current.*` value. Empty SSA live-after sets do not imply absent GC
roots: registered-slot root state, leases and all emitted safepoints remain
necessary.

## Why the representation is broad

`_native_managed_origins` in `self_backend_precise_stackmaps.py` discovers
managed/ambiguous SSA values across the entire function: rooted loads and
pointer transfers/joins. `_native_managed_liveness` gives every block a row
spanning the resulting function-wide T, then allocates uses, definitions,
live-in and live-out matrices of B×W words each. The final backwards scan
still handles the observed local values across calls.

For top, one matrix has 2,317,291 word slots; four matrices have a nominal
74,153,312-byte i64 payload. Init's figures are 2,288,720 slots and
73,239,040 bytes. These are logical native payload sizes, not measured host
RSS or allocator capacity. The scratch owners are function-local and close
before the next function's liveness analysis, so the two figures must not be
added and presented as peak memory.

The evidence points toward broad dense domains and very large generated
functions, rather than repeated fixed-point convergence. It does not yet
establish how much total emission time a different representation would save.

## The source of the two large bodies

The real source contains 56 top-level classes and 112 methods.
`generation_lowering.py:466–482` retains ordinary ClassDefs in source-ordered
module statements. Later, `generation_lowering.py:900–918` emits class
module-init and then top-init for this library shape.

`class_gen.py:4724–4780` emits each class construction into module-init and
also exposes the same `_emit_class_init` lowering for executed class statements.
`stmt_dispatch_lowering.py:474–478` invokes that path from top-init's body.
This confirms duplicate emitted construction work; it does not justify
skipping a runtime construction.

Both external entrypoints must be preserved: `pipeline.py:2254–2259`
explicitly advertises module init/top-init to embedding callers. Their
execution schedules and guards differ. Any shared-code design must be
reviewed separately for source order, fresh class identity, global bindings,
defaults, metaclasses, decorators, exceptions, roots and cleanup. No such
optimization is implemented or qualified by this readback.
