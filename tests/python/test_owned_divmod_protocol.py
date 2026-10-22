"""divmod uses its own protocol and preserves arbitrary user results."""

import contextlib
import io
import os
from pathlib import Path
import re
import subprocess

import pytest

from pcc.backend.owned_object_emit import emit_owned_object
from pcc.frontends.python.owned_runtime_build import _compile_runtime_module, runtime_ir_passes
from pcc.frontends.python.pipeline import compile_python
from pcc.frontends.python import parser, type_infer
from pcc.frontends.python.py_ast import Assign, DynType, FloatType, FuncDef, IntType, TupleType
from pcc.ir.optimization.driver import optimize_ir


RESULT = '''class Answer:
    def __init__(self):
        self.payload = "PAYLOAD"
    def __getitem__(self, index):
        return "INDEX" + str(index)
marker = Answer()
class Number:
    def __divmod__(self, other):
        return marker
    def __floordiv__(self, other):
        raise AssertionError("divmod used floordiv")
    def __mod__(self, other):
        raise AssertionError("divmod used mod")
def main():
    answer = divmod(Number(), Number())
    assert answer is marker
    assert answer.payload == "PAYLOAD"
    assert answer[0] == "INDEX0"
    print("DIVMOD_RESULT_OK")
main()
'''

PROTOCOL = '''events = []
base_answer = object()
reflected_answer = object()
class Base:
    def __divmod__(self, other):
        events.append("base")
        return base_answer
    def __rdivmod__(self, other):
        events.append("base-reflected")
        return reflected_answer
class Child(Base):
    def __rdivmod__(self, other):
        events.append("child-reflected")
        return reflected_answer
class Inherited(Base):
    pass
class Shy(Base):
    def __rdivmod__(self, other):
        events.append("shy")
        return NotImplemented
class Declines:
    def __divmod__(self, other):
        events.append("declines")
        return NotImplemented
    def __rdivmod__(self, other):
        raise AssertionError("same type reflected method retried")
class Right:
    def __rdivmod__(self, other):
        events.append("right")
        return reflected_answer
def main():
    assert divmod(Base(), Child()) is reflected_answer
    assert divmod(Base(), Inherited()) is base_answer
    assert divmod(Base(), Shy()) is base_answer
    assert divmod(Declines(), Right()) is reflected_answer
    assert divmod(1, Right()) is reflected_answer
    assert divmod(1.5, Right()) is reflected_answer
    try:
        divmod(Declines(), Declines())
    except TypeError:
        pass
    else:
        raise AssertionError("NotImplemented escaped")
    assert events == ["child-reflected", "base", "shy", "base", "declines", "right", "right", "right", "declines"]
    print("DIVMOD_PROTOCOL_OK")
main()
'''

NUMERIC = '''def main():
    print(divmod(17, 5), divmod(-17, 5), divmod(17, -5))
    print(divmod(10 ** 20, 7))
    print(divmod(-(10 ** 20), 7))
    print(divmod(10 ** 30, 10 ** 10))
    print(divmod(1.0, 0.1), divmod(7.5, -2.0))
    print(divmod(-0.0, 2.0), divmod(0.0, -2.0))
    infinity = float("inf")
    print(divmod(1.0, infinity), divmod(-1.0, infinity))
    quotient, remainder = divmod(infinity, 2.0)
    assert quotient != quotient and remainder != remainder
    quotient, remainder = divmod(float("nan"), 2.0)
    assert quotient != quotient and remainder != remainder
    for divisor in (0, 0.0):
        try:
            divmod(7, divisor)
        except ZeroDivisionError:
            pass
        else:
            raise AssertionError("zero divisor accepted")
    print("DIVMOD_NUMERIC_OK")
main()
'''

EXPECTED_NUMERIC = '''(3, 2) (-4, 3) (-4, -3)
(14285714285714285714, 2)
(-14285714285714285715, 5)
(100000000000000000000, 0)
(9.0, 0.09999999999999995) (-4.0, -0.5)
(-0.0, 0.0) (-0.0, -0.0)
(0.0, 1.0) (-1.0, inf)
DIVMOD_NUMERIC_OK
'''

ALIASED_RESULT = '''import gc
finalized = []
class Left:
    def __divmod__(self, other):
        return self
    def __del__(self):
        finalized.append("left")
class Right:
    def __del__(self):
        finalized.append("right")
        gc.collect()
def main():
    answer = divmod(Left(), Right())
    assert isinstance(answer, Left)
    assert finalized == ["right"]
    del answer
    gc.collect()
    assert finalized == ["right", "left"]
    print("DIVMOD_ALIAS_OK")
main()
'''

OWNER_LIFETIMES = '''finalized = []
class Number:
    def __divmod__(self, other):
        return self
    def __del__(self):
        finalized.append(1)
def deleted():
    answer = divmod(Number(), 1)
    del answer
def rebound():
    answer = divmod(Number(), 1)
    answer = None
def discarded():
    divmod(Number(), 1)
def main():
    deleted()
    assert finalized == [1]
    rebound()
    assert finalized == [1, 1]
    discarded()
    assert finalized == [1, 1, 1]
    print("DIVMOD_LIFETIMES_OK")
main()
'''

PROGRAMS = ((RESULT, "DIVMOD_RESULT_OK\n"),
            (PROTOCOL, "DIVMOD_PROTOCOL_OK\n"),
            (NUMERIC, EXPECTED_NUMERIC),
            (ALIASED_RESULT, "DIVMOD_ALIAS_OK\n"),
            (OWNER_LIFETIMES, "DIVMOD_LIFETIMES_OK\n"))
PROGRAM_IDS = ("arbitrary-result", "reflected-order", "numeric", "aliased-result", "owner-lifetimes")


def test_divmod_result_type_requires_two_numeric_operands():
    module = parser.parse('''class Number:
    def __divmod__(self, other):
        return None
def inspect():
    dynamic = divmod(Number(), Number())
    mixed = divmod(1.5, Number())
    integers = divmod(7, 3)
    floats = divmod(7.5, 3)
''', "divmod_types.py")
    module = type_infer.infer_module(module)
    function = next(stmt for stmt in module.body
                    if isinstance(stmt, FuncDef) and stmt.name == "inspect")
    values = [stmt.value for stmt in function.body if isinstance(stmt, Assign)]
    assert isinstance(values[0].ty, DynType)
    assert isinstance(values[1].ty, DynType)
    assert isinstance(values[2].ty, TupleType)
    assert all(isinstance(ty, IntType) for ty in values[2].ty.elems)
    assert isinstance(values[3].ty, TupleType)
    assert all(isinstance(ty, FloatType) for ty in values[3].ty.elems)


@pytest.mark.parametrize("source_text,expected", PROGRAMS, ids=PROGRAM_IDS)
def test_divmod_reference(source_text, expected):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exec(source_text, {})
    assert output.getvalue() == expected


@pytest.mark.parametrize("source_text,expected", PROGRAMS, ids=PROGRAM_IDS)
def test_divmod_program_reaches_owned_emitter(tmp_path, monkeypatch,
                                            source_text, expected):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "divmod_protocol.py"
    output = tmp_path / "divmod_protocol.ll"
    source.write_text(source_text)
    compile_python(str(source), str(output), emit_llvm_only=True,
                   backend="self", libpython_mode="off", ir_scaffold_mode="on")
    text = output.read_text()
    bodies = re.findall(r'^define[^\n]*\{\n(.*?)^\}', text, re.M | re.S)
    callers = [body for body in bodies if '@py_obj_divmod(' in body]
    assert callers, 'program must actually lower divmod calls'
    assert all('@py_obj_floordiv(' not in body for body in callers)
    assert all('@py_obj_mod(' not in body for body in callers)
    data = emit_owned_object(text, "arm64-apple-darwin23.6.0")
    assert data[:4] == b"\xcf\xfa\xed\xfe"


@pytest.mark.parametrize("module", ["py_protocol_runtime", "py_capi_number_runtime"])
def test_divmod_runtime_reaches_owned_emitter(tmp_path, module):
    runtime = Path(__file__).absolute().parents[2] / "pcc" / "runtime"
    output = tmp_path / (module + ".ll")
    target = "arm64-apple-darwin23.6.0"
    _compile_runtime_module(module, str(runtime / "py" / (module + ".py")),
                            str(output), target)
    text = output.read_text()
    assert '@py_obj_divmod(' in text
    data = emit_owned_object(optimize_ir(text, runtime_ir_passes(str(runtime))), target)
    assert data[:4] == b"\xcf\xfa\xed\xfe"


def test_divmod_rhs_exception_unwinds_left_owner_at_emitter(tmp_path, monkeypatch):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "divmod_raises.py"
    output = tmp_path / "divmod_raises.ll"
    source.write_text('''import gc
import weakref
references = []
finalized = []
class Left:
    def __del__(self):
        finalized.append(1)
def left():
    value = Left()
    references.append(weakref.ref(value))
    return value
def right():
    raise ValueError("right operand")
def main():
    try:
        divmod(left(), right())
    except ValueError:
        pass
    gc.collect()
    assert references[0]() is None
    assert finalized == [1]
main()
''')
    compile_python(str(source), str(output), emit_llvm_only=True,
                   backend="self", libpython_mode="off", ir_scaffold_mode="on")
    data = emit_owned_object(output.read_text(), "arm64-apple-darwin23.6.0")
    assert data[:4] == b"\xcf\xfa\xed\xfe"


def test_dynamic_divmod_owner_is_consumed_on_delete_rebind_and_discard(tmp_path, monkeypatch):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "divmod_owner.py"
    output = tmp_path / "divmod_owner.ll"
    source.write_text(OWNER_LIFETIMES)
    compile_python(str(source), str(output), emit_llvm_only=True,
                   backend="self", libpython_mode="off", ir_scaffold_mode="on")
    text = output.read_text()
    for function in ("deleted", "rebound"):
        body = re.search(r'^define[^\n]*@user_divmod_owner_' + function +
                         r'\([^\n]*\{\n(.*?)^\}', text, re.M | re.S)
        assert body is not None
        marked_owner = re.search(r'store i1 1, ptr %answer\.owned', body[1])
        assert marked_owner is not None, function
        after_owner = body[1][marked_owner.end():]
        # The owner is retired by releasing the deleted value or by clearing
        # its root slot (directly or through an identity-cast alias).
        releases = list(re.finditer(
            r'@pcc_gc_release\(ptr %del\.answer|'
            r'@pcc_gc_store_root\(ptr %answer\.(?:addr|release\.gc\.slot)[^,]*, ptr null\)',
            after_owner,
        ))
        if function == "deleted":
            # ``del`` adds one release before the function-exit cleanup.
            assert len(releases) >= 2, function
        else:
            rebind = re.search(r'store ptr %none[^,]*, ptr %answer\.addr', after_owner)
            assert rebind is not None and releases and releases[0].start() < rebind.start(), function
    discard = re.search(r'^define[^\n]*@user_divmod_owner_discarded\([^\n]*\{\n(.*?)^\}',
                        text, re.M | re.S)
    assert discard is not None
    # The protocol result is published into its owned output root; the
    # discarded statement takes that owner and releases it.
    call = re.search(r'@py_obj_divmod\(', discard[1])
    assert call is not None
    released_result = re.search(r'@pcc_gc_release(?:_known)?\(ptr %call\.slot\.take\.', discard[1][call.end():])
    assert released_result is not None
    data = emit_owned_object(text, "arm64-apple-darwin23.6.0")
    assert data[:4] == b"\xcf\xfa\xed\xfe"


@pytest.mark.integration
@pytest.mark.parametrize("source_text,expected", PROGRAMS, ids=PROGRAM_IDS)
def test_native_divmod_protocol(tmp_path, pcc_runtime_archive,
                                python_program_compiler, source_text, expected):
    source = tmp_path / "divmod_protocol.py"
    binary = tmp_path / "divmod_protocol"
    source.write_text(source_text)
    python_program_compiler(str(source), str(binary), backend="self",
                            libpython_mode="off",
                            runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        environment = dict(os.environ, PCC_GC_BACKEND=str(backend))
        environment.pop("LC_ALL", None)
        result = subprocess.run([str(binary)], capture_output=True, text=True,
                                timeout=30, env=environment)
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stdout == expected, (backend, result.stdout, result.stderr)
