"""super() results reach their consumer's owning root before call cleanup.

Host checks exercise the actual lowering and owned IR verifier. Native output
and moving-collector behavior are covered separately by the integration test.
"""
from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest

from pcc.backend.self_backend_kernel import get_indexed_function_kernel
from pcc.frontends.python.codegen.errors import L1CodegenError
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.py_lift import parse_and_lift
from pcc.frontends.python.type_infer import infer_module
from tests.owned_ir_validation import verify_ir_text
from tests.python.owned_regression_support import (
    assert_owned_program,
    explicit_owned_runtime,
)
from tests.python.root_slot_contract import RootSlotContract


ALIGNMENT = '''class Base:
    def value(self):
        return 2
class Child(Base):
    def probe(self):
        return super().value() + 3
def main():
    print(Child().probe())
main()
'''

CASES = {
    "instance": '''class Base:
    def value(self, value):
        return value
class Child(Base):
    def probe(self, value, callback):
        return callback(super().value(value))
''',
    "classmethod": '''class Base:
    @classmethod
    def value(cls, value):
        return value
class Child(Base):
    @classmethod
    def probe(cls, value, callback):
        return callback(super().value(value))
''',
    "staticmethod": '''class Base:
    @staticmethod
    def value(value):
        return value
class Child(Base):
    def probe(self, value, callback):
        return callback(super().value(value))
''',
    "keywords": '''class Base:
    def value(self, value, *, suffix=3):
        return [value, suffix]
class Child(Base):
    def probe(self, value, callback):
        return callback(super().value(value, suffix=4))
''',
    "variadic": '''class Base:
    def value(self, value, *rest, suffix=3, **extra):
        return [value, rest, suffix, extra]
class Child(Base):
    def probe(self, value, callback):
        return callback(super().value(value, 5, 6, suffix=4, marker=7))
''',
    "splats": '''class Base:
    def value(self, value, *rest, suffix=3, **extra):
        return [value, rest, suffix, extra]
class Child(Base):
    def probe(self, value, callback):
        return callback(super().value(*(value, 5, 6), **{"suffix": 4, "marker": 7}))
''',
    "explicit": '''class Base:
    def value(self, value):
        return value
class Child(Base):
    def probe(self, value, callback):
        return callback(super(Child, self).value(value))
''',
    "caught": '''class Base:
    def value(self, value):
        raise ValueError("base failed")
class Child(Base):
    def probe(self, value, callback):
        try:
            return callback(super().value(value))
        except ValueError:
            return [9]
''',
}


def _emit(source, monkeypatch, threads="0"):
    monkeypatch.setenv("PCC_WITH_THREADS", threads)
    module = infer_module(parse_and_lift(source, "super_result.py", "super_result"))
    codegen = L1CodeGen(module, ir_scaffold_mode="on")
    codegen._strict_no_libpython = True
    text = str(codegen.generate(module))
    parsed = verify_ir_text(text)
    function = next(fn for fn in parsed.functions if fn.name == "user_super_result_Child_probe")
    blocks = get_indexed_function_kernel(function).materialize_legacy_blocks(function)
    return text, parsed, blocks


def _super_lookups(instructions):
    return {ins.data[0] for ins in instructions
            if ins.kind == "call" and ins.data[2] == "py_super_lookup"}


def _is_super_invocation(ins, looked_up):
    # The native branch calls the looked-up function directly or through an
    # identity-cast alias named ``.super.fn.``.
    return ins.kind == "call" and (
        ins.data[2] in ("py_func_call", "py_func_call_kwargs")
        or ".super.fn." in ins.data[2]
        or (ins.data[3] and ins.data[2] in looked_up))


def _contract(parsed, blocks, *, static=False):
    roots = RootSlotContract(blocks, parsed.globals_)
    looked_up = _super_lookups(ins for _, _, ins in roots.rows)
    invocations = [(block, index, ins) for block, index, ins in roots.rows
                   if _is_super_invocation(ins, looked_up)]
    assert len(invocations) == (1 if static else 2)
    outputs = []
    for block, index, invoke in invocations:
        published = block.instructions[index + 1]
        assert published.kind == "store" and published.data[1] == invoke.data[0], (
            "super result must publish immediately, before any lease, error check or cleanup"
        )
        output = roots.slot(published.data[3])
        roots.require_owning(output, allow_lifo=True)
        outputs.append(output)
    assert len(set(outputs)) == 1, "both super dispatch branches must share the consumer root"
    reloads = [ins for _, _, ins in roots.rows if ins.kind == "load"
               and ins.data[0].startswith("super.result.current.")]
    assert len(reloads) == 1 and roots.slot(reloads[0].data[3]) == outputs[0]
    assert not any(ins.kind == "phi" and ".super.ret." in str(ins.data)
                   for _, _, ins in roots.rows)
    roots.assert_frame_exits()
    return invocations, roots


@pytest.mark.parametrize("threads", ["0", "1"])
def test_original_super_arithmetic_result_is_an_authoritative_operand(monkeypatch, threads):
    _text, parsed, blocks = _emit(ALIGNMENT, monkeypatch, threads)
    _contract(parsed, blocks)


@pytest.mark.parametrize("threads", ["0", "1"])
@pytest.mark.parametrize("kind", list(CASES))
def test_super_result_dispatch_branches_publish_into_same_root(monkeypatch, threads, kind):
    source = "def keep(value):\n    return value\n" + CASES[kind]
    text, parsed, blocks = _emit(source, monkeypatch, threads)
    invocations, roots = _contract(parsed, blocks, static=kind == "staticmethod")
    if kind in ("keywords", "variadic", "splats"):
        assert any(ins.data[2] == "py_func_call_kwargs" for _, _, ins in invocations)
    if kind != "staticmethod":
        cleanup = next(block for block in blocks
                       if block.name.startswith("super.publication.arguments.cleanup."))
        calls = [ins.data[2] for ins in cleanup.instructions if ins.kind == "call"]
        assert "pcc_gc_unpin" in calls and "pcc_gc_release" in calls
        assert cleanup.terminator.kind == "br"
        if kind in ("keywords", "variadic", "splats"):
            assert calls.count("pcc_gc_release") >= 2
        assert "super.pyfunc.args.tmp.root" in text


@pytest.mark.parametrize("branch", ["raw", "pyfunc"])
def test_result_contract_rejects_missing_publication(monkeypatch, branch):
    _text, parsed, blocks = _emit(ALIGNMENT, monkeypatch)
    block = next(block for block in blocks if ".super." + branch + "." in block.name)
    looked_up = _super_lookups(ins for item in blocks for ins in item.instructions)
    index = next(index for index, ins in enumerate(block.instructions)
                 if _is_super_invocation(ins, looked_up))
    assert block.instructions[index + 1].kind == "store"
    blocks = [SimpleNamespace(name=item.name, instructions=list(item.instructions),
                              terminator=item.terminator) for item in blocks]
    block = next(item for item in blocks if item.name == block.name)
    block.instructions.pop(index + 1)
    with pytest.raises(AssertionError, match="must publish immediately"):
        _contract(parsed, blocks)


@pytest.mark.parametrize("annotation,expression", [("float", "2.5"), ("None", "None")])
def test_super_scalar_and_void_results_keep_scalar_boxing(monkeypatch, annotation, expression):
    source = ("def keep(value):\n    return value\nclass Base:\n"
              f"    def value(self) -> {annotation}:\n        return {expression}\n"
              "class Child(Base):\n    def probe(self, callback):\n"
              "        return callback(super().value())\n")
    text, parsed, blocks = _emit(source, monkeypatch)
    roots = RootSlotContract(blocks, parsed.globals_)
    roots.assert_frame_exits()
    assert "super.result.current" not in text
    if annotation == "float":
        assert "@py_float_from_f64" in text


@pytest.mark.parametrize("kind,helper", [
    ("instance", "_emit_direct_method_ptr_call"),
    ("staticmethod", "_emit_static_method_ptr_call"),
])
def test_raw_method_results_cannot_be_published_as_managed_objects(monkeypatch, kind, helper):
    original = getattr(L1CodeGen, helper)

    def raw_method(self, method_ptr, method_fn, *args, **kwargs):
        self._manual_pointer_abi_functions.add(method_fn.name)
        return original(self, method_ptr, method_fn, *args, **kwargs)

    monkeypatch.setattr(L1CodeGen, helper, raw_method)
    with pytest.raises(L1CodegenError, match="raw-pointer method ABI cannot publish"):
        _emit("def keep(value):\n    return value\n" + CASES[kind], monkeypatch)


NATIVE_SOURCE = ALIGNMENT + '''
class ReturnBase:
    def value(self, value, *, suffix=None):
        if suffix is None:
            raise ValueError("missing suffix")
        return value
class ReturnChild(ReturnBase):
    def probe(self, value, callback):
        return callback(super().value(value, suffix=4))
    def fail(self, value, callback):
        try:
            return callback(super().value(value))
        except ValueError:
            return "caught"
def keep(value):
    import gc
    gc.collect()
    return value
def check():
    values = [7, 8]
    child = ReturnChild()
    print(child.probe(values, keep) is values)
    print(child.fail(values, keep))
check()
'''


def test_super_result_cpython_reference(tmp_path):
    path = tmp_path / "super_result_reference.py"
    path.write_text(NATIVE_SOURCE, encoding="utf-8")
    result = subprocess.run([sys.executable, str(path)], capture_output=True,
                            text=True, timeout=20)
    assert (result.returncode, result.stdout, result.stderr) == (0, "5\nTrue\ncaught\n", "")


@pytest.mark.integration
def test_super_result_native_matches_cpython_under_five_collectors(
    tmp_path, explicit_owned_runtime, capfd,
):
    from pcc.frontends.python.pipeline import compile_python

    # Original arithmetic regression plus borrowed-object identity, keyword
    # binding and an exception raised by the base method.

    # Reuse explicit source/codegen admission and durable per-collector
    # execution receipts. This fixture cannot provision a runtime or pcc1.
    assert_owned_program(
        NATIVE_SOURCE, "5\nTrue\ncaught\n", tmp_path,
        compile_python, "pcc0", explicit_owned_runtime, capfd,
    )
