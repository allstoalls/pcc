# Unapplied class-init outline candidate V1

Recovery snapshot only. Independent source review is still in progress. Every
PCC import, test collection, compiler, native, mechanism and performance gate
for these exact bytes is UNRUN. Do not apply this packet to production or run
it as an admitted gate. A reviewed successor must resolve the known issue.

## Exact base and recovery

The production base is commit
`02b9bc2d3c071bbc3e547a63e045daa73cc6e1bc`, whose pcc subtree is
`d234174fbac80575ea822f6100f65fe914dd4498`. The inspected source-container
inventory is `ea395e18a173ec4172c8badd68bbccb53a66390e4c130b8a78f6702fbc177635`;
only its pcc subtree is asserted equal to that production commit. Its unrelated
older tests/docs are not relabeled as an exact checkout.

The four public files are production.patch, tests.patch, README.md and
manifest.json. Exact preimages/postimages and byte lengths are in the manifest.
The production patch changes only class_gen.py and generation_lowering.py. The
test patch adds tests/python/test_class_init_outlining.py. A restoration should
first verify each base hash and absence of the new test, then apply production
and test patches in an ordinary separate source copy, verify every postimage,
and verify all unchanged baseline files. No runtime archive is supplied or
newly qualified. Private working copies of complete pre/post files are not
part of the four-file public bundle.

The reviewed design was preserved in commit
`775e5e44038f173b6658a36c5b899477588cb54d` under
`docs/research/unapplied-candidates/class-init-outlining/`. V1 implements a
strict subset: a private noinline i32 status helper for closed-literal direct
module classes, both external entrypoints retained, every actual invocation
performing fresh construction. It adds no Python recursion activation.
The original _emit_class_init body is unchanged. The generated function uses
its own lexical owner/root state; runtime semantics still require execution.

## Known review finding at preservation

V1 does not retain original decorator eligibility before declaration strips
no-op decorators. A class such as @final can therefore outline in class-only
init while its top-init occurrence remains inline. That violates the promised
first-version exclusion and creates an asymmetric result. It is a confirmed
source/scope issue, not an executed runtime regression. Retain original
qualification facts before stripping and add a no-op-decorator exclusion case
in a versioned successor. Do not overwrite this recovery snapshot. Other
independent-review findings may still arrive.

## Prepared tests and bounds

The new host-only file has 26 intended parametrized nodes (not collected). It
uses real parsing, inference, L1 generation, owned IR verification and default
owned passes, plus both inline modes. It includes an inline-disabled oracle,
public ABI and source-order checks, selected root operations and state
restoration, excluded shapes, and injected structural error branches. Those
branches are not native runtime fault coverage. None of these tests has run.

After source review and a separately admitted successor, the proposed focused
command is `python -m pytest -x -n0 -vv --tb=short
tests/python/test_class_init_outlining.py`. The coordinator must provide the
existing sole-process guard and shared lock: 300 seconds, hard AS4GiB/NPROC0,
4GiB free reserve, fresh outputs and complete source seals. This is a proposal,
not execution authorization. No heavy module, runtime build, native run or
full Stage1 is included.

The real c_ast source census found 55 candidate class shapes, but V1 has not
been evaluated on its typed module. Counts are not measured savings. The ARM
worklist and x86 bridge experiments remain separately closed as HOLD.
