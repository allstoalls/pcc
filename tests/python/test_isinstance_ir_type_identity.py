"""IR class objects and builtin function types have distinct identities."""
import os
import re
import subprocess

import pytest

from pcc.backend.owned_object_emit import emit_owned_object
from pcc.frontends.python import type_infer
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.py_lift import parse_and_lift


IR_CLASS_NAMES = (
    "IRBuilder", "IntType", "PointerType", "VoidType", "DoubleType",
    "FloatType", "HalfType", "ArrayType", "FunctionType", "Constant",
    "Function", "Module", "GlobalVariable", "IdentifiedStructType",
    "LiteralStructType", "Block", "Argument", "Context", "Value",
)

IR_IDENTITY_PROGRAM = '''from pcc.ir.compat import ir
import types
class FunctionType:
    def __init__(self, return_type, args):
        self.return_type = return_type
        self.args = args
    def __str__(self):
        return "i64 (i64)"
def ordinary(value=0):
    return value
def main():
    i64 = ir.IntType(64)
    ftype = ir.FunctionType(i64, [i64])
    fake = FunctionType(i64, [i64])
    module = ir.Module("identity")
    callee = ir.Function(module, ftype, name="callee")
    body = ir.Function(module, ir.FunctionType(i64, []), name="body")
    builder = ir.IRBuilder(body.append_basic_block("entry"))
    slot = builder.alloca(ftype.as_pointer(), name="callback")
    builder.store(callee, slot)
    loaded = builder.load(slot, name="fptr")
    builder.ret(ir.Constant(i64, 0))
    assert isinstance(ftype, ir.FunctionType)
    assert isinstance(loaded.type, ir.PointerType)
    assert isinstance(loaded.type.pointee, ir.FunctionType)
    assert isinstance(ftype, (ir.FunctionType,))
    assert not isinstance(fake, ir.FunctionType)
    assert not isinstance(fake, (ir.FunctionType,))
    assert isinstance(ordinary, types.FunctionType)
    assert isinstance(ordinary, (types.FunctionType,))
    assert not isinstance(ftype, types.FunctionType)
    assert not isinstance(ordinary, ir.FunctionType)
    assert isinstance(fake, FunctionType)
    print("IR_CLASS_IDENTITY_OK")
main()
'''

ATTRIBUTE_IDENTITY_PROGRAM = '''import types
class FunctionType:
    pass
class Other:
    pass
class Provider:
    def __init__(self):
        self.FunctionType = FunctionType
        self.NoneType = FunctionType
        self.CodeType = FunctionType
        self.str = FunctionType
        self.ValueError = FunctionType
def ordinary():
    return 0
def check(value, provider):
    assert isinstance(value, provider.FunctionType)
    assert isinstance(value, (provider.FunctionType,))
    assert isinstance(value, provider.NoneType)
    assert isinstance(value, provider.CodeType)
    assert isinstance(value, provider.str)
    assert isinstance(value, provider.ValueError)
def main():
    provider = Provider()
    value = FunctionType()
    check(value, provider)
    assert not isinstance(ordinary, provider.FunctionType)
    assert not isinstance(None, provider.NoneType)
    assert not isinstance("text", provider.str)
    provider.FunctionType = Other
    assert not isinstance(value, provider.FunctionType)
    assert isinstance(Other(), provider.FunctionType)
    assert isinstance(ordinary, types.FunctionType)
    print("ATTRIBUTE_CLASS_IDENTITY_OK")
main()
'''

PROGRAMS = {
    "ir_identity": (IR_IDENTITY_PROGRAM, "IR_CLASS_IDENTITY_OK\n"),
    "attribute_identity": (ATTRIBUTE_IDENTITY_PROGRAM, "ATTRIBUTE_CLASS_IDENTITY_OK\n"),
}


def _generate(source, native_exports=None, *, target=None):
    module = type_infer.infer_module(parse_and_lift(source, "<classinfo>", "classinfo"))
    codegen = L1CodeGen(module, ir_scaffold_mode="on")
    if target is not None:
        codegen._target_triple = target
    if native_exports is not None:
        codegen._native_module_exports = dict(codegen._native_module_exports or {})
        codegen._native_module_exports.update(native_exports)
    return str(codegen.generate(module))


def _body(text, name):
    found = re.search(r"^define[^\n]*@user_classinfo_" + name + r"\([^\n]*\)[^\n]*\{\n(.*?)^\}", text, re.M | re.S)
    assert found is not None, (name, text)
    return found.group(1)


@pytest.mark.parametrize("name", IR_CLASS_NAMES)
@pytest.mark.parametrize("tuple_form", (False, True))
def test_ir_classinfo_uses_the_ir_provider_class(name, tuple_form):
    classinfo = "(ir." + name + ",)" if tuple_form else "ir." + name
    text = _generate("from pcc.ir.compat import ir\ndef check(value):\n    return isinstance(value, " + classinfo + ")\n")
    body = _body(text, "check")
    assert "@.class.pcc_ir_ir." + name in body
    assert "@py_isinstance(" in body
    assert "@py_obj_type_tag(" not in body


@pytest.mark.parametrize("name", ("FunctionType", "NoneType", "CodeType", "str", "ValueError"))
@pytest.mark.parametrize("tuple_form", (False, True))
def test_attribute_classinfo_does_not_take_its_tail_as_a_builtin(name, tuple_form):
    classinfo = "(provider." + name + ",)" if tuple_form else "provider." + name
    text = _generate("def check(value, provider):\n    return isinstance(value, " + classinfo + ")\n")
    body = _body(text, "check")
    assert "@py_obj_isinstance(" in body
    assert "@py_obj_type_tag(" not in body
    assert "@py_exc_builtin_class(" not in body
    assert "@py_func_code_class_cache" not in body


@pytest.mark.parametrize("name", ("kind", "FunctionType", "str", "ValueError"))
@pytest.mark.parametrize("tuple_form", (False, True))
def test_parameter_classinfo_binding_wins_over_a_builtin_name(name, tuple_form):
    classinfo = "(" + name + ",)" if tuple_form else name
    text = _generate("def check(value, " + name + "):\n    return isinstance(value, " + classinfo + ")\n")
    body = _body(text, "check")
    assert "@py_obj_isinstance(" in body
    assert "@py_obj_type_tag(" not in body
    assert "@py_exc_builtin_class(" not in body


@pytest.mark.parametrize("target", ("arm64-apple-darwin", "x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu", "x86_64-pc-windows-msvc"))
def test_ir_function_classinfo_reaches_the_owned_object_emitter(tmp_path, target):
    text = _generate("from pcc.ir.compat import ir\ndef check(value):\n    return isinstance(value, ir.FunctionType)\n", target=target)
    assert 'target triple = "' + target + '"' in text
    assert "@.class.pcc_ir_ir.FunctionType" in _body(text, "check")
    output = tmp_path / "classinfo.o"
    output.write_bytes(emit_owned_object(text, target))
    assert output.stat().st_size > 0


@pytest.mark.parametrize("tuple_form", (False, True))
def test_local_class_named_function_type_wins_over_the_builtin(tuple_form):
    classinfo = "(FunctionType,)" if tuple_form else "FunctionType"
    text = _generate("class FunctionType:\n    pass\ndef check(value):\n    return isinstance(value, " + classinfo + ")\n")
    body = _body(text, "check")
    assert "@.class.classinfo.FunctionType" in body, body
    assert "@py_obj_isinstance(" in body
    assert "@py_obj_type_tag(" not in body


@pytest.mark.parametrize("classinfo", ("Exception", "(ValueError, Exception)", "(types.FunctionType, Exception)"))
def test_classinfo_cache_creation_keeps_and_reloads_the_operand(tmp_path, classinfo):
    target = "arm64-apple-darwin"
    text = _generate("import types\ndef check(value):\n    return isinstance(value, " + classinfo + ")\n", {"types": {}}, target=target)
    body = _body(text, "check")
    cache = body.index("@py_exc_builtin_class(")
    predicate = body.index("@py_obj_isinstance(", cache)
    assert body.index("@pcc_gc_store_root(") < cache
    assert "@pcc_gc_load_ptr(" in body[cache:predicate]
    assert "@pcc_gc_frame_leave_lifo(" in body[predicate:]
    output = tmp_path / "lease.o"
    output.write_bytes(emit_owned_object(text, target))


@pytest.mark.parametrize("name", ("FunctionType", "NoneType", "CodeType"))
@pytest.mark.parametrize("tuple_form", (False, True))
def test_types_provider_keeps_its_actual_builtin_type_route(name, tuple_form):
    classinfo = "(provider." + name + ",)" if tuple_form else "provider." + name
    text = _generate("import types as provider\ndef check(value):\n    return isinstance(value, " + classinfo + ")\n", {"types": {}})
    body = _body(text, "check")
    if name == "CodeType":
        assert "@py_func_code_class_cache" in body, body
        assert "@py_obj_isinstance(" in body
    else:
        assert "@py_obj_type_tag(" in body, body
        assert "@.class.pcc_ir_ir.FunctionType" not in body


@pytest.mark.parametrize("name", PROGRAMS)
def test_function_bearing_identity_program_reaches_the_owned_emitter(tmp_path, name):
    target = "arm64-apple-darwin"
    text = _generate(PROGRAMS[name][0], {"types": {}}, target=target)
    output = tmp_path / (name + ".o")
    output.write_bytes(emit_owned_object(text, target))
    assert output.stat().st_size > 0


@pytest.mark.integration
@pytest.mark.parametrize("name", PROGRAMS)
def test_native_classinfo_identity_executes_all_collectors(tmp_path, monkeypatch, pcc_runtime_archive, python_program_compiler, name):
    source_text, expected = PROGRAMS[name]
    source = tmp_path / (name + ".py")
    source.write_text(source_text)
    binary = tmp_path / name
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off", ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend), PCC_GC_REFCOUNT_PROVENANCE_PROBE="2", PATH="/nonexistent"))
        (tmp_path / (name + "-gc" + str(backend) + ".stdout")).write_text(result.stdout)
        (tmp_path / (name + "-gc" + str(backend) + ".stderr")).write_text(result.stderr)
        assert result.returncode == 0 and result.stdout == expected and result.stderr == "", (backend, result.returncode, result.stdout, result.stderr)


def test_dynamic_tuple_preserves_ir_provider_class_identity():
    text = _generate('from pcc.ir.compat import ir\ndef check(value, kind):\n'
                     '    return isinstance(value, (ir.FunctionType, kind))\n')
    body = _body(text, 'check')
    assert '@.class.pcc_ir_ir.FunctionType' in body
    assert body.count('@py_obj_isinstance(') == 1
    assert '@py_obj_type_tag(' not in body


def test_ir_classinfo_object_emitter_still_rejects_a_different_target():
    text = _generate(
        "from pcc.ir.compat import ir\ndef check(value):\n"
        "    return isinstance(value, ir.FunctionType)\n",
        target="x86_64-unknown-linux-gnu",
    )
    with pytest.raises(ValueError, match="target triple mismatch"):
        emit_owned_object(text, "arm64-apple-darwin")
