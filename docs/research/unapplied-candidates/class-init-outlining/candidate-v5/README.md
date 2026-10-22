# Class-init outlining V5: owned attribute membership correction

This is an unapplied production/test successor to V4. It corrects an actual
production TypeError found by the first focused host gate. V5 execution,
native semantics, heavy-module effects and performance remain UNRUN.
Independent source review is complete for the production/test delta; this
is source clearance only. No V5 test or compiler execution has occurred.

## Preserved failure and precise cause

V4 was preserved in commit `313582e25cbc83056022291379ba53b4cb370d3a`.
Its actual gate collected 27 cases and stopped at 0 PASS / 1 FAIL / 26 UNRUN.
The first class-helper reuse raised TypeError at the declaration's noinline
membership check. This was a production bug, not an invalid test assumption.
The unchanged failure receipt is preserved in commit
`79011c00553de17cfdb0f7e8169842a7b2e73ae8`; full qualification SHA256 is
`8c2ed55e5e8ddd42d398531c3bbee9ca5adb25d4cceaaa09e526c871c75824d7`.

The exact owned `pcc/ir/ir.py` FunctionAttributes has `add` and `__bool__`,
but no containment or iteration protocol. It stores canonical attributes in
its initialized `_attrs: list[str]`, which Function serialization also reads.
`pcc/ir/compat.py` aliases all frontend builders to this owned implementation.
The one-line correction checks `"noinline" not in helper.attributes._attrs`.
It adds no IR API, catches no error and retains the same function type,
module identity, i32() ABI, internal linkage, noinline and nonempty-body checks.
The FunctionAttributes definition and all other production code are unchanged
from V4. Missing noinline must still reject the declaration explicitly.

## Regression scope

The original 27 structural cases are unchanged. Two new parametrized cases
intercept the real top-entry reuse after the class-only init has already
emitted its helper. Each asserts the other declaration properties and that
the generated helper initially has noinline, then replaces its attributes
with a genuine owned FunctionAttributes instance. One is empty; the other
contains only alwaysinline. Both require the exact L1CodegenError and complete
incompatible-declaration message. They do not substitute an iterable fake.
The existing successful-shape cases continue to require two entrypoint calls
to one noinline construction body through default and explicit inline passes.

There are 29 statically intended host nodes. This is not an executed collection
or a test pass. Neither the original failure nor V5 proves native behavior.

## Exact recovery

The public bundle consists of successor.patch, README.md and manifest.json.
The patch is an incremental delta from the exact V4 postimages, not a complete
replacement of the archived original candidate.

Start from production commit `02b9bc2d3c071bbc3e547a63e045daa73cc6e1bc`.
Verify baseline hashes and absence of the new test, then in a fresh ordinary
source copy apply these preserved patches in order:

1. candidate-v1/production.patch and candidate-v1/tests.patch, from commit
   `8ee714b914a1a70cd8e6950389f7fb38fbe3c370`.
2. The V4 successor.patch from commit
   `313582e25cbc83056022291379ba53b4cb370d3a`.
3. This V5 successor.patch.

All paths are beneath
`docs/research/unapplied-candidates/class-init-outlining/`.
Verify the three final file hashes and every unchanged baseline file. Only
class_gen.py and the test change relative to V4; generation_lowering.py is
included in the manifest to bind the full candidate and is byte-identical.

The retained baseline container inventory is
`ea395e18a173ec4172c8badd68bbccb53a66390e4c130b8a78f6702fbc177635`.
Its complete pcc tree `d234174fbac80575ea822f6100f65fe914dd4498` matches
02b9 production. Its older unrelated tests/docs are not relabeled as a full
02b9 checkout. No other optimization or OSError candidate is included.

## Proposed next gate

After independent source review, preservation and separate admission, the
coordinator may run the same single test file, now with 29 intended nodes:

`python -m pytest -x -n0 -vv --tb=short -o addopts= tests/python/test_class_init_outlining.py`

Use the unchanged sole-process guard, shared lock, 300 seconds, hard AS4GiB,
NPROC0, 4GiB free reserve, fresh outputs and full before/after source seals.
Stop at the first failure and retain actual collection/outcomes. The selected
host route uses real parsing, inference, generation and owned IR verification;
it requires no runtime build, native code, subprocess or ctypes capability.
There is no admission here for heavier modules or native/performance work.
