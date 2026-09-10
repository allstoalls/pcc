"""Control-flow and ownership regressions for Python for-target rebinding."""

from __future__ import annotations

import re
import subprocess
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from pcc.py_frontend.codegen.for_loop_lowering import (
    _for_prepare_owned_object_target,
    ir,
)
from pcc.py_frontend.codegen.errors import L1CodegenError


def _compile_to_ll(tmp_path: Path, source: str, name: str) -> str:
    from pcc.py_frontend.pipeline import compile_python

    src = tmp_path / f"{name}.py"
    out = tmp_path / f"{name}.ll"
    src.write_text(textwrap.dedent(source), encoding="utf-8")
    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    return out.read_text(encoding="utf-8")


def _function_body(ir_text: str, suffix: str) -> str:
    match = re.search(
        r"define\s+[^\n]*@[A-Za-z0-9_]*"
        + re.escape(suffix)
        + r"\s*\([^)]*\)[^{]*\{(.+?)\n\}",
        ir_text,
        re.DOTALL,
    )
    assert match is not None, ir_text
    return match.group(1)


def test_live_cpython_native_target_join_remains_fail_closed():
    """Do not replace an arbitrary foreign object with a lossy pcc projection."""
    host = SimpleNamespace(
        env={"item": (object(), ir.IntType(8).as_pointer(), object())},
        _cpy_env_flags={"item": True},
        # A live pre-loop value is the whole reason the two representations
        # would have to join; the drop below is only reachable for a dead one.
        _for_target_pre_value_is_dead=lambda _ident: False,
    )

    with pytest.raises(
        L1CodegenError,
        match="cannot join a CPython-backed for-target with a native object binding",
    ):
        _for_prepare_owned_object_target(host, "item", object())


# ``fnmatch.filter`` is the CPython-domain iterable these cases need: it has
# no native provider, so its result is a raw libpython object, and its answer
# is short and deterministic.  A native ``list`` parameter supplies the other
# domain.
_JOIN_CASES = {
    "cpython_binding_then_native_loop": (
        """
        token = fnmatch.filter(values, "*.py")
        total = 0
        for token in values:
            total = total + 1
        return total
        """,
        True,
    ),
    "native_loop_then_cpython_loop": (
        """
        total = 0
        for token in values:
            total = total + 1
        for token in fnmatch.filter(values, "*.py"):
            total = total + 1
        return total
        """,
        True,
    ),
    "read_only_before_the_loop": (
        """
        token = fnmatch.filter(values, "*.py")
        total = len(token)
        for token in values:
            total = total + 1
        return total
        """,
        True,
    ),
    "read_after_the_loop_is_live": (
        """
        token = fnmatch.filter(values, "*.py")
        for token in values:
            pass
        return len(token)
        """,
        False,
    ),
    "binding_loop_nested_in_binding_loop": (
        """
        total = 0
        for token in values:
            for token in fnmatch.filter(values, "*.py"):
                total = total + 1
        return total
        """,
        False,
    ),
    "read_reachable_through_an_enclosing_back_edge": (
        """
        token = fnmatch.filter(values, "*.py")
        total = 0
        n = 0
        while n < 2:
            total = total + len(token)
            for token in values:
                total = total + 1
            n = n + 1
        return total
        """,
        False,
    ),
}


@pytest.mark.parametrize("case", sorted(_JOIN_CASES))
def test_mixed_domain_for_target_joins_only_when_the_pre_value_is_dead(
    tmp_path, case
):
    """A for-target may cross representation domains iff nothing observes it.

    ``for x in it:`` rebinds ``x``; the pre-loop value survives only along the
    zero-iteration edge.  A CPython pointer and a native pcc object are both
    ``i8*`` but carry different ownership, and the name holds one compile-time
    domain flag, so preserving one representation into the other's slot is
    what has to be refused -- not the rebinding itself.  When no read can
    reach the zero-iteration edge the old binding is simply dropped.

    The last three cases are the ones that keep the analysis honest: a read
    after the loop, a binding loop nested in another (whose body reads belong
    to either loop), and a read that an enclosing ``while`` back edge makes
    reachable again after the loop.
    """
    body, should_compile = _JOIN_CASES[case]
    source = "import fnmatch\n\n\ndef probe(values: list) -> int:" + textwrap.dedent(
        body
    ).replace("\n", "\n    ")
    if should_compile:
        _compile_to_ll(tmp_path, source, "join_" + case)
        return
    with pytest.raises(L1CodegenError, match="for-target"):
        _compile_to_ll(tmp_path, source, "join_" + case)


def test_mixed_domain_for_target_join_matches_cpython_at_runtime(tmp_path):
    """Both join directions, on the zero- and nonzero-iteration edge.

    Compiled against libpython so the CPython arm actually runs rather than
    being replaced by a fail-closed stub, and compared to CPython on the same
    file -- the drop is only correct if the observable answer is unchanged.
    """
    from pcc.py_frontend.pipeline import compile_python

    source = textwrap.dedent(
        '''
        import fnmatch


        def native_after_cpython(values: list) -> list:
            token = fnmatch.filter(["x.py", "y.txt"], "*.py")
            out = []
            for token in values:
                out.append(token)
            return out


        def cpython_after_native(values: list) -> list:
            out = []
            for token in values:
                out.append(token)
            for token in fnmatch.filter(["p.py", "q.txt"], "*.py"):
                out.append(token)
            return out


        def main() -> None:
            print(native_after_cpython(["a", "b"]))
            print(cpython_after_native(["a"]))
            print(native_after_cpython([]))
            print(cpython_after_native([]))


        main()
        '''
    )
    src = tmp_path / "for_target_join_domains.py"
    exe = tmp_path / "for_target_join_domains.out"
    src.write_text(source, encoding="utf-8")
    compile_python(str(src), str(exe), libpython_mode="on")
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=180, check=True
    )
    reference = subprocess.run(
        ["python3", str(src)], text=True, capture_output=True, timeout=180, check=True
    )
    assert run.stdout == reference.stdout
    assert run.stdout.splitlines() == [
        "['a', 'b']",
        "['a', 'p.py']",
        "[]",
        "['p.py']",
    ]


def test_zero_iteration_preserves_prior_target_and_owned_root(tmp_path):
    ir_text = _compile_to_ll(
        tmp_path,
        """
        from typing import Any

        def last(values: list[Any]) -> Any:
            item: int = 7
            for item in values:
                pass
            return item
        """,
        "for_target_scalar_object_join",
    )
    body = _function_body(ir_text, "last")
    assert re.search(r"%item\.addr(?:\.\d+)? = alloca ptr", body), body
    assert "item.for.obj.addr" not in body, body
    # ``item: int = 7`` is boxed on the object slot: either the historical
    # ``py_int_from_i64`` call or, since the exact-int tagged lane, a tagged
    # immediate handed to the ownership-transferring root store.
    assert re.search(r"@py_int_from_i64\(|%int\.lit\.tagged", body), body
    assert "@pcc_gc_frame_enter" in body, body
    assert re.search(r"@pcc_gc_store_root(?:_take)?\(", body), body
    # The slot is always owned in this shape, so the optimizer may fold the
    # dynamic ownership flag to a literal true.  Either representation must
    # retain the error-exit release; requiring the pre-folded SSA name made
    # this test reject stronger constant ownership evidence.
    # The for-target error cleanup is named ``<ident>.for.err.*`` (see
    # ``_for_target_error_cleanup``); the bare ``<ident>.err.*`` release in the
    # same body belongs to the ordinary local unwind and is a literal-false
    # select here, so anchoring on it tested nothing.  The slot is always
    # owned in this shape, so the optimizer may fold the dynamic ownership
    # flag to a literal true; either representation must retain the release.
    assert "item.for.err.owned" in body or re.search(
        r"%item\.for\.err\.release\.value[^=]*=\s*select\s+i1\s+true",
        body,
    ), body
    assert not re.search(
        r"store\s+ptr\s+(?!null\b)[^,]+,\s+ptr\s+%item\.addr",
        body,
    ), body


def test_for_target_root_store_is_the_only_old_owner_release(tmp_path):
    ir_text = _compile_to_ll(
        tmp_path,
        """
        from typing import Any

        def replace(values: list[Any]) -> Any:
            item: int = 7
            for item in values:
                pass
            return item
        """,
        "for_target_root_store_transfer",
    )
    body = _function_body(ir_text, "replace")
    # Rebinding is one root-slot replacement.  The generic branchy local
    # release before root-store would make pcc_gc_store_root decref the stale
    # old slot value a second time.
    transfer_match = re.search(
        r"(?ms)^call\.cont\.\d+:.*?^\s*br label %for\.lst\.step\.\d+",
        body,
    )
    assert transfer_match is not None, body
    loop_body = transfer_match.group(0)
    assert "item.owned.release" not in loop_body, loop_body
    # The incoming element's reference moves into the slot with one
    # ownership-transferring root store; the former pin / store_root / unpin
    # / release quartet is gone (per-op row ``for_over_list``).
    assert "@pcc_gc_store_root_take(" in loop_body, loop_body
    assert "@pcc_gc_store_root(" not in loop_body, loop_body
    assert "@pcc_gc_pin(" not in loop_body, loop_body
    assert "@pcc_gc_release(" not in loop_body, loop_body


def test_enumerate_then_dyn_list_and_reversed_reuse_one_valid_target_slot(tmp_path):
    ir_text = _compile_to_ll(
        tmp_path,
        """
        def probe(lines: list[str]) -> int:
            indexes = []
            index: int = 99
            for index, line in enumerate(lines):
                indexes.append(index)
            total: int = 0
            for index in indexes:
                total = total + index
            for index in reversed(indexes):
                total = total + index
            return total + index
        """,
        "for_target_enumerate_reuse",
    )
    body = _function_body(ir_text, "probe")
    assert re.search(r"%index\.addr(?:\.\d+)? = alloca ptr", body), body
    assert "index.for.obj.addr" not in body, body
    assert body.count("@pcc_gc_store_root") >= 2, body
    assert not re.search(r"store\s+ptr\s+%for\.elem[^,]*,\s+ptr\s+%index\.addr", body), body


def test_range_induction_is_separate_from_python_visible_target(tmp_path):
    ir_text = _compile_to_ll(
        tmp_path,
        """
        def probe() -> int:
            total: int = 0
            for index in range(3):
                total = total + index
                index = 100
            return total
        """,
        "range_target_rebind",
    )
    body = _function_body(ir_text, "probe")
    # mem2reg may promote the private counter alloca to a phi, while the
    # semantic ``int`` target may use its exact-object projection.  The two
    # names must nevertheless remain distinct and body rebinding must never
    # publish 100 into compiler induction state.
    assert re.search(
        r"%index\.range\.addr(?:\.\d+)?(?:\.0)? = (?:alloca|phi) i64",
        body,
    ), body
    assert "index.for.obj.addr" in body or re.search(
        r"%index\.addr(?:\.\d+)? = alloca i64",
        body,
    ), body
    assert not re.search(
        r"store i64 100, ptr %index\.range\.addr",
        body,
    ), body
    assert re.search(
        r"%next[^=]*= add i64 %index\.range\.addr[^,]*, 1",
        body,
    ), body


def test_nested_same_name_range_has_independent_induction_counters(tmp_path):
    ir_text = _compile_to_ll(
        tmp_path,
        """
        def probe() -> int:
            total: int = 0
            for index in range(3):
                for index in range(2):
                    total = total + 1
            return total
        """,
        "range_nested_same_target",
    )
    body = _function_body(ir_text, "probe")
    assert len(re.findall(
        r"%index\.range\.addr(?:\.\d+)?(?:\.0)? = (?:alloca|phi) i64",
        body,
    )) == 2, body
    assert "index.for.obj.addr" in body or re.search(
        r"%index\.addr(?:\.\d+)? = alloca i64",
        body,
    ), body


def test_unbound_enumerate_target_is_planned_before_later_dyn_rebind(tmp_path):
    ir_text = _compile_to_ll(
        tmp_path,
        """
        def probe(lines: list[str]) -> int:
            indexes = []
            for index, line in enumerate(lines):
                indexes.append(index)
            for index in indexes:
                pass
            for index in reversed(indexes):
                pass
            return len(indexes)
        """,
        "for_target_unbound_enumerate_join",
    )
    body = _function_body(ir_text, "probe")
    # The enumerate assignment and both dynamic loops share an entry-planned
    # object slot.  In particular, the second loop must not box an i64 alloca
    # that the zero-iteration enumerate path never initialized.
    assert re.search(r"%index\.addr(?:\.\d+)? = alloca ptr", body), body
    assert "index.for.obj.addr" not in body, body
    assert not re.search(r"%index\.addr(?:\.\d+)? = alloca i64", body), body
    assert "@py_int_from_i64" in body, body


def test_dyn_tuple_unpack_reuses_planned_exact_int_slot_with_valid_ir(tmp_path):
    """A later Dyn tuple target must honor the function-level int plan.

    Type inference intentionally leaves elements from ``list[Any]`` dynamic.
    The earlier ``enumerate`` binding still commits ``index`` to one exact-int
    object slot for the whole function, so tuple unpack must transfer the
    owned object into that slot instead of unboxing it to i64 first.
    """
    from llvmlite import binding as llvm

    ir_text = _compile_to_ll(
        tmp_path,
        """
        from typing import Any

        def probe(lines: list[str]) -> int:
            for index, line in enumerate(lines):
                pass
            candidate: dict[str, object] = {"events": []}
            events = list(candidate["events"])
            for index, kind, payload in events:
                pass
            return index
        """,
        "for_target_dyn_tuple_unpack_join",
    )
    body = _function_body(ir_text, "probe")
    assert re.search(r"%index\.addr(?:\.\d+)? = alloca ptr", body), body
    assert not re.search(
        r"store\s+ptr\s+%(?:m\.int_unbox|unpack\.0)[^,]*,\s+ptr\s+%index\.addr",
        body,
    ), body
    unpack_pos = body.index("%unpack.0")
    next_unpack_pos = body.index("%unpack.1", unpack_pos + 1)
    first_transfer = body[unpack_pos:next_unpack_pos]
    assert "@pcc_gc_store_root" in first_transfer, first_transfer
    llvm.parse_assembly(ir_text).verify()


def test_dyn_for_target_can_rebind_to_exact_int_in_the_planned_slot(tmp_path):
    ir_text = _compile_to_ll(
        tmp_path,
        """
        from typing import Any
        from pcc.extern import c_abi_typed_export

        @c_abi_typed_export("pcc_for_then_exact", "i64", ("ptr",))
        def probe(values: list[Any]) -> int:
            value: int = 7
            for value in values:
                pass
            value = 1 << 70
            return value
        """,
        "for_target_dyn_then_exact",
    )
    body = _function_body(ir_text, "pcc_for_then_exact")
    assert len(re.findall(r"%value\.addr(?:\.\d+)? = alloca ptr", body)) == 1, body
    assert "value.for.obj.addr" not in body, body
    assert "@py_int_shl" in body, body
    assert "@py_int_to_i64" in body, body
    assert not re.search(r"store\s+i64\s+[^,]+,\s+ptr\s+%value\.addr", body), body


def test_method_dyn_for_target_restores_its_exact_int_projection(tmp_path):
    ir_text = _compile_to_ll(
        tmp_path,
        """
        from typing import Any

        class Counter:
            def probe(self, values: list[Any]) -> int:
                value: int = 7
                for value in values:
                    pass
                value = 1 << 70
                return value
        """,
        "method_for_target_dyn_then_exact",
    )
    body = _function_body(ir_text, "Counter_probe")
    assert len(re.findall(r"%value\.addr(?:\.\d+)? = alloca ptr", body)) == 1, body
    assert "value.for.obj.addr" not in body, body
    assert "@py_int_shl" in body, body
    assert not re.search(r"store\s+i64\s+[^,]+,\s+ptr\s+%value\.addr", body), body


def test_for_target_join_covers_zero_nonzero_break_continue_and_error_edges(tmp_path):
    ir_text = _compile_to_ll(
        tmp_path,
        """
        from typing import Any

        def choose(values: list[Any], stop: bool) -> Any:
            value: int = 41
            try:
                for value in values:
                    if value == "skip":
                        continue
                    if stop:
                        break
            except ValueError:
                pass
            return value
        """,
        "for_target_control_edges",
    )
    body = _function_body(ir_text, "choose")
    assert re.search(r"%value\.addr(?:\.\d+)? = alloca ptr", body), body
    assert "value.for.obj.addr" not in body, body
    assert re.search(r"%value(?:\.for)?\.err\.release\.value", body), body
    assert "@pcc_gc_frame_leave" in body, body
    assert "@pcc_gc_release" in body, body


def test_enumerate_i64_target_rebound_from_dyn_list_verifies_and_runs(
    tmp_path,
    monkeypatch,
    pcc_py_runtime_archive,
):
    from pcc.py_frontend.pipeline import compile_python

    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(pcc_py_runtime_archive))
    src = tmp_path / "for_target_join_run.py"
    exe = tmp_path / "for_target_join_run.out"
    fixture = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "for_target_representation_join_runtime.py"
    )
    src.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
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
    assert run.stdout == "7\nb\n99\n6\n0\n3\n7\n1\n7\nb\n3\n6\n"


def test_for_target_owned_state_is_function_local(tmp_path):
    ir_text = _compile_to_ll(
        tmp_path,
        """
        from typing import Any

        def first(values: list[Any]) -> Any:
            item: int = 7
            for item in values:
                pass
            return item

        def second(item: Any) -> Any:
            return item
        """,
        "for_target_function_state_isolation",
    )
    first = _function_body(ir_text, "first")
    second = _function_body(ir_text, "second")
    assert re.search(r"%item\.addr(?:\.\d+)? = alloca ptr", first), first
    assert "item.for.obj.addr" not in first, first
    assert "item.for.obj.addr" not in second, second
    assert "item.for.err" not in second, second
    assert "ret.retain" in second, second


def test_pipeline_init_rewriter_distinct_target_static_canary():
    repo_root = Path(__file__).resolve().parents[2]
    source = (repo_root / "pcc/py_frontend/pipeline_libpython.py").read_text(
        encoding="utf-8"
    )
    assert "for init_call_index in init_call_lines:" in source
    assert "for index in init_call_lines:" not in source


def test_repeated_dict_iteration_reuses_one_native_object_target(tmp_path):
    """A copied dict and its typed source must agree on the target lane."""
    ir_text = _compile_to_ll(
        tmp_path,
        """
        def names_after_refresh(values: dict[str, str]) -> list[str]:
            previous = values.copy()
            names = []
            for key in previous:
                names.append(key)
            for key in values:
                if key not in previous:
                    names.append(key)
            return names
        """,
        "for_target_repeated_dict_iteration",
    )
    body = _function_body(ir_text, "names_after_refresh")
    assert len(re.findall(r"%key\.for\.obj\.addr(?:\.\d+)? = alloca ptr", body)) == 1, body
    assert "%key.addr" not in body, body
    assert body.count("@py_dict_keys") == 2, body
