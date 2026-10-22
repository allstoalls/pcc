from __future__ import annotations

import pytest

from pcc.backend import BackendUnavailable
from pcc.backend.self_backend_dispatch import emit_self_asm


_TRIPLE = 'target triple = "arm64-apple-darwin25.5.0"\n'


def _rejects(body: str, check: str) -> None:
    with pytest.raises(BackendUnavailable, match=rf"self IR verifier \[{check}\]"):
        emit_self_asm(_TRIPLE + body)


def test_self_ir_verifier_rejects_non_dominating_ssa_use():
    _rejects(
        """
define i64 @bad_dominance(i1 %cond) {
entry:
  br i1 %cond, label %use, label %definition
use:
  %result = add i64 %later, 1
  ret i64 %result
definition:
  %later = add i64 40, 1
  ret i64 %later
}
""",
        "ssa-dominance",
    )


def test_self_ir_verifier_keeps_duplicate_definition_diagnostic_after_indexing():
    _rejects(
        """
define i64 @bad_duplicate(i1 %cond) {
entry:
  %value = add i64 1, 2
  br i1 %cond, label %left, label %right
left:
  %value = add i64 3, 4
  ret i64 %value
right:
  ret i64 %value
}
""",
        "ssa-definition",
    )


def test_self_ir_verifier_rejects_operand_type_disagreement():
    _rejects(
        """
define i32 @bad_type(i64 %arg) {
entry:
  %result = add i32 %arg, 1
  ret i32 %result
}
""",
        "operand-type",
    )


def test_self_ir_verifier_rejects_phi_with_repeated_and_missing_predecessor():
    _rejects(
        """
define i64 @bad_phi(i1 %cond) {
entry:
  br i1 %cond, label %left, label %right
left:
  %l = add i64 1, 2
  br label %merge
right:
  %r = add i64 3, 4
  br label %merge
merge:
  %joined = phi i64 [ %l, %left ], [ %l, %left ]
  ret i64 %joined
}
""",
        "phi-predecessors",
    )


def _parallel_edge_phi(incoming: str, terminator: str) -> str:
    return _TRIPLE + """
define i64 @parallel_edges(i64 %tag) {
entry:
  """ + terminator + """
fallback:
  br label %merge
merge:
  %value = phi i64 """ + incoming + """
  ret i64 %value
}
"""


@pytest.mark.parametrize("terminator,incoming", [
    ("switch i64 %tag, label %fallback [ i64 1, label %merge i64 2, label %merge ]",
     "[ 0, %fallback ], [ 7, %entry ], [ 7, %entry ]"),
    ("br i1 true, label %merge, label %merge",
     "[ 0, %fallback ], [ 7, %entry ], [ 7, %entry ]"),
])
def test_self_ir_verifier_accepts_equal_phi_values_on_parallel_edges(terminator, incoming):
    assert "_parallel_edges:" in emit_self_asm(_parallel_edge_phi(incoming, terminator))


@pytest.mark.parametrize("incoming", [
    "[ 0, %fallback ], [ 7, %entry ]",
    "[ 0, %fallback ], [ 7, %entry ], [ 7, %entry ], [ 7, %entry ]",
    "[ 0, %fallback ], [ 7, %entry ], [ 8, %entry ]",
])
def test_self_ir_verifier_rejects_parallel_edge_phi_count_or_value_mismatch(incoming):
    terminator = "switch i64 %tag, label %fallback [ i64 1, label %merge i64 2, label %merge ]"
    with pytest.raises(BackendUnavailable, match=r"self IR verifier \[phi-predecessors\]"):
        emit_self_asm(_parallel_edge_phi(incoming, terminator))


def test_self_ir_verifier_rejects_swapped_phi_value_edges():
    _rejects(
        """
define i64 @swapped_phi(i1 %cond) {
entry:
  br i1 %cond, label %left, label %right
left:
  %l = add i64 1, 2
  br label %merge
right:
  %r = add i64 3, 4
  br label %merge
merge:
  %joined = phi i64 [ %r, %left ], [ %l, %right ]
  ret i64 %joined
}
""",
        "ssa-dominance",
    )


def test_self_ir_verifier_rejects_missing_branch_target():
    _rejects(
        """
define void @bad_target() {
entry:
  br label %missing
}
""",
        "terminator",
    )


def _indexed_cfg_function(blocks, inline_targets=()):
    from pcc.backend.self_backend_ir import (
        ParsedBlock,
        ParsedFunction,
        ParsedInstr,
        TypeDesc,
    )
    from pcc.backend.self_backend_kernel import (
        INLINE_ERROR_EDGE_WIDTH,
        get_indexed_function_kernel,
    )

    func = ParsedFunction(
        name="cfg_adjacency",
        ret_type=TypeDesc("void"),
        args=[],
        is_global=True,
        is_vararg=False,
        blocks=[
            ParsedBlock(name, terminator=ParsedInstr(kind, data))
            for name, kind, data in blocks
        ],
    )
    kernel = get_indexed_function_kernel(func)
    # These graph-only fixtures populate the real indexed edge plane; CFG
    # construction reads its target IDs independently of instruction checks.
    for source, targets in inline_targets:
        start = len(kernel.error_edge_scalars) // INLINE_ERROR_EDGE_WIDTH
        kernel.error_edge_spans.set2_unchecked(source * 2, start, len(targets))
        for target in targets:
            kernel.error_edge_scalars.append4(source, 0, -1, target)
            kernel.error_edge_scalars.append4(0, 0, -1, 0)
    return func, kernel


def test_cfg_preserves_order_and_deduplicates_all_edge_kinds():
    from pcc.backend.self_backend_ir import TypeDesc
    from pcc.backend.self_backend_verify import _build_cfg

    func, kernel = _indexed_cfg_function(
        [
            ("entry", "switch", (
                TypeDesc("int", 64), "0", "right",
                ((0, "left"), (1, "right"), (2, "merge"), (3, "left")),
            )),
            ("left", "br_cond", ("true", "merge", "merge")),
            ("right", "br", ("merge",)),
            ("merge", "ret_void", ()),
            ("error", "br", ("error",)),
        ],
        [(0, [4, 1, 4, 3]), (1, [4, 4, 3]), (2, [0, 4]), (4, [4])],
    )

    _buckets, predecessors, successors = _build_cfg(func, kernel)

    assert successors == [[2, 1, 3, 4], [3, 4], [3, 0, 4], [], [4]]
    assert predecessors == [[2], [0], [0], [0, 1, 2], [0, 1, 2, 4]]


def test_cfg_high_fan_in_does_not_rescan_predecessors(monkeypatch):
    from pcc.backend import self_backend_verify

    source_count = 512
    func, kernel = _indexed_cfg_function(
        [
            (f"source.{index}", "br_cond", ("true", "merge", "merge"))
            for index in range(source_count)
        ] + [("merge", "ret_void", ())],
        [(index, [source_count, source_count]) for index in range(source_count)],
    )
    assert kernel.cfg_successor_count(0) == 4

    class CountedSourceIndex(int):
        comparisons = 0

        def __eq__(self, other):
            CountedSourceIndex.comparisons += 1
            return int.__eq__(self, other)

        __hash__ = int.__hash__

    def counted_enumerate(values):
        for index, value in enumerate(values):
            yield CountedSourceIndex(index), value

    # Count source-ID equality operations instead of imposing a wall-time
    # threshold. Scanning the growing join predecessor list is quadratic.
    monkeypatch.setattr(
        self_backend_verify, "enumerate", counted_enumerate, raising=False,
    )
    _buckets, predecessors, successors = self_backend_verify._build_cfg(func, kernel)

    assert CountedSourceIndex.comparisons == 0
    assert predecessors == (
        [[] for _ in range(source_count)] + [list(range(source_count))]
    )
    assert successors == [[source_count] for _ in range(source_count)] + [[]]


@pytest.mark.parametrize("edge_kind", ["terminator", "inline"])
def test_cfg_preserves_missing_target_diagnostic(edge_kind):
    from pcc.backend.self_backend_verify import _build_cfg

    blocks = [("entry", "br", ("missing",))]
    inline_targets = []
    if edge_kind == "inline":
        blocks = [("entry", "ret_void", ())]
        inline_targets = [(0, [-1])]
    func, kernel = _indexed_cfg_function(blocks, inline_targets)

    with pytest.raises(BackendUnavailable) as error:
        _build_cfg(func, kernel)

    assert str(error.value) == (
        "self IR verifier [terminator] in 'cfg_adjacency': "
        "block 'entry' branches to missing block "
        "'<invalid-inline-or-terminator-target>'"
    )


def test_self_ir_verifier_rejects_return_type_disagreement():
    _rejects(
        """
define i64 @bad_return() {
entry:
  ret i32 1
}
""",
        "terminator",
    )


def test_self_ir_verifier_accepts_well_formed_loop_phi():
    asm = emit_self_asm(
        _TRIPLE
        + """
define i64 @good_loop(i64 %limit) {
entry:
  br label %loop
loop:
  %index = phi i64 [ 0, %entry ], [ %next, %loop ]
  %next = add i64 %index, 1
  %keep_going = icmp slt i64 %next, %limit
  br i1 %keep_going, label %loop, label %done
done:
  ret i64 %next
}
"""
    )
    assert "_good_loop:" in asm


def test_self_ir_verifier_treats_undef_as_a_constant_value():
    asm = emit_self_asm(
        _TRIPLE
        + """
define i64 @freeze_undef() {
entry:
  %value = freeze i64 undef
  ret i64 %value
}
"""
    )
    assert "_freeze_undef:" in asm


def _dense_dominators_oracle(predecessors: list[list[int]]) -> list[set[int]]:
    # The pre-2026-08-15 dense algorithm, kept as a correctness oracle only.
    # It is O(blocks^2) memory and must never run on real modules again
    # (72k-block module tops made stage1 exceed 50 GiB RSS).
    block_count = len(predecessors)
    all_blocks = set(range(block_count))
    dominators = [set(all_blocks) for _index in range(block_count)]
    dominators[0] = {0}
    changed = True
    while changed:
        changed = False
        for index in range(1, block_count):
            preds = predecessors[index]
            if preds:
                new_set = set(dominators[preds[0]])
                for pred in preds[1:]:
                    new_set.intersection_update(dominators[pred])
                new_set.add(index)
            else:
                new_set = {index}
            if new_set != dominators[index]:
                dominators[index] = new_set
                changed = True
    return dominators


def _successors_of(predecessors: list[list[int]]) -> list[list[int]]:
    successors: list[list[int]] = [[] for _ in predecessors]
    for block, preds in enumerate(predecessors):
        for pred in preds:
            successors[pred].append(block)
    return successors


def test_dominator_intervals_match_dense_oracle():
    from pcc.backend.self_backend_verify import (
        _block_dominates,
        _compute_dominators,
    )

    cases = [
        # diamond
        [[], [0], [0], [1, 2]],
        # loop with latch and exit
        [[], [0, 2], [1], [1]],
        # many error paths joining one shared err.exit
        [[], [0], [0], [1], [1], [2], [3, 4, 5], [6]],
        # nested loop + shared exit
        [[], [0, 3], [1, 2], [2], [1]],
        # malformed: predecessor-free non-entry block
        [[], [0], []],
    ]
    for predecessors in cases:
        oracle = _dense_dominators_oracle(predecessors)
        intervals = _compute_dominators(
            predecessors, _successors_of(predecessors)
        )
        n = len(predecessors)
        for dom_block in range(n):
            for block in range(n):
                assert _block_dominates(intervals, dom_block, block) == (
                    dom_block in oracle[block]
                ), (predecessors, dom_block, block)


def test_dominator_intervals_stay_linear_on_huge_chain():
    from pcc.backend.self_backend_verify import (
        _block_dominates,
        _compute_dominators,
    )

    n = 100_000
    predecessors = [[] for _ in range(n)]
    successors = [[] for _ in range(n)]
    for block in range(1, n):
        predecessors[block].append(block - 1)
        successors[block - 1].append(block)
    intervals = _compute_dominators(predecessors, successors)
    assert _block_dominates(intervals, 0, n - 1)
    assert _block_dominates(intervals, n // 2, n - 1)
    assert not _block_dominates(intervals, n - 1, 0)


def test_self_ir_verifier_accepts_shared_error_exit_join():
    asm = emit_self_asm(
        _TRIPLE
        + """
define i64 @shared_err_exit(i1 %a, i1 %b) {
entry:
  %base = add i64 1, 2
  br i1 %a, label %one, label %two
one:
  br i1 %b, label %ok, label %err.exit
two:
  br i1 %b, label %ok, label %err.exit
ok:
  ret i64 %base
err.exit:
  %code = phi i64 [ 1, %one ], [ 2, %two ]
  %sum = add i64 %code, %base
  ret i64 %sum
}
"""
    )
    assert "_shared_err_exit:" in asm


def test_self_ir_verifier_accepts_vector_comparison_result_type():
    asm = emit_self_asm(
        _TRIPLE
        + """
define <2 x i1> @vector_compare(<2 x i64> %lhs, <2 x i64> %rhs) {
entry:
  %result = icmp eq <2 x i64> %lhs, %rhs
  ret <2 x i1> %result
}
"""
    )
    assert "_vector_compare:" in asm
