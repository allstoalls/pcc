from __future__ import annotations

import copy
from pathlib import Path
import re
import statistics
import subprocess
import textwrap

import pytest

from pcc import guarded_i64_dot, guarded_loop_counter, i64_buffer

from pcc.frontends.python.guarded_loop_plan import (
    FAST_OPERATIONS,
    GUARD_ORDER,
    SCALAR_OPERATIONS,
    DotLoopCandidate,
    RuntimeObservation,
    TargetCost,
    build_dot_loop_plan,
    evaluate_guards,
    owner_lowering_contract,
    plan_from_payload,
)


_D = "3" * 64


def _candidate(**updates):
    values = {
        "source_id": "1" * 64,
        "function_id": "bench.dot",
        "left_type_id": "pcc.buffer.i64.readonly",
        "right_type_id": "pcc.buffer.i64.readonly",
        "left_layout_version": "2" * 64,
        "right_layout_version": "2" * 64,
        "function_version": _D,
        "globals_version": "4" * 64,
        "left_buffer_version": 7,
        "right_buffer_version": 9,
        "trip_count": 64,
        "left_stride_bytes": 8,
        "right_stride_bytes": 8,
        "left_alignment": 16,
        "right_alignment": 16,
        "left_integer_range": (-1000, 1000),
        "right_integer_range": (-1000, 1000),
    }
    values.update(updates)
    return DotLoopCandidate.create(**values)


def _cost(target="self-aarch64-darwin", **updates):
    values = {
        "target": target,
        "vector_lanes": 2,
        "scalar_cost": 1000,
        "fast_cost": 300,
        "guard_cost": 100,
        "minimum_speedup_basis_points": 500,
    }
    values.update(updates)
    return TargetCost.create(**values)


def test_removed_llvm_loop_target_and_owner_are_rejected():
    with pytest.raises(ValueError, match="unsupported loop-plan target"):
        _cost("llvm")
    plan = build_dot_loop_plan(_candidate(), _cost())
    with pytest.raises(ValueError, match="unsupported guarded-loop owner"):
        owner_lowering_contract("llvm", plan)


def _observation(candidate, **updates):
    values = {
        "left_type_id": candidate.left_type_id,
        "right_type_id": candidate.right_type_id,
        "left_layout_version": candidate.left_layout_version,
        "right_layout_version": candidate.right_layout_version,
        "function_version": candidate.function_version,
        "globals_version": candidate.globals_version,
        "left_buffer_version": candidate.left_buffer_version,
        "right_buffer_version": candidate.right_buffer_version,
        "trip_count": candidate.trip_count,
        "aliases": False,
        "left_stride_bytes": candidate.left_stride_bytes,
        "right_stride_bytes": candidate.right_stride_bytes,
        "left_alignment": candidate.left_alignment,
        "right_alignment": candidate.right_alignment,
        "left_integer_range": candidate.left_integer_range,
        "right_integer_range": candidate.right_integer_range,
    }
    values.update(updates)
    return RuntimeObservation(**values)


def test_guarded_dot_plan_has_exact_guard_and_slow_path_contract():
    candidate = _candidate()
    plan = build_dot_loop_plan(candidate, _cost())

    assert plan.accepted
    assert tuple(guard.kind for guard in plan.guards) == GUARD_ORDER
    assert plan.fast_operations == FAST_OPERATIONS
    assert plan.scalar_operations == SCALAR_OPERATIONS
    assert "python-int-multiply-promote" in " ".join(plan.scalar_operations)
    assert "python-int-add-promote" in " ".join(plan.scalar_operations)
    assert "fast.overflow.restart-scalar-at-zero" in plan.fast_operations
    assert evaluate_guards(plan, _observation(candidate)).hit


@pytest.mark.parametrize(
    ("updates", "miss_guard"),
    [
        ({"left_type_id": "foreign.buffer"}, "left-exact-type"),
        ({"right_layout_version": "5" * 64}, "right-layout-version"),
        ({"function_version": "6" * 64}, "function-version"),
        ({"globals_version": "7" * 64}, "globals-version"),
        ({"left_buffer_version": 8}, "left-buffer-version"),
        ({"trip_count": 63}, "trip-count"),
        ({"aliases": True}, "no-alias"),
        ({"left_stride_bytes": 16}, "left-unit-stride"),
        ({"right_alignment": 4}, "right-alignment"),
        ({"left_integer_range": (-1001, 1000)}, "left-integer-range"),
    ],
)
def test_each_runtime_guard_miss_selects_ordered_scalar_path(updates, miss_guard):
    candidate = _candidate()
    plan = build_dot_loop_plan(candidate, _cost())
    result = evaluate_guards(plan, _observation(candidate, **updates))

    assert not result.hit
    assert result.miss_guard == miss_guard
    assert ("slow-path", 1) in result.counters
    assert plan.scalar_operations[0] == "scalar.index.zero"


@pytest.mark.parametrize(
    ("candidate_updates", "cost_updates", "reason"),
    [
        ({"exact_builtin_buffers": False}, {}, "not-exact-builtin-buffers"),
        ({"readonly": False}, {}, "observable-store-or-mutation"),
        ({"effects": ("read:left", "call:dunder")}, {}, "unproved-or-observable-effect"),
        ({"exception_order": ("right-load", "left-load")}, {}, "exception-order-mismatch"),
        ({"left_stride_bytes": 16}, {}, "non-unit-stride"),
        ({"left_alignment": 4}, {}, "insufficient-alignment"),
        ({"trip_count": 1}, {}, "trip-count-below-target-lanes"),
        (
            {"left_integer_range": (-(1 << 62), 1 << 62)},
            {},
            "integer-range-needs-python-promotion",
        ),
        ({}, {"fast_cost": 950}, "target-cost-not-profitable"),
    ],
)
def test_unproved_legality_or_profitability_rejects_without_fast_ops(
    candidate_updates,
    cost_updates,
    reason,
):
    plan = build_dot_loop_plan(
        _candidate(**candidate_updates),
        _cost(**cost_updates),
    )

    assert not plan.accepted
    assert reason in plan.rejection_reasons
    assert plan.guards == ()
    assert plan.fast_operations == ()
    assert owner_lowering_contract("self-aarch64-darwin", plan) == SCALAR_OPERATIONS


def test_both_self_targets_consume_identical_owner_neutral_order():
    candidate = _candidate()
    plans = [
        build_dot_loop_plan(candidate, _cost("self-aarch64-darwin")),
        build_dot_loop_plan(candidate, _cost("self-x86_64-linux")),
    ]
    lowered = [
        owner_lowering_contract(plan.target_cost.target, plan) for plan in plans
    ]

    assert lowered[0] == lowered[1]
    assert lowered[0][: len(GUARD_ORDER)] == tuple(
        "guard." + kind for kind in GUARD_ORDER
    )
    assert "guard.miss.branch-scalar-at-zero" in lowered[0]
    assert lowered[0][-len(SCALAR_OPERATIONS) :] == SCALAR_OPERATIONS


def test_plan_payload_is_deterministic_and_strictly_validated():
    plan = build_dot_loop_plan(_candidate(), _cost())
    rebuilt = plan_from_payload(plan.payload())

    assert rebuilt == plan
    assert rebuilt.digest() == plan.digest()

    reordered = copy.deepcopy(plan.payload())
    reordered["guards"][0], reordered["guards"][1] = (
        reordered["guards"][1],
        reordered["guards"][0],
    )
    with pytest.raises(ValueError, match="guard order"):
        plan_from_payload(reordered)

    unknown = copy.deepcopy(plan.payload())
    unknown["new_semantics"] = True
    with pytest.raises(ValueError, match="fields"):
        plan_from_payload(unknown)


def test_invalid_candidate_identity_and_target_fail_closed():
    with pytest.raises(ValueError, match="source digest"):
        _candidate(source_id="not-a-digest")
    with pytest.raises(ValueError, match="unsupported loop-plan target"):
        _cost("metal")


def _production_source() -> str:
    return textwrap.dedent(
        """
        import pcc

        def dot(
            left: pcc.i64_buffer[4],
            right: pcc.i64_buffer[4],
        ) -> int:
            return pcc.guarded_i64_dot(left, right)

        left = pcc.i64_buffer[4](1, 2, 3, 4)
        right = pcc.i64_buffer[4](5, 6, 7, 8)
        same = pcc.i64_buffer[4](1, 2, 3, 4)
        huge = pcc.i64_buffer[2](9223372036854775807, 9223372036854775807)
        twos = pcc.i64_buffer[2](2, 2)
        print(dot(left, right))
        print(pcc.guarded_i64_dot(same, same))
        print(pcc.guarded_i64_dot(huge, twos))
        print(pcc.guarded_loop_counter("candidate"))
        print(pcc.guarded_loop_counter("guard_hit"))
        print(pcc.guarded_loop_counter("guard_miss"))
        print(pcc.guarded_loop_counter("overflow"))
        print(pcc.guarded_loop_counter("scalar_fallback"))
        print(pcc.guarded_loop_counter("fast_result"))
        """
    ).lstrip()


def _production_ir(source: str | None = None) -> str:
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python import type_infer
    from pcc.frontends.python.codegen import layer1

    text = source if source is not None else _production_source()
    ast_module = parse_and_lift(text, "<guarded-loop>", "guarded_loop_mod")
    typed = type_infer.infer_module(ast_module)
    codegen = layer1.L1CodeGen(typed, ir_scaffold_mode="on")
    return str(codegen.generate(typed))


def test_host_i64_buffer_oracle_preserves_python_int_on_miss_and_overflow():
    before = {name: guarded_loop_counter(name) for name in (
        "candidate",
        "guard_hit",
        "guard_miss",
        "overflow",
        "scalar_fallback",
        "fast_result",
    )}
    left = i64_buffer[4](1, 2, 3, 4)
    right = i64_buffer[4](5, 6, 7, 8)
    huge = i64_buffer[2]((1 << 63) - 1, (1 << 63) - 1)
    twos = i64_buffer[2](2, 2)

    assert guarded_i64_dot(left, right) == 70
    assert guarded_i64_dot(left, left) == 30
    assert guarded_i64_dot(huge, twos) == 4 * ((1 << 63) - 1)
    after = {name: guarded_loop_counter(name) for name in before}
    assert {name: after[name] - before[name] for name in before} == {
        "candidate": 3,
        "guard_hit": 2,
        "guard_miss": 1,
        "overflow": 1,
        "scalar_fallback": 2,
        "fast_result": 1,
    }


def test_host_i64_buffer_constructor_is_exact_and_bounded():
    assert i64_buffer[2](-1, 2) == (
        (-1).to_bytes(8, "little", signed=True)
        + (2).to_bytes(8, "little", signed=True)
    )
    with pytest.raises(TypeError, match="exact int"):
        i64_buffer[1](True)
    with pytest.raises(OverflowError, match="signed i64"):
        i64_buffer[1](1 << 63)
    with pytest.raises(TypeError, match="exactly 2"):
        i64_buffer[2](1)
    with pytest.raises(ValueError, match="unknown guarded-loop counter"):
        guarded_loop_counter("unknown")


def test_production_lowering_emits_exact_owner_neutral_guard_fast_slow_shape():
    ir_text = _production_ir()
    positions = []
    for guard in GUARD_ORDER:
        marker = "guarded.dot." + guard
        position = ir_text.find(marker)
        assert position >= 0, marker
        positions.append(position)
    assert positions == sorted(positions)
    for symbol in (
        "@pcc_py_type_of",
        "@py_i64_buffer_layout_version",
        "@py_i64_buffer_version",
        "@py_bytes_len",
        "@py_i64_buffer_data",
        "@llvm.smul.with.overflow.i64",
        "@py_i64_buffer_dot_scalar",
    ):
        assert symbol in ir_text
    assert "@py_guarded_loop_counter_add" in ir_text
    assert "@py_guarded_loop_counter_get" in ir_text
    assert "load i64" in ir_text
    assert re.search(r"\bcall [^\n]*@py_cpy_", ir_text) is None


def test_production_guarded_loop_ir_is_accepted_by_both_self_target_owners():
    from pcc.backend.self_backend_dispatch import emit_self_asm
    from pcc.frontends.python.pipeline_targets import ir_text_with_target_triple

    ir_text = _production_ir()
    darwin_ir = ir_text_with_target_triple(ir_text, "arm64-apple-darwin")
    linux_ir = ir_text_with_target_triple(
        ir_text, "x86_64-unknown-linux-gnu"
    )
    darwin = emit_self_asm(darwin_ir, "arm64-apple-darwin")
    linux = emit_self_asm(linux_ir, "x86_64-unknown-linux-gnu")
    assert "smulh" in darwin
    assert "imul" in linux


@pytest.mark.parametrize("backend", [pytest.param(None, id="default-self"), pytest.param("self", id="explicit-self")])
def test_production_guard_hit_alias_and_overflow_match_host(tmp_path, backend):
    from pcc.frontends.python.pipeline import compile_python

    label = backend or "default"
    source = tmp_path / ("guarded_loop_" + label + ".py")
    executable = tmp_path / ("guarded_loop_" + label)
    source.write_text(_production_source(), encoding="utf-8")
    compile_python(
        str(source),
        str(executable),
        backend=backend,
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    completed = subprocess.run(
        [str(executable)],
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    assert completed.stdout == (
        "70\n"
        "30\n"
        "36893488147419103228\n"
        "3\n2\n1\n1\n2\n1\n"
    )


@pytest.mark.parametrize(
    "source",
    (
        "import pcc\na = pcc.i64_buffer[2](1, 2)\n"
        "b = pcc.i64_buffer[3](1, 2, 3)\n"
        "pcc.guarded_i64_dot(a, b)\n",
        "import pcc\npcc.i64_buffer[1](True)\n",
        "from pcc import i64_buffer\ni64_buffer[2](1)\n",
    ),
)
def test_production_typed_buffer_discovery_rejects_unproved_shapes(source):
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python import type_infer
    from pcc.frontends.python.types import PyFrontendError

    ast_module = parse_and_lift(source, "<guarded-loop-bad>", "guarded_bad")
    with pytest.raises(PyFrontendError):
        type_infer.infer_module(ast_module)


@pytest.mark.integration
@pytest.mark.parametrize("backend", [pytest.param(None, id="default-self"), pytest.param("self", id="explicit-self")])
def test_guarded_loop_pinned_multisample_speed_and_miss_budget(tmp_path, backend):
    from pcc.frontends.python.pipeline import compile_python

    repository = Path(__file__).resolve().parents[2]
    source = repository / "benchmarks" / "python" / "guarded_i64_dot.py"
    executable = tmp_path / ("guarded_i64_dot_" + (backend or "default"))
    compile_python(
        str(source),
        str(executable),
        backend=backend,
        libpython_mode="off",
        ir_scaffold_mode="on",
    )

    def measure(mode: str, rounds: int) -> float:
        completed = subprocess.run(
            [str(executable), mode, str(rounds)],
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
        )
        return float(completed.stdout.strip())

    for mode in ("scalar", "hit", "miss"):
        measure(mode, 2_000)
    samples = {"scalar": [], "hit": [], "miss": []}
    for _sample in range(7):
        for mode in ("scalar", "hit", "miss"):
            samples[mode].append(measure(mode, 20_000))
    medians = {name: statistics.median(values) for name, values in samples.items()}
    assert medians["hit"] <= medians["scalar"] * 0.95, medians
    assert medians["miss"] <= medians["scalar"] * 1.02, medians
