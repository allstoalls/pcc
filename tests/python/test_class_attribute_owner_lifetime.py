"""Ordinary class namespaces own values; declaration globals are identities."""
from __future__ import annotations

from collections import deque
import re
from types import SimpleNamespace

import pytest

from pcc.frontends.python.codegen.class_gen import ClassLowering
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.py_ast import DynType, RawPointerType
from pcc.frontends.python.py_lift import parse_and_lift
from pcc.frontends.python.type_infer import infer_module


def _emit(source):
    module = infer_module(parse_and_lift(source, "attribute_owner.py", "attribute_owner"))
    codegen = L1CodeGen(module, ir_scaffold_mode="on")
    codegen._strict_no_libpython = True
    codegen._prefer_native_callable_values = True
    return str(codegen.generate(module))


def _assert_no_cached_values(text):
    declarations = re.findall(r"^(@\.classattr\.[^ ]+) = internal global ptr null$", text, re.M)
    assert declarations
    for global_name in declarations:
        # These addresses may remain registered/used as lexical identities,
        # but no object value may be published to or loaded from the cache.
        stores = re.findall(r"^  store [^\n]*, ptr " + re.escape(global_name) + r"(?:,|$)", text, re.M)
        loads = re.findall(r"^  [^\n]*load ptr, ptr " + re.escape(global_name) + r"(?:,|$)", text, re.M)
        assert not stores, stores
        assert not loads, loads
    assert "class.attribute.initializer" in text


def test_failed_local_class_keeps_no_global_value_owner():
    text = _emit('''import weakref
references = []
class Token:
    pass
def make():
    token = Token()
    references.append(weakref.ref(token))
    return token
def fail():
    raise ValueError('later default')
def construct():
    class Broken:
        fields = make()
        def target(self, value=fields, later=fail()):
            return value
    return Broken
''')
    _assert_no_cached_values(text)
    assert "class.body.unwind" in text
    assert "func.default.class.name" in text


def test_class_instance_and_inherited_reads_use_live_receiver():
    text = _emit('''class Base:
    fields = ('base',)
    def read(self):
        return self.fields
class Child(Base):
    pass
def probe(obj: Base):
    return obj.fields
def inherited(obj: Child):
    return obj.fields
Base.fields = ('replacement',)
''')
    _assert_no_cached_values(text)
    for name in ("Base_read", "probe", "inherited"):
        body = re.search(r"^define [^\n]*@user_attribute_owner_" + name + r"\([^\n]*\).*?^}", text, re.M | re.S)
        assert body is not None, name
        assert "class.attribute.value" in body.group(0)
        assert "@py_obj_getattr(" in body.group(0)
    assert "class.attribute.store.value" in text
    assert "@py_obj_setattr(" in text


def _assert_scalar_conversion_owner(body):
    roots = re.findall(r"(%class\.attribute\.value\.operand[-\w.]*) = alloca ptr", body)
    assert len(roots) == 1, roots
    root = roots[0]
    aliases = dict(re.findall(r"(%[-\w.]+) = bitcast ptr (%[-\w.]+) to ptr", body))

    def physical(value):
        visited = set()
        while value in aliases:
            assert value not in visited
            visited.add(value)
            value = aliases[value]
        return value

    loaded_from = dict(re.findall(r"(%[-\w.]+) = load ptr, ptr (%[-\w.]+)", body))
    blocks = {}
    current = None
    for line in body.splitlines()[1:-1]:
        label = re.match(r"^([-\w.$]+):", line)
        if label:
            current = label.group(1)
            blocks[current] = []
        elif current is not None:
            blocks[current].append(line.strip())
    queue = deque([(next(iter(blocks)), False, frozenset(), False)])
    visited = set()
    conversions = 0
    converted_returns = 0
    while queue:
        block, live, leases, converted = queue.popleft()
        state = (block, live, leases, converted)
        if state in visited:
            continue
        visited.add(state)
        active = set(leases)
        successors = []
        for line in blocks[block]:
            acquire = re.search(r"(%[-\w.]+) = call [^\n]*@pcc_gc_foreign_lease_acquire\(ptr (%[-\w.]+)\)", line)
            if acquire and physical(acquire.group(2)) == root:
                active.add(acquire.group(1))
            release = re.search(
                r"@(?:pcc_gc_foreign_lease_release|py_cleanup_one_lease_preserving_exception)"
                r"\(ptr (%[-\w.]+), i64 (%[-\w.]+)\)", line)
            if release and physical(release.group(1)) == root:
                active.discard(release.group(2))
            # The exception-preserving cleanup helpers clear the roots they get.
            cleanup = re.search(
                r"@py_cleanup_(?:one|two)_roots?_preserving_exception\(([^\n)]*)\)", line)
            if cleanup and root in {physical(arg) for arg in re.findall(r"ptr (%[-\w.]+)", cleanup.group(1))}:
                if converted:
                    assert not active, (block, active)
                live = False
            conversion = re.search(r"@py_float_to_f64\(ptr (%[-\w.]+)\)", line)
            if conversion:
                assert physical(loaded_from[conversion.group(1)]) == root
                assert live and active, (block, live, active)
                converted = True
                conversions += 1
            store = re.search(r"store ptr (null|%[-\w.]+), ptr (%[-\w.]+)", line)
            if store and physical(store.group(2)) == root:
                live = store.group(1) != "null"
            clear = re.search(r"@pcc_gc_store_root\(ptr (%[-\w.]+), ptr null\)", line)
            if clear and physical(clear.group(1)) == root:
                if converted:
                    assert not active, (block, active)
                live = False
            if line.startswith("br "):
                successors.extend(re.findall(r"label %([-\w.$]+)", line))
            if line.startswith("ret ") and converted:
                assert not live and not active, (block, live, active)
                converted_returns += 1
        queue.extend((name, live, frozenset(active), converted) for name in successors)
    assert conversions and converted_returns


def test_scalar_class_read_keeps_owner_until_conversion():
    # Float exercises scalar conversion. Ordinary Python int deliberately
    # uses the separate exact-integer object path, even with annotations.
    text = _emit('''class Holder:
    count: float = 7.0
    def read(self) -> float:
        return self.count
''')
    _assert_no_cached_values(text)
    body = re.search(r"^define [^\n]*@user_attribute_owner_Holder_read\([^\n]*\).*?^}", text, re.M | re.S)
    assert body is not None
    _assert_scalar_conversion_owner(body.group(0))


@pytest.mark.parametrize("value", ("[object()]", "{'key': object()}", "(object(),)", "frozenset((1, 2))", "make_default"))
def test_ordinary_initializer_uses_authoritative_producer(value):
    text = _emit("def make_default():\n    return object()\nclass Holder:\n    value = " + value + "\n")
    _assert_no_cached_values(text)


def _classification():
    parent = SimpleNamespace(
        _native_module_exports={}, ast_module=SimpleNamespace(name="test"),
        _runtime_port_module=False, _freestanding_module=False,
        _is_valueclass_payload_type=lambda ty: getattr(ty, "valueclass", False),
    )
    lower = ClassLowering(parent)
    info = SimpleNamespace(
        name="Holder", valueclass=False, metaclass_name=None, expanded_cd=None,
        owning_module=None, export_class_name="Holder", bases_ast=(),
        enum_members={}, enum_string_members={},
        class_attrs={"value": (object(), DynType(name="dyn"))},
    )
    lower.classes[info.name] = info
    return parent, lower, info


@pytest.mark.parametrize("route", ("ordinary", "valueclass", "metaclass", "runtime", "declared_raw", "new_raw"))
def test_legacy_layout_routes_remain_explicit(route):
    parent, lower, info = _classification()
    value_ty = DynType(name="dyn")
    if route == "valueclass":
        info.valueclass = True
    elif route == "metaclass":
        info.metaclass_name = "Prepared"
    elif route == "runtime":
        parent._runtime_port_module = True
    elif route == "declared_raw":
        info.class_attrs["value"] = (object(), RawPointerType(name="c_ptr"))
    elif route == "new_raw":
        value_ty = RawPointerType(name="c_ptr")
    assert lower.uses_live_class_attribute(info, "value", value_ty) is (route == "ordinary")
