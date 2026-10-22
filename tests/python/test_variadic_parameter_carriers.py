"""Python variadic element annotations must not replace their body carriers."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import re

import pytest

from pcc.frontends.python.parser import parse
from pcc.frontends.python.py_ast import (
    ClassDef,
    DictType,
    DynType,
    For,
    FuncDef,
    StrType,
    TupleType,
)
from pcc.frontends.python.py_lift import parse_and_lift
from pcc.frontends.python.type_infer import infer_module
from tests.python.test_shared_call_binding import _emit, _function
from tests.python.owned_regression_support import (
    assert_owned_program,
    explicit_owned_runtime,
)


def _infer(source, parser="native"):
    module = (parse(source, "carriers.py") if parser == "host" else
              parse_and_lift(source, "carriers.py", "carriers"))
    return infer_module(module)


@pytest.mark.parametrize("parser", ("host", "native"))
@pytest.mark.parametrize("annotation", ("str", "int", "float", "bool", "list[int]", "tuple[int, str]"))
@pytest.mark.parametrize("kind", ("*", "**"))
def test_body_carrier_preserves_the_formal_element_annotation(parser, annotation, kind):
    source = ("def probe(" + kind + "parts: " + annotation + "):\n"
              "    return parts\n")
    module = _infer(source, parser)
    function = module.body[0]
    annotation_type = function.args[0].annotation
    local_type = function.body[0].value.ty
    if kind == "*":
        assert isinstance(local_type, TupleType)
        assert local_type.name == "tuple_variadic"
        assert local_type.elems == (annotation_type,)
    else:
        assert isinstance(local_type, DictType)
        assert isinstance(local_type.key, StrType)
        assert local_type.value == annotation_type
    # Re-inference must never wrap the formal annotation or the carrier again.
    assert infer_module(module).body[0] == function


@pytest.mark.parametrize("parser", ("host", "native"))
def test_positional_index_slice_and_iteration_use_the_tuple_element(parser):
    function = _infer("""def probe(*parts: int):
    first = parts[0]
    tail = parts[1:]
    for item in tail:
        return item
""", parser).body[0]
    annotation = function.args[0].annotation
    first, tail, loop = function.body
    assert first.value.ty == annotation
    assert isinstance(tail.value.ty, TupleType)
    assert tail.value.ty.name == "tuple_variadic"
    assert tail.value.ty.elems == (annotation,)
    assert isinstance(loop, For)
    assert loop.target.ty == annotation
    assert loop.body[0].value.ty == annotation


@pytest.mark.parametrize("parser", ("host", "native"))
def test_keyword_lookup_and_iteration_keep_value_and_key_types(parser):
    function = _infer("""def probe(**parts: int):
    value = parts['name']
    for key in parts:
        return key
""", parser).body[0]
    lookup, loop = function.body
    assert lookup.value.ty == function.args[0].annotation
    assert isinstance(loop.target.ty, StrType)
    assert isinstance(loop.body[0].value.ty, StrType)


@pytest.mark.parametrize("parser", ("host", "native"))
@pytest.mark.parametrize("kind", ("*", "**"))
@pytest.mark.parametrize("annotation", ("", ": Unpack[tuple[int, str]]", ": Unpack[Ts]"))
def test_unknown_variadic_annotations_retain_dynamic_inference(parser, kind, annotation):
    source = ("from typing import Unpack, TypeVarTuple\nTs = TypeVarTuple('Ts')\n"
              "def probe(" + kind + "parts" + annotation + "):\n    return parts\n")
    function = next(stmt for stmt in _infer(source, parser).body if isinstance(stmt, FuncDef))
    assert isinstance(function.args[0].annotation, DynType)
    assert isinstance(function.body[0].value.ty, DynType)


@pytest.mark.parametrize("kind,index,helper", (
    ("*", "0", "py_tuple_getitem"),
    ("**", "'name'", "py_dict_getitem"),
))
@pytest.mark.parametrize("annotation", ("str", "int", "float", "bool"))
def test_variadic_body_uses_container_ir_and_pointer_abi(kind, index, helper, annotation):
    text = _emit("def probe(" + kind + "parts: " + annotation + "):\n"
                 "    return parts[" + index + "]\n")
    body = _function(text)
    assert "(ptr %parts)" in body.splitlines()[0]
    assert "@" + helper + "(" in body
    assert "@py_str_index(" not in body
    assert "@py_obj_subscript(" not in body


@pytest.mark.parametrize("kind", ("", "staticmethod", "classmethod"))
def test_method_variadics_use_body_carriers(kind):
    decorator = "    @" + kind + "\n" if kind else ""
    receiver = "" if kind == "staticmethod" else "cls, " if kind == "classmethod" else "self, "
    text = _emit("class Holder:\n" + decorator + "    def probe(" + receiver +
                 "*parts: str, **named: int):\n"
                 "        return parts[0], named['name']\n")
    body = _function(text, "user_binding_Holder_probe")
    assert "@py_tuple_getitem(" in body
    assert "@py_dict_getitem(" in body
    assert "@py_str_index(" not in body


def test_scalar_variadic_calls_bind_and_forward_pointer_carriers():
    text = _emit('''def target(*parts: int, **named: int):
    return parts, named
def direct():
    return target(1, 2, value=3)
def dynamic():
    alias = target
    return alias(1, value=3)
def keyword():
    return target(value=3)
''')
    target = _function(text, "user_binding_target")
    assert "(ptr %parts, ptr %named)" in target.splitlines()[0]
    adapter = _function(text, "user_binding_target_native_adapter")
    loads = re.findall(
        r"(%[^ ]+) = call ptr [^\n]*@py_tuple_get_known\(ptr [^,]+, i64 ([01])\)",
        adapter,
    )
    assert [index for _, index in loads] == ["0", "1"]
    forwarded = "@user_binding_target(ptr " + loads[0][0] + ", ptr " + loads[1][0] + ")"
    assert forwarded in adapter
    assert "@py_int_to_" not in adapter
    assert "@py_int_from_" not in adapter
    for name in ("direct", "dynamic", "keyword"):
        body = _function(text, "user_binding_" + name)
        assert "@py_obj_call_slots(" in body
        assert "@user_binding_target(" not in body


def test_original_os_join_body_indexes_and_slices_the_tuple():
    root = Path(__file__).resolve().parents[2]
    module = parse_and_lift((root / "pcc/stdlib/os.py").read_text(), "os.py", "binding")
    owner = next(stmt for stmt in module.body if isinstance(stmt, ClassDef) and stmt.name == "_path")
    method = next(stmt for stmt in owner.body if isinstance(stmt, FuncDef) and stmt.name == "join")
    function = replace(method, name="probe", decorators=(), is_method=False)
    module = infer_module(replace(module, body=(function,)))
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    codegen = L1CodeGen(module, ir_scaffold_mode="on")
    codegen._strict_no_libpython = True
    text = str(codegen.generate(module))
    body = _function(text)
    assert "@py_tuple_getitem(" in body
    assert "@py_tuple_slice(" in body
    assert "@py_str_index(" not in body
    assert "@py_str_slice(" not in body


VARIADIC_PROGRAM = '''def collect(*parts: str, **named: int):
    assert isinstance(parts, tuple)
    assert isinstance(named, dict)
    return parts, named

def first(*parts: str):
    return parts[0]

def summarize(*parts: int, **named: int):
    total = 0
    for part in parts[1:]:
        total += part
    for key in named:
        total += named[key]
    return total

class Holder:
    def instance(self, *parts: str, **named: int):
        return parts[0], named['value']
    @staticmethod
    def static(*parts: str):
        return parts[1:]
    @classmethod
    def classed(cls, **named: int):
        return named['value']

def outer():
    def nested(*parts: str, **named: int):
        return parts[0], named['value']
    return nested('nested', value=7)

def check():
    assert collect() == ((), {})
    assert collect('one', value=3) == (('one',), {'value': 3})
    assert collect('one', 'two', left=3, right=4) == (('one', 'two'), {'left': 3, 'right': 4})
    alias = first
    assert first('direct', 'tail') == 'direct'
    assert alias('alias', 'tail') == 'alias'
    assert summarize(1, 2, 3, last=4) == 9
    assert Holder().instance('method', value=5) == ('method', 5)
    assert Holder.static('head', 'tail') == ('tail',)
    assert Holder.classed(value=6) == 6
    assert outer() == ('nested', 7)
    assert collect.__annotations__ == {'parts': str, 'named': int}
    print('VARIADIC_CARRIERS_OK')

check()
'''


def test_variadic_reference_program(capsys):
    exec(compile(VARIADIC_PROGRAM, "<variadic-carriers>", "exec", dont_inherit=True), {})
    assert capsys.readouterr().out == "VARIADIC_CARRIERS_OK\n"


def test_variadic_semantic_program_lowers_strictly():
    text = _emit(VARIADIC_PROGRAM)
    assert not re.search(r"\bcall [^\n]*@py_cpy_", text)
    assert "strict.nolib.stub" not in text


def test_c_variadic_export_keeps_fixed_parameter_and_varargs_abi(tmp_path):
    from pcc.frontends.python.pipeline import compile_python
    source = tmp_path / "c_variadic.py"
    output = source.with_suffix(".ll")
    source.write_text('''from pcc import i64
from pcc.extern import c_abi_variadic_export
from pcc.unsafe import va_start, va_arg_i64, va_end
__pcc_freestanding__ = True
@c_abi_variadic_export('carrier_c_control')
def probe(seed: i64) -> i64:
    cursor = va_start()
    value = va_arg_i64(cursor)
    va_end(cursor)
    return seed + value
''')
    compile_python(str(source), str(output), emit_llvm_only=True,
                   python_library=True, libpython_mode="off", backend="self")
    text = output.read_text()
    body = _function(text, "carrier_c_control")
    assert re.search(r"@carrier_c_control\(i64 %seed, \.\.\.\)", body)
    assert re.search(r"@llvm\.va_start(?:\.p0)?\(", body)
    assert "va_arg " in body
    assert "@py_tuple_" not in body


@pytest.mark.integration
@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_variadic_carriers_execute_five_gc(
    python_program_compiler, request, explicit_owned_runtime, tmp_path, capfd,
):
    assert_owned_program(
        VARIADIC_PROGRAM, "VARIADIC_CARRIERS_OK\n", tmp_path,
        python_program_compiler, request.node.callspec.params["python_program_compiler"],
        explicit_owned_runtime, capfd,
    )
