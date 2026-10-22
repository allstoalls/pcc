# Class-init outlining V6: CFG-aware source-order regression

This unapplied successor changes only the focused host test. The V5 production
postimages remain byte-identical. Independent source review is complete for
the exact test delta; all V6 execution remains UNRUN. No performance or native
acceptance is claimed.

## Preserved V5 result and read-only diagnosis

V5 was preserved in commit `299704c73a37c359b39a9360a6754e92a1ee08f6`.
Its actual gate collected 29 cases: 10 PASS / 1 FAIL / 18 UNRUN. The six
body-sharing/default-and-inline cases, two missing-noinline rejection cases,
and two root/constructor oracle cases passed. The next source-order case
failed its cross-block string-offset assertion. That outcome is preserved in
commit `cd6b466d3de99cf96cfa08f2c9f7d33100451a32`, with full qualification
SHA256 `3a1d9955fedf62016696cb7e21edf6836390a9be8327a47c5afca1a281997c1f`.
The original V4 production TypeError and failed receipt remain preserved too.

The failed assertion's retained, complete top-function IR shows correct
selected execution order despite inverted text offsets. The first class
helper executes before the function-value cache branch. A cache hit goes to
the common binding block; a miss constructs the function and its success path
joins that same block. The actual between binding publication, marker-after
assignment and second helper execute in that order within the join. The
constructor's negative-status error path cannot reach that binding or the
second helper.

Existing user_function_lowering.py creates cached/create/join blocks at
lines 3143-3145 before appending the constructor's continuation blocks.
Consequently the join, including the second helper, is printed before the
constructor's adapter reference. String offsets 23927 and 7440 compare block
layout, not execution order. This is a test-oracle defect, not evidence of
production reordering. The readback used only the already-retained assertion
text, without a compiler invocation or diagnostic rerun. Its decoded IR hash
is `815876512cb40d86443bc57eeb7cf8b26d18bf6151dcfac6670062fd57673e2a`.

## Test-only correction

The AST statement-dispatch order and module guard assertions remain. Helper
identities are now bound to the original ClassDef positions and each call
must occur exactly once. The replacement assertion follows named CFG edges
from entry and requires reachable, unique instruction sites and instruction-
aware dominance: first class helper, actual between binding, second helper.
It verifies that creation cannot bypass the constructor, a cache hit may
bypass it, construction success can reach the binding, and the exact
negative-status constructor failure cannot reach binding or the second class.
It does not depend on textual block ordering or generated numeric suffixes.

No node was removed, skipped or deselected. The intended total remains 29.
No other existing test assertion or production path is changed.

## Exact recovery and scope

The public bundle contains successor.patch, README.md and manifest.json.
The patch is incremental from the exact V5 postimages. Recover in a fresh
ordinary copy of production commit
`02b9bc2d3c071bbc3e547a63e045daa73cc6e1bc` by applying, in order:

1. V1 production.patch and tests.patch, preserved in
   `8ee714b914a1a70cd8e6950389f7fb38fbe3c370`.
2. V4 successor.patch, preserved in
   `313582e25cbc83056022291379ba53b4cb370d3a`.
3. V5 successor.patch, preserved in
   `299704c73a37c359b39a9360a6754e92a1ee08f6`.
4. This V6 successor.patch.

All archived candidate artifacts are below
`docs/research/unapplied-candidates/class-init-outlining/`.
Verify each predecessor, all final hashes and every unchanged baseline file.
The manifest binds both unchanged production postimages as well as the new
test. Production compiler checksum remains
`68581b5d714a4d11835199b80fd6cbcb6e3178245c79dcef6843d73469b1a5e5`;
the pcc tree remains `dc63c2a946f533f7b31ce63db2f759a044fdc37f`.
A successor's full source inventory is necessarily new because its test changed.

The retained baseline inventory ea395e18a173ec4172c8badd68bbccb53a66390e4c130b8a78f6702fbc177635
has the complete production pcc tree d234174fbac80575ea822f6100f65fe914dd4498;
its older unrelated tests/docs are not relabeled as a whole 02b9 checkout.
No other optimization or OSError candidate is included.

After separate admission and preservation, the coordinator may run the same
single file once with the same limits and complete source seals:

`python -m pytest -x -n0 -vv --tb=short -o addopts= tests/python/test_class_init_outlining.py`

The bounds remain 300 seconds, hard AS4GiB/NPROC0, 4GiB free reserve and the
exclusive shared lock. Stop at the first failure. Real parsing, inference,
generation and owned IR verification remain the selected host scope, with no
native/runtime build, subprocess or ctypes requirement. Heavy-module,
performance and native gates require separate admission.
