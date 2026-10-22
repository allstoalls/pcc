"""Generic builtin-base super initialization and original defaultdict shape."""
import contextlib
import io
import os
import re
import subprocess

import pytest

from pcc.frontends.python import type_infer
from pcc.frontends.python.codegen.errors import L1CodegenError
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.pipeline import compile_python
from pcc.frontends.python.py_lift import parse_and_lift


BASIC = '''class Bag(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
    def again(self, args, kwargs):
        return super().__init__(*args, **kwargs)
class Child(Bag):
    pass
def main():
    bag = Child(*({"first": 1},), **{"second": 2})
    assert bag["first"] == 1 and bag["second"] == 2 and len(bag) == 2
    assert bag.again((), {}) is None and len(bag) == 2
    assert bag.again(([('third', 3)],), {"first": 4}) is None
    assert bag["first"] == 4 and bag["third"] == 3 and len(bag) == 3
    print("DICT_SUPER_OK")
main()
'''

ERRORS = '''class Bag(dict):
    def __init__(self):
        super().__init__()
    def again(self, args, kwargs):
        return super().__init__(*args, **kwargs)
def broken():
    yield ("first", 1)
    yield ("bad", 2, 3)
def main():
    bag = Bag()
    try:
        bag.again(({}, {}), {"later": 2})
    except TypeError:
        pass
    else:
        raise AssertionError("dict arity accepted")
    assert len(bag) == 0
    try:
        bag.again((broken(),), {"later": 2})
    except ValueError:
        pass
    else:
        raise AssertionError("invalid pair accepted")
    assert bag["first"] == 1 and len(bag) == 1
    print("DICT_SUPER_OK")
main()
'''

MAPPING = '''events = []
class Source:
    @property
    def keys(self):
        events.append(1)
        return self.list_keys
    def list_keys(self):
        events.append(2)
        return ["first", "bad"]
    def __getitem__(self, key):
        events.append(3)
        if key == "bad":
            raise ValueError("mapping failed")
        return 17
class Bag(dict):
    def __init__(self):
        super().__init__()
    def again(self, source):
        return super().__init__(*(source,), later=29)
def main():
    bag = Bag()
    try:
        bag.again(Source())
    except ValueError as error:
        assert str(error) == "mapping failed"
    else:
        raise AssertionError("mapping exception swallowed")
    assert events == [1, 1, 2, 3, 3], events
    assert bag["first"] == 17 and len(bag) == 1
    print("DICT_SUPER_OK")
main()
'''

ORDER = '''events = []
def mark(number, value):
    events.append(number)
    return value
class Bag(dict):
    def __init__(self):
        super().__init__()
    def again(self):
        super().__init__(*mark(1, ({"a": 1},)), **mark(2, {"b": 2}), c=mark(3, 3), **mark(4, {"d": 4}))
    def duplicate(self):
        super().__init__(**mark(5, {"a": 1}), a=mark(6, 2), **mark(7, {"c": 3}))
def main():
    bag = Bag()
    bag.again()
    assert events == [1, 2, 3, 4] and len(bag) == 4
    try:
        bag.duplicate()
    except TypeError:
        pass
    else:
        raise AssertionError("duplicate keyword accepted")
    assert events == [1, 2, 3, 4, 5, 6] and bag["a"] == 1
    print("DICT_SUPER_OK")
main()
'''

SLOTS = '''class Bag(dict):
    __slots__ = ("tag",)
    def __init__(self, source):
        super().__init__(*(source,), extra=2)
        self.tag = 7
def main():
    bag = Bag({"first": 1})
    assert bag["first"] == 1 and bag["extra"] == 2 and bag.tag == 7
    print("DICT_SUPER_OK")
main()
'''

EXCEPTIONS = '''class Problem(Exception):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
    def again(self, args, kwargs):
        return super().__init__(*args, **kwargs)
def main():
    error = Problem(*(1, "two"), **{})
    assert error.args == (1, "two")
    assert error.again(("three",), {}) is None and error.args == ("three",)
    try:
        error.again(("four",), {"bad": 1})
    except TypeError:
        pass
    else:
        raise AssertionError("exception keyword accepted")
    assert error.args == ("three",)
    print("DICT_SUPER_OK")
main()
'''

DEFAULTDICT = '''from collections import defaultdict
def main():
    bag = defaultdict(list, [("ready", [1])], extra=[2])
    assert bag["ready"] == [1] and bag["extra"] == [2]
    bag["new"].append(3)
    assert bag["new"] == [3] and len(bag) == 3
    print("DICT_SUPER_OK")
main()
'''

GC_CALLBACKS = '''import gc
class Key:
    def __init__(self, value):
        self.value = value
    def __hash__(self):
        gc.collect()
        return 1
    def __eq__(self, other):
        gc.collect()
        return self.value == other.value
class Bag(dict):
    def __init__(self, values):
        super().__init__(*(values,), marker=7)
def pairs():
    gc.collect()
    yield (Key(1), [11])
    gc.collect()
    yield (Key(2), [22])
    gc.collect()
    yield (Key(1), [33])
def main():
    bag = Bag(pairs())
    gc.collect()
    assert bag[Key(1)] == [33] and bag[Key(2)] == [22] and bag["marker"] == 7
    print("DICT_SUPER_OK")
main()
'''

EXPLICIT_SUPER = '''events = []
shared = None
def mark():
    events.append(1)
    return {"bad": 2}
class Bag(dict):
    def __init__(self):
        super().__init__()
    @staticmethod
    def reset():
        From = Bag
        return super(From, shared).__init__(*( {"value": 7}, ))
    @staticmethod
    def invalid():
        return super(Bag, 7).__init__(mark())
def main():
    global shared
    shared = Bag()
    assert Bag.reset() is None and shared["value"] == 7
    try:
        Bag.invalid()
    except TypeError:
        pass
    else:
        raise AssertionError("invalid explicit super accepted")
    assert events == []
    print("DICT_SUPER_OK")
main()
'''

PAIR_OWNERS = '''import gc
events = []
class Token:
    def __del__(self):
        events.append("dropped")
class Bag(dict):
    def __init__(self, source):
        super().__init__(*(source,))
def pair():
    yield "key"
    yield 1
    yield Token()
    events.append("resumed")
def main():
    try:
        Bag([pair()])
    except ValueError:
        pass
    else:
        raise AssertionError("three-item pair accepted")
    gc.collect()
    assert events == ["resumed", "dropped"], events
    print("DICT_SUPER_OK")
main()
'''

SLOTTED_OWNERS = '''import gc
import weakref
finalized = []
class Token:
    def __del__(self):
        finalized.append(1)
class Bag(dict):
    __slots__ = ()
    def __init__(self, source):
        super().__init__(*(source,))
class Problem(Exception):
    __slots__ = ()
    def __init__(self, value):
        super().__init__(*(value,))
def main():
    value = Token()
    reference = weakref.ref(value)
    bag = Bag({"value": value})
    del value
    gc.collect()
    assert reference() is bag["value"]
    del bag
    gc.collect()
    assert reference() is None and finalized == [1]
    value = Token()
    reference = weakref.ref(value)
    problem = Problem(value)
    del value
    gc.collect()
    assert reference() is not None
    del problem
    gc.collect()
    assert reference() is None and finalized == [1, 1]
    print("DICT_SUPER_OK")
main()
'''

PROGRAMS = dict(basic=BASIC, errors=ERRORS, mapping=MAPPING, order=ORDER,
                slots=SLOTS, exceptions=EXCEPTIONS, defaultdict=DEFAULTDICT,
                gc_callbacks=GC_CALLBACKS, explicit_super=EXPLICIT_SUPER,
                pair_owners=PAIR_OWNERS, slotted_owners=SLOTTED_OWNERS)


def _emit(source):
    module = type_infer.infer_module(parse_and_lift(source, "dict_super.py", "dict_super"))
    codegen = L1CodeGen(module, ir_scaffold_mode="on")
    codegen._strict_no_libpython = True
    return str(codegen.generate(module))


@pytest.mark.parametrize("name", list(PROGRAMS))
def test_dict_super_reference(name):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exec(PROGRAMS[name], {})
    assert output.getvalue() == "DICT_SUPER_OK\n"


@pytest.mark.parametrize("name", [name for name in PROGRAMS if name != "defaultdict"])
def test_dict_super_owned_ir(name):
    text = _emit(PROGRAMS[name])
    expected = "py_exception_subclass_init_slots" if name == "exceptions" else "py_dict_subclass_init_slots"
    assert re.search(r"call i64[^\n]*@" + expected + r"\(", text)
    assert "super.init.args" in text and "super.init.from" in text
    assert "name '*' is not defined" not in text
    assert "name '**' is not defined" not in text


def test_unknown_foreign_super_initializer_fails_explicitly():
    with pytest.raises(L1CodegenError, match="no implemented initializer"):
        _emit("""class Bag(list):
    def __init__(self, values):
        super().__init__(*values)
""")


@pytest.mark.integration
@pytest.mark.parametrize("name", list(PROGRAMS))
def test_dict_super_native(tmp_path, pcc_runtime_archive, name):
    source = tmp_path / ("dict_super_" + name + ".py")
    output = source.with_suffix("")
    source.write_text(PROGRAMS[name])
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        environment = dict(os.environ, PATH="", PCC_HOST_PYTHON="/usr/bin/false",
                           PCC_HOST_PCC="/usr/bin/false", PCC_GC_BACKEND=str(backend))
        environment.pop("LC_ALL", None)
        result = subprocess.run([str(output)], env=environment, capture_output=True,
                                text=True, timeout=20)
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stdout == "DICT_SUPER_OK\n" and result.stderr == ""



def test_super_initializer_helpers_have_native_exports():
    from pcc.frontends.python.codegen.host_contract import L1_CODEGEN_HOST_METHODS
    from pcc.frontends.python.codegen._l1_codegen_static_methods import L1_CODEGEN_STATIC_METHODS
    from pcc.frontends.python.codegen.layer1_support import _default_native_module_exports
    static = {entry["name"]: entry for entry in L1_CODEGEN_STATIC_METHODS}
    exports = _default_native_module_exports("pcc.frontends.python.codegen.layer1")
    native = {entry["name"]: entry for entry in exports["pcc.frontends.python.codegen.layer1"]["L1CodeGen"]["methods"]}
    signatures = {
        "_foreign_super_initializer_base": ("self", "info"),
        "_emit_foreign_super_init_slots": ("self", "expr", "from_class", "super_args", "base"),
    }
    for name, parameters in signatures.items():
        assert name in L1_CODEGEN_HOST_METHODS
        assert static[name] == native[name]
        assert tuple(parameter["name"] for parameter in static[name]["call_sig"]) == parameters
        assert tuple(parameter["has_default"] for parameter in static[name]["call_sig"]) == (False,) * len(parameters)
