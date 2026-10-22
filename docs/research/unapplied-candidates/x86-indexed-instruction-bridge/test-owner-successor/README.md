# X86 bridge V3: test snapshot-boundary correction

This is an unapplied, test-only successor to the preserved V2 bridge candidate.
It changes only `tests/python/test_x86_64_indexed_instruction_bridge.py`.
Apply the original V2 `candidate.patch`, then this `test-only.patch`.
Production and the existing lifetime-test correction remain byte-identical.

## Why the original assertion was invalid

The actual first host gate collected 109 selected tests and deselected exactly
the named native executable test. It stopped at the first new equality case:
0 PASS, 1 FAIL, 108 UNRUN. Both block maps were empty, but their identities
differed. The failed gate, logs and full source seals are retained as failures;
this successor does not relabel them.

Unchanged `prepare_parsed_function` in
`pcc/backend/self_backend_prepare.py` assigns a new `block_map` at line 23.
`prepare_parsed_module_for_target` invokes it before either baseline or
candidate `_emit_function` begins. The former emitter's cleanup contract is
to restore the exact blocks/map it receives at its own function-entry boundary,
not to reverse an earlier preparation operation. The existing lifetime
regression already captures at that boundary.

The new V2 test captured the map before public preparation. Its corrupt-flag
direct arm made the same mistake. V3 records both objects at the actual
`_emit_function` entry in both places. The eager corrupt-flag oracle still
captures before its direct materialization call, which is the owner it invokes.

Every original identity (`is`), emptiness, exception, no-selector-on-corruption,
assembly/object, decoded-contract and global-cleanup assertion is retained.
The main spy also checks exact invocation count and each function's identity
and order; the flag spy requires the exact target and one invocation. This
corrects the tested boundary without relaxing cleanup or output requirements.

## Unchanged production and next gate

The production postimage remains
`145713c125d1d4e4e72c8d499646cd5c85874f4b6a83d3fd3c23dde934cea1ca`.
Its complete pcc subtree remains `5882f6f16ccfd1d9cbe55c06426e8888f51e9ab7`,
and its observed codegen checksum remains
`610f289ab84407149dbe231ac35e1b190079366bf852ccbafd0bdcc5ec85ddab`.
Only the full source inventory changes because this test file changes.

V3 is UNRUN. The proposed rerun keeps the same 109-test selection, native-node
exclusion and existing 300-second/4-GiB hard-AS/NPROC0 supervised boundary,
shared exclusive lock and 4-GiB free reserve. It needs a separately sealed
ordinary candidate source and admission. The real-module experiment remains
held pending this host result. No production, runtime or native qualification
claim follows from this source-only correction.

The V2 multiply-invalid diagnostic-precedence caveat, mocked all-kind helper
scope and missing native ABI/GC proof remain unchanged.

Bundled files: `test-only.patch`, `manifest.json`, and `README.md`. The original
V2 full patch is not duplicated in this successor.
