from __future__ import annotations

import os
from pathlib import Path
import subprocess
import textwrap

import pytest


REPO_ROOT = Path(__file__).absolute().parents[2]


def _generate_ir(source: str, module_name: str = "fresh_append") -> str:
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python import type_infer
    from pcc.frontends.python.codegen.layer1 import L1CodeGen

    lifted = parse_and_lift(source, "<fresh-native-instance-append>", module_name)
    typed = type_infer.infer_module(lifted)
    return str(L1CodeGen(typed, ir_scaffold_mode="on").generate(typed))


def _function_body(ir_text: str, module_name: str, function_name: str) -> str:
    marker = "@user_" + module_name + "_" + function_name + "("
    start = ir_text.index(marker)
    start = ir_text.rfind("define ", 0, start)
    return ir_text[start : ir_text.index("\n}", start) + 2]


ROOT_JOIN_SOURCE = '''class Unit:
    def __init__(self, name, path, content):
        self.name = name
        self.path = path
        self.content = content
def read(path, enabled):
    if enabled:
        with open(path, "r") as f:
            return [Unit(path, path, f.read())], path
    units = []
    with open(path, "r") as f:
        units.append(Unit(path, path, f.read()))
    return units, path
'''


@pytest.mark.parametrize("target", ("arm64-apple-darwin", "x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu", "x86_64-pc-windows-msvc"))
def test_fresh_append_receiver_reload_keeps_error_root_states_equal(target):
    from pcc.backend.owned_object_emit import emit_owned_object

    text = _generate_ir(ROOT_JOIN_SOURCE, "append_root_join")
    assert len(emit_owned_object(text, target)) > 0


def test_native_file_with_marks_its_target_bound_before_the_body():
    text = _generate_ir(ROOT_JOIN_SOURCE, "append_root_join")
    body = _function_body(text, "append_root_join", "read")
    marks_bound = any("store i1 1" in line and ".bound.f.owned" in line for line in body.splitlines())
    assert marks_bound, "with open binding remained unbound in its body"


@pytest.mark.integration
def test_fresh_append_inside_with_runs_both_branches_all_collectors(tmp_path, monkeypatch, pcc_runtime_archive, python_program_compiler):
    payload = tmp_path / "payload.txt"
    payload.write_text("native value")
    source = tmp_path / "append_root_join.py"
    source.write_text(ROOT_JOIN_SOURCE + '''import sys
def main(path):
    for enabled in (False, True):
        units, folder = read(path, enabled)
        assert folder == path and len(units) == 1
        assert units[0].content == "native value"
    print("APPEND_WITH_ROOT_JOIN_OK")
main(sys.argv[1])
''')
    binary = tmp_path / "append_root_join"
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off", ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary), str(payload)], env=dict(os.environ, PCC_GC_BACKEND=str(backend), PCC_GC_REFCOUNT_PROVENANCE_PROBE="2"), capture_output=True, text=True, timeout=20)
        assert result.returncode == 0 and result.stdout == "APPEND_WITH_ROOT_JOIN_OK\n" and result.stderr == "", (backend, result.returncode, result.stdout, result.stderr)


def test_direct_fresh_instance_append_uses_trusted_store_after_root_reloads():
    ir_text = _generate_ir(
        textwrap.dedent(
            """
            class Rec:
                def __init__(self, value: int) -> None:
                    self.value = value

            def local_case(value: int) -> int:
                records: list[Rec] = []
                records.append(Rec(value))
                return len(records)

            def param_case(records: list[Rec], value: int) -> int:
                records.append(Rec(value))
                return len(records)
            """
        )
    )

    for function_name in ("local_case", "param_case"):
        body = _function_body(ir_text, "fresh_append", function_name)
        instance_at = body.index("@py_instance_new")
        item_store_at = body.index("@pcc_gc_store_root", instance_at)
        item_reload_at = body.index("list.item.current", item_store_at)
        append_at = body.index("@py_list_append_fresh_native_instance", item_reload_at)
        release_at = body.index("@pcc_gc_release", append_at)
        root_clear_at = body.index("@pcc_gc_store_root", release_at)

        assert instance_at < item_store_at < item_reload_at < append_at
        assert append_at < release_at < root_clear_at
        assert "call void (ptr, ptr) @py_list_append(" not in body
        # The receiver was evaluated before the constructor and its unchanged,
        # unboxed local binding is loaded a second time from the GC root just
        # before the trusted append.  Name the receiver slot explicitly so the
        # item's own root reload cannot satisfy this assertion by accident.
        reload_window = body[item_reload_at:append_at]
        receiver_slot_at = reload_window.index("records.gc.slot")
        receiver_reload_at = reload_window.index("@pcc_gc_load", receiver_slot_at)
        assert receiver_slot_at < receiver_reload_at

    # Only the list-level proof-bearing ABI is frontend-visible.  User IR must
    # never gain direct access to the lower-level trusted slot store.
    assert "@pcc_gc_store_ptr_fresh_native_instance" not in ir_text


def test_non_proven_constructor_append_cases_stay_on_generic_store():
    ir_text = _generate_ir(
        textwrap.dedent(
            """
            class Rec:
                def __init__(self, value: int) -> None:
                    self.value = value

            Ctor = Rec

            class Child(Rec):
                pass

            def make(value: int) -> Rec:
                return Rec(value)

            def aliased_item(value: int) -> int:
                records: list[Rec] = []
                item = Rec(value)
                records.append(item)
                return len(records)

            def factory_item(value: int) -> int:
                records: list[Rec] = []
                records.append(make(value))
                return len(records)

            def constructor_alias(value: int) -> int:
                records: list[Rec] = []
                records.append(Ctor(value))
                return len(records)

            def keyword_constructor(value: int) -> int:
                records: list[Rec] = []
                records.append(Rec(value=value))
                return len(records)

            def inherited_constructor(value: int) -> int:
                records: list[Child] = []
                records.append(Child(value))
                return len(records)

            global_records: list[Rec] = []

            def global_receiver(value: int) -> int:
                global_records.append(Rec(value))
                return len(global_records)

            def nonlocal_receiver(value: int) -> int:
                records: list[Rec] = []

                def rebind() -> None:
                    nonlocal records
                    records = []

                records.append(Rec(value))
                rebind()
                return len(records)
            """
        )
    )

    # A name writable through ``nonlocal`` is closure-boxed before lowering;
    # its receiver becomes a subscript rather than a stable local Name.  It
    # must stay generic because a constructor callback could rebind the cell
    # after Python has already evaluated the original receiver.
    for function_name in (
        "aliased_item",
        "factory_item",
        "constructor_alias",
        "keyword_constructor",
        "inherited_constructor",
        "global_receiver",
        "nonlocal_receiver",
    ):
        body = _function_body(ir_text, "fresh_append", function_name)
        assert "call void (ptr, ptr) @py_list_append(" in body, body
        assert "@py_list_append_fresh_native_instance" not in body


def test_trusted_append_keeps_all_gc_barriers_and_borrowed_item_accounting():
    py_obj = (REPO_ROOT / "pcc/runtime/py/py_obj.py").read_text(
        encoding="utf-8"
    )
    py_list = (REPO_ROOT / "pcc/runtime/py/py_list.py").read_text(
        encoding="utf-8"
    )
    runtime_abi = (REPO_ROOT / "pcc/frontends/python/codegen/runtime_abi.py").read_text(
        encoding="utf-8"
    )

    py_store = py_obj.split(
        'def pcc_gc_store_ptr_fresh_native_instance(owner, slot, value) -> None:', 1
    )[1].split('@c_abi_export("pcc_gc_store_root")', 1)[0]
    for block in (py_store,):
        assert "pcc_gc_note_store" in block
        assert "pcc_gc_note_relocation_read" in block
        assert "pcc_gc_note_slot_write_barrier" in block
        assert "old" in block
        assert "py_decref(old)" in block
    assert "_gc_incref_fresh_native_instance(value)" in py_store

    py_incref = py_obj.split(
        "def _gc_incref_fresh_native_instance(o) -> None:", 1
    )[1].split('@c_abi_export("pcc_gc_store_ptr")', 1)[0]

    assert "_ptr_can_have_header" not in py_incref
    assert "pcc_gc_pointer_is_managed" not in py_incref
    assert "py_incref(o)" in py_incref
    assert "pcc_refcount_incref(o)" in py_incref
    assert "PY_TYPE_USER_CLASS_START" in py_incref
    # User class tags run up to PY_TYPE_CEXT_TAG_BASE; the old ``tag > 500``
    # bound skipped refcounting past roughly the 440th class.
    assert "tag >= (0x10000)" in py_incref
    assert "tag > 500" not in py_incref
    assert "backend == 1 or backend == 2" in py_incref
    assert "backend == 3" in py_incref
    assert "backend == 4" in py_incref
    assert "PY_FLAG_IMMORTAL" in py_incref
    assert "pcc_gc_note_relocation_read(o)" in py_incref

    py_append = py_list.split(
        'def py_list_append_fresh_native_instance(lst, item) -> None:', 1
    )[1].split('@c_abi_export("py_list_get")', 1)[0]
    for block in (py_append,):
        assert "grow_if_needed" in block
        assert "store_ptr" in block or "= NULL" in block
        assert "pcc_gc_store_ptr_fresh_native_instance" in block

    assert '"py_list_append_fresh_native_instance"' in runtime_abi
    assert '"pcc_gc_store_ptr_fresh_native_instance"' not in runtime_abi

    # The ordinary ABI remains the universal fallback and retains the exact
    # generic provenance-query path.
    py_generic = py_obj.split(
        'def pcc_gc_store_ptr(owner, slot, value) -> None:', 1
    )[1].split('@c_abi_export("pcc_gc_store_ptr_fresh_native_instance")', 1)[0]
    assert "py_incref(value)" in py_generic


def test_fresh_instance_append_preserves_identity_finalizer_and_weakref_all_gcs(
    tmp_path: Path,
    monkeypatch,
    pcc_runtime_archive: Path,
):
    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / "fresh_append_gc_matrix.py"
    source.write_text(
        textwrap.dedent(
            """
            import gc
            import weakref

            callbacks = []

            def on_dead(dead) -> None:
                callbacks.append(1)

            class WeakRec:
                def __init__(self, value: int) -> None:
                    self.value = value

            class FinRec:
                finalized = 0

                def __init__(self, value: int) -> None:
                    self.value = value

                def __del__(self) -> None:
                    FinRec.finalized = FinRec.finalized + 1

            def check_weakref() -> None:
                records: list[WeakRec] = []
                records.append(WeakRec(7))
                first = records.pop()
                ref = weakref.ref(first, on_dead)
                print(ref() is first)
                print(first.value)
                first = None
                records = None
                gc.collect()
                gc.collect()
                print(len(callbacks))
                print(ref() is None)

            def check_finalizer() -> None:
                records: list[FinRec] = []
                records.append(FinRec(8))
                print(FinRec.finalized)
                held = records.pop()
                print(held.value)
                held = None
                records = None
                gc.collect()
                gc.collect()
                print(FinRec.finalized)

            def main() -> None:
                check_weakref()
                check_finalizer()

            if __name__ == "__main__":
                main()
            """
        ).lstrip(),
        encoding="utf-8",
    )
    expected = ["True", "7", "1", "True", "0", "8", "1"]
    runtimes = (("pcc-python", pcc_runtime_archive),)
    monkeypatch.delenv("PCC_GC_BACKEND", raising=False)
    for runtime_name, runtime_archive in runtimes:
        executable = tmp_path / ("fresh_append_" + runtime_name + ".out")
        monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(runtime_archive))
        compile_python(
            str(source),
            str(executable),
            backend="self",
            libpython_mode="off",
            ir_scaffold_mode="on",
        )
        for backend in range(5):
            run_env = dict(os.environ)
            run_env.pop("LC_ALL", None)
            run_env["PCC_GC_BACKEND"] = str(backend)
            result = subprocess.run(
                [str(executable)],
                capture_output=True,
                text=True,
                timeout=20,
                env=run_env,
            )
            assert result.returncode == 0, (
                runtime_name
                + f" PCC_GC_BACKEND={backend}\n"
                + result.stdout
                + result.stderr
            )
            assert result.stdout.strip().splitlines() == expected, (
                runtime_name + f" PCC_GC_BACKEND={backend}"
            )
