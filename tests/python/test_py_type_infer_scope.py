from pcc.frontends.python.py_ast import DictType, DynType, IntType, StrType
from pcc.frontends.python.type_infer import _Scope, _is_assignable


class _DictTypeShell:
    name = "dict"

    def __init__(self, key, value):
        self.key = key
        self.value = value


class _UnparameterizedDictShell:
    name = "dict"


def test_scope_lookup_updates_existing_binding_without_duplicate_name():
    scope = _Scope()
    int_ty = IntType(name="int", width=64, signed=True)
    str_ty = StrType(name="str")

    scope.define("value", int_ty)
    scope.update("value", str_ty)

    assert scope.lookup_local("value") is str_ty
    assert list(scope.bindings) == ["value"]


def test_scope_lookup_walks_parent_after_index_miss():
    parent = _Scope()
    str_ty = StrType(name="str")

    parent.define("shared", str_ty)

    child = _Scope(parent)

    assert child.lookup("shared") is str_ty
    assert child.lookup("missing") is None


def test_scope_update_shadows_inherited_binding():
    parent = _Scope()
    int_ty = IntType(name="int", width=64, signed=True)
    str_ty = StrType(name="str")
    parent.define("value", int_ty)

    child = _Scope(parent)
    child.update("value", str_ty)

    assert parent.lookup("value") is int_ty
    assert child.lookup("value") is str_ty
    assert child.lookup_local("value") is str_ty


def test_assignable_accepts_structural_dict_type_shell():
    str_ty = StrType(name="str")
    dyn_ty = DynType(name="dyn")
    declared = DictType(name="dict", key=str_ty, value=dyn_ty)
    got = _DictTypeShell(key=str_ty, value=dyn_ty)

    assert _is_assignable(declared, got)


def test_assignable_accepts_unparameterized_dict_type_shell():
    str_ty = StrType(name="str")
    dyn_ty = DynType(name="dyn")
    declared = _UnparameterizedDictShell()
    got = DictType(name="dict", key=str_ty, value=dyn_ty)

    assert _is_assignable(declared, got)
