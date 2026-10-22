# Two CI witness corrections

These are unapplied recovery patches against commit
`8aa2881c005d11dfeba05c0275386b954eeb312c`. They change two disjoint test
files and no production code, runner, workflow, memory cap or deadline.
Both patches passed independent source review. Their corrected real CI
gates remain UNRUN. Preserve the original failures from run 38036457418.

## Owned-provider census

Apply `handoff-closure.patch` to
`tests/python/test_indexed_handoff_closed_world.py`.

- Patch SHA256: `ed07c2ccac356c31d3999bff7257523dd6fb50ef65e382e05e6104b20a01a1e3`
- Preimage: `1c3a2d496b00cb34abfd47aa62e8387f9f64e4f40b9e5a3f72a896718330def4`
- Postimage: `1d48b533b50517c47ff8f2efbdb8ea6d9e5b2900de4b3ec85e146472d304f010`

The real unsplit/split object comparison and two real worker-lifetime tests
passed. The following closed-world gate failed because its exact expected
closure omitted the owned `warnings` provider imported inside `os.fsync`.
The patch accounts for that fifth provider and its source hash, retaining
every later helper-body, zero-fallback, IR-verification and ARM-emission
assertion. One added pure-AST import-census test passed under the bounded
single-process host gate. This does not establish the corrected real compile
gate or authorize enabling the split path without its CI prerequisite.

## GC4 callback/cleanup witness

Apply `float-movement-preconditions.patch` to
`tests/python/test_native_float_callback_movement.py`.

- Patch SHA256: `50860f111f567c755146e3746f64df1ebdf339eba6d97c850420b8ee2f255eb6`
- Preimage: `9c5786ccfd66ad215963a6096927666394f651333825ab5b76f5795ae36c9039`
- Postimage: `2c31c7c6e1fac812978727fa0832f26b135be6673cdadd4bf48e15e0cfdd2906`

The original float and class-doc programs passed each requested GC0-4 run.
The separate strict movement program then reported positive callback
forwards but zero cleanup forwards and failed. Its original receipt lacked
the pending remembered-root count; the queue explanation remains a
source-grounded inference rather than a measured backlog.

Each phase now creates a fresh live cohort and records metric 42. Preparation
uses at most ceil(initial pending / 8) small-budget steps, requires a strict
decline at every step and pending zero before the measured relocation window.
Both original positive-selection/positive-forward requirements, TypeError,
exact event order and once-only finalization remain. The same four externs
and 20-second native timeout are retained. No local GC4 test was run; native
preconditions and successful movement must be established by normal CI.

Apply either patch first; neither depends on the other. Both resulting files
remain mode 100644. The production PCC tree remains
`e0fe5ee61793cc74d4b1564d4cb6787c13d884a4`. Saving these recovery files does
not modify the active tests or retroactively change any failed CI receipt.
