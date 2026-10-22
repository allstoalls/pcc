"""Typed-int projection gates for default tagged ints and explicit raw-i64 mode."""

from __future__ import annotations

import re
import subprocess
import textwrap
from pathlib import Path

import pytest


def _compile_to_ll(tmp_path: Path, source: str, name: str) -> str:
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / f"{name}.py"
    out = tmp_path / f"{name}.ll"
    src.write_text(source, encoding="utf-8")
    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        libpython_mode="off",
    )
    return out.read_text(encoding="utf-8")


def _fn_body(ir_text: str, fn_suffix: str) -> str | None:
    pattern = re.compile(
        r"define\s+[^\n]*?@[A-Za-z0-9_]*"
        + re.escape(fn_suffix)
        + r"\s*\([^)]*\)[^{]*\{(.+?)\n\}",
        re.DOTALL,
    )
    m = pattern.search(ir_text)
    return m.group(1) if m else None


_TYPED_LOOP = textwrap.dedent("""
    def bench(n: int) -> int:
        acc: int = 0
        i: int = 0
        while i < n:
            acc = acc + (i % 7) + (i // 13)
            i = i + 1
        return acc

    print(bench(10000))
    """)


_TYPED_LIST_LOOP = textwrap.dedent("""
    def sum_ints(xs: list[int]) -> int:
        s: int = 0
        for x in xs:
            s = s + x
        return s

    print(sum_ints([1, 2, 3, 4]))
    """)


_TYPED_FLOAT_LOOP = textwrap.dedent("""
    def bench(n: int) -> float:
        acc: float = 0.0
        i: int = 0
        while i < n:
            acc = (acc + 1.0) * 4.0 / 2.0
            i = i + 1
        return acc

    print(bench(6))
    """)


_TYPED_PURE_FLOAT = textwrap.dedent("""
    def scale(x: float, y: float) -> float:
        return (x + y) * 2.0

    print(scale(1.25, 2.5))
    """)


_TYPED_FUNCTION_CALL_LOOP = textwrap.dedent("""
    def bump(x: int) -> int:
        return x + 2

    def step(i: int) -> int:
        return bump(i % 7)

    def bench(n: int) -> int:
        total: int = 0
        i: int = 0
        while i < n:
            total = total + step(i)
            i = i + 1
        return total

    print(bench(2100000))
    """)


_TYPED_RANGE_LOOP = textwrap.dedent("""
    def sum_range(n: int) -> int:
        total: int = 0
        for i in range(n):
            total = total + i
        return total

    print(sum_range(10))
    """)


_TYPED_RANGE_ESCAPE = textwrap.dedent("""
    from typing import Any

    def keep(value: Any) -> Any:
        return value

    def bump(value: int) -> int:
        return value + 1

    def last(n: int) -> int:
        result: int = 0
        for i in range(n):
            result = bump(keep(i))
        return result

    print(last(5))
    """)


def _enable_unsafe_i64(monkeypatch):
    monkeypatch.setenv("PCC_PYTHON_TYPED_INT_ABI", "unsafe-i64")


def _assert_no_class_or_valuebox_allocation(body: str) -> None:
    assert "@py_instance_new" not in body, body
    assert "@py_valuebox_new" not in body, body


def test_c_abi_i64_branch_keeps_negative_i64_min_in_scalar_lane(tmp_path):
    ir_text = _compile_to_ll(
        tmp_path,
        textwrap.dedent(
            """
            from pcc.extern import c_abi_typed_export

            @c_abi_typed_export("pcc_test_i64_min_branch", "i64", ("i64",))
            def pcc_test_i64_min_branch(flag: int) -> int:
                value: int = 7
                if flag < 0:
                    value = -0x8000000000000000
                return value
            """
        ),
        "typed_i64_min_branch",
    )
    body = _fn_body(ir_text, "pcc_test_i64_min_branch")
    assert body is not None, ir_text
    assert "phi  i64" in body or "phi i64" in body, body
    assert not re.search(r"phi\s+ptr.*null", body), body
    assert "@py_int_neg" not in body, body
    assert "@py_int_to_i64" not in body, body


def test_c_abi_i64_branch_boxes_fallthrough_when_other_branch_needs_bignum(
    tmp_path,
):
    ir_text = _compile_to_ll(
        tmp_path,
        textwrap.dedent(
            """
            from pcc.extern import c_abi_typed_export

            @c_abi_typed_export("pcc_test_bignum_branch", "i64", ("i64",))
            def pcc_test_bignum_branch(flag: int) -> int:
                value: int = 7
                if flag < 0:
                    value = 1 << 70
                return value
            """
        ),
        "typed_bignum_branch",
    )
    body = _fn_body(ir_text, "pcc_test_bignum_branch")
    assert body is not None, ir_text
    assert not re.search(r"phi\s+ptr.*null", body), body
    assert "@py_int_shl" in body, body
    # The literal must reach this path as an *object*, so the bignum branch and
    # this one agree on representation.  Either boxing form satisfies that: a
    # `py_int_from_i64` call, or the cheaper materialised tagged immediate
    # `inttoptr i64 15` (== (7 << 1) | 1), which is the documented value
    # projection of a small int and is just as much an object.  Asserting only
    # the call form made this gate red at HEAD once literals started lowering
    # to tagged constants; what matters is that 7 is not left as a raw i64.
    assert re.search(r"@py_int_from_i64\(i64 7\)", body) or re.search(
        r"int\.lit\.tagged[^=]*= inttoptr i64 15 to ptr", body
    ), body
    assert "@py_int_to_i64" in body, body


def test_c_abi_i64_while_boxes_zero_iteration_entry_when_body_needs_bignum(
    tmp_path,
):
    ir_text = _compile_to_ll(
        tmp_path,
        textwrap.dedent(
            """
            from pcc.extern import c_abi_typed_export

            @c_abi_typed_export("pcc_test_bignum_while", "i64", ("i64",))
            def pcc_test_bignum_while(n: int) -> int:
                value: int = 7
                while n < 0:
                    value = 1 << 70
                    n = n + 1
                return value
            """
        ),
        "typed_bignum_while",
    )
    body = _fn_body(ir_text, "pcc_test_bignum_while")
    assert body is not None, ir_text
    assert not re.search(r"phi\s+ptr.*null", body), body
    assert "@py_int_shl" in body, body
    # The literal must reach this path as an *object*, so the bignum branch and
    # this one agree on representation.  Either boxing form satisfies that: a
    # `py_int_from_i64` call, or the cheaper materialised tagged immediate
    # `inttoptr i64 15` (== (7 << 1) | 1), which is the documented value
    # projection of a small int and is just as much an object.  Asserting only
    # the call form made this gate red at HEAD once literals started lowering
    # to tagged constants; what matters is that 7 is not left as a raw i64.
    assert re.search(r"@py_int_from_i64\(i64 7\)", body) or re.search(
        r"int\.lit\.tagged[^=]*= inttoptr i64 15 to ptr", body
    ), body
    assert "@py_int_to_i64" in body, body


def test_c_abi_i64_parameter_uses_one_boxed_slot_when_branch_needs_bignum(
    tmp_path,
):
    ir_text = _compile_to_ll(
        tmp_path,
        textwrap.dedent(
            """
            from pcc.extern import c_abi_typed_export

            @c_abi_typed_export(
                "pcc_test_bignum_parameter", "i64", ("i64", "i64")
            )
            def pcc_test_bignum_parameter(value: int, flag: int) -> int:
                if flag < 0:
                    value = 1 << 70
                return value
            """
        ),
        "typed_bignum_parameter",
    )
    body = _fn_body(ir_text, "pcc_test_bignum_parameter")
    assert body is not None, ir_text
    assert re.search(r"%value\.addr(?:\.\d+)? = alloca ptr", body), body
    assert re.search(r"@py_int_from_i64\(i64 %value\)", body), body
    assert not re.search(r"phi\s+ptr.*null", body), body
    assert "@py_int_shl" in body, body
    assert "@py_int_to_i64" in body, body


def test_boxed_int_parameter_is_retained_before_exact_branch_rebind(tmp_path):
    ir_text = _compile_to_ll(
        tmp_path,
        textwrap.dedent(
            """
            def choose(value: int, flag: int) -> int:
                if flag < 0:
                    value = 1 << 70
                return value

            print(choose(7, 0))
            """
        ),
        "typed_bignum_boxed_parameter",
    )
    body = _fn_body(ir_text, "choose")
    assert body is not None, ir_text
    assert re.search(r"@pcc_gc_retain\(ptr %value\)", body), body
    assert re.search(r"%value\.addr(?:\.\d+)? = alloca ptr", body), body
    assert "@py_int_shl" in body, body


def test_boxed_method_int_parameter_roots_before_raw_branch_rebind(tmp_path):
    ir_text = _compile_to_ll(
        tmp_path,
        textwrap.dedent(
            """
            class Log:
                def __init__(self) -> None:
                    self.events = []

                def fork(self, event_count: int = -1) -> int:
                    if event_count < 0:
                        event_count = len(self.events)
                    return event_count
            """
        ),
        "typed_method_int_parameter",
    )
    body = _fn_body(ir_text, "Log_fork")
    assert body is not None, ir_text
    retain = body.index("@pcc_gc_retain(ptr %event_count)")
    root = body.index("%event_count.addr", retain)
    compare = body.index("@py_int_cmp", root)
    assert retain < root < compare, body
    assert "store ptr %event_count, ptr %event_count.addr" not in body, body


def test_exact_target_boxes_raw_name_without_second_retain(tmp_path):
    ir_text = _compile_to_ll(
        tmp_path,
        textwrap.dedent(
            """
            from pcc.extern import c_abi_typed_export

            @c_abi_typed_export(
                "pcc_test_exact_name_copy", "i64", ("i64", "i64")
            )
            def pcc_test_exact_name_copy(source: int, flag: int) -> int:
                value: int = source
                if flag < 0:
                    value = 1 << 70
                return value
            """
        ),
        "typed_bignum_name_copy",
    )
    body = _fn_body(ir_text, "pcc_test_exact_name_copy")
    assert body is not None, ir_text
    assert re.search(r"@py_int_from_i64\(i64 %source", body), body
    assert "value.local.copy.retain" not in body, body
    assert "@py_int_shl" in body, body


@pytest.mark.parametrize("operator", ["+=", "<<="])
def test_c_abi_i64_augassign_uses_exact_object_slot(tmp_path, operator):
    ir_text = _compile_to_ll(
        tmp_path,
        textwrap.dedent(
            f"""
            from pcc.extern import c_abi_typed_export

            @c_abi_typed_export("pcc_test_exact_augassign", "i64", ())
            def pcc_test_exact_augassign() -> int:
                value: int = 7
                value {operator} 70
                return value
            """
        ),
        "typed_bignum_augassign_" + operator.replace("=", "eq").replace("<", "l"),
    )
    body = _fn_body(ir_text, "pcc_test_exact_augassign")
    assert body is not None, ir_text
    assert re.search(r"%value\.addr(?:\.\d+)? = alloca ptr", body), body
    assert "@py_int_add" in body or "@py_int_shl" in body, body
    assert not re.search(r"store\s+i64\s+[^,]+,\s+ptr\s+%value\.addr", body), body
    assert "@py_int_to_i64" in body, body


def test_c_abi_i64_ifexpr_joins_in_exact_object_projection(tmp_path):
    ir_text = _compile_to_ll(
        tmp_path,
        textwrap.dedent(
            """
            from pcc.extern import c_abi_typed_export

            @c_abi_typed_export("pcc_test_exact_ifexpr", "i64", ("i64",))
            def pcc_test_exact_ifexpr(flag: int) -> int:
                value: int = (1 << 70) if flag < 0 else 7
                return value
            """
        ),
        "typed_bignum_ifexpr",
    )
    body = _fn_body(ir_text, "pcc_test_exact_ifexpr")
    assert body is not None, ir_text
    assert re.search(r"phi\s+ptr", body), body
    assert "@py_int_shl" in body, body
    assert "@py_int_to_i64" in body, body


def test_c_abi_i64_destructuring_branch_uses_one_exact_object_slot(tmp_path):
    ir_text = _compile_to_ll(
        tmp_path,
        textwrap.dedent(
            """
            from pcc.extern import c_abi_typed_export

            @c_abi_typed_export("pcc_test_exact_unpack", "i64", ("i64",))
            def pcc_test_exact_unpack(flag: int) -> int:
                value: int = 7
                if flag < 0:
                    (value,) = (1 << 70,)
                return value
            """
        ),
        "typed_bignum_unpack_branch",
    )
    body = _fn_body(ir_text, "pcc_test_exact_unpack")
    assert body is not None, ir_text
    assert len(re.findall(r"%value\.addr(?:\.\d+)? = alloca ptr", body)) == 1, body
    assert "@py_int_shl" in body, body
    assert "@py_int_to_i64" in body, body
    assert not re.search(r"store\s+i64\s+[^,]+,\s+ptr\s+%value\.addr", body), body


def test_class_method_exact_branch_uses_one_object_projection(tmp_path):
    ir_text = _compile_to_ll(
        tmp_path,
        textwrap.dedent(
            """
            class Counter:
                def choose(self, flag: int) -> int:
                    value: int = 7
                    if flag < 0:
                        value = 1 << 70
                    return value

            print(Counter().choose(0))
            """
        ),
        "typed_bignum_method",
    )
    body = _fn_body(ir_text, "Counter_choose")
    assert body is not None, ir_text
    assert re.search(r"%value\.addr(?:\.\d+)? = alloca ptr", body), body
    assert "@py_int_shl" in body, body
    assert not re.search(r"phi\s+ptr.*null", body), body


def test_exact_int_branch_comparison_uses_object_comparator(tmp_path):
    ir_text = _compile_to_ll(
        tmp_path,
        textwrap.dedent(
            """
            from pcc.extern import c_abi_typed_export

            @c_abi_typed_export("pcc_test_exact_compare", "i64", ("i64",))
            def pcc_test_exact_compare(flag: int) -> int:
                value: int = 7
                if flag < 0:
                    value = 1 << 70
                if value > (1 << 69):
                    return 1
                return 0
            """
        ),
        "typed_bignum_compare",
    )
    body = _fn_body(ir_text, "pcc_test_exact_compare")
    assert body is not None, ir_text
    assert "@py_int_cmp" in body, body
    assert "@py_int_shl" in body, body


def test_generator_exact_int_operands_have_managed_frame_slots(tmp_path):
    """Generator exact-int temporaries must not depend on may-park effects."""
    ir_text = _compile_to_ll(
        tmp_path,
        textwrap.dedent(
            """
            def squares(n: int):
                limit: int = 1 << 70
                for i in range(n):
                    if limit > (1 << 69):
                        yield i * i

            print(list(squares(4)))
            """
        ),
        "generator_exact_int_frame",
    )
    assert "__pcc_vthread_delegate_pcc_exact_int_lhs" in ir_text, ir_text
    assert "__pcc_vthread_delegate_pcc_exact_int_compare_lhs" in ir_text, ir_text
    assert "@py_int_mul" in ir_text, ir_text
    assert "@py_int_cmp" in ir_text, ir_text


def test_exact_int_branch_slot_is_owned_rooted_and_balanced_on_error(tmp_path):
    ir_text = _compile_to_ll(
        tmp_path,
        textwrap.dedent(
            """
            from pcc.extern import c_abi_typed_export

            @c_abi_typed_export("pcc_test_exact_root", "i64", ("i64",))
            def pcc_test_exact_root(flag: int) -> int:
                value: int = 7
                if flag < 0:
                    value = 1 << 70
                return value
            """
        ),
        "typed_bignum_root_balance",
    )
    body = _fn_body(ir_text, "pcc_test_exact_root")
    assert body is not None, ir_text
    assert len(re.findall(r"%value\.addr(?:\.\d+)? = alloca ptr", body)) == 1, body
    assert "@pcc_gc_frame_enter" in body, body
    assert "@pcc_gc_store_root" in body, body
    assert "@pcc_gc_release" in body, body
    assert "@pcc_gc_frame_leave" in body, body
    assert "err.exit" in body, body


def test_exact_int_huge_literal_power_is_not_folded_by_the_compiler(tmp_path):
    ir_text = _compile_to_ll(
        tmp_path,
        textwrap.dedent(
            """
            from pcc.extern import c_abi_typed_export

            @c_abi_typed_export("pcc_test_huge_pow", "i64", ())
            def pcc_test_huge_pow() -> int:
                value: int = 2 ** 1000000000
                return value
            """
        ),
        "typed_bignum_huge_pow",
    )
    body = _fn_body(ir_text, "pcc_test_huge_pow")
    assert body is not None, ir_text
    assert "@py_int_pow" in body, body
    # The exponent reaches the runtime call either boxed at run time or as
    # the tagged constant (1000000000 << 1) | 1.
    assert "1000000000" in body or "2000000001" in body, body


def test_exact_int_huge_literal_shift_is_not_folded_by_the_compiler(tmp_path):
    ir_text = _compile_to_ll(
        tmp_path,
        textwrap.dedent(
            """
            from pcc.extern import c_abi_typed_export

            @c_abi_typed_export("pcc_test_huge_shift", "i64", ())
            def pcc_test_huge_shift() -> int:
                value: int = 1 << 1000000000
                return value
            """
        ),
        "typed_bignum_huge_shift",
    )
    body = _fn_body(ir_text, "pcc_test_huge_shift")
    assert body is not None, ir_text
    assert "@py_int_shl" in body, body
    # The exponent reaches the runtime call either boxed at run time or as
    # the tagged constant (1000000000 << 1) | 1.
    assert "1000000000" in body or "2000000001" in body, body


def test_exact_int_chained_assignment_plans_each_name_as_one_object_slot(tmp_path):
    ir_text = _compile_to_ll(
        tmp_path,
        textwrap.dedent(
            """
            from pcc.extern import c_abi_typed_export

            @c_abi_typed_export("pcc_test_exact_chain", "i64", ("i64",))
            def pcc_test_exact_chain(flag: int) -> int:
                left: int = 7
                right: int = 9
                if flag < 0:
                    left = right = 1 << 70
                return left + right
            """
        ),
        "typed_bignum_assignment_chain",
    )
    body = _fn_body(ir_text, "pcc_test_exact_chain")
    assert body is not None, ir_text
    assert len(re.findall(r"%left\.addr(?:\.\d+)? = alloca ptr", body)) == 1, body
    assert len(re.findall(r"%right\.addr(?:\.\d+)? = alloca ptr", body)) == 1, body
    assert "@py_int_add" in body, body


def test_exact_int_rebind_pins_replacement_before_releasing_previous_value(
    tmp_path,
):
    ir_text = _compile_to_ll(
        tmp_path,
        textwrap.dedent(
            """
            from pcc.extern import c_abi_typed_export

            @c_abi_typed_export("pcc_test_exact_rebind_pin", "i64", ("i64",))
            def pcc_test_exact_rebind_pin(flag: int) -> int:
                value: int = 1 << 70
                if flag < 0:
                    value = 1 << 71
                return value
            """
        ),
        "typed_bignum_rebind_pin",
    )
    body = _fn_body(ir_text, "pcc_test_exact_rebind_pin")
    assert body is not None, ir_text
    # A fresh exact value is not yet in the local's frame root.  Pin it before
    # the relocation-aware root-store atomically retains the replacement and
    # releases the previous slot owner; then unpin and consume the temporary.
    assert re.search(
        r"(?s)call ptr (?:\([^)]*\) )?@py_int_shl.*?"
        r"call void (?:\([^)]*\) )?@pcc_gc_pin.*?"
        r"bitcast ptr %value\.addr.*?"
        r"call void (?:\([^)]*\) )?@pcc_gc_store_root.*?"
        r"call void (?:\([^)]*\) )?@pcc_gc_unpin.*?"
        r"call void (?:\([^)]*\) )?@pcc_gc_release",
        body,
    ), body


def test_exact_int_branch_and_zero_iteration_runtime_behavior(
    tmp_path,
    monkeypatch,
    pcc_runtime_archive,
):
    from pcc.frontends.python.pipeline import compile_python

    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(pcc_runtime_archive))
    src = tmp_path / "typed_exact_control_flow_run.py"
    exe = tmp_path / "typed_exact_control_flow_run.out"
    src.write_text(
        textwrap.dedent(
            """
            from pcc.extern import c_abi_typed_export

            @c_abi_typed_export("pcc_exact_fallthrough", "i64", ("i64",))
            def pcc_exact_fallthrough(flag: int) -> int:
                value: int = 7
                if flag < 0:
                    value = 1 << 70
                return value

            @c_abi_typed_export("pcc_exact_zero_loop", "i64", ("i64",))
            def pcc_exact_zero_loop(n: int) -> int:
                value: int = 7
                while n < 0:
                    value = 1 << 70
                    n = 0
                return value

            def exact_compare(flag: int) -> bool:
                value: int = 7
                if flag < 0:
                    value = 1 << 70
                return value > (1 << 69)

            print(pcc_exact_fallthrough(0))
            print(pcc_exact_zero_loop(0))
            print(exact_compare(-1))
            print(exact_compare(0))
            """
        ),
        encoding="utf-8",
    )
    compile_python(
        str(src),
        str(exe),
        libpython_mode="off",
        backend="self",
    )
    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )
    assert run.stdout == "7\n7\nTrue\nFalse\n"


def test_proven_bounded_typed_int_loop_defaults_to_unboxed_shape(tmp_path):
    ir_text = _compile_to_ll(tmp_path, _TYPED_LOOP, "typed_loop_tagged")
    assert re.search(
        r"define\s+(?:external\s+)?i64\s+@user_[A-Za-z0-9_]*_bench\s*\(i64\s+%n\)",
        ir_text,
    ), ir_text
    body = _fn_body(ir_text, "bench")
    assert body is not None, ir_text
    assert "@py_int_add" not in body, body
    assert "@py_int_mod" not in body, body
    assert "@py_int_floordiv" not in body, body
    assert "srem" in body, body
    assert "sdiv" in body, body
    assert "@py_obj_call" not in body, body
    assert "@py_cpy_" not in body, body
    _assert_no_class_or_valuebox_allocation(body)


def test_unsafe_i64_typed_int_loop_uses_unboxed_function_abi(
    tmp_path,
    monkeypatch,
):
    _enable_unsafe_i64(monkeypatch)
    ir_text = _compile_to_ll(tmp_path, _TYPED_LOOP, "typed_loop_unboxed")
    assert re.search(
        r"define\s+(?:external\s+)?i64\s+@user_[A-Za-z0-9_]*_bench\s*\(i64\s+%n\)",
        ir_text,
    ), ir_text
    body = _fn_body(ir_text, "bench")
    assert body is not None, ir_text
    assert "@py_int_cmp" not in body, body
    assert "@py_int_mod" not in body, body
    assert "@py_int_floordiv" not in body, body
    assert "@py_int_from_i64" not in body, body
    assert "@py_obj_call" not in body, body
    assert "@py_obj_truthy" not in body, body
    assert "srem" in body, body
    assert "sdiv" in body, body
    assert "low.while.cond" in body, body


def test_proven_literal_list_accumulator_defaults_to_unboxed_shape(tmp_path):
    ir_text = _compile_to_ll(tmp_path, _TYPED_LIST_LOOP, "typed_list_int_tagged")
    assert re.search(
        r"define\s+(?:external\s+)?i64\s+@user_[A-Za-z0-9_]*_sum_ints\s*\(ptr\s+%xs\)",
        ir_text,
    ), ir_text
    body = _fn_body(ir_text, "sum_ints")
    assert body is not None, ir_text
    assert "@py_int_add" not in body, body
    assert "@py_list_get_i64_nonnegative" in body, body
    assert "@py_list_get(" not in body, body
    assert "@py_list_get_i64(" not in body, body
    assert "@py_obj_call" not in body, body
    assert "@py_cpy_" not in body, body
    _assert_no_class_or_valuebox_allocation(body)


def test_unsafe_i64_typed_list_int_loop_keeps_accumulator_unboxed(
    tmp_path,
    monkeypatch,
):
    _enable_unsafe_i64(monkeypatch)
    ir_text = _compile_to_ll(tmp_path, _TYPED_LIST_LOOP, "typed_list_int_loop")
    assert re.search(
        r"define\s+(?:external\s+)?i64\s+@user_[A-Za-z0-9_]*_sum_ints\s*\(ptr\s+%xs\)",
        ir_text,
    ), ir_text
    body = _fn_body(ir_text, "sum_ints")
    assert body is not None, ir_text
    assert "@py_list_len" in body, body
    assert "@py_list_get_i64_nonnegative" in body, body
    assert "@py_list_get(" not in body, body
    assert "@py_list_get_i64(" not in body, body
    assert "@py_int_to_i64" not in body, body
    assert "@py_int_add" not in body, body
    assert "@py_obj_getitem" not in body, body
    assert "@py_obj_call" not in body, body
    assert "@py_cpy_" not in body, body


def test_proven_direct_call_accumulator_defaults_to_unboxed_calls(tmp_path):
    ir_text = _compile_to_ll(
        tmp_path,
        _TYPED_FUNCTION_CALL_LOOP,
        "typed_function_call_loop_tagged",
    )
    for fn_name, arg_name in (
        ("bump", "x"),
        ("step", "i"),
        ("bench", "n"),
    ):
        assert re.search(
            rf"define\s+(?:external\s+)?i64\s+@user_[A-Za-z0-9_]*_{fn_name}\s*\(i64\s+%{arg_name}\)",
            ir_text,
        ), ir_text
    bump_body = _fn_body(ir_text, "bump")
    step_body = _fn_body(ir_text, "step")
    bench_body = _fn_body(ir_text, "bench")
    assert bump_body is not None, ir_text
    assert step_body is not None, ir_text
    assert bench_body is not None, ir_text
    assert "@py_int_add" not in bump_body, bump_body
    assert "@py_int_mod" not in step_body, step_body
    assert re.search(
        r"call\s+i64\s+(?:\([^)]*\)\s*)?@user_[A-Za-z0-9_]*_bump\s*\(i64\s+%",
        step_body,
    ), step_body
    assert re.search(
        r"call\s+i64\s+(?:\([^)]*\)\s*)?@user_[A-Za-z0-9_]*_step\s*\(i64\s+%",
        bench_body,
    ), bench_body
    for body in (bump_body, step_body, bench_body):
        assert "@py_obj_call" not in body, body
        assert "@py_cpy_" not in body, body
        _assert_no_class_or_valuebox_allocation(body)


@pytest.mark.parametrize(
    ("source", "case_name"),
    (
        (
            _TYPED_LOOP.replace(
                "print(bench(10000))",
                "saved = bench\nprint(saved(10000))",
            ),
            "escaped",
        ),
        (
            _TYPED_LOOP.replace(
                "print(bench(10000))",
                "print(bench(9223372036854775807))",
            ),
            "unbounded",
        ),
    ),
)
def test_unproven_scalar_loop_keeps_arbitrary_precision_boxed_abi(
    tmp_path,
    source,
    case_name,
):
    ir_text = _compile_to_ll(tmp_path, source, "typed_loop_" + case_name)
    assert re.search(
        r"define\s+(?:external\s+)?ptr\s+@user_[A-Za-z0-9_]*_bench\s*\(ptr\s+%n\)",
        ir_text,
    ), ir_text
    body = _fn_body(ir_text, "bench")
    assert body is not None, ir_text
    assert "@py_int_add" in body, body


def test_out_of_i64_literal_list_keeps_arbitrary_precision_boxed_loop(tmp_path):
    source = _TYPED_LIST_LOOP.replace(
        "[1, 2, 3, 4]",
        "[1, 9223372036854775808]",
    )
    ir_text = _compile_to_ll(tmp_path, source, "typed_list_int_out_of_range")
    body = _fn_body(ir_text, "sum_ints")
    assert body is not None, ir_text
    assert "@py_int_add" in body, body
    assert "@py_list_get_i64_nonnegative" not in body, body


def test_unsafe_i64_typed_function_call_loop_uses_unboxed_direct_calls(
    tmp_path,
    monkeypatch,
):
    _enable_unsafe_i64(monkeypatch)
    ir_text = _compile_to_ll(
        tmp_path,
        _TYPED_FUNCTION_CALL_LOOP,
        "typed_function_call_loop_unboxed",
    )
    for fn_name, arg_name in (
        ("bump", "x"),
        ("step", "i"),
        ("bench", "n"),
    ):
        assert re.search(
            rf"define\s+(?:external\s+)?i64\s+@user_[A-Za-z0-9_]*_{fn_name}\s*\(i64\s+%{arg_name}\)",
            ir_text,
        ), ir_text
    bump_body = _fn_body(ir_text, "bump")
    step_body = _fn_body(ir_text, "step")
    bench_body = _fn_body(ir_text, "bench")
    assert bump_body is not None, ir_text
    assert step_body is not None, ir_text
    assert bench_body is not None, ir_text
    assert re.search(
        r"call\s+i64\s+(?:\([^)]*\)\s*)?@user_[A-Za-z0-9_]*_bump\s*\(i64\s+%",
        step_body,
    ), step_body
    assert re.search(
        r"call\s+i64\s+(?:\([^)]*\)\s*)?@user_[A-Za-z0-9_]*_step\s*\(i64\s+%",
        bench_body,
    ), bench_body
    for body in (bump_body, step_body, bench_body):
        assert "@py_obj_call" not in body, body
        assert "@py_int_" not in body, body
        assert "@py_cpy_" not in body, body


def test_typed_list_i64_runtime_helpers_match_c_fast_path():
    import pcc

    repo_root = Path(pcc.__file__).resolve().parents[1]
    py_list_src = repo_root / "pcc" / "runtime" / "py" / "py_list.py"
    text = py_list_src.read_text(encoding="utf-8")
    for helper in (
        "py_list_get_i64",
        "py_list_get_i64_nonnegative",
        "py_list_len",
    ):
        match = re.search(
            # Stop at the next top-level definition (decorated or not): the
            # helper after py_list_get_i64_nonnegative is an undecorated
            # private def, and the old lookahead ran past it.
            rf"def {helper}\([^)]*\).*?(?=\n\n(?:@|def )|\Z)",
            text,
            re.DOTALL,
        )
        assert match is not None, helper
        body = match.group(0)
        assert "_list_is_sane" not in body, body


def test_unsafe_i64_typed_list_int_loop_runs_without_libpython(tmp_path, monkeypatch):
    _enable_unsafe_i64(monkeypatch)
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "typed_list_int_loop_run.py"
    exe = tmp_path / "typed_list_int_loop_run.out"
    src.write_text(_TYPED_LIST_LOOP, encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        libpython_mode="off",
        backend="self",
    )
    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )
    assert run.stdout == "10\n"


def test_unsafe_i64_typed_list_int_loop_falls_back_for_heap_int_elements(
    tmp_path,
    monkeypatch,
):
    _enable_unsafe_i64(monkeypatch)
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "typed_list_heap_int_loop_run.py"
    exe = tmp_path / "typed_list_heap_int_loop_run.out"
    src.write_text(
        textwrap.dedent("""
        def sum_ints(xs: list[int]) -> int:
            s: int = 0
            for x in xs:
                s = s + x
            return s

        print(sum_ints([4611686018427387904, 1]))
        """),
        encoding="utf-8",
    )
    compile_python(
        str(src),
        str(exe),
        libpython_mode="off",
        backend="self",
    )
    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )
    assert run.stdout == "4611686018427387905\n"


def test_typed_float_only_signature_uses_unboxed_low_ir_function_abi(tmp_path):
    ir_text = _compile_to_ll(
        tmp_path,
        _TYPED_PURE_FLOAT,
        "typed_pure_float_unboxed",
    )
    assert re.search(
        r"define\s+(?:external\s+)?double\s+@user_[A-Za-z0-9_]*_scale\s*"
        r"\(double\s+%x,\s*double\s+%y\)",
        ir_text,
    ), ir_text
    body = _fn_body(ir_text, "scale")
    assert body is not None, ir_text
    assert "fadd" in body, body
    assert "fmul" in body, body
    assert "@py_float_" not in body, body
    assert "@py_int_" not in body, body
    assert "@py_obj_call" not in body, body
    assert "@py_cpy_" not in body, body


def test_unsafe_i64_typed_float_loop_uses_int_counter_param(tmp_path, monkeypatch):
    _enable_unsafe_i64(monkeypatch)
    ir_text = _compile_to_ll(tmp_path, _TYPED_FLOAT_LOOP, "typed_float_loop_unboxed")
    assert re.search(
        r"define\s+(?:external\s+)?double\s+@user_[A-Za-z0-9_]*_bench\s*\(i64\s+%n\)",
        ir_text,
    ), ir_text
    body = _fn_body(ir_text, "bench")
    assert body is not None, ir_text
    assert "low.while.cond" in body, body
    assert "fadd" in body, body
    assert "fmul" in body, body
    assert "fdiv" in body, body
    assert "@py_float_" not in body, body
    assert "@py_int_" not in body, body
    assert "@py_obj_call" not in body, body
    assert "@py_obj_truthy" not in body, body
    assert "@py_cpy_" not in body, body


def test_unsafe_i64_typed_float_loop_low_ir_can_be_disabled_as_layer1_oracle(
    tmp_path,
    monkeypatch,
):
    _enable_unsafe_i64(monkeypatch)
    monkeypatch.setenv("PCC_PYTHON_LOW_IR", "off")
    ir_text = _compile_to_ll(tmp_path, _TYPED_FLOAT_LOOP, "typed_float_loop_legacy")
    body = _fn_body(ir_text, "bench")
    assert body is not None, ir_text
    assert "low.while.cond" not in body, body
    assert "fadd" in body, body
    assert "@py_float_" not in body, body


def test_unsafe_i64_typed_float_loop_runs_without_libpython(tmp_path, monkeypatch):
    _enable_unsafe_i64(monkeypatch)
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "typed_float_loop_run.py"
    exe = tmp_path / "typed_float_loop_run.out"
    src.write_text(_TYPED_FLOAT_LOOP, encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        libpython_mode="off",
        backend="self",
    )
    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )
    expected = 0.0
    for _i in range(6):
        expected = (expected + 1.0) * 4.0 / 2.0
    assert run.stdout == f"{expected}\n"


def test_typed_int_abi_off_matches_boxed_tagged_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("PCC_PYTHON_TYPED_INT_ABI", "off")
    ir_text = _compile_to_ll(tmp_path, _TYPED_LOOP, "typed_loop_boxed")
    assert re.search(
        r"define\s+(?:external\s+)?ptr\s+@user_[A-Za-z0-9_]*_bench\s*\(ptr\s+%n\)",
        ir_text,
    ), ir_text
    body = _fn_body(ir_text, "bench")
    assert body is not None, ir_text
    assert "int.tag.fast" in body, body
    assert "tag.add" in body, body
    assert "@py_int_cmp" in body, body
    assert "@py_int_mod" in body, body
    assert "@py_int_floordiv" in body, body
    _assert_no_class_or_valuebox_allocation(body)


def test_unsafe_i64_typed_int_low_ir_can_be_disabled_as_layer1_oracle(
    tmp_path,
    monkeypatch,
):
    _enable_unsafe_i64(monkeypatch)
    low_ir_text = _compile_to_ll(tmp_path, _TYPED_LOOP, "typed_loop_low_ir")
    low_body = _fn_body(low_ir_text, "bench")
    assert low_body is not None, low_ir_text
    assert "low.while.cond" in low_body, low_body

    monkeypatch.setenv("PCC_PYTHON_LOW_IR", "off")
    legacy_text = _compile_to_ll(tmp_path, _TYPED_LOOP, "typed_loop_legacy")
    legacy_body = _fn_body(legacy_text, "bench")
    assert legacy_body is not None, legacy_text
    assert "low.while.cond" not in legacy_body, legacy_body
    assert "@py_int_mod" not in legacy_body, legacy_body
    assert "@py_int_floordiv" not in legacy_body, legacy_body
    assert "srem" in legacy_body, legacy_body
    assert "sdiv" in legacy_body, legacy_body


def test_unsafe_i64_typed_int_unboxed_loop_runs_without_libpython(
    tmp_path,
    monkeypatch,
):
    _enable_unsafe_i64(monkeypatch)
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "typed_loop_run.py"
    exe = tmp_path / "typed_loop_run.out"
    src.write_text(_TYPED_LOOP, encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        libpython_mode="off",
        backend="self",
    )
    expected = str(sum((i % 7) + (i // 13) for i in range(10000))) + "\n"
    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )
    assert run.stdout == expected


_TYPED_DIRECT_CALL = textwrap.dedent("""
    def add(a: int, b: int) -> int:
        return a + b

    def bench(n: int) -> int:
        return add(n, 1)

    print(bench(41))
    """)


_TYPED_TAGGED_MUL = textwrap.dedent("""
    def mul(a: int, b: int) -> int:
        return a * b

    def main():
        print(mul(6, 7))
        print(mul(4611686018427387903, 2))
        print(mul(1099511627776, 1099511627776))

    main()
    """)


def test_typed_int_annotations_default_to_boxed_tagged_abi(tmp_path):
    ir_text = _compile_to_ll(tmp_path, _TYPED_DIRECT_CALL, "typed_direct_call_boxed")
    assert re.search(
        r"define\s+(?:external\s+)?ptr\s+@user_[A-Za-z0-9_]*_add\s*" r"\(ptr\s+%a,\s*ptr\s+%b\)",
        ir_text,
    ), ir_text
    assert not re.search(
        r"define\s+(?:external\s+)?i64\s+@user_[A-Za-z0-9_]*_add\s*\(",
        ir_text,
    ), ir_text
    body = _fn_body(ir_text, "add")
    assert body is not None, ir_text
    assert "@py_int_add" in body, body


def test_typed_int_direct_call_defaults_to_boxed_tagged_shape_and_runs(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    ir_text = _compile_to_ll(tmp_path, _TYPED_DIRECT_CALL, "typed_direct_call_tagged")
    add_body = _fn_body(ir_text, "add")
    bench_body = _fn_body(ir_text, "bench")
    assert add_body is not None, ir_text
    assert bench_body is not None, ir_text
    assert "int.tag.fast" in add_body, add_body
    assert "tag.add" in add_body, add_body
    assert "@py_int_add" in add_body, add_body
    assert re.search(
        r"call\s+ptr\s+(?:\([^)]*\)\s*)?@user_[A-Za-z0-9_]*_add\s*\(ptr\s+%n,",
        bench_body,
    ), bench_body
    assert "low.call" not in bench_body, bench_body
    assert "@py_obj_call" not in bench_body, bench_body
    _assert_no_class_or_valuebox_allocation(add_body)
    _assert_no_class_or_valuebox_allocation(bench_body)

    src = tmp_path / "typed_direct_call_tagged.py"
    exe = tmp_path / "typed_direct_call_tagged.out"
    src.write_text(_TYPED_DIRECT_CALL, encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        libpython_mode="off",
        backend="self",
    )
    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )
    assert run.stdout == "42\n"


def test_for_range_induction_keeps_raw_i64_lane_under_boxed_int_mode(tmp_path):
    ir_text = _compile_to_ll(tmp_path, _TYPED_RANGE_LOOP, "typed_range_induction")
    assert re.search(
        r"define\s+(?:external\s+)?ptr\s+@user_[A-Za-z0-9_]*_sum_range\s*\(ptr\s+%n\)",
        ir_text,
    ), ir_text
    body = _fn_body(ir_text, "sum_range")
    assert body is not None, ir_text
    assert re.search(r"%i\.range\.addr[^=]*=\s+phi\s+i64", body), body
    assert re.search(r"icmp\s+slt\s+i64\s+%i\.range\.addr", body), body
    assert re.search(r"add\s+i64\s+%i\.range\.addr", body), body
    assert "range.int.obj" in body, body
    assert "@py_int_from_i64" in body, body
    assert "@py_obj_call" not in body, body
    assert "@py_cpy_" not in body, body


def test_for_range_raw_lane_reboxes_before_dyn_and_typed_calls(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    ir_text = _compile_to_ll(tmp_path, _TYPED_RANGE_ESCAPE, "typed_range_escape")
    body = _fn_body(ir_text, "last")
    assert body is not None, ir_text
    assert re.search(r"%i\.range\.addr[^=]*=\s+phi\s+i64", body), body
    assert "range.int.obj" in body, body
    assert "@py_int_from_i64" in body, body
    assert re.search(
        r"call\s+ptr\s+(?:\([^)]*\)\s*)?@user_[A-Za-z0-9_]*_keep\s*\(ptr\s+%",
        body,
    ), body
    assert re.search(
        r"call\s+ptr\s+(?:\([^)]*\)\s*)?@user_[A-Za-z0-9_]*_bump\s*\(ptr\s+%",
        body,
    ), body
    assert not re.search(
        r"call\s+i64\s+(?:\([^)]*\)\s*)?@user_[A-Za-z0-9_]*_(?:keep|bump)\s*\(",
        body,
    ), body
    assert "@py_cpy_" not in body, body

    src = tmp_path / "typed_range_escape.py"
    exe = tmp_path / "typed_range_escape.out"
    src.write_text(_TYPED_RANGE_ESCAPE, encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        libpython_mode="off",
        backend="self",
    )
    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )
    assert run.stdout == "5\n"


def test_tagged_int_mul_uses_inline_overflow_fast_path(tmp_path):
    ir_text = _compile_to_ll(tmp_path, _TYPED_TAGGED_MUL, "typed_tagged_mul")
    body = _fn_body(ir_text, "mul")
    assert body is not None, ir_text
    assert "int.tag.fast" in body, body
    assert "llvm.smul.with.overflow.i64" in ir_text, ir_text
    assert "tag.mul" in body, body
    assert "@py_int_mul" in body, body
    assert "tag.fits" in body, body


def test_tagged_int_mul_fast_path_runs_without_libpython(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "typed_tagged_mul_run.py"
    exe = tmp_path / "typed_tagged_mul_run.out"
    src.write_text(_TYPED_TAGGED_MUL, encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        libpython_mode="off",
        backend="self",
    )
    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )
    assert run.stdout.splitlines() == [
        "42",
        "9223372036854775806",
        "1208925819614629174706176",
    ]


def test_unsafe_i64_typed_int_direct_call_uses_low_ir_and_runs_without_libpython(
    tmp_path,
    monkeypatch,
):
    _enable_unsafe_i64(monkeypatch)
    from pcc.frontends.python.pipeline import compile_python

    ir_text = _compile_to_ll(tmp_path, _TYPED_DIRECT_CALL, "typed_direct_call")
    body = _fn_body(ir_text, "bench")
    assert body is not None, ir_text
    assert "low.call" in body, body
    assert re.search(r"call\s+i64\s+(?:\([^)]*\)\s*)?@user_[A-Za-z0-9_]*_add", body), body
    assert "@py_int_add" not in body, body

    src = tmp_path / "typed_direct_call.py"
    exe = tmp_path / "typed_direct_call.out"
    src.write_text(_TYPED_DIRECT_CALL, encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        libpython_mode="off",
        backend="self",
    )
    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )
    assert run.stdout == "42\n"


def test_typed_int_abi_decision_stays_off_runtime_cache():
    root = Path(__file__).absolute().parents[2]
    source = (root / "pcc/frontends/python/codegen/typed_int_abi.py").read_text(
        encoding="utf-8"
    )
    match = re.search(
        r"\n    def _funcdef_uses_unboxed_typed_int_abi"
        r"\(self, fd: FuncDef\).*?"
        r"\n    def _typed_int_func_for_name",
        source,
        re.DOTALL,
    )
    assert match is not None
    method_source = match.group(0)
    assert "_unboxed_typed_int_abi_cache" not in method_source
    assert "dict.get" not in method_source
