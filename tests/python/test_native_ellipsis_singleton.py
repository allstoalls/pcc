"""Ellipsis is a real native singleton, distinct from a shadowable name."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from pcc.frontends.python.parser import parse
from pcc.frontends.python.py_ast import Name, TupleType
from pcc.frontends.python.py_lift import parse_and_lift
from tests.python.test_shared_call_binding import _emit
from tests.python.owned_regression_support import (
    assert_owned_program,
    assert_reference_program,
    explicit_owned_runtime,
)
from ir_pointer_aliases import canonical_pointer, function_bodies, pointer_bitcast_aliases


ELLIPSIS_PROGRAM = '''\
NotImplementedType = type(NotImplemented)
EllipsisType = type(Ellipsis)

def take(value):
    return value

def local_shadow(Ellipsis):
    return Ellipsis, ...

def capture():
    def saved(value=...):
        return value
    return saved()

def probe():
    assert Ellipsis is ...
    assert Ellipsis is not None
    assert Ellipsis is not NotImplemented
    assert type(Ellipsis) is EllipsisType
    assert type(...) is EllipsisType
    assert EllipsisType is not type(None)
    assert EllipsisType is not NotImplementedType
    assert EllipsisType.__name__ == "ellipsis"
    assert isinstance(Ellipsis, EllipsisType)
    assert not isinstance(None, EllipsisType)
    assert EllipsisType() is Ellipsis
    assert type(...)() is Ellipsis
    assert take(...) is Ellipsis
    assert capture() is Ellipsis
    assert bool(Ellipsis)
    assert repr(Ellipsis) == "Ellipsis"
    assert str(Ellipsis) == "Ellipsis"
    assert repr([None, Ellipsis, ...]) == "[None, Ellipsis, Ellipsis]"
    assert Ellipsis == ...
    assert Ellipsis != None
    assert Ellipsis != NotImplemented
    values = {None: "none", Ellipsis: "ellipsis", NotImplemented: "notimpl"}
    assert len(values) == 3
    assert values[...] == "ellipsis"
    before = hash(Ellipsis)
    for i in range(200):
        transient = [i, ..., str(i)]
        assert transient[1] is Ellipsis
    assert before == hash(...)
    shadow, literal = local_shadow("local")
    assert shadow == "local"
    assert literal is Ellipsis
    class Shadow:
        Ellipsis = "class"
        named = Ellipsis
        literal = ...
    assert Shadow.named == "class"
    assert Shadow.literal is Ellipsis
    try:
        EllipsisType(1)
    except TypeError as error:
        assert str(error) == "EllipsisType takes no arguments"
    else:
        raise AssertionError("constructor accepted positional argument")
    try:
        EllipsisType(value=1)
    except TypeError as error:
        assert str(error) == "EllipsisType takes no arguments"
    else:
        raise AssertionError("constructor accepted keyword argument")
    try:
        take(Ellipsis)()
    except TypeError as error:
        assert str(error) == "'ellipsis' object is not callable"
    else:
        raise AssertionError("singleton is callable")
    print(Ellipsis)
    print("ELLIPSIS_NATIVE_OK")

probe()
'''

ELLIPSIS_SHADOW_PROGRAM = '''\
Ellipsis = "global"
EllipsisType = type(...)
def global_shadow():
    return Ellipsis, ...
shadow, literal = global_shadow()
assert shadow == "global"
assert type(literal) is EllipsisType
assert Ellipsis == "global"
assert literal is ...
print("ELLIPSIS_SHADOW_OK")
'''


@pytest.mark.parametrize("parser", (parse, lambda source, filename: parse_and_lift(source, filename, "ellipsis")))
def test_literal_syntax_is_distinct_from_shadowable_name(parser):
    module = parser("Ellipsis = 7\nliteral = ...\nnamed = Ellipsis\n", "ellipsis.py")
    literal, named = module.body[1].value, module.body[2].value
    assert isinstance(literal, Name) and literal.ident == "..."
    assert isinstance(named, Name) and named.ident == "Ellipsis"


def test_native_variadic_tuple_annotation_keeps_literal_marker():
    module = parse_and_lift("def take(value: tuple[int, ...]):\n    return value\n", "ellipsis.py", "ellipsis")
    annotation = module.body[0].args[0].annotation
    assert isinstance(annotation, TupleType)
    assert annotation.name == "tuple_variadic"
    assert len(annotation.elems) == 1


@pytest.mark.parametrize("expression", ("Ellipsis", "..."))
def test_type_argument_copies_from_authoritative_singleton_slot(expression):
    text = _emit("def probe():\n    return type(" + expression + ")\n")
    body = next(body for name, body in function_bodies(text) if name == "user_binding_probe")
    aliases = pointer_bitcast_aliases(body)
    aliases.update(re.findall(r'^\s*(%[\w.$]+) = bitcast ptr (@py_Ellipsis) to ptr\s*$', body, re.M))
    copies = re.findall(r'\bcall [^\n]*@pcc_gc_root_copy_lease\(ptr (%[\w.$]+), ptr ([%@][\w.$]+)\)', body)
    assert any(canonical_pointer(source, aliases) == "@py_Ellipsis" for _target, source in copies), body
    assert "@py_type_builtin(" in body


@pytest.mark.parametrize("source", (
    "def probe(Ellipsis):\n    return type(Ellipsis)\n",
    "Ellipsis = 'global'\ndef probe():\n    return type(Ellipsis)\n",
    "def Ellipsis():\n    return 7\ndef probe():\n    return Ellipsis\n",
    "class Ellipsis:\n    pass\ndef probe():\n    return Ellipsis\n",
    "from math import pi as Ellipsis\ndef probe():\n    return Ellipsis\n",
))
def test_shadowed_name_does_not_load_the_singleton(source):
    text = _emit(source)
    body = next(body for name, body in function_bodies(text) if name == "user_binding_probe")
    assert "@py_Ellipsis" not in body


def test_original_types_singleton_definitions_lower_strictly():
    text = _emit("NotImplementedType = type(NotImplemented)\nEllipsisType = type(Ellipsis)\n")
    assert "@py_NotImplemented" in text
    assert "@py_Ellipsis" in text
    assert text.count("@py_type_builtin(") >= 3


@pytest.mark.parametrize("program", (ELLIPSIS_PROGRAM, ELLIPSIS_SHADOW_PROGRAM), ids=("singleton", "global_shadow"))
def test_semantic_program_lowers_without_ownership_exemptions(program):
    assert "@py_Ellipsis" in _emit(program)


@pytest.mark.integration
@pytest.mark.parametrize("program, expected", ((ELLIPSIS_PROGRAM, "Ellipsis\nELLIPSIS_NATIVE_OK\n"),
                                               (ELLIPSIS_SHADOW_PROGRAM, "ELLIPSIS_SHADOW_OK\n")),
                         ids=("singleton", "global_shadow"))
@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_native_ellipsis_five_gc(python_program_compiler, request, explicit_owned_runtime, tmp_path, capfd, program, expected):
    mode = request.node.callspec.params["python_program_compiler"]
    assert_owned_program(program, expected, tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime,
                         capfd, provenance_probe="2")
    assert (tmp_path / "compiler-wrapper.stderr").read_text() == ""


def test_ellipsis_tag_is_shared_by_all_public_abi_projections():
    from pcc.runtime.py import py_abi_constants as runtime
    from pcc.frontends.python.codegen import freestanding_abi_constants as compiler
    from pcc.frontends.python.codegen.port_abi_exports import PORT_ABI_NATIVE_EXPORTS
    from pcc.runtime.freestanding_abi_spec import ABI_SPEC
    assert runtime.PY_TYPE_ELLIPSIS == compiler.PY_TYPE_ELLIPSIS == 33
    assert ABI_SPEC["object.type.ellipsis"] == 33
    assert compiler.ABI_CONSTANTS["object.type.ellipsis"] == 33
    assert PORT_ABI_NATIVE_EXPORTS["pcc.runtime.py.py_abi_constants"]["PY_TYPE_ELLIPSIS"]["value"] == 33
    assert runtime.PY_TYPE_ELLIPSIS not in (runtime.PY_TYPE_NONE, runtime.PY_TYPE_BOOL)
    header = (Path(__file__).resolve().parents[2] / "pcc/runtime/include/py_runtime.h").read_text()
    assert re.search(r"PY_TYPE_ELLIPSIS\s*=\s*33\s*,", header)


@pytest.mark.parametrize("module", ("py_substrate", "py_capi_exc_runtime",
                                   "py_obj_ops_dispatch", "py_obj_ops_compare",
                                   "py_obj_stubs", "py_print_fmt", "py_obj",
                                   "py_gc_backend"))
def test_singleton_storage_and_capi_alias_emit_the_same_object(module, tmp_path):
    from pcc.frontends.python import pipeline
    source = Path(__file__).resolve().parents[2] / "pcc/runtime/py" / (module + ".py")
    output = tmp_path / (module + ".ll")
    pipeline.compile_python(str(source), str(output), emit_llvm_only=True,
                            libpython_mode="off", python_library=True)
    text = output.read_text()
    assert "strict.nolib.stub:" not in text
    if module == "py_substrate":
        assert re.search(r"@py_ellipsis_storage = global .*i64 1, i32 33, i32 1", text)
        assert "@py_Ellipsis = global ptr @py_ellipsis_storage" in text
    elif module == "py_capi_exc_runtime":
        assert "@Py_Ellipsis = global ptr @py_ellipsis_storage" in text
        assert "pcc_capi_ellipsis_sentinel" not in text


@pytest.mark.parametrize("program, expected", ((ELLIPSIS_PROGRAM, "Ellipsis\nELLIPSIS_NATIVE_OK\n"),
                                               (ELLIPSIS_SHADOW_PROGRAM, "ELLIPSIS_SHADOW_OK\n")),
                         ids=("singleton", "global_shadow"))
def test_reference_semantics(program, expected, tmp_path):
    assert_reference_program(program, expected, tmp_path)


def test_singleton_emitter_is_in_native_host_contract():
    from inspect import signature
    from pcc.frontends.python.codegen.host_contract import L1_CODEGEN_HOST_METHODS
    from pcc.frontends.python.codegen._l1_codegen_static_methods import L1_CODEGEN_STATIC_METHODS
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.codegen.layer1_support import _default_native_module_exports
    static = {entry["name"]: entry for entry in L1_CODEGEN_STATIC_METHODS}
    methods = _default_native_module_exports("pcc.frontends.python.codegen.layer1")["pcc.frontends.python.codegen.layer1"]["L1CodeGen"]["methods"]
    native = {entry["name"]: entry for entry in methods}
    name = "_emit_ellipsis_literal"
    assert tuple(entry["name"] for entry in L1_CODEGEN_STATIC_METHODS) == L1_CODEGEN_HOST_METHODS
    assert static[name] == native[name]
    assert tuple(item["name"] for item in static[name]["call_sig"]) == tuple(signature(getattr(L1CodeGen, name)).parameters)
