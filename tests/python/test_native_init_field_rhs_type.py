from __future__ import annotations

import re
import subprocess
import textwrap
from pathlib import Path

import pytest

from pcc.frontends.python.pipeline import compile_python


REPO = Path(__file__).resolve().parents[2]
CONTEXTUAL_METHOD_FIXTURE = (
    REPO / "tests" / "fixtures" / "contextual_class_method_extern_args.py"
)


def _function_body(ir_text: str, fn_name_suffix: str) -> str:
    pattern = re.compile(
        r"define\s+[^\n]*?@[A-Za-z0-9_]*"
        + re.escape(fn_name_suffix)
        + r"\s*\([^)]*\)[^{]*\{(.+?)\n\}",
        re.DOTALL,
    )
    match = pattern.search(ir_text)
    assert match is not None, ir_text
    return match.group(1)


def _source() -> str:
    return textwrap.dedent(
        """
        from copy import copy

        class Box:
            def __init__(self, xs: list[int]):
                self.xs = copy(xs)

            def first(self) -> int:
                return self.xs[0]

        def main() -> None:
            b = Box([41])
            print(b.first() + 1)

        main()
        """
    ).lstrip()


def test_init_copy_rhs_preserves_field_type_in_ir(tmp_path):
    src = tmp_path / "init_copy_field.py"
    ll = tmp_path / "init_copy_field.ll"
    src.write_text(_source(), encoding="utf-8")

    compile_python(
        str(src),
        str(ll),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
        emit_llvm_only=True,
    )
    body = _function_body(ll.read_text(encoding="utf-8"), "Box_first")

    assert "@py_list_get" in body
    assert "@py_obj_getitem" not in body


def test_init_copy_rhs_field_type_runs_no_libpython(tmp_path):
    src = tmp_path / "init_copy_field.py"
    exe = tmp_path / "init_copy_field.out"
    src.write_text(_source(), encoding="utf-8")

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)

    assert run.returncode == 0, run.stderr
    assert run.stdout == "42\n"


def _method_arg_source() -> str:
    return textwrap.dedent(
        """
        from copy import copy

        class Core:
            scratch: list[int]

            def __init__(self, scratch: list[int]):
                self.scratch = scratch

        class Machine:
            def __init__(self, cores: list[Core]):
                self.cores = copy(cores)

            def run(self) -> None:
                for core in self.cores:
                    self.step(core)

            def step(self, core) -> None:
                dispatch = {"alu": self.alu}
                dispatch["alu"](core, 1)

            def alu(self, core, index) -> None:
                print(core.scratch[index])

        def main() -> None:
            m = Machine([Core([10, 42])])
            m.run()

        main()
        """
    ).lstrip()


def test_class_self_call_argument_types_reach_literal_dispatch_target(tmp_path):
    src = tmp_path / "class_method_arg_flow.py"
    ll = tmp_path / "class_method_arg_flow.ll"
    src.write_text(_method_arg_source(), encoding="utf-8")

    compile_python(
        str(src),
        str(ll),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
        emit_llvm_only=True,
    )
    body = _function_body(ll.read_text(encoding="utf-8"), "Machine_alu")

    assert "@py_list_get" in body
    assert "@py_obj_getattr" not in body


def test_class_self_call_argument_types_run_no_libpython(tmp_path):
    src = tmp_path / "class_method_arg_flow.py"
    exe = tmp_path / "class_method_arg_flow.out"
    src.write_text(_method_arg_source(), encoding="utf-8")

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)

    assert run.returncode == 0, run.stderr
    assert run.stdout == "42\n"


def _contextual_method_source() -> str:
    """Load the executable full-module contextual-argument source."""
    return CONTEXTUAL_METHOD_FIXTURE.read_text(encoding="utf-8")


def _method_argument_provenance_source() -> str:
    return textwrap.dedent(
        """
        from pcc.unsafe import load_i64, stack_alloc, store_i64

        class ProvenanceProbe:
            def record(self, raw, boxed: object, allocating: list[int]) -> None:
                print(boxed)
                store_i64(raw, 8, len(allocating))

            def run(self) -> None:
                raw = stack_alloc(16)
                self.record(raw, 100, [10, 20, 30])
                print(load_i64(raw, 8))

        ProvenanceProbe().run()
        """
    ).lstrip()


def test_full_context_method_ints_follow_emitted_abi(
    tmp_path,
):
    src = tmp_path / "contextual_method.py"
    ll = tmp_path / "contextual_method.ll"
    src.write_text(_contextual_method_source(), encoding="utf-8")
    compile_python(
        str(src),
        str(ll),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
        emit_llvm_only=True,
    )
    ir_text = ll.read_text(encoding="utf-8")
    body = _function_body(ir_text, "ContextualFillApp_fill")
    extern_call = next(line for line in body.splitlines() if "@memset" in line)

    # Unannotated Python arguments use the object ABI. The extern edge must
    # decode these Python ints to its declared machine widths.
    assert "@py_int_to_i64_lane(" in body
    header = next(
        line for line in ir_text.splitlines()
        if line.startswith("define ") and "ContextualFillApp_fill(" in line
    )
    assert re.search(r"\(ptr %self, ptr %byte_value, ptr %count, ptr %offset\)", header)
    assert re.search(
        r"@memset\(ptr\s+[^,]+,\s+i32\s+[^,]+,\s+i64\s+[^)]+\)",
        extern_call,
    ), extern_call

    caller = _function_body(ir_text, "ContextualFillApp_exercise")
    method_call = next(
        line
        for line in caller.splitlines()
        if "ContextualFillApp_fill" in line and "call" in line
    )
    assert re.search(
        r"ContextualFillApp_fill\(ptr\s+[^,]+,\s*"
        r"ptr\s+[^,]+,\s*ptr\s+[^,]+,\s*ptr\s+[^)]+\)",
        method_call,
    ), method_call


@pytest.mark.parametrize("backend", [pytest.param(None, id="default-self"), pytest.param("self", id="explicit-self")])
def test_full_context_class_method_never_exposes_boxed_int_tag(
    backend,
    tmp_path,
    monkeypatch,
    pcc_runtime_archive,
):
    src = tmp_path / f"contextual_method_{backend}.py"
    exe = tmp_path / f"contextual_method_{backend}.out"
    src.write_text(_contextual_method_source(), encoding="utf-8")
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(pcc_runtime_archive))

    compile_python(
        str(src),
        str(exe),
        backend=backend,
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)

    assert run.returncode == 0, run.stderr
    # Twelve 0x07 bytes from offset 8: 0x0707070707070707, then 0x07070707.
    assert run.stdout == "12\n0\n506381209866536711\n117901063\n0\n0\n12\n"


def test_method_argument_provenance_pins_managed_but_not_raw_pointer(tmp_path):
    src = tmp_path / "method_argument_provenance.py"
    ll = tmp_path / "method_argument_provenance.ll"
    src.write_text(_method_argument_provenance_source(), encoding="utf-8")

    compile_python(
        str(src),
        str(ll),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
        emit_llvm_only=True,
    )
    body = _function_body(ll.read_text(encoding="utf-8"), "ProvenanceProbe_run")
    # An unannotated Python argument receives an int object representing the
    # address. That object is managed; the original machine address is not.
    call_match = re.search(
        r"call\s+(?:void|ptr)(?: \([^\n)]*\))?\s+@[^(\n]*ProvenanceProbe_record\("
        r"ptr\s+(?P<receiver>%[^, ]+),\s*"
        r"(?:ptr|i64)\s+(?P<raw>%[^, ]+),\s*"
        r"ptr\s+(?P<boxed>%[^, ]+),\s*"
        r"ptr\s+(?P<allocating>%[^) ]+)\)",
        body,
    )

    assert call_match is not None, body
    raw = call_match.group("raw")
    box_match = re.search(
        re.escape(raw)
        + r" = call ptr(?: \(i64\))? @py_int_from_i64\(i64 (?P<address>%[^) ]+)\)",
        body,
    )
    assert box_match is not None, body
    address = box_match.group("address")
    for operation in ("pin", "unpin", "release"):
        assert f"@pcc_gc_{operation}(ptr {address})" not in body
    # Receiver, address-int object, ordinary boxed int, and allocating list
    # retain their managed-value leases across argument evaluation and call.
    for group in ("receiver", "raw", "boxed", "allocating"):
        managed = call_match.group(group)
        assert re.search(
            r"call void(?: \(ptr\))? @pcc_gc_pin\(ptr " + re.escape(managed) + r"\)",
            body,
        ), body
        # One unpin is the success edge and another is the call-error edge.
        assert len(re.findall(
            r"call void(?: \(ptr\))? @pcc_gc_unpin\(ptr " + re.escape(managed) + r"\)",
            body,
        )) >= 2
    for group in ("raw", "boxed", "allocating"):
        owned = call_match.group(group)
        assert len(re.findall(
            r"call void(?: \(ptr\))? @pcc_gc_release\(ptr " + re.escape(owned) + r"\)",
            body,
        )) >= 2
    receiver = call_match.group("receiver")
    assert not re.search(
        r"call void(?: \(ptr\))? @pcc_gc_release\(ptr " + re.escape(receiver) + r"\)",
        body,
    )


@pytest.mark.parametrize("backend", [pytest.param(None, id="default-self"), pytest.param("self", id="explicit-self")])
def test_method_argument_provenance_runs_no_libpython(backend, tmp_path):
    src = tmp_path / f"method_argument_provenance_{backend}.py"
    exe = tmp_path / f"method_argument_provenance_{backend}.out"
    src.write_text(_method_argument_provenance_source(), encoding="utf-8")

    compile_python(
        str(src),
        str(exe),
        backend=backend,
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)

    assert run.returncode == 0, run.stderr
    assert run.stdout == "100\n3\n"
