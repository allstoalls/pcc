"""Object-root source-body and IR checks; native coverage is opt-in."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re

import pytest

from pcc.runtime.py import py_abi_constants as abi
from tests.python.test_foreign_address_leases import _functions
from tests.python.test_shared_call_binding import _emit, _function
from tests.python.owned_regression_support import (
    assert_owned_program,
    assert_reference_program,
    explicit_owned_runtime,
)

RUNTIME = Path(__file__).resolve().parents[2] / "pcc/runtime/py"


@dataclass(eq=False)
class _Allocation:
    size: int
    fields: dict = field(default_factory=dict)
    capi_type: bool = False


class _PredicateModel:
    """Execute production bodies with raw memory/refcount operations modeled."""

    def __init__(self):
        self.globals = {}
        self.allocations = []
        self.retains = []
        self.releases = []
        self.errors = []
        self.fail_allocation = False
        self.ns = {name: getattr(abi, name) for name in dir(abi) if name.isupper()}
        self.ns.update(
            null=lambda: None,
            cstr=lambda value: value,
            ptr_is_null=lambda value: int(value is None),
            ptr_eq=lambda first, second: int(first is second),
            is_tagged_int=lambda value: int(isinstance(value, int)),
            global_addr=lambda name: name,
            global_load_ptr=lambda name: self.globals.get(name),
            global_store_ptr=self.globals.__setitem__,
            malloc=self.allocate,
            free=lambda value: None,
            memset=lambda value, fill, size: value.fields.clear(),
            load_i32=self.load,
            load_i64=self.load,
            load_ptr=self.load,
            store_i32=self.store,
            store_i64=self.store,
            store_ptr=self.store,
            ptr_add=lambda value, offset: (value, offset),
            atomic_rmw_i32=self.rmw,
            pcc_gc_pointer_register=lambda value: 0,
            pcc_gc_note_relocation_read=lambda value: value,
            pcc_gc_load_ptr=self.gc_load,
            pcc_py_gc_minor_graph_lock=lambda: None,
            pcc_py_gc_minor_graph_unlock=lambda: None,
            pcc_capi_is_cext_type_tag=lambda tag: int(tag >= abi.PY_TYPE_CEXT_TAG_BASE),
            _type_of=self.type_of,
            _ptr_can_have_header=lambda value: isinstance(value, _Allocation),
            _ptr_is_class_of_validated_instance=lambda value: self.type_of(value) == abi.PY_TYPE_CLASS,
            pcc_capi_is_type_object_value=lambda value: int(isinstance(value, _Allocation) and value.capi_type),
            pcc_capi_type_object_issubclass=lambda derived, cls: int(derived is cls),
            pcc_diagnostics_runtime_log_event_code=lambda *args: None,
            py_tuple_len=lambda value: self.load(value, 16),
            py_tuple_get=self.tuple_get,
            py_incref=self.retains.append,
            py_decref=self.releases.append,
            py_exc_new=lambda tag, message: (tag, message),
            py_raise_owned=self.errors.append,
            _type_error=lambda message: self.errors.append((3, message)),
            py_exc_matches=lambda value, cls: 0,
            py_class_new=lambda name, *args: self.klass(name),
        )
        _functions(RUNTIME / "py_substrate.py", {"py_subs_object_root"}, self.ns)
        _functions(RUNTIME / "py_class.py", {"_object_root", "py_isinstance"}, self.ns)
        _functions(RUNTIME / "py_obj_ops_dispatch.py", {
            "_builtin_type_class_for_tag", "_return_builtin_type",
            "py_obj_isinstance", "py_obj_issubclass", "py_builtin_type_class_tag",
        }, self.ns)
        _functions(RUNTIME / "py_weakref.py", {"_weakref_target_is_base_object"}, self.ns)
        _functions(RUNTIME / "py_capi_object_runtime.py", {"PyObject_IsInstance"}, self.ns)

    def allocate(self, size):
        if self.fail_allocation:
            return None
        result = _Allocation(size)
        self.allocations.append(result)
        return result

    def load(self, value, offset):
        return value.fields.get(offset, 0)

    def store(self, value, offset, item):
        value.fields[offset] = item

    def gc_load(self, owner, slot):
        return self.globals.get(slot) if isinstance(slot, str) else self.load(*slot)

    def rmw(self, operation, value, offset, bits, order):
        assert operation == "or" and order == "relaxed"
        previous = self.load(value, offset)
        self.store(value, offset, previous | bits)
        return previous

    def type_of(self, value):
        return abi.PY_TYPE_INT if isinstance(value, int) else self.load(value, 8)

    def heap(self, tag):
        result = self.allocate(abi.PYCLASSOBJECT_SIZE)
        self.store(result, 8, tag)
        return result

    def klass(self, name="Child", base=None):
        if base is None:
            base = self.ns["_object_root"]()
        result = self.heap(abi.PY_TYPE_CLASS)
        self.store(result, abi.PYCLASSOBJECT_NAME_OFFSET, name)
        mro = self.allocate(2 * abi.C_POINTER_SIZE)
        self.store(mro, 0, result)
        self.store(mro, abi.C_POINTER_SIZE, base)
        self.store(result, abi.PYCLASSOBJECT_N_MRO_OFFSET, 2)
        self.store(result, abi.PYCLASSOBJECT_MRO_OFFSET, mro)
        return result

    def instance(self, cls):
        result = self.heap(abi.PY_TYPE_USER_CLASS_START)
        self.store(result, abi.PYINSTANCEOBJECT_CLS_OFFSET, cls)
        return result

    def tuple(self, *members):
        result = self.heap(abi.PY_TYPE_TUPLE)
        self.store(result, 16, len(members))
        for index, value in enumerate(members):
            self.store(result, 24 + index * abi.C_POINTER_SIZE, value)
        return result

    def tuple_get(self, value, index):
        result = self.load(value, 24 + index * abi.C_POINTER_SIZE)
        self.retains.append(result)
        return result

    def root(self):
        return self.ns["_builtin_type_class_for_tag"](-1)


@pytest.mark.parametrize("first_provider", ("_object_root", "py_subs_object_root", None))
def test_object_builtin_reuses_the_mro_root_and_owned_return(first_provider):
    model = _PredicateModel()
    earlier = model.ns[first_provider]() if first_provider else None
    root = model.root()
    assert root is model.globals["py_object_root_cache"]
    if earlier is not None:
        assert root is earlier
    assert model.ns["_object_root"]() is root
    assert model.ns["py_subs_object_root"]() is root
    assert model.load(root, abi.PYCLASSOBJECT_N_MRO_OFFSET) == 1
    assert model.load(model.load(root, abi.PYCLASSOBJECT_MRO_OFFSET), 0) is root
    assert model.load(root, abi.PYOBJECTHEADER_FLAGS_OFFSET) == (
        abi.PY_FLAG_IMMORTAL | abi.PY_FLAG_GC_MALLOC_ALLOC | 2
    )
    assert model.root() is root
    assert model.retains == [root, root]
    assert len(model.allocations) == 2


def test_object_root_allocation_failure_is_not_published_or_retained():
    model = _PredicateModel()
    model.fail_allocation = True
    assert model.root() is None
    assert not model.globals and not model.retains


def test_mro_root_identity_works_before_the_builtin_alias_cache_is_created():
    model = _PredicateModel()
    child = model.klass()
    root = model.globals["py_object_root_cache"]
    assert "pcc_type_cls_object" not in model.globals
    assert model.load(root, abi.PYOBJECTHEADER_FLAGS_OFFSET) & 2
    assert model.ns["py_builtin_type_class_tag"](root) == -1
    for value in (7, model.heap(abi.PY_TYPE_STR), child, root):
        assert model.ns["py_obj_isinstance"](value, root) == 1
    assert model.ns["py_obj_issubclass"](child, root) == 1
    assert model.ns["_weakref_target_is_base_object"](model.instance(root)) == 1
    assert model.ns["_weakref_target_is_base_object"](model.instance(child)) == 0
    assert not model.errors


@pytest.mark.parametrize("tag", (
    abi.PY_TYPE_NONE, abi.PY_TYPE_BOOL, abi.PY_TYPE_INT, abi.PY_TYPE_FLOAT,
    abi.PY_TYPE_STR, abi.PY_TYPE_LIST, abi.PY_TYPE_DICT, abi.PY_TYPE_TUPLE,
    abi.PY_TYPE_SET, abi.PY_TYPE_BYTES, abi.PY_TYPE_BYTEARRAY,
    abi.PY_TYPE_FUNC, abi.PY_TYPE_CLASS, abi.PY_TYPE_EXC,
    abi.PY_TYPE_INSTANCE, abi.PY_TYPE_USER_CLASS_START,
    abi.PY_TYPE_CEXT_TAG_BASE,
))
def test_every_heap_value_is_an_object_without_an_instance_layout_probe(tag):
    model = _PredicateModel()
    assert model.ns["py_obj_isinstance"](model.heap(tag), model.root()) == 1
    assert not model.errors


def test_tagged_ints_and_class_objects_are_objects():
    model = _PredicateModel()
    root = model.root()
    for value in (0, 42, -7, root, model.klass()):
        assert model.ns["py_obj_isinstance"](value, root) == 1
    assert not model.errors


@pytest.mark.parametrize("capi_type", (False, True))
def test_dynamic_class_objects_are_subclasses_of_object(capi_type):
    model = _PredicateModel()
    derived = model.heap(abi.PY_TYPE_CEXT_TAG_BASE) if capi_type else model.klass()
    derived.capi_type = capi_type
    assert model.ns["py_obj_issubclass"](derived, model.root()) == 1
    assert not model.errors


def test_extension_classinfo_keeps_its_existing_nonmatching_path():
    model = _PredicateModel()
    extension_class = model.heap(abi.PY_TYPE_CEXT_TAG_BASE)
    extension_class.capi_type = True
    assert model.ns["py_obj_isinstance"](7, extension_class) == 0
    assert not model.errors


@pytest.mark.parametrize("predicate", ("py_obj_isinstance", "py_obj_issubclass"))
@pytest.mark.parametrize("invalid_kind", ("none", "tagged", "heap"))
def test_invalid_classinfo_raises_type_error(predicate, invalid_kind):
    model = _PredicateModel()
    invalid = {"none": model.heap(abi.PY_TYPE_NONE), "tagged": 7,
               "heap": model.heap(abi.PY_TYPE_LIST)}[invalid_kind]
    assert model.ns[predicate](model.klass(), invalid) == -1
    name = "isinstance" if predicate == "py_obj_isinstance" else "issubclass"
    noun = "type" if name == "isinstance" else "class"
    plural = "types" if noun == "type" else "classes"
    assert model.errors == [(3, name + "() arg 2 must be a " + noun
                              + ", a tuple of " + plural + ", or a union")]


def test_internal_absent_class_cache_remains_a_nonmatch():
    model = _PredicateModel()
    assert model.ns["py_obj_isinstance"](model.heap(abi.PY_TYPE_INT), None) == 0
    assert not model.errors


def test_capi_isinstance_preserves_success_nonmatch_and_error():
    model = _PredicateModel()
    root = model.root()
    custom = model.klass()
    assert model.ns["PyObject_IsInstance"](7, root) == 1
    assert model.ns["PyObject_IsInstance"](7, custom) == 0
    assert model.ns["PyObject_IsInstance"](7, 7) == -1
    assert len(model.errors) == 1 and model.errors[0][0] == 3


@pytest.mark.parametrize("predicate", ("py_obj_isinstance", "py_obj_issubclass"))
def test_object_tuple_classinfo_short_circuits_in_source_order(predicate):
    model = _PredicateModel()
    operand = model.klass()
    root = model.root()
    initial_retains = len(model.retains)
    match_first = model.tuple(model.tuple(), model.tuple(root, 7))
    assert model.ns[predicate](operand, match_first) == 1
    assert not model.errors
    assert model.ns[predicate](operand, model.tuple(7, root)) == -1
    assert len(model.errors) == 1 and model.errors[0][0] == 3
    assert sorted(map(id, model.retains[initial_retains:])) == sorted(map(id, model.releases))


@pytest.mark.parametrize("invalid_kind", ("null", "tagged", "heap"))
@pytest.mark.parametrize("tuple_form", (False, True))
def test_issubclass_validates_first_argument_before_object_match(invalid_kind, tuple_form):
    model = _PredicateModel()
    invalid = {"null": None, "tagged": 7, "heap": model.heap(abi.PY_TYPE_LIST)}[invalid_kind]
    root = model.root()
    classinfo = model.tuple(model.tuple(), model.tuple(root)) if tuple_form else root
    assert model.ns["py_obj_issubclass"](invalid, classinfo) == -1
    assert model.errors == [(3, "issubclass() arg 1 must be a class")]


@pytest.mark.parametrize("nested", (False, True))
def test_issubclass_empty_tuple_does_not_validate_first_argument(nested):
    model = _PredicateModel()
    classinfo = model.tuple(model.tuple()) if nested else model.tuple()
    reference = ((),) if nested else ()
    assert issubclass(7, reference) is False
    assert model.ns["py_obj_issubclass"](7, classinfo) == 0
    assert not model.errors


def test_user_class_named_object_does_not_become_universal():
    model = _PredicateModel()
    root = model.root()
    shadow = model.klass("object")
    child = model.klass(base=shadow)
    unrelated = model.klass()
    assert model.ns["py_obj_isinstance"](model.instance(shadow), shadow) == 1
    assert model.ns["py_obj_isinstance"](model.instance(unrelated), shadow) == 0
    assert model.ns["py_obj_isinstance"](7, shadow) == 0
    assert model.ns["py_obj_issubclass"](child, shadow) == 1
    assert model.ns["py_obj_issubclass"](unrelated, shadow) == 0
    assert model.ns["py_obj_issubclass"](root, shadow) == 0


@pytest.mark.parametrize("classinfo", ("object", "(object,)", "(str, object)", "(object, str)"))
def test_builtin_object_check_is_true_but_still_evaluates_operand_once(classinfo):
    body = _function(_emit("def make():\n    return [1]\ndef probe():\n    return isinstance(make(), " + classinfo + ")\n"))
    assert len(re.findall(r"call[^\n]*@user_binding_make\(", body)) == 1
    assert re.search(r"zext i1 1 to i32|icmp ne i1 1, 0|or i1 (?:[^\n]*, 1|1,)", body)
    assert "@py_obj_type_tag(" not in body or "str" in classinfo


@pytest.mark.parametrize("classinfo", ("object", "(object,)", "(str, object)"))
def test_shadowed_object_classinfo_keeps_runtime_identity(classinfo):
    body = _function(_emit("def probe(value, object):\n    return isinstance(value, " + classinfo + ")\n"))
    assert "@py_obj_isinstance(" in body


@pytest.mark.parametrize("prefix, assignment, classinfo, runtime", (
    ("import builtins\n", "", "builtins.object", False),
    ("import builtins as provider\n", "", "(provider.object,)", False),
    ("from builtins import object as Root\n", "", "Root", True),
    ("", "    root = object\n", "root", True),
    ("", "    root = object\n", "(str, root)", True),
    ("class Other:\n    pass\nobject = Other\n", "", "object", True),
    ("class object:\n    pass\n", "", "object", True),
))
def test_object_aliases_and_bindings_preserve_identity(prefix, assignment, classinfo, runtime):
    body = _function(_emit(prefix + "def probe(value):\n" + assignment
                           + "    return isinstance(value, " + classinfo + ")\n"))
    assert ("@py_obj_isinstance(" in body) is runtime
    if not runtime:
        assert "zext i1 1 to i32" in body


def test_attribute_named_object_is_not_assumed_to_be_the_builtin():
    body = _function(_emit("def probe(value, provider):\n    return isinstance(value, provider.object)\n"))
    assert "@py_obj_isinstance(" in body
    assert "@py_obj_getattr(" in body


def test_integer_classinfo_is_boxed_before_runtime_validation():
    body = _function(_emit("def probe(value, kind: int):\n    return isinstance(value, kind)\n"))
    assert re.search(r"@py_obj_isinstance\(ptr [^,]+, ptr [^)]+\)", body)


@pytest.mark.parametrize("expression", (
    "builtins.object", "(builtins.object,)", "(kind, builtins.object)",
))
@pytest.mark.parametrize("predicate", ("isinstance", "issubclass"))
def test_builtin_module_object_operands_stay_native(expression, predicate):
    body = _function(_emit("import builtins\ndef probe(value, kind):\n"
                           "    return " + predicate + "(value, " + expression + ")\n"))
    assert not re.search(r"\bcall\b[^\n]*@py_cpy_", body)
    if predicate == "issubclass" or "kind" in expression:
        assert "@py_builtin_type_for_tag(i64 -1)" in body
        assert "@py_obj_" + predicate + "(" in body


@pytest.mark.parametrize("predicate", ("isinstance", "issubclass"))
def test_builtin_module_object_override_is_read_as_a_value(predicate):
    body = _function(_emit("import builtins\nclass Replacement:\n    pass\n"
                           "builtins.object = Replacement\ndef probe(value):\n"
                           "    return " + predicate + "(value, builtins.object)\n"))
    assert "@.modattr.builtins.object" in body
    assert "@py_obj_" + predicate + "(" in body
    assert "@py_builtin_type_for_tag(i64 -1)" not in body


@pytest.mark.parametrize("signature,arguments", (
    ("A, object", "A, object"),
    ("object", "A, object"),
    ("A", "A, object"),
    ("value, kind", "value, kind"),
))
def test_issubclass_uses_runtime_bindings_even_when_names_match_known_classes(signature, arguments):
    body = _function(_emit("class A:\n    pass\ndef probe(" + signature + "):\n    return issubclass(" + arguments + ")\n"))
    assert "@py_obj_issubclass(" in body


def test_issubclass_roots_both_operands_and_cleans_up_the_predicate_error_path():
    body = _function(_emit("def probe(value, provider):\n    return issubclass(value, (provider.kind, object))\n"))
    predicate = body.index("@py_obj_issubclass(")
    assert "issubclass.derived" in body[:predicate]
    assert "issubclass.classinfo" in body[:predicate]
    assert "@py_obj_getattr(" in body[:predicate]
    assert "@py_tuple_new(" in body[:predicate]
    assert "@py_err_occurred(" in body[predicate:]
    assert "@pcc_gc_take_pinned_slot(" in body[predicate:]
    assert "extern.argument.cleanup" in body


PROGRAM = '''import builtins
from builtins import object as Root
import gc
import weakref

class A:
    pass
class B(A):
    pass
class Other:
    pass

def instance_check(value, kind):
    return isinstance(value, kind)
def subclass_check(value, kind):
    return issubclass(value, kind)
def shadow(value, object):
    return isinstance(value, object)
def shadow_subclass(A, object):
    return issubclass(A, object)
def ordinary():
    return 3

def main():
    early_root = A.__mro__[-1]
    assert instance_check(7, early_root)
    assert instance_check(A, early_root)
    assert subclass_check(A, early_root)
    early_object = early_root()
    try:
        early_object.field = 1
    except AttributeError:
        pass
    else:
        raise AssertionError('MRO root object accepted attributes')
    try:
        weakref.ref(early_object)
    except TypeError:
        pass
    else:
        raise AssertionError('MRO root object accepted weak references')
    root = builtins.object
    assert root is early_root
    assert root is Root
    assert A.__mro__[-1] is root
    assert B.__mro__[-1] is root
    assert root.__mro__ == (root,)
    first = root()
    second = Root()
    assert first is not second
    assert type(first) is root
    for value in (None, True, 7, -7, 2 ** 100, 1.5, 'text', b'bytes', [], {}, (), set(), A(), A, ordinary, root):
        assert isinstance(value, object)
        assert isinstance(value, builtins.object)
        assert isinstance(value, (str, object))
        assert isinstance(value, root)
        assert instance_check(value, root)
        assert instance_check(value, ((), (root, 7)))
    for cls in (A, B, int, bool, str, list, tuple, type, root):
        assert subclass_check(cls, root)
        assert subclass_check(cls, ((), (root, 7)))
    assert shadow(A(), A)
    assert not shadow(7, A)
    assert shadow_subclass(B, A)
    assert not shadow_subclass(Other, A)
    assert not instance_check(A(), Other)
    assert not subclass_check(A, Other)
    for invalid in (7, None, A()):
        assert not subclass_check(invalid, ())
        assert not subclass_check(invalid, ((),))
        try:
            subclass_check(invalid, root)
        except TypeError:
            pass
        else:
            raise AssertionError('invalid derived value accepted')
        try:
            instance_check(first, (invalid, root))
        except TypeError:
            pass
        else:
            raise AssertionError('invalid isinstance classinfo accepted')
        try:
            subclass_check(A, (invalid, root))
        except TypeError:
            pass
        else:
            raise AssertionError('invalid issubclass classinfo accepted')
    try:
        first.field = 1
    except AttributeError:
        pass
    else:
        raise AssertionError('base object accepted attributes')
    try:
        weakref.ref(first)
    except TypeError:
        pass
    else:
        raise AssertionError('base object accepted weak references')
    child = B()
    child.field = first
    assert child.field is first
    reference = weakref.ref(child)
    assert reference() is child
    del child
    gc.collect()
    assert reference() is None
    print('OBJECT_TYPE_IDENTITY_OK')
main()
'''


def test_object_identity_program_reference(tmp_path):
    assert_reference_program(PROGRAM, "OBJECT_TYPE_IDENTITY_OK\n", tmp_path)


def test_object_identity_program_lowers_without_cpython_calls():
    text = _emit(PROGRAM)
    assert "@py_obj_isinstance(" in text
    assert "@py_obj_issubclass(" in text
    assert not re.search(r"\bcall\b[^\n]*@py_cpy_", text)


@pytest.mark.integration
@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_object_identity_native_five_gc(python_program_compiler, request,
                                        explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params["python_program_compiler"]
    assert_owned_program(PROGRAM, "OBJECT_TYPE_IDENTITY_OK\n", tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime,
                         capfd, provenance_probe="2")
    assert (tmp_path / "compiler-wrapper.stderr").read_text() == ""
