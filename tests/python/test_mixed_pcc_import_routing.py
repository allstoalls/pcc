"""Per-member import admission preserves markers and actual module exports."""

import re

import pytest

from pcc.backend.owned_object_emit import emit_owned_object
from pcc.frontends.python.codegen.import_lowering import ImportLoweringMixin
from pcc.frontends.python.codegen.native_modules import NativeModuleAliasMixin
from pcc.frontends.python.pipeline import compile_python
from pcc.frontends.python.py_ast import ClassDef, FuncDef, ImportFrom, SourceSpan
from pcc.frontends.python.py_lift import parse_and_lift
from pcc.frontends.python.type_infer import infer_module
from tests.python.test_native_adapter_error_context import PROGRAM


def test_canonical_mixed_markers_preserve_valueclass_and_raw_fields(tmp_path, monkeypatch):
    typed = infer_module(parse_and_lift(PROGRAM, "adapter_scope.py", "adapter_scope"))
    carrier = next(stmt for stmt in typed.body if isinstance(stmt, ClassDef) and stmt.name == "Carrier")
    consume = next(stmt for stmt in carrier.body if isinstance(stmt, FuncDef) and stmt.name == "consume")
    value_type = consume.args[1].annotation
    assert value_type.valueclass
    assert [(name, ty.name) for name, ty in value_type.fields] == [
        ("first", "pcc.i64"), ("second", "pcc.i64"),
    ]
    assert consume.return_ty.name == "pcc.i64"
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "adapter_scope.py"
    output = tmp_path / "adapter_scope.ll"
    source.write_text(PROGRAM)
    compile_python(str(source), str(output), emit_llvm_only=True, python_library=True,
                   backend="self", libpython_mode="off", ir_scaffold_mode="on")
    ir_text = output.read_text()
    assert "@user_pcc___getattr__" not in ir_text
    assert "cpy.fromimport.pcc" not in ir_text
    assert "native.adapter.input.unwind" in ir_text
    assert emit_owned_object(ir_text, "arm64-apple-darwin")[:4] == b"\xcf\xfa\xed\xfe"


def test_machine_marker_callable_aliases_reuse_owned_integer_admission(tmp_path):
    source = tmp_path / "marker_aliases.py"
    output = tmp_path / "marker_aliases.ll"
    source.write_text('''from pcc import valueclass as Decorator, i64 as Signed, u64 as Unsigned
def probe():
    assert Signed(7) == 7
    assert Unsigned(8) == 8
probe()
''')
    compile_python(str(source), str(output), emit_llvm_only=True, python_library=True,
                   backend="self", libpython_mode="off", ir_scaffold_mode="on")
    text = output.read_text()
    assert "cpy.fromimport.pcc" not in text
    assert "@user_pcc___getattr__" not in text
    assert emit_owned_object(text, "arm64-apple-darwin")[:4] == b"\xcf\xfa\xed\xfe"


def test_known_marker_members_leave_real_and_unknown_members_on_module_route():
    class RoutingHost(ImportLoweringMixin, NativeModuleAliasMixin):
        def __init__(self):
            self._native_builtin_value_aliases = {}
            self._native_module_exports = {"pcc": {"build": {"kind": "function"}}}
            self._sibling_module_inits = {"pcc": "real-pcc-init"}
            self.events = []

        def _is_extern_scaffold_import_module(self, _name):
            return False

        def _is_test_facade_import_module(self, _name):
            return False

        def _resolve_relative_import(self, stmt):
            return stmt.module

        def _emit_compiled_module_ensure_initialized(self, name):
            self.events.append(("initialize", name))

        def _resolve_pcc_native_extension_path(self, _name):
            return None

        def _native_import_from_submodule(self, _module, _member):
            return None

        def _bind_native_cross_module_imports(self, stmt, module, _exports):
            self.events.append(("module-members", module, stmt.names))

    host = RoutingHost()
    host._emit_import_from(ImportFrom(SourceSpan("mixed.py", 1, 0, 1, 60), "pcc", (
        ("valueclass", "Decorator"), ("i64", "Signed"),
        ("build", "real_build"), ("no_such_member", "unknown"),
    )))
    assert host._native_builtin_value_aliases == {
        "Decorator": "pcc.valueclass", "Signed": "builtins.int",
    }
    assert host.events == [
        ("initialize", "pcc"),
        ("module-members", "pcc", (("build", "real_build"), ("no_such_member", "unknown"))),
    ]


def test_owned_path_constructor_import_keeps_its_runtime_binding():
    class RoutingHost(ImportLoweringMixin, NativeModuleAliasMixin):
        def __init__(self):
            self._native_builtin_value_aliases = {}
            self._native_module_exports = {"pathlib": {"Path": {"kind": "class"}}}
            self._sibling_module_inits = {"pathlib": "owned-pathlib-init"}
            self.events = []

        def _is_extern_scaffold_import_module(self, _name):
            return False

        def _is_test_facade_import_module(self, _name):
            return False

        def _resolve_relative_import(self, stmt):
            return stmt.module

        def _emit_compiled_module_ensure_initialized(self, name):
            self.events.append(("initialize", name))

        def _resolve_pcc_native_extension_path(self, _name):
            return None

        def _native_import_from_submodule(self, _module, _member):
            return None

        def _bind_native_cross_module_imports(self, stmt, module, _exports):
            self.events.append(("module-members", module, stmt.names))

    host = RoutingHost()
    host._emit_import_from(ImportFrom(SourceSpan("path.py", 1, 0, 1, 40), "pathlib",
                                    (("Path", "Constructor"),)))
    assert host.events == [
        ("initialize", "pathlib"),
        ("module-members", "pathlib", (("Path", "Constructor"),)),
    ]


def test_path_constructors_are_owned_classes_instead_of_builtin_value_aliases():
    class RoutingHost(NativeModuleAliasMixin):
        def __init__(self):
            self._native_builtin_value_aliases = {}

    host = RoutingHost()
    stmt = ImportFrom(SourceSpan("path.py", 1, 0, 1, 40), "pathlib",
                      (("Path", "Constructor"), ("PurePath", None)))
    assert not host._register_native_builtin_import_from_aliases(stmt, "pathlib")
    assert host._native_builtin_value_aliases == {}


def test_unknown_member_is_not_admitted_as_a_marker(tmp_path):
    source = tmp_path / "unknown_member.py"
    output = tmp_path / "unknown_member.ll"
    source.write_text("from pcc import valueclass, i64, no_such_member\n")
    compile_python(str(source), str(output), emit_llvm_only=True, python_library=True,
                   backend="self", libpython_mode="off", ir_scaffold_mode="on")
    text = output.read_text()
    # Strict missing-provider IR must remain a real failure path. Admitting the
    # known members cannot convert the rest of this statement into a no-op.
    assert "@py_raise" in text
    assert "pcc" in text


@pytest.mark.parametrize("header,callee", [
    ("from pcc import i64", "i64"),
    ("from pcc import valueclass, i64", "i64"),
    ("from pcc import i64 as Signed, u64 as Unsigned", "Signed"),
])
def test_module_marker_values_are_seeded_before_function_lowering(tmp_path, header, callee):
    source = tmp_path / "marker_calls.py"
    output = tmp_path / "marker_calls.ll"
    source.write_text(header + "\n" + "module_marker = " + callee + "\n"
                      + "def run():\n    return " + callee + "(7)\n"
                      + "def indirect():\n    return module_marker(8)\n"
                      + "def local():\n    from pcc import i64 as Make\n    return Make(9)\n"
                      + "print(run(), indirect(), local())\n")
    compile_python(str(source), str(output), emit_llvm_only=True, python_library=True,
                   backend="self", libpython_mode="off", ir_scaffold_mode="on")
    text = output.read_text()
    run = re.search(r"^define[^\n]*@user_marker_calls_run\([^\n]*\)[^\n]*\{\n(.*?)^\}", text, re.M | re.S)[1]
    # A successfully encoded NameError path is not a callable implementation.
    # This direct call must lower through the owned integer constructor.
    assert "name.unbound" not in run
    assert "@py_func_call" not in run and "@py_obj_call" not in run
    assert "@py_int_from_i64" in run or "inttoptr" in run
    # First-class uses also need a real value published in module storage,
    # rather than a name-only annotation/scaffold binding.
    assert "@.modvar.marker_calls.module_marker = global ptr" in text
    assert re.search(r"(?:store|pcc_gc_store_root)[^\n]*module_marker", text)
    assert re.search(r"call[^\n]*@py_builtin_type_for_tag\(i64 2\)", text)
    assert emit_owned_object(text, "arm64-apple-darwin")[:4] == b"\xcf\xfa\xed\xfe"
