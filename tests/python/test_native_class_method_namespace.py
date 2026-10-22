"""Class namespaces expose their own unbound methods for MRO collection."""
import ast
import contextlib
import io
import os
from pathlib import Path
from types import ModuleType

import pytest
import subprocess
import sys
from pcc.frontends.python.pipeline import compile_python
from pcc.runtime.py import py_abi_constants as abi
from tests.python.test_set_call_slot_roots import Block, Memory, Object
from tests.python.owned_regression_support import assert_owned_program, explicit_owned_runtime


def test_class_namespace_method_collection_matches_cpython(tmp_path, pcc_runtime_archive):
    source = tmp_path / 'method_namespace.py'
    source.write_text('''
import gc
class Base:
    def p_first(self, value):
        return value + 1
    def p_second(self, value):
        return value * 2
class Derived(Base):
    def p_first(self, value):
        return value + 2

def collect(instance):
    table = {}
    cls = type(instance)
    for base in cls.__mro__:
        for name, function in base.__dict__.items():
            if name.startswith("p_") and callable(function):
                table.setdefault(name, function)
    return table

def main():
    obj = Derived()
    table = collect(obj)
    print(sorted(table))
    print("p_first" in Derived.__dict__, "p_second" in Derived.__dict__)
    gc.collect()
    print(table["p_first"](obj, 20), table["p_second"](obj, 21))
    print(getattr(obj, "p_first")(20), getattr(obj, "p_second")(21))
main()
''', encoding='utf-8')
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=10)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / 'method_namespace'
    compile_python(str(source), str(binary), backend='self', libpython_mode='off',
                   runtime_archive=str(pcc_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f'GC{gc}: {result.stderr}; {result.stdout}'
        assert result.stdout == expected.stdout, f'GC{gc}: {result.stdout}'


# The following tests execute the production reflection and owner-frame bodies
# against the existing deterministic moving-memory model. Special-method binding
# and sorting are independent runtime contracts; their callback stubs here use
# CPython as a semantic oracle, and are not native qualification.


class DirMemory(Memory):
    def __init__(self, phase="none", failure=None):
        super().__init__(phase, fail_copy=1 if failure == "copy" else 0,
                         fail_register=3 if failure == "register" else 0)
        self.failure = failure
        self.module_class = self.make("module", abi.PY_TYPE_CLASS)
        self.module_class.leases = 1000
        self.module_class_root = Block()
        self.write(self.module_class_root, 0, self.module_class)
        self.ns.update({name: getattr(abi, name) for name in dir(abi) if name.isupper()})
        self.ns.update({
            "pcc_gc_root_copy_borrowed_lease": self.copy,
            "pcc_gc_note_slot_write_barrier": lambda *_: None,
            "py_tls_exc_swap_slot": self.swap_error,
            "py_runtime_error_if_unset": self.runtime_error,
            "_special_native_instance": lambda obj: isinstance(obj, Object) and obj.tag == abi.PY_TYPE_INSTANCE,
            "global_load_ptr": lambda name: self.module_class if name == "pcc_runtime_module_class_cache" else self.none,
            "global_addr": lambda name: self.module_class_root if name == "pcc_runtime_module_class_cache" else None,
            "py_obj_special_call_slots": self.special_call,
            "py_obj_call_slots": self.ordinary_call,
            "py_obj_getattr": self.getattr,
            "py_str_new": lambda value, length: self.make(value[:length], abi.PY_TYPE_STR),
            "py_dict_get": self.dict_get,
            "py_dict_keys": self.dict_keys,
            "py_obj_sorted_slots": self.sorted,
            "pcc_platform_abort": lambda: (_ for _ in ()).throw(AssertionError("lease invariant")),
        })
        path = Path(__file__).resolve().parents[2] / "pcc/runtime/py/py_class.py"
        tree = ast.parse(path.read_text())
        names = {"_special_open", "_special_error", "_special_copy", "_special_adopt",
                 "_special_drop", "_special_close", "_special_publish", "py_obj_dir_slots"}
        nodes = []
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id.startswith("_DIR_"):
                self.ns[node.targets[0].id] = ast.literal_eval(node.value)
            if isinstance(node, ast.FunctionDef) and (node.name.startswith("_dir_") or node.name in names):
                node.decorator_list = []
                nodes.append(node)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), self.ns)

    def store_root(self, slot, value):
        old = self.read(slot, 0)
        super().store_root(slot, value)
        if self.failure == "cleanup" and isinstance(old, Object) and value is None:
            self.error = (RuntimeError, "cleanup callback")

    def runtime_error(self, helper, message):
        if self.error is None:
            self.error = (15, message)

    def swap_error(self, slot):
        error = self.read(slot, 0)
        self.write(slot, 0, self.error)
        self.error = error

    def managed(self, value):
        if value is None:
            return self.none
        return self.make(value, abi.PY_TYPE_DICT if isinstance(value, dict) else abi.PY_TYPE_LIST)

    def invoke(self, callback, result):
        self.collect("callback")
        try:
            self.write(result, 0, self.managed(callback()))
            return 0
        except Exception as error:
            self.error = (type(error), str(error))
            return -1

    def special_call(self, source, name, args, kwargs, result, handled):
        assert name == "__dir__" and args is None and kwargs is None
        receiver = self.read(source, 0)
        assert receiver.alive and receiver.leases > 0
        value = receiver.value
        descriptor = next((base.__dict__[name] for base in type(value).__mro__ if name in base.__dict__), None)
        if descriptor in (object.__dir__, type.__dir__, ModuleType.__dir__):
            self.write(handled, 0, 0)
            return 0
        self.write(handled, 0, 1)
        def call():
            bound = descriptor.__get__(value, type(value)) if hasattr(descriptor, "__get__") else descriptor
            return bound()
        return self.invoke(call, result)

    def ordinary_call(self, method, args, kwargs, result):
        assert args is None and kwargs is None
        value = self.read(method, 0)
        assert value.alive and value.leases > 0
        return self.invoke(value.value, result)

    def getattr(self, receiver, name):
        assert receiver.alive and receiver.leases > 0 and name == "__dict__"
        self.collect("callback")
        if self.failure == "lookup":
            self.error = (ValueError, "lookup failed")
            return None
        return self.managed(getattr(receiver.value, name))

    def dict_get(self, dictionary, key):
        assert dictionary.alive and dictionary.leases > 0
        assert key.alive and key.leases > 0
        if key.value not in dictionary.value:
            return None
        return self.managed(dictionary.value[key.value])

    def dict_keys(self, dictionary):
        assert dictionary.alive and dictionary.leases > 0
        self.collect("callback")
        if self.failure == "keys":
            self.error = (MemoryError, "keys failed")
            return None
        return self.managed(list(dictionary.value))

    def sorted(self, source, key, compare, reverse, result):
        value = self.read(source, 0)
        assert value.alive and value.leases > 0
        assert key is None and compare is None and reverse == 0
        return self.invoke(lambda: sorted(value.value), result)

    def run(self, receiver, pending=None):
        cls = self.module_class if type(receiver) is ModuleType else self.make("ordinary", abi.PY_TYPE_CLASS)
        bases = [cls]
        if isinstance(receiver, ModuleType) and cls is not self.module_class:
            bases.append(self.module_class)
        cls.fields[abi.PYCLASSOBJECT_N_MRO_OFFSET] = len(bases)
        cls.fields[abi.PYCLASSOBJECT_MRO_OFFSET] = Block()
        cls.fields[abi.PYCLASSOBJECT_MRO_OFFSET].fields = {index * abi.C_POINTER_SIZE: base for index, base in enumerate(bases)}
        value = self.make(receiver, abi.PY_TYPE_INSTANCE)
        value.fields[abi.PYINSTANCEOBJECT_CLS_OFFSET] = cls
        caller = Block()
        self.write(caller, 0, value)
        self.write(caller, 8, None)
        self.roots[object()] = caller
        self.roots[object()] = self.add(caller, 8)
        self.error = pending
        status = self.ns["py_obj_dir_slots"](caller, self.add(caller, 8))
        result = self.read(caller, 8)
        assert len(self.roots) == 2 and self.depth == 0
        assert all(obj.leases == (1000 if obj in (self.none, self.module_class) else 0) for obj in self.objects if obj.alive)
        assert self.read(caller, 0).refs == 1
        if result is not None:
            assert result.refs == 1
        return status, result.value if result is not None else None


@pytest.mark.parametrize("phase", ("register", "copy", "acquire", "callback", "release", "move", "drop", "unregister", "lock"))
def test_dir_module_production_bodies_move_at_every_boundary(phase):
    module = ModuleType("grammar")
    module.zeta, module.alpha = 9, 1
    memory = DirMemory(phase)
    status, result = memory.run(module)
    assert status == 0 and result == dir(module) and memory.error is None
    assert memory.collections > 0


@pytest.mark.parametrize("kind", ("instance", "metaclass", "module", "descriptor", "ignored-instance-binding"))
def test_dir_custom_result_is_sorted_without_mutating_source(kind):
    original = ["zeta", "alpha", "alpha"]
    class Custom:
        def __dir__(self):
            return original
    class Meta(type):
        def __dir__(cls):
            return tuple(original)
    class Class(metaclass=Meta):
        pass
    class Descriptor:
        def __get__(self, receiver, owner):
            return lambda: iter(original)
    class Described:
        __dir__ = Descriptor()
    value = {"instance": Custom(), "metaclass": Class, "descriptor": Described()}.get(kind)
    if kind == "module":
        value = ModuleType("grammar")
        value.__dir__ = lambda: original
    elif kind == "ignored-instance-binding":
        value = Custom()
        value.__dir__ = lambda: ["wrong"]
    memory = DirMemory("callback")
    assert memory.run(value) == (0, sorted(original))
    assert original == ["zeta", "alpha", "alpha"]


@pytest.mark.parametrize("failure", ("register", "copy", "lookup", "keys"))
def test_dir_failure_drops_all_scratch_owners_and_preserves_error(failure):
    memory = DirMemory("release", failure)
    status, result = memory.run(ModuleType("grammar"))
    assert status == -1 and result is None and memory.error is not None


@pytest.mark.parametrize("callback", (lambda: 42, lambda: ["name", 3], None))
def test_dir_invalid_module_override_is_a_real_error(callback):
    module = ModuleType("grammar")
    module.__dir__ = callback
    memory = DirMemory("release")
    status, result = memory.run(module)
    assert status == -1 and result is None and memory.error[0] is TypeError


def test_dir_custom_error_and_pending_exception_keep_identity():
    class Broken:
        def __dir__(self):
            raise ValueError("selected callback")
    memory = DirMemory("release")
    status, result = memory.run(Broken(), pending=(KeyError, "outer"))
    assert (status, result, memory.error) == (-1, None, (ValueError, "selected callback"))
    memory = DirMemory("release")
    pending = (KeyError, "outer")
    assert memory.run(ModuleType("grammar"), pending=pending)[0] == 0
    assert memory.error is pending


@pytest.mark.parametrize("value", (object(), type("Plain", (), {})(), type("Class", (), {}), 42, []))
def test_default_dir_fails_explicitly_until_namespaces_are_complete(value):
    memory = DirMemory("release")
    status, result = memory.run(value)
    assert status == -1 and result is None
    assert "complete class and builtin namespaces" in memory.error[1]


DIR_MODULE_SOURCE = '''alpha = 1
zeta = 9
def rule(value):
    return value + 1
'''

DIR_NATIVE_PROGRAM = '''import gc
import dir_fixture

def collect(module):
    _items = [(k, getattr(module, k)) for k in dir(module)]
    return _items

class Custom:
    def __dir__(self):
        gc.collect()
        return ["zeta", "alpha", "alpha"]
class Meta(type):
    def __dir__(cls):
        gc.collect()
        return ("zeta", "alpha")
class CustomClass(metaclass=Meta):
    pass
class Raising:
    def __dir__(self):
        raise ValueError("reflection failed")
class Bad:
    def __dir__(self):
        return 42

def module_names():
    gc.collect()
    return ["zeta", "alpha"]
def parameter(dir, value):
    return dir(value)
def replacement(value):
    return ["replacement"]
def take(*, value, later=None, **extras):
    return value
def later_error():
    gc.collect()
    raise ValueError("later")

def main():
    names = dir(dir_fixture)
    assert names == sorted(names)
    assert "alpha" in names and "zeta" in names and "rule" in names
    items = collect(dir_fixture)
    values = dict(items)
    assert values["alpha"] == 1 and values["zeta"] == 9
    assert values["rule"](4) == 5
    setattr(dir_fixture, "later", 17)
    assert "later" in dir(dir_fixture)
    delattr(dir_fixture, "later")
    assert "later" not in dir(dir_fixture)
    assert dir(Custom()) == ["alpha", "alpha", "zeta"]
    assert dir(CustomClass) == ["alpha", "zeta"]
    assert parameter(replacement, dir_fixture) == ["replacement"]
    setattr(dir_fixture, "__dir__", module_names)
    assert dir(dir_fixture) == ["alpha", "zeta"]
    assert collect(dir_fixture) == [("alpha", 1), ("zeta", 9)]
    assert take(value=dir(Custom())) == ["alpha", "alpha", "zeta"]
    try:
        take(value=dir(Custom()), later=later_error())
    except ValueError as error:
        assert str(error) == "later"
    else:
        assert False
    try:
        dir(Raising())
    except ValueError as error:
        assert str(error) == "reflection failed"
    else:
        assert False
    try:
        dir(Bad())
    except TypeError:
        pass
    else:
        assert False
    setattr(dir_fixture, "__dir__", None)
    try:
        dir(dir_fixture)
    except TypeError:
        pass
    else:
        assert False
    delattr(dir_fixture, "__dir__")
    assert "rule" in dir(dir_fixture)
    dir(Custom())
    gc.collect()
    print("DIR_OWNERSHIP_OK")
main()
'''


def test_dir_native_witness_reference(tmp_path, monkeypatch):
    fixture = tmp_path / "dir_fixture.py"
    fixture.write_text(DIR_MODULE_SOURCE)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "dir_fixture", raising=False)
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        exec(DIR_NATIVE_PROGRAM, {})
    assert stream.getvalue() == "DIR_OWNERSHIP_OK\n"
    monkeypatch.delitem(sys.modules, "dir_fixture", raising=False)


@pytest.mark.integration
@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_dir_native_five_gc(python_program_compiler, request, explicit_owned_runtime,
                            tmp_path, capfd):
    (tmp_path / "dir_fixture.py").write_text(DIR_MODULE_SOURCE)
    mode = request.node.callspec.params["python_program_compiler"]
    assert_owned_program(DIR_NATIVE_PROGRAM, "DIR_OWNERSHIP_OK\n", tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime,
                         capfd, provenance_probe="2")


def test_dir_module_subclass_uses_canonical_identity_in_mro():
    class Module(ModuleType):
        pass
    module = Module("grammar")
    module.alpha = 1
    memory = DirMemory("release")
    assert memory.run(module) == (0, dir(module))
    # A matching class name alone never grants the module capability.
    fake = type("module", (), {})()
    assert DirMemory().run(fake)[0] == -1


def test_dir_lookup_binding_failure_survives_cleanup_callbacks():
    class Descriptor:
        def __get__(self, receiver, owner):
            raise ValueError("binding failed")
    class Value:
        __dir__ = Descriptor()
    memory = DirMemory("release", failure="cleanup")
    assert memory.run(Value()) == (-1, None)
    assert memory.error == (ValueError, "binding failed")


def test_dir_rejects_missing_source_and_occupied_result_without_mutation():
    memory = DirMemory()
    slot = Block()
    memory.write(slot, 0, memory.none)
    assert memory.ns["py_obj_dir_slots"](slot, slot) == -1
    assert memory.read(slot, 0) is memory.none
    assert memory.ns["py_obj_dir_slots"](None, slot) == -1
    assert memory.ns["py_obj_dir_slots"](slot, None) == -1
    assert not memory.roots


def _dir_lock_probe(text, move, missing=False):
    receiver = object()
    original_class = object()
    relocated_class = object()
    mro = object()
    class_offset, mro_length_offset, mro_offset = 1, 2, 3
    state = {
        "locked": False,
        "class": None if missing else original_class,
        "events": [],
    }

    def lock():
        state["events"].append("acquire")
        if move and not missing:
            state["class"] = relocated_class
        state["locked"] = True

    def unlock():
        state["events"].append("release")
        state["locked"] = False

    def global_load(name):
        state["events"].append("global-after" if state["locked"] else "global-before")
        return state["class"]

    def read(owner, slot):
        assert state["locked"]
        base, offset = slot
        if base == "global":
            return state["class"]
        if base is receiver and offset == class_offset:
            return state["class"]
        if base is mro and offset == 0:
            return state["class"]
        raise AssertionError(slot)

    namespace = {
        "_special_native_instance": lambda _: True,
        "global_load_ptr": global_load,
        "global_addr": lambda _: ("global", 0),
        "null": lambda: None,
        "ptr_is_null": lambda value: int(value is None),
        "pcc_py_gc_minor_graph_lock": lock,
        "pcc_py_gc_minor_graph_unlock": unlock,
        "pcc_gc_load_ptr": read,
        "ptr_add": lambda pointer, offset: (pointer, offset),
        "load_i32": lambda pointer, offset: 1,
        "load_ptr": lambda pointer, offset: mro,
        "ptr_eq": lambda first, second: int(first is second),
        "PYINSTANCEOBJECT_CLS_OFFSET": class_offset,
        "PYCLASSOBJECT_N_MRO_OFFSET": mro_length_offset,
        "PYCLASSOBJECT_MRO_OFFSET": mro_offset,
        "C_POINTER_SIZE": 8,
    }
    node = next(
        node for node in ast.parse(text).body
        if isinstance(node, ast.FunctionDef) and node.name == "_dir_is_module"
    )
    exec(compile(ast.Module(body=[node], type_ignores=[]), "actual_dir_body", "exec"), namespace)
    result = namespace["_dir_is_module"](receiver)
    assert not state["locked"]
    return {"result": result, "events": state["events"]}


@pytest.mark.parametrize("move,missing", ((False, False), (True, False), (True, True)))
def test_dir_loads_canonical_module_class_after_parking_lock(move, missing):
    path = Path(__file__).resolve().parents[2] / "pcc/runtime/py/py_class.py"
    result = _dir_lock_probe(path.read_text(), move, missing)
    assert result["result"] == (0 if missing else 1)
    assert result["events"][0] == "acquire"
    assert result["events"][-1] == "release"
