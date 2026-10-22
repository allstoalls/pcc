"""list.pop / del list[i] raise IndexError instead of silent NULL.

py_list_pop returned NULL on empty/out-of-range pops WITHOUT raising (the
last '/* TODO(phase3): raise ... */' family in py_list.c), so `[].pop()`
produced NULL-adjacent garbage through the typed marshal path and
try/except IndexError never fired. The fix raises in both the C runtime and
the pcc-Python port (grow-failure branches raise MemoryError the same way)
and adds the frontend post-call err-check on every generated py_list_pop
call site (typed method, dyn method, dyn dispatch, del-subscript).

Runs under --backend self --python-libpython=off on both runtime tiers:
the default tier links the pcc-Python port archive, the cc tier links the
C sources (they had the same gap and must stay mirrored).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

PROGRAM = """
a = [1, 2, 3]
try:
    a.pop(10)
    print('NO RAISE pos')
except IndexError:
    print('IndexError pos')
try:
    a.pop(-10)
    print('NO RAISE neg')
except IndexError:
    print('IndexError neg')
b = []
try:
    b.pop()
    print('NO RAISE empty')
except IndexError:
    print('IndexError empty')
c = [7]
try:
    del c[5]
    print('NO RAISE del')
except IndexError:
    print('IndexError del')


def as_dyn(x):
    return x


d = as_dyn([4, 5])
try:
    d.pop(9)
    print('NO RAISE dyn')
except IndexError:
    print('IndexError dyn')
print('dyn popped', d.pop())
print('popped', a.pop())
print('len', len(a))
"""

EXPECTED = [
    "IndexError pos",
    "IndexError neg",
    "IndexError empty",
    "IndexError del",
    "IndexError dyn",
    "dyn popped 5",
    "popped 3",
    "len 2",
]


@pytest.mark.parametrize("runtime_cc", ["port"])
def test_list_pop_and_del_raise_indexerror_no_libpython(tmp_path, runtime_cc):
    src = tmp_path / "prog.py"
    src.write_text(PROGRAM, encoding="utf-8")
    exe = tmp_path / "prog_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env.pop("PCC_RUNTIME_CC", None)
    build = subprocess.run(
        [
            "uv", "run", "pcc", "--backend", "self", "--python-libpython=off",
            "--ir-scaffold=on", str(src), "-o", str(exe),
        ],
        text=True, capture_output=True, timeout=300, env=env,
    )
    assert build.returncode == 0, build.stderr
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=30, env=env
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip().splitlines() == EXPECTED


_POP_OWNED_CASES = {
    "deepcopy": "def probe(value):\n    return copy.deepcopy(value.pop())\n",
    "negative": "def probe(value):\n    return copy.deepcopy(value.pop(-2))\n",
    "index": "def probe(value, index):\n    return copy.deepcopy(value.pop(index))\n",
    "temporary": "def probe(value, index):\n    return copy.deepcopy((value,)[0].pop(index))\n",
    "later_argument": "def probe(value):\n    return take(first=value.pop(), later=fail())\n",
    "caught_error": "def probe(value, index):\n    try:\n        return copy.deepcopy(value.pop(index))\n    except IndexError:\n        return None\n",
    "typed": "def probe(value: list, index):\n    return copy.deepcopy(value.pop(index))\n",
    "typed_int": "def probe(value: list[int]):\n    return value.pop()\n",
    "typed_float": "def probe(value: list[float]):\n    return value.pop()\n",
    "typed_bool": "def probe(value: list[bool]):\n    return value.pop()\n",
    "literal": "def probe(value):\n    return copy.deepcopy([value].pop())\n",
}


@pytest.mark.parametrize("case", _POP_OWNED_CASES)
def test_pop_owned_result_and_operand_lifetimes(case):
    import re
    from tests.python.test_lambda_adapter_scope_lifetime import check_frames
    from tests.python.test_lambda_constructor_roots import emit, immediate_store

    text = emit("import copy\ndef fail():\n    raise ValueError('later')\n"
                "def take(*, first, later, **extras):\n    return first\n" + _POP_OWNED_CASES[case])
    bodies = re.findall(r"^define[^\n]*\{\n.*?^}", text, re.M | re.S)
    bodies = [body for body in bodies if re.search(r"\bcall[^\n]*@py_list_pop\(", body)]
    assert bodies
    for body in bodies:
        original_body = body
        aliases = dict(re.findall(r"(%[-\w.]+) = bitcast ptr (%[-\w.]+) to ptr", body))
        def physical(match):
            value = match.group(0)
            seen = set()
            while value in aliases:
                assert value not in seen
                seen.add(value)
                value = aliases[value]
            return value
        body = re.sub(r"%[-\w.]+", physical, body)
        # The transferred list owner is published before a lease release,
        # pending-error check, or operand destruction can collect it.
        assert immediate_store(body, "py_list_pop") >= 1
        assert "@py_obj_type_tag(" in body
        assert "list.pop.generic" in body
        assert "@py_obj_call_slots(" in body
        assert "dyn.pop.result" not in body and "dyn.list.result" not in body
        assert "@pcc_gc_foreign_lease_acquire(ptr %list.pop.receiver" in body
        assert re.search(r"@pcc_gc_store_root\(ptr %list.pop.receiver[^\n]*ptr null\)", body)
        if "@py_obj_call_slots(" in body:
            assert immediate_store(body, "py_obj_getattr") >= 1
        if case in ("negative", "index", "temporary", "caught_error", "typed"):
            assert "@py_index_i64_checked_slots(" in body
            assert "@py_obj_as_int64(" not in body
        if case == "typed_int":
            assert "@py_int_to_i64_lane(" not in body
        check_frames(original_body)
    assert not re.search(r"\bcall[^\n]*@py_cpy_", text)


POP_OWNED_PROGRAM = """\
import copy
import gc


events = []


def as_dyn(value):
    return value


class Index:
    def __init__(self, values):
        self.values = values

    def __index__(self):
        events.append('index')
        self.values.append(99)
        gc.collect()
        return -1

    def __del__(self):
        events.append('index dropped')
        gc.collect()


class BrokenIndex:
    def __index__(self):
        raise OverflowError('from index')


def custom_pop(index):
    events.append('invoke')
    gc.collect()
    return [index]


class Custom:
    @property
    def pop(self):
        events.append('lookup')
        return custom_pop


class ListOverride(list):
    def pop(self, index=-1):
        events.append('list override')
        return [index]


class Missing:
    @property
    def pop(self):
        events.append('lookup error')
        raise ValueError('lookup')


def receiver():
    events.append('receiver')
    return Custom()


def argument():
    events.append('argument')
    gc.collect()
    return 17


class Watched:
    def __init__(self, label):
        self.label = label

    def __del__(self):
        events.append(self.label)
        gc.collect()


def temporary_list():
    return [Watched('removed dropped'), Watched('remaining dropped')]


def later():
    events.append('later')
    raise ValueError('later')


def take(*, first, second, **extras):
    return first


def annotated_pop(values: list, index):
    return values.pop(index)


def pop_int(values: list[int]):
    return values.pop()


def main():
    values = as_dyn([[1], [2], [3]])
    print(copy.deepcopy(values.pop()), copy.deepcopy(values.pop(-2)), values)
    original = [4]
    values = as_dyn([original])
    print(values.pop() is original, len(values))
    values = as_dyn([1])
    print(copy.deepcopy(values.pop(Index(values))), values)
    print(events)
    events.clear()
    for index in [100, -100]:
        try:
            as_dyn([1]).pop(index)
        except IndexError:
            print('range error')
    try:
        as_dyn([1]).pop(100000000000000000000)
    except OverflowError:
        print('index overflow')
    try:
        as_dyn([1]).pop(1.5)
    except TypeError:
        print('type error')
    try:
        as_dyn([1]).pop(BrokenIndex())
    except OverflowError as error:
        print(str(error))
    try:
        as_dyn([]).pop()
    except IndexError:
        print('empty error')
    print(copy.deepcopy(receiver().pop(argument())))
    print(events)
    events.clear()
    print(annotated_pop(Custom(), 23))
    print(events)
    events.clear()
    print(annotated_pop(ListOverride(), 29))
    print(events)
    events.clear()
    try:
        as_dyn(Missing()).pop(argument())
    except ValueError:
        print(events)
    events.clear()
    print(as_dyn({'key': [6]}).pop('key'))
    print(as_dyn({7}).pop())
    try:
        as_dyn({}).pop()
    except TypeError:
        print('dict arity error')
    try:
        take(first=temporary_list().pop(0), second=later())
    except ValueError:
        print('later caught')
    gc.collect()
    print(events)
    print(pop_int([100000000000000000000]))
    return 0


main()
"""

POP_OWNED_EXPECTED = """\
[3] [1] [[2]]
True 0
99 [1]
['index', 'index dropped']
range error
range error
index overflow
type error
from index
empty error
[17]
['receiver', 'lookup', 'argument', 'invoke']
[23]
['lookup', 'invoke']
[29]
['list override']
['lookup error']
[6]
7
dict arity error
later caught
['remaining dropped', 'later', 'removed dropped']
100000000000000000000
"""


def test_pop_owned_program_reference(tmp_path):
    import sys
    source = tmp_path / "pop_reference.py"
    source.write_text(POP_OWNED_PROGRAM)
    result = subprocess.run([sys.executable, str(source)], text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout == POP_OWNED_EXPECTED


def test_pop_owned_program_codegen():
    import re
    from tests.python.test_lambda_constructor_roots import emit, immediate_store
    text = emit(POP_OWNED_PROGRAM)
    assert "strict.nolib.stub" not in text
    assert immediate_store(text, "py_list_pop") >= 1
    assert not re.search(r"\bcall[^\n]*@py_cpy_", text)


@pytest.mark.integration
def test_pop_owned_native_semantics(tmp_path):
    from pcc.frontends.python.pipeline import compile_python
    source = tmp_path / "pop_owned.py"
    binary = tmp_path / "pop_owned"
    source.write_text(POP_OWNED_PROGRAM)
    compile_python(str(source), str(binary), ir_scaffold_mode="on", libpython_mode="off")
    result = subprocess.run([str(binary)], text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout == POP_OWNED_EXPECTED


@pytest.mark.parametrize("projection", ["i64", "u64"])
def test_pop_owned_explicit_machine_projection_checks_errors(projection):
    import re
    from tests.python.test_lambda_constructor_roots import emit, immediate_store
    from tests.python.test_lambda_adapter_scope_lifetime import check_frames
    text = emit("from pcc import " + projection + "\n"
                "def probe(values: list[" + projection + "]) -> " + projection + ":\n"
                "    return values.pop()\n")
    body = next(body for body in re.findall(r"^define[^\n]*\{\n.*?^}", text, re.M | re.S)
                if "@py_list_pop(" in body)
    assert re.match(r"define external i64 @", body)
    assert immediate_store(body, "py_list_pop") >= 1
    conversion = body.index("@py_int_to_i64_lane(")
    tail = body[conversion:]
    # This retains the existing checked signed-i64 boundary for both marker
    # names. It does not assert support for the upper half of unsigned u64.
    assert "@py_err_occurred(" in tail
    assert tail.index("@py_err_occurred(") < tail.index("@pcc_gc_store_root(")
    check_frames(body)


@pytest.mark.parametrize("value,overflow", [
    (-(1 << 63), False), ((1 << 63) - 1, False),
    (-(1 << 63) - 1, True), (1 << 63, True), ((1 << 64) - 1, True),
])
def test_pop_owned_machine_runtime_preserves_overflow_error(value, overflow):
    from pcc.runtime.py import py_abi_constants as abi
    from tests.python.test_foreign_address_leases import _functions

    magnitude = abs(value)
    digits = []
    while magnitude:
        limb = magnitude & 0xffffffff
        digits.append(limb if limb < (1 << 31) else limb - (1 << 32))
        magnitude >>= 32
    obj = {
        abi.PYOBJECTHEADER_TYPE_TAG_OFFSET: abi.PY_TYPE_INT,
        abi.PYINTOBJECT_SIGN_OFFSET: -1 if value < 0 else 1,
        abi.PYINTOBJECT_NDIGITS_OFFSET: len(digits),
    }
    obj.update({abi.PYINTOBJECT_DIGITS_OFFSET + i * 4: limb for i, limb in enumerate(digits)})
    errors = []
    namespace = dict(vars(abi))
    namespace.update(
        is_tagged_int=lambda value: False,
        ptr_is_null=lambda value: value is None,
        load_i32=lambda obj, offset: obj[offset],
        store_i32=lambda obj, offset, value: obj.__setitem__(offset, value),
        py_raise_owned=errors.append, py_exc_new=lambda kind, message: (kind, message),
        cstr=lambda value: value,
    )
    _functions(Path(__file__).resolve().parents[2] / "pcc/runtime/py/py_int_convert.py",
               {"_set_overflow", "_load_u32", "py_int_to_i64", "py_int_to_i64_lane"}, namespace)
    flag = {0: -1}
    result = namespace["py_int_to_i64_lane"](obj, flag)
    assert flag[0] == int(overflow)
    if overflow:
        assert result == 0
        assert len(errors) == 1 and errors[0][0] == 15  # OverflowError, never silent zero.
    else:
        assert result == value and not errors
