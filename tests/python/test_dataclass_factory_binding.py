"""Dataclass factories are captured callables, invoked for omitted fields."""

import contextlib
import io
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess

import pytest

from pcc.backend.owned_object_emit import emit_owned_object
from pcc.backend.self_backend_kernel import get_indexed_function_kernel
from pcc.backend.self_backend_parse import parse_self_backend_module
from pcc.frontends.python.pipeline import compile_python


# These are cross-emission checks: frontend and emitter must use one target.
_OBJECT_TARGET = "arm64-apple-darwin"


BUILTINS = '''from dataclasses import dataclass, field
@dataclass
class Buffers:
    chunks: list = field(default_factory=list)
    symbols: list = field(default_factory=list)
    relocations: list = field(default_factory=list)
def main():
    left = Buffers()
    right = Buffers()
    assert left.chunks is not right.chunks
    assert left.symbols is not right.symbols
    assert left.relocations is not right.relocations
    left.chunks.append(1)
    assert right.chunks == []
    supplied = []
    explicit = Buffers(chunks=supplied, symbols=None)
    assert explicit.chunks is supplied
    assert explicit.symbols is None
    constructor = Buffers
    first_class = constructor()
    assert first_class.chunks is not left.chunks
    print("DATACLASS_BUFFERS_OK")
main()
'''

CUSTOM = '''from dataclasses import dataclass, field
events = []
def make():
    events.append("make")
    return []
@dataclass
class Base:
    rows: object = field(default_factory=make)
@dataclass
class Child(Base):
    extra: object = field(default_factory=make)
assert events == []
def replacement():
    raise AssertionError("factory binding changed")
make = replacement
def main():
    left = Base()
    right = Base()
    assert left.rows is not right.rows
    supplied = object()
    assert Base(supplied).rows is supplied
    assert Base(rows=None).rows is None
    child = Child()
    assert child.rows is not child.extra
    assert events == ["make", "make", "make", "make"]
    print("DATACLASS_CUSTOM_OK")
main()
'''

ORDINARY = '''from dataclasses import field
events = []
def make():
    events.append(1)
    return []
def ordinary(value=field(default_factory=make)):
    return value
def main():
    first = ordinary()
    second = ordinary()
    assert first is second
    assert first.default_factory is make
    assert events == []
    print("ORDINARY_DEFAULT_OK")
main()
'''

OWNERS = '''from dataclasses import dataclass, field
import gc
import weakref
references = []
finalized = []
class Product:
    def __del__(self):
        finalized.append(1)
def first():
    value = Product()
    references.append(weakref.ref(value))
    return value
def second():
    raise ValueError("factory failed")
@dataclass
class Broken:
    token: object = None
    left: object = field(default_factory=first)
    right: object = field(default_factory=second)
def main():
    for keywords in (False, True):
        try:
            if keywords:
                Broken(token=None)
            else:
                Broken()
        except ValueError as error:
            assert str(error) == "factory failed"
    gc.collect()
    assert len(references) == 2
    assert references[0]() is None
    assert references[1]() is None
    assert finalized == [1, 1]
    print("DATACLASS_UNWIND_OK")
main()
'''

GENERATOR = '''from dataclasses import dataclass, field
events = []
def factory():
    events.append(1)
    yield 7
@dataclass
class Box:
    value: object = field(default_factory=factory)
def main():
    left = Box()
    right = Box()
    assert left.value is not right.value
    assert events == []
    assert next(left.value) == 7
    assert events == [1]
    print("DATACLASS_GENERATOR_OK")
main()
'''

CALLABLE = '''from dataclasses import dataclass, field
events = []
class Maker:
    def __call__(self):
        events.append("call")
        return object()
def choose():
    events.append("choose")
    return Maker()
@dataclass
class Box:
    value: object = field(default_factory=choose())
assert events == ["choose"]
def main():
    left = Box()
    right = Box()
    assert left.value is not right.value
    assert Box(value=None).value is None
    assert events == ["choose", "call", "call"]
    print("DATACLASS_CALLABLE_OK")
main()
'''

INHERITED_CAPTURE = '''from dataclasses import dataclass, field
events = []
def make():
    events.append(1)
    return []
original_make = make
@dataclass
class Base:
    rows: object = field(default_factory=make)
def replacement():
    raise AssertionError("inherited factory was recaptured")
make = replacement
@dataclass
class Child(Base):
    extra: object = field(default_factory=original_make)
def replacement_init(self):
    raise AssertionError("inherited metadata used current __init__")
Child.__init__ = replacement_init
@dataclass
class Grandchild(Child):
    third: object = field(default_factory=original_make)
def main():
    base = Base()
    child = Grandchild()
    assert base.rows is not child.rows
    assert child.rows is not child.extra
    assert child.extra is not child.third
    assert events == [1, 1, 1, 1]
    print("DATACLASS_INHERITED_CAPTURE_OK")
main()
'''

ORDER = '''from dataclasses import dataclass, field
events = []
class Maker:
    def __init__(self, value):
        self.value = value
    def __call__(self):
        return [self.value]
def mark(value):
    events.append(value)
    return Maker(value)
@dataclass
class Box:
    earlier = mark(1)
    value: object = field(default_factory=earlier)
    chosen: object = field(default_factory=mark(2))
    earlier = mark(3)
def ordinary(value=mark(4)):
    return value
assert events == [1, 2, 3, 4]
def main():
    box = Box()
    assert box.value == [1]
    assert box.chosen == [2]
    assert ordinary() is ordinary()
    assert events == [1, 2, 3, 4]
    assert not any("__pcc_dataclass_factory" in key for key in Box.__dict__)
    print("DATACLASS_ORDER_OK")
main()
'''

SAME_LINE_ORDER = ORDER.replace(
    "    earlier = mark(1)\n    value: object = field(default_factory=earlier)\n    chosen: object = field(default_factory=mark(2))\n    earlier = mark(3)",
    "    earlier = mark(1); value: object = field(default_factory=earlier); chosen: object = field(default_factory=mark(2)); earlier = mark(3)",
)

PREPARED_ORDER = ORDER.replace("@dataclass\nclass Box:", '''class Meta(type):
    @classmethod
    def __prepare__(cls, name, bases):
        return {}
    def __new__(cls, name, bases, namespace):
        return type(name, bases, namespace)
@dataclass
class Box(metaclass=Meta):''')

CAPTURE_FAILURE = '''from dataclasses import dataclass, field
import gc
import weakref
references = []
finalized = []
class Maker:
    def __call__(self):
        return []
    def __del__(self):
        finalized.append(1)
def first():
    value = Maker()
    references.append(weakref.ref(value))
    return value
def second():
    raise ValueError("capture failed")
def main():
    try:
        @dataclass
        class Broken:
            left: object = field(default_factory=first())
            right: object = field(default_factory=second())
    except ValueError as error:
        assert str(error) == "capture failed"
    gc.collect()
    assert references[0]() is None
    assert finalized == [1]
    print("DATACLASS_CAPTURE_UNWIND_OK")
main()
'''

FUNCTION_DEFAULTS = '''events = []
def mark():
    events.append(1)
    return []
def ordinary(value=mark()):
    return value
assert events == [1]
def create():
    def inner(value=mark()):
        return value
    return inner
def main():
    left = ordinary()
    right = ordinary()
    alias = ordinary
    assert left is right
    assert alias is ordinary
    assert alias() is left
    assert events == [1]
    first = create()
    second = create()
    assert first() is first()
    assert first() is not second()
    assert events == [1, 1, 1]
    print("FUNCTION_DEFAULTS_ONCE_OK")
main()
'''

REBOUND_DEFAULTS = '''events = []
def mark(value):
    events.append(value)
    return []
def original(value=mark(1)):
    return value
saved = original
def replacement(value=mark(2)):
    return value
original = replacement
assert events == [1, 2]
def main():
    first = original()
    for i in range(8):
        assert original() is first
        assert replacement() is first
        assert saved() is not first
    assert original is replacement
    assert events == [1, 2]
    print("REBOUND_DEFAULTS_OWNERS_OK")
main()
'''

PROGRAMS = {
    "buffers": (BUILTINS, "DATACLASS_BUFFERS_OK\n"),
    "custom": (CUSTOM, "DATACLASS_CUSTOM_OK\n"),
    "ordinary": (ORDINARY, "ORDINARY_DEFAULT_OK\n"),
    "unwind": (OWNERS, "DATACLASS_UNWIND_OK\n"),
    "generator": (GENERATOR, "DATACLASS_GENERATOR_OK\n"),
    "callable": (CALLABLE, "DATACLASS_CALLABLE_OK\n"),
    "inherited_capture": (INHERITED_CAPTURE, "DATACLASS_INHERITED_CAPTURE_OK\n"),
    "order": (ORDER, "DATACLASS_ORDER_OK\n"),
    "prepared_order": (PREPARED_ORDER, "DATACLASS_ORDER_OK\n"),
    "capture_failure": (CAPTURE_FAILURE, "DATACLASS_CAPTURE_UNWIND_OK\n"),
    "same_line_order": (SAME_LINE_ORDER, "DATACLASS_ORDER_OK\n"),
    "function_defaults": (FUNCTION_DEFAULTS, "FUNCTION_DEFAULTS_ONCE_OK\n"),
    "rebound_defaults": (REBOUND_DEFAULTS, "REBOUND_DEFAULTS_OWNERS_OK\n"),
}


PREPARED_PROJECTION = '''from model import Box
class Child(Box):
    pass
def project(box: Box):
    return box.value
def inherited(box: Child):
    return box.chosen
def replace(box: Box, value):
    box.value = value
def main():
    box = Box()
    assert project(box) == [1]
    assert inherited(Child()) == [2]
    supplied = object()
    replace(box, supplied)
    assert project(box) is supplied
    assert Box.__static_attributes__ == ()
    print("PREPARED_PROJECTION_OK")
main()
'''


def _body(text, symbol):
    match = re.search(r"^define[^\n]*@" + re.escape(symbol) + r"\([^\n]*\)[^\n]*\{\n(.*?)^\}", text, re.M | re.S)
    assert match is not None, symbol
    return match[1]


def _assert_owned_dynamic_calls(text, symbol, expected_count):
    """Check dispatch and authoritative roots, independently of IR spelling."""
    match = re.search(
        r"^define[^\n]*@" + re.escape(symbol) + r"\([^\n]*\)[^\n]*\{\n.*?^\}",
        text, re.M | re.S,
    )
    assert match is not None, symbol
    target = re.search(r'^target triple = "[^"\n]+"$', text, re.M)
    assert target is not None
    function = parse_self_backend_module(target[0] + "\n" + match[0]).functions[0]
    kernel = get_indexed_function_kernel(function)
    blocks = [kernel.diagnostic_block(i) for i in range(len(kernel.block_names))]
    instructions = [(b, i, instruction) for b, block in enumerate(blocks)
                    for i, instruction in enumerate(block.instructions)]
    aliases = {instruction.data[1]: instruction.data[3]
               for _, _, instruction in instructions
               if instruction.kind == "cast" and instruction.data[0] == "bitcast"}

    def slot(value):
        seen = set()
        while value in aliases:
            assert value not in seen, "cyclic pointer aliases"
            seen.add(value)
            value = aliases[value]
        return value

    allocas = {instruction.data[0] for _, _, instruction in instructions
               if instruction.kind == "alloca"}
    calls = [(b, i, instruction.data) for b, i, instruction in instructions
             if instruction.kind == "call"]
    assert not any(call[2] in ("py_obj_call", "py_obj_call_method")
                   for _, _, call in calls), "raw callable dispatch lost slot owners"
    registrations = {slot(call[4][1][1]): (b, i) for b, i, call in calls
                     if call[2] == "pcc_gc_frame_enter_lifo"}
    releases = {slot(call[4][0][1]) for _, _, call in calls
                if call[2] == "pcc_gc_frame_leave_lifo"}
    predecessors = [set() for _ in blocks]
    for b in range(len(blocks)):
        for j in range(kernel.cfg_successor_count(b)):
            predecessors[kernel.cfg_successor_id(b, j)].add(b)

    def dominates(owner, use):
        if owner[0] == use[0]:
            return owner[1] < use[1]
        pending, seen = [use[0]], {owner[0]}
        while pending:
            b = pending.pop()
            if b in seen:
                continue
            if b == 0:
                return False
            seen.add(b)
            pending.extend(predecessors[b])
        return True

    dispatches = [(b, i, call) for b, i, call in calls
                  if call[2] == "py_obj_call_slots"]
    assert len(dispatches) == expected_count
    for b, i, call in dispatches:
        assert call[1].describe() == "i64" and not call[3]
        assert len(call[4]) == 4
        for operand_type, value in call[4]:
            assert operand_type.kind == "ptr"
            root = slot(value)
            assert root in allocas and root in registrations and root in releases
            assert dominates(registrations[root], (b, i)), root
        # A failed call must take its error edge before consuming the result.
        check = blocks[b].instructions[i + 1]
        assert check.kind == "icmp" and check.data[0] == "slt"
        assert check.data[3:] == (call[0], "0")
        branch = blocks[b].terminator
        assert branch.kind == "br_cond" and branch.data[0] == check.data[1]
        assert branch.data[1] != branch.data[2]


@pytest.mark.parametrize("program,expected", list(PROGRAMS.values()), ids=list(PROGRAMS))
def test_reference_factory_binding(program, expected):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exec(program, {})
    assert output.getvalue() == expected


@pytest.mark.parametrize("program,name", [(BUILTINS, "factory_buffers"), (CUSTOM, "factory_custom")], ids=["builtins", "custom"])
def test_factories_compile_as_callable_signature_defaults(tmp_path, monkeypatch, program, name):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / (name + ".py")
    output = tmp_path / (name + ".ll")
    source.write_text(program)
    compile_python(str(source), str(output), emit_llvm_only=True, backend="self", libpython_mode="off", ir_scaffold_mode="on", target_triple=_OBJECT_TARGET)
    text = output.read_text()
    initializer = _body(text, "_pcc_py_module_init_" + name)
    assert "func.sig.factory" in initializer
    assert "field.list" not in initializer
    if name == "factory_custom":
        assert re.search(r"call ptr[^\n]*@user_factory_custom_make\(", initializer) is None
    artifact = emit_owned_object(text, _OBJECT_TARGET)
    assert artifact[:4] == b"\xcf\xfa\xed\xfe"


def test_runtime_field_is_not_exported_as_a_dataclass_factory(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline_exports import _export_call_sig
    from pcc.frontends.python.py_lift import parse_and_lift
    module = parse_and_lift(ORDINARY, "ordinary.py", "ordinary")
    function = next(statement for statement in module.body if getattr(statement, "name", None) == "ordinary")
    assert "default_factory" not in _export_call_sig(function.args)[0]
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "ordinary.py"
    output = tmp_path / "ordinary.ll"
    source.write_text(ORDINARY)
    from pcc.frontends.python.pipeline import compile_python_multi
    provider = Path(__file__).resolve().parents[2] / "pcc" / "stdlib" / "dataclasses.py"
    compile_python_multi([str(source), str(provider)], str(output), module_names=["ordinary", "dataclasses"], entry_module="ordinary", emit_llvm_only=True, backend="self", libpython_mode="off", ir_scaffold_mode="on", target_triple=_OBJECT_TARGET)
    text = output.read_text()
    initializer = _body(text, "main")
    assert "func.sig.factory" not in initializer
    assert re.search(r"call ptr[^\n]*@user_ordinary_make\(", initializer) is None
    assert "user_dataclasses_field" in text
    assert "strict.nolib.stub" not in _body(text, "user_dataclasses_field")
    assert "strict.nolib.stub" not in _body(text, "user_ordinary_main")
    ordinary_calls = _body(text, "user_ordinary_main")
    assert re.search(r"call ptr[^\n]*@user_dataclasses_field\(", ordinary_calls) is None
    _assert_owned_dynamic_calls(text, "user_ordinary_main", expected_count=2)
    for module_text in re.split(r"^; ---- module: [^\n]* ----\n", text, flags=re.M)[1:]:
        assert emit_owned_object(module_text, _OBJECT_TARGET)[:4] == b"\xcf\xfa\xed\xfe"


@pytest.mark.parametrize("marker,annotation", [("runtime_port", "int"), ("freestanding", "i64")])
def test_raw_negative_default_keeps_direct_abi(tmp_path, monkeypatch, marker, annotation):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "raw_defaults.py"
    output = tmp_path / "raw_defaults.ll"
    source.write_text(
        "from pcc.extern import c_abi_export\n"
        + "__pcc_" + marker + "__ = True\n"
        + ("@c_abi_export('raw_helper')\n" if marker == "freestanding" else "")
        + "def helper(value: " + annotation + " = -1) -> " + annotation + ":\n"
        + "    return value\n"
        + "@c_abi_export('raw_default')\n"
        + "def raw_default() -> " + annotation + ":\n"
        + "    return helper()\n"
    )
    compile_python(str(source), str(output), emit_llvm_only=True, python_library=True,
                   backend="self", libpython_mode="off", ir_scaffold_mode="on", target_triple=_OBJECT_TARGET)
    text = output.read_text()
    caller = _body(text, "raw_default")
    callee = "raw_helper" if marker == "freestanding" else "user_raw_defaults_helper"
    assert re.search(r"call i64(?: \([^\n]*\))? @" + callee + r"\(i64 -1\)", caller)
    assert "@py_obj_call(" not in caller
    assert "func.sig" not in caller
    assert "@py_func_new" not in caller
    assert emit_owned_object(text, _OBJECT_TARGET)[:4] == b"\xcf\xfa\xed\xfe"


def test_dataclass_physical_fields_match_exports_before_method_writes(tmp_path):
    from pcc.frontends.python.pipeline_context import build_closed_world_context
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.type_infer import infer_module
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.py_ast import ClassDef
    source = tmp_path / "layout.py"
    source.write_text('''from dataclasses import dataclass, field
@dataclass
class Buffer:
    segment: str
    flags: int
    chunks: list = field(default_factory=list)
    size: int = 0
    error: object = None
    def append(self, value):
        self.size += len(value)
        self.error = value
        self.chunks.append(value)
''')
    typed = infer_module(parse_and_lift(source.read_text(), str(source), "layout"))
    codegen = L1CodeGen(typed, emit_cpy_main_exitcode=False, ir_scaffold_mode="on")
    class_def = next(statement for statement in typed.body if isinstance(statement, ClassDef))
    info = codegen.class_lowering.declare_class(class_def)
    context = build_closed_world_context([str(source)], ["layout"])
    exports = context[1]
    assert info.field_names == list(exports["layout"]["Buffer"]["field_names"])
    assert info.field_names == ["segment", "flags", "chunks", "size", "error"]


def test_prepared_factory_fields_use_runtime_names(tmp_path, monkeypatch):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "prepared_fields.py"
    output = tmp_path / "prepared_fields.ll"
    source.write_text(PREPARED_ORDER)
    compile_python(str(source), str(output), emit_llvm_only=True, backend="self",
                   libpython_mode="off", ir_scaffold_mode="on", target_triple=_OBJECT_TARGET)
    text = output.read_text()
    initializer = _body(text, "user_prepared_fields_Box___init__")
    caller = _body(text, "user_prepared_fields_main")
    assert "@py_instance_set_field(" not in initializer
    assert "@py_obj_setattr(" in initializer
    assert "@py_instance_get_field(" not in caller
    assert "@py_obj_getattr(" in caller
    assert "strict.nolib.stub" not in initializer + caller
    assert emit_owned_object(text, _OBJECT_TARGET)[:4] == b"\xcf\xfa\xed\xfe"


def test_prepared_layout_proof_reaches_imported_and_inherited_fields(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python_multi
    from pcc.frontends.python.pipeline_context import build_closed_world_context
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    model = tmp_path / "model.py"
    app = tmp_path / "app.py"
    output = tmp_path / "projection.ll"
    model.write_text(PREPARED_ORDER.replace("main()\n", ""))
    app.write_text(PREPARED_PROJECTION)
    paths = [str(app), str(model)]
    names = ["app", "model"]
    _, exports, _ = build_closed_world_context(paths, names)
    assert exports["model"]["Box"]["dynamic_field_layout"] is True
    assert exports["app"]["Child"]["dynamic_field_layout"] is True
    assert exports["model"]["Box"]["field_names"] == ("value", "chosen")
    assert exports["app"]["Child"]["field_names"] == ("value", "chosen")
    compile_python_multi(paths, str(output), module_names=names, entry_module="app",
                         emit_llvm_only=True, backend="self", libpython_mode="off",
                         ir_scaffold_mode="on", target_triple=_OBJECT_TARGET)
    text = output.read_text()
    for function in ("project", "inherited"):
        caller = _body(text, "user_app_" + function)
        assert "@py_instance_get_field(" not in caller
        assert "@py_obj_getattr(" in caller
        assert "strict.nolib.stub" not in caller
    store = _body(text, "user_app_replace")
    assert "@py_instance_set_field(" not in store
    assert "@py_obj_setattr(" in store
    for module_text in re.split(r"^; ---- module: [^\n]* ----\n", text, flags=re.M)[1:]:
        assert emit_owned_object(module_text, _OBJECT_TARGET)[:4] == b"\xcf\xfa\xed\xfe"


def test_definition_publishes_reassigned_function_slot_before_class_capture(tmp_path, monkeypatch):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "promotion.py"
    output = tmp_path / "promotion.ll"
    source.write_text(CUSTOM)
    compile_python(str(source), str(output), emit_llvm_only=True, backend="self", libpython_mode="off", ir_scaffold_mode="on", target_triple=_OBJECT_TARGET)
    text = output.read_text()
    entry = _body(text, "main")
    class_creation = entry.find("@py_class_new(")
    # The global slot is stored directly, or through an identity alias.
    slots = {"@.modvar.promotion.make"}
    slots |= set(re.findall(r"^  (%[^ =]+) = bitcast ptr @\.modvar\.promotion\.make to ptr$", entry, re.M))
    stores = re.findall(r"@pcc_gc_store_root\(ptr ([%@][^,]+), ptr (%[^)]+)\)", entry[:class_creation])
    published = any(slot in slots for slot, value in stores)
    assert published, "definition must populate the promoted function global before its capture"
    assert "pcc.assign.binding.publish.make" in entry or "pcc.def.binding.publish.make" in entry
    assert emit_owned_object(text, _OBJECT_TARGET)[:4] == b"\xcf\xfa\xed\xfe"


def test_factory_binder_runtime_reaches_owned_emitter(tmp_path):
    from pcc.frontends.python.owned_runtime_build import _compile_runtime_module, runtime_ir_passes
    from pcc.ir.optimization.driver import optimize_ir
    runtime = Path(__file__).resolve().parents[2] / "pcc" / "runtime"
    output = tmp_path / "py_func.ll"
    target = "arm64-apple-darwin23.6.0"
    _compile_runtime_module("py_func", str(runtime / "py" / "py_func.py"), str(output), target)
    text = output.read_text()
    factory = _body(text, "user_py_func__bind_dataclass_factory")
    assert "@py_obj_call_deferred" in factory
    assert "@py_gen_run_may_park_sync" not in factory
    assert "@pcc_gc_frame_enter" in factory
    assert "strict.nolib.stub" not in factory
    data = emit_owned_object(optimize_ir(text, runtime_ir_passes(str(runtime))), target)
    assert data[:4] == b"\xcf\xfa\xed\xfe"


@pytest.mark.parametrize("name", ["unwind", "generator", "callable", "inherited_capture", "prepared_order", "capture_failure", "function_defaults", "rebound_defaults"])
def test_factory_execution_controls_reach_owned_emitter(tmp_path, monkeypatch, name):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / ("factory_" + name + ".py")
    output = tmp_path / ("factory_" + name + ".ll")
    source.write_text(PROGRAMS[name][0])
    compile_python(str(source), str(output), emit_llvm_only=True, backend="self", libpython_mode="off", ir_scaffold_mode="on", target_triple=_OBJECT_TARGET)
    assert emit_owned_object(output.read_text(), _OBJECT_TARGET)[:4] == b"\xcf\xfa\xed\xfe"


def test_factory_default_wire_keeps_omission_kind_and_origin():
    from pcc.frontends.python.pipeline_exports import _native_export_from_wire, _native_export_to_wire, export_dataclass_factory_default
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.py_ast import ClassDef, Call
    module = parse_and_lift(CALLABLE, "callable.py", "callable")
    class_def = next(statement for statement in module.body if isinstance(statement, ClassDef) and statement.name == "Box")
    field = class_def.body[0]
    default = export_dataclass_factory_default(field.value)
    argument = {"name": "value", "kind": "pos", "annotation": ("dyn",), "default": default, "has_default": True}
    restored = _native_export_from_wire(json.loads(json.dumps(_native_export_to_wire(argument))))
    assert restored["has_default"] is True
    assert isinstance(restored["default"], Call)
    assert restored["default"].func.ident == "__pcc_dataclass_factory_default__"
    assert restored["default"].span.file == "<pcc-dataclass-factory>"
    ordinary = parse_and_lift("def f(value=__pcc_dataclass_factory_default__(maker)):\n    return value\n", "ordinary.py", "ordinary").body[0].args[0].default
    assert _native_export_to_wire({**argument, "default": ordinary})["has_default"] is False


@pytest.mark.parametrize("program", [ORDER, SAME_LINE_ORDER], ids=["lines", "same_line"])
def test_factory_identity_capture_follows_field_definition_order(tmp_path, monkeypatch, program):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "factory_order.py"
    output = tmp_path / "factory_order.ll"
    source.write_text(program)
    compile_python(str(source), str(output), emit_llvm_only=True, backend="self", libpython_mode="off", ir_scaffold_mode="on", target_triple=_OBJECT_TARGET)
    text = output.read_text()
    initializer = _body(text, "_pcc_py_module_init_factory_order")
    calls = list(re.finditer(r"call ptr[^\n]*@user_factory_order_mark\(", initializer))
    assert len(calls) == 3
    constants = dict(re.findall(r"^  (%[^ =]+) = inttoptr i64 (-?\d+) to ptr$", initializer, re.M))
    values = []
    for call in calls:
        line = initializer[call.start():].splitlines()[0]
        operand = re.search(r"@user_factory_order_mark\(ptr (%[^) ,]+)\)", line)
        assert operand is not None, line
        values.append(int(constants[operand[1]]) >> 1)
    assert values == [1, 2, 3]
    assert "class.factory.capture" in initializer
    assert "strict.nolib.stub" not in _body(text, "user_factory_order_main")
    assert emit_owned_object(text, _OBJECT_TARGET)[:4] == b"\xcf\xfa\xed\xfe"


@pytest.mark.parametrize("local", [False, True], ids=["program-main", "function-local"])
def test_executing_class_entry_binds_factory_capture_slots(tmp_path, monkeypatch, local):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "factory_entry.py"
    output = tmp_path / "factory_entry.ll"
    if local:
        program = '''from dataclasses import dataclass, field
def main():
    @dataclass
    class Buffers:
        chunks: object = field(default_factory=list)
    left = Buffers()
    right = Buffers()
    assert left.chunks is not right.chunks
    print("LOCAL_FACTORY_ENTRY_OK")
main()
'''
    else:
        program = BUILTINS
    source.write_text(program)
    compile_python(str(source), str(output), emit_llvm_only=True, backend="self", libpython_mode="off", ir_scaffold_mode="on", target_triple=_OBJECT_TARGET)
    text = output.read_text()
    entry = _body(text, "user_factory_entry_main" if local else "main")
    capture_count = entry.count("class.factory.capture")
    assert capture_count > 0, "executing class entry must populate the capture slots"
    assert "func.sig.factory" in entry
    assert "strict.nolib.stub" not in entry
    assert emit_owned_object(text, _OBJECT_TARGET)[:4] == b"\xcf\xfa\xed\xfe"


def test_imported_custom_factory_uses_live_captured_signature(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python_multi
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    model = tmp_path / "model.py"
    app = tmp_path / "app.py"
    output = tmp_path / "cross.ll"
    model.write_text(CUSTOM.replace("main()\n", ""))
    app.write_text('''from model import Base, Child
from dataclasses import dataclass, field
@dataclass
class Local(Base):
    extra: object = field(default_factory=list)
def main():
    left = Base()
    right = Base()
    assert left.rows is not right.rows
    child = Child()
    assert child.rows is not child.extra
    constructor = Base
    assert constructor().rows is not left.rows
    assert Base(rows=None).rows is None
    local = Local()
    assert local.rows is not local.extra
    assert Local().rows is not local.rows
    print("DATACLASS_CROSS_OK")
main()
''')
    compile_python_multi([str(app), str(model)], str(output), module_names=["app", "model"], entry_module="app", emit_llvm_only=True, backend="self", libpython_mode="off", ir_scaffold_mode="on", target_triple=_OBJECT_TARGET)
    text = output.read_text()
    caller = _body(text, "user_app_main")
    _assert_owned_dynamic_calls(text, "user_app_main", expected_count=7)
    assert "@user_model_make(" not in caller
    assert "strict.nolib.stub" not in caller
    for module_text in re.split(r"^; ---- module: [^\n]* ----\n", text, flags=re.M)[1:]:
        assert emit_owned_object(module_text, _OBJECT_TARGET)[:4] == b"\xcf\xfa\xed\xfe"


@pytest.mark.integration
def test_native_prepared_layout_projection(tmp_path, pcc_runtime_archive):
    from pcc.frontends.python.pipeline import compile_python_multi
    model = tmp_path / "model.py"
    app = tmp_path / "app.py"
    binary = tmp_path / "projection"
    model.write_text(PREPARED_ORDER.replace("main()\n", ""))
    app.write_text(PREPARED_PROJECTION)
    compile_python_multi([str(app), str(model)], str(binary), module_names=["app", "model"],
                         entry_module="app", backend="self", libpython_mode="off",
                         ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive))
    for gc in range(5):
        environment = dict(os.environ, PCC_GC_BACKEND=str(gc))
        environment.pop("LC_ALL", None)
        result = subprocess.run([str(binary)], env=environment, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, (gc, result.stderr)
        assert result.stdout == "PREPARED_PROJECTION_OK\n"
        assert result.stderr == ""


@pytest.mark.integration
def test_native_imported_custom_factory_and_layout(tmp_path, pcc_runtime_archive):
    from pcc.frontends.python.pipeline import compile_python_multi
    model = tmp_path / "model.py"
    app = tmp_path / "app.py"
    binary = tmp_path / "cross"
    model.write_text(CUSTOM.replace("main()\n", ""))
    app.write_text('''from model import Base, Child
from dataclasses import dataclass, field
@dataclass
class Local(Base):
    extra: object = field(default_factory=list)
def main():
    left = Base()
    right = Base()
    assert left.rows is not right.rows
    child = Child()
    assert child.rows is not child.extra
    constructor = Base
    assert constructor().rows is not left.rows
    assert Base(rows=None).rows is None
    local = Local()
    assert local.rows is not local.extra
    assert Local().rows is not local.rows
    print("DATACLASS_CROSS_OK")
main()
''')
    compile_python_multi([str(app), str(model)], str(binary), module_names=["app", "model"], entry_module="app", backend="self", libpython_mode="off", ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive))
    for gc in range(5):
        environment = dict(os.environ, PCC_GC_BACKEND=str(gc))
        environment.pop("LC_ALL", None)
        result = subprocess.run([str(binary)], env=environment, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, (gc, result.stderr)
        assert result.stdout == "DATACLASS_CROSS_OK\n"


@pytest.mark.integration
@pytest.mark.parametrize("name", list(PROGRAMS))
def test_native_factory_binding(tmp_path, pcc_runtime_archive, python_program_compiler, name):
    source = tmp_path / ("factory_" + name + ".py")
    binary = tmp_path / "factory"
    source.write_text(PROGRAMS[name][0])
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off", runtime_archive=str(pcc_runtime_archive))
    for gc in range(5):
        environment = dict(os.environ, PCC_GC_BACKEND=str(gc))
        environment.pop("LC_ALL", None)
        result = subprocess.run([str(binary)], env=environment, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, (gc, result.stderr)
        assert result.stdout == PROGRAMS[name][1]


@pytest.fixture
def host_binder(monkeypatch):
    """Exercise the real two binders with host stand-ins for the raw ABI.

    Callback rooting is covered by the runtime object-emitter/native gates;
    this test owns omission selection, exact values and partial tuple unwind.
    """
    import pcc.unsafe as unsafe
    # Declaration-only intrinsics reserve native storage at module load. The
    # binder model never reads that storage; execution intrinsics stay strict.
    for name in ("define_thread_local_i32", "define_global_i32",
                 "define_global_i64", "define_global_ptr_null"):
        monkeypatch.setattr(unsafe, name, lambda *args: None)
    path = Path(__file__).resolve().parents[2] / "pcc" / "runtime" / "py" / "py_func.py"
    spec = importlib.util.spec_from_file_location("factory_binding_host_py_func", path)
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)
    none_value = object()
    allocated = set()
    state = {"error": None}

    def new_tuple(size):
        value = [None] * size
        allocated.add(id(value))
        return value

    def release(value):
        if id(value) in allocated:
            allocated.remove(id(value))
            value.clear()

    def factory_call(factory, *owners):
        try:
            result = factory()
            return none_value if result is None else result
        except Exception as error:
            state["error"] = error
            return None

    def runtime_error_if_unset(helper, message):
        if state["error"] is None:
            state["error"] = RuntimeError(message)
        return None

    def validate_kwargs(kwargs):
        # Model the owned keyword ABI called before either binder consumes it.
        if kwargs is None or kwargs is none_value:
            return 0
        if not isinstance(kwargs, dict):
            state["error"] = TypeError("call keyword arguments must be a dict")
            return -1
        if any(not isinstance(key, str) for key in kwargs):
            state["error"] = TypeError("keywords must be strings")
            return -1
        return 0

    overrides = {
        "null": lambda: None,
        "cstr": lambda value: value,
        "py_err_occurred": lambda: int(state["error"] is not None),
        "py_runtime_error_if_unset": runtime_error_if_unset,
        "ptr_is_null": lambda value: int(value is None),
        "is_tagged_int": lambda value: int(type(value) is int),
        "load_i32": lambda value, offset: runtime.PY_TYPE_TUPLE if isinstance(value, (tuple, list)) else runtime.PY_TYPE_DICT if isinstance(value, dict) else 0,
        "load_i64": lambda value, offset: len(value),
        "load_ptr": lambda value, offset: value[(offset - 24) // 8],
        "ptr_add": lambda value, offset: (value, offset),
        "pcc_gc_load_ptr": lambda owner, slot: slot[0][(slot[1] - 24) // 8],
        "py_int_value_i64": int,
        "py_obj_truthy": lambda value: int(bool(value)),
        "py_tuple_new": new_tuple,
        "py_tuple_len": len,
        "py_tuple_get": lambda value, index: value[index],
        "py_tuple_set_item": lambda value, index, item: value.__setitem__(index, item),
        "py_dict_contains": lambda value, key: int(key in value),
        "py_dict_get": lambda value, key: value[key],
        "py_dict_del": lambda value, key: value.pop(key),
        "py_dict_len": len,
        "py_call_merge_kwargs": lambda value, kwargs: dict(kwargs or {}),
        "py_call_validate_kwargs": validate_kwargs,
        "py_decref": release,
        "py_incref": lambda value: None,
        "_bind_dataclass_factory": factory_call,
    }
    for name, value in overrides.items():
        monkeypatch.setattr(runtime, name, value)

    def bind(factory, args=(), kwargs=None, flag=2):
        signature = ("__pcc_func_signature_v1__", ("value",), (0,), (flag,), (factory,))
        if kwargs is None:
            return runtime._bind_signature_no_kwargs(signature, args, None)
        return runtime._bind_signature(signature, args, kwargs, None)
    return runtime, bind, none_value, state


@pytest.mark.parametrize("keywords", [False, True], ids=["positional", "keyword"])
def test_binders_call_factories_only_for_missing_fields(host_binder, keywords):
    runtime, bind, none_value, state = host_binder
    calls = []
    def factory():
        calls.append(1)
        return []
    omitted = {} if keywords else None
    left = bind(factory, kwargs=omitted)[0]
    right = bind(factory, kwargs=omitted)[0]
    assert left is not right
    supplied = []
    explicit = bind(factory, kwargs={"value": supplied}) if keywords else bind(factory, args=(supplied,))
    assert explicit[0] is supplied
    explicit_none = bind(factory, kwargs={"value": none_value}) if keywords else bind(factory, args=(none_value,))
    assert explicit_none[0] is none_value
    assert calls == [1, 1]
    ordinary = bind(supplied, kwargs=omitted, flag=True)
    assert ordinary[0] is supplied
    assert state["error"] is None


@pytest.mark.parametrize("keywords", [False, True], ids=["positional", "keyword"])
def test_binders_preserve_generator_factory_results(host_binder, keywords):
    runtime, bind, none_value, state = host_binder
    events = []
    def factory():
        events.append(1)
        yield 7
    value = bind(factory, kwargs={} if keywords else None)[0]
    assert events == []
    assert next(value) == 7
    assert events == [1]


@pytest.mark.parametrize("keywords", [False, True], ids=["positional", "keyword"])
def test_factory_failure_releases_prior_bound_products(host_binder, keywords):
    import weakref
    runtime, bind, none_value, state = host_binder
    finalized = []
    references = []
    class Product:
        def __del__(self):
            finalized.append(1)
    def first():
        product = Product()
        references.append(weakref.ref(product))
        return product
    def second():
        raise ValueError("factory failed")
    signature = ("__pcc_func_signature_v1__", ("first", "second"), (0, 0), (2, 2), (first, second))
    if keywords:
        result = runtime._bind_signature(signature, (), {}, None)
    else:
        result = runtime._bind_signature_no_kwargs(signature, (), None)
    assert result is None
    assert str(state["error"]) == "factory failed"
    assert references[0]() is None
    assert finalized == [1]
