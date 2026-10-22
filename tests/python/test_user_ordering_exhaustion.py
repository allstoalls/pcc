"""Host unit models of ordering exhaustion, not a native/GC qualification.

Execute the real Python-level bodies from the three production owners. Explicit
boundary doubles model raw slots, owned descriptor selection, calls, payloads
and exception TLS; they do not import or replace a production runtime module.
In particular, the frame/lease implementation and native compiler are NOT run.
The native counterpart is test_native_ordering_semantics.py.
"""

from __future__ import annotations

import ast
import operator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from pcc.runtime.py import py_abi_constants as abi


OPS = (
    ("py_obj_lt", 0, operator.lt, "__lt__", "__gt__"),
    ("py_obj_le", 1, operator.le, "__le__", "__ge__"),
    ("py_obj_gt", 4, operator.gt, "__gt__", "__lt__"),
    ("py_obj_ge", 5, operator.ge, "__ge__", "__le__"),
)
ORACLES = {op: oracle for _, op, oracle, _, _ in OPS}
ROOT = Path(__file__).resolve().parents[2]


def _load_bodies(namespace, relative, names, constant_prefixes=()):
    source = ROOT / relative
    nodes = []
    for node in ast.parse(source.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id.startswith(constant_prefixes):
                nodes.append(node)
        if isinstance(node, ast.FunctionDef) and node.name in names:
            node.decorator_list = []
            nodes.append(node)
    assert {n.name for n in nodes if isinstance(n, ast.FunctionDef)} == set(names)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace)


@dataclass
class _Slot:
    values: dict = field(default_factory=dict)
    offset: int = 0


def _add(slot, offset):
    return _Slot(slot.values, slot.offset + offset)


def _load(slot, offset=0):
    return slot.values.get(slot.offset + offset)


def _store(slot, offset, value):
    slot.values[slot.offset + offset] = value


def _decline(self, other):
    return NotImplemented


class _OrderingModel:
    """The descriptor/pointer boundary is deliberately a small unit model."""

    def __init__(self):
        self.error = None
        self.calls = []
        self.markers = []
        self.modes = []
        self.adopts = []
        self.conversions = []
        self.accepted = None
        self.namespace = {
            name: value for name, value in vars(abi).items()
            if name.startswith("PY_") or name == "C_POINTER_SIZE"
        }
        self.namespace.update(
            stack_alloc=lambda size: _Slot(),
            ptr_add=_add,
            load_ptr=_load,
            load_i64=lambda slot, offset: _load(slot, offset) or 0,
            store_ptr=_store,
            store_i64=_store,
            ptr_is_null=lambda obj: obj is None,
            ptr_eq=lambda a, b: a is b,
            null=lambda: None,
            cstr=lambda value: value,
            py_err_occurred=lambda: self.error is not None,
            py_runtime_error_if_unset=self._runtime_error,
            py_obj_issubclass=lambda a, b: int(issubclass(a, b)),
            _special_resolve_type=self._resolve,
            _special_select_owned=self._select,
            _binary_selected_attempt=self._attempt,
            _binary_selected_same=lambda slots, tokens, a, b: int(_load(a, 8) is _load(b, 8)),
            _tuple_sequence_default_operation=lambda slots, record: 0,
            py_obj_binary_special_call_slots=self._binary_slots,
            pcc_gc_root_copy_borrowed_lease=self._copy,
            _named_adopt=self._adopt,
            _named_convert=self._convert,
            _named_binary=self._named_binary,
            _type_of=self._tag,
            pcc_capi_is_cext_type_tag=lambda tag: 0,
            py_str_payload=lambda obj: str.__str__(obj) if isinstance(obj, str) else None,
            py_tuple_payload=lambda obj: tuple(obj) if isinstance(obj, tuple) else None,
            _cmp_threeway=lambda a, b: (
                abi.PY_OBJ_CMP_UNORDERED if a != a or b != b else (a > b) - (a < b)
            ),
            _bytes_cmp=lambda a, b: (a > b) - (a < b),
            _sequence_order=lambda a, b, op, is_list: int(ORACLES[op](a, b)),
            py_set_issubset=lambda a, b: int(a.issubset(b)),
            py_set_issuperset=lambda a, b: int(a.issuperset(b)),
            py_set_len=len,
            _order_type_error=self._type_error,
        )
        _load_bodies(
            self.namespace, "pcc/runtime/py/py_class.py",
            {"_binary_special_body"}, ("_BINARY_",),
        )
        _load_bodies(
            self.namespace, "pcc/runtime/py/py_protocol_runtime.py",
            {"_named_body", "py_user_order_dispatch"}, ("_NAMED_",),
        )
        _load_bodies(
            self.namespace, "pcc/runtime/py/py_obj_ops_compare.py",
            {"_user_order", "_obj_order", "_builtin_order", "_order_payload",
             "_order_verdict", "_is_number_tag", "_bytes_orderable",
             *(entry for entry, _, _, _, _ in OPS)},
            ("_ORDER_",),
        )

    def _runtime_error(self, owner, message):
        if self.error is None:
            self.error = RuntimeError(message)

    def _type_error(self, a, b, op):
        self.error = TypeError("ordering exhausted")

    @staticmethod
    def _tag(obj):
        return {
            str: abi.PY_TYPE_STR, tuple: abi.PY_TYPE_TUPLE,
            list: abi.PY_TYPE_LIST, int: abi.PY_TYPE_INT,
            bool: abi.PY_TYPE_BOOL, float: abi.PY_TYPE_FLOAT,
            set: abi.PY_TYPE_SET, bytes: abi.PY_TYPE_BYTES,
        }.get(type(obj), abi.PY_TYPE_INSTANCE)

    def _resolve(self, slots, tokens, source, target):
        obj = _load(slots, source * abi.C_POINTER_SIZE)
        # Mirrors the relevant boundary: exact tuple has a semantic type;
        # exact str does not yet expose ordering descriptors in py_class.
        cls = None if type(obj) is str else type(obj)
        _store(slots, target * abi.C_POINTER_SIZE, cls)
        return 0

    def _select(self, slots, tokens, class_index, name, method_index, record):
        cls = _load(slots, class_index * abi.C_POINTER_SIZE)
        method = None
        present = False
        for owner in (() if cls is None else cls.__mro__):
            # str ordering descriptors are absent in PCC's current namespace;
            # tuple's are real first-class descriptors. No fake module import.
            if owner not in (str, object) and name in owner.__dict__:
                method = owner.__dict__[name]
                present = True
                break
        _store(record, 0, int(present))
        _store(record, abi.C_POINTER_SIZE, method)
        _store(slots, method_index * abi.C_POINTER_SIZE, method)
        return 0

    def _attempt(self, slots, tokens, receiver, cls, method, record, argument, extra_slot):
        # Ordering is binary: only pow()'s ternary protocol passes a trailing
        # argument slot.
        assert extra_slot is None
        if not _load(record):
            return 0
        obj = _load(slots, receiver * abi.C_POINTER_SIZE)
        other = _load(slots, argument * abi.C_POINTER_SIZE)
        callback = _load(slots, method * abi.C_POINTER_SIZE)
        self.calls.append((type(obj), callback))
        try:
            result = callback(obj, other)
        except Exception as exc:
            self.error = exc
            return -1
        if result is NotImplemented:
            return 0
        self.accepted = result
        return 1

    def _binary_slots(self, left, right, name, rname, mode, result, handled):
        slots, tokens = _Slot(), _Slot()
        _store(slots, self.namespace["_BINARY_LEFT"] * abi.C_POINTER_SIZE, _load(left))
        _store(slots, self.namespace["_BINARY_RIGHT"] * abi.C_POINTER_SIZE, _load(right))
        self.accepted = None
        status = self.namespace["_binary_special_body"](
            slots, tokens, name, rname, mode, handled, None,
        )
        self.markers.append(_load(handled))
        if status == 0 and self.accepted is not None:
            _store(result, 0, self.accepted)
        return status

    @staticmethod
    def _copy(target, source):
        _store(target, 0, _load(source))
        return 1

    def _adopt(self, slots, tokens, index):
        self.adopts.append(index)
        assert _load(slots, index * abi.C_POINTER_SIZE) is not None
        return 0

    def _convert(self, slots, conversion, scalar):
        self.conversions.append(conversion)
        assert conversion == 2
        _store(scalar, 0, int(bool(_load(slots, self.namespace["_NAMED_RESULT"] * abi.C_POINTER_SIZE))))
        return 0

    def _named_binary(self, a, b, name, rname, mode, conversion, scalar, handled):
        # Boundary double for the unchanged owning frame wrapper. Execute the
        # real _named_body but do not model/count this as native lease cleanup.
        self.modes.append(mode)
        slots, tokens, borrowed = _Slot(), _Slot(), _Slot()
        _store(borrowed, 0, a)
        _store(borrowed, abi.C_POINTER_SIZE, b)
        _store(borrowed, 2 * abi.C_POINTER_SIZE, None)
        _store(handled, 0, 1)
        _store(scalar, 0, 0)
        status = self.namespace["_named_body"](
            slots, tokens, borrowed, name, rname, 1, mode, conversion, scalar, handled,
        )
        if status != 0:
            _store(handled, 0, 1)
            _store(scalar, 0, 0)
        return status

    def compare(self, entry, left, right):
        return self.namespace[entry](left, right)


@pytest.mark.parametrize("entry,op,oracle,name,rname", OPS, ids=["lt", "le", "gt", "ge"])
@pytest.mark.parametrize("base,payloads", [(str, ("a", "b")), (tuple, ((1,), (2,)))], ids=["str", "tuple"])
@pytest.mark.parametrize("same_object", [False, True], ids=["distinct", "same"])
def test_both_selected_declines_do_not_revive_builtin_payload(
    entry, op, oracle, name, rname, base, payloads, same_object,
):
    cls = type("Declines", (base,), {method: _decline for row in OPS for method in row[3:]})
    left, right = cls(payloads[0]), cls(payloads[1])
    if same_object:
        right = left
    with pytest.raises(TypeError):
        oracle(left, right)
    model = _OrderingModel()
    assert model.compare(entry, left, right) == 0
    assert isinstance(model.error, TypeError)
    assert model.modes == [2]
    assert model.markers == [2]
    assert len(model.calls) == 2
    assert model.adopts == model.conversions == []


@pytest.mark.parametrize("entry,op,oracle,name,rname", OPS)
@pytest.mark.parametrize("present", ["neither", "left", "right"])
def test_missing_builtin_descriptor_keeps_payload_fallback(entry, op, oracle, name, rname, present):
    left_cls = type("Left", (str,), {name: _decline} if present == "left" else {})
    right_cls = type("Right", (str,), {rname: _decline} if present == "right" else {})
    left, right = left_cls("a"), right_cls("b")
    model = _OrderingModel()
    assert model.compare(entry, left, right) == int(oracle(left, right))
    assert model.error is None
    assert model.markers == [0]
    assert len(model.calls) == int(present != "neither")


@pytest.mark.parametrize("entry,op,oracle,name,rname", OPS)
@pytest.mark.parametrize("answer", [False, True])
def test_reflected_subclass_answer_keeps_priority(entry, op, oracle, name, rname, answer):
    left_cls = type("Left", (str,), {name: _decline, rname: _decline})

    def reflected(self, other):
        return answer

    right_cls = type("Right", (left_cls,), {rname: reflected})
    left, right = left_cls("a"), right_cls("b")
    model = _OrderingModel()
    assert model.compare(entry, left, right) == int(oracle(left, right)) == int(answer)
    assert model.error is None
    assert model.calls == [(right_cls, reflected)]
    assert model.markers == [1]
    assert model.adopts == [model.namespace["_NAMED_RESULT"]]
    assert model.conversions == [2]


@pytest.mark.parametrize("entry,op,oracle,name,rname", OPS)
def test_callback_error_is_not_replaced_with_type_error(entry, op, oracle, name, rname):
    error = ValueError("callback")

    def raises(self, other):
        raise error

    cls = type("Raises", (str,), {name: raises, rname: _decline})
    model = _OrderingModel()
    assert model.compare(entry, cls("a"), cls("b")) == 0
    assert model.error is error
    assert model.markers == [1]
    assert model.adopts == model.conversions == []


def test_pending_tls_error_wins_over_exhaustion_marker():
    model = _OrderingModel()
    pending = ValueError("pending")
    model.error = pending
    # Isolate the comparison entrypoint's guard after frame/TLS restoration.
    model.namespace["py_user_order_dispatch"] = lambda a, b, op: -2
    cls = type("Value", (str,), {})
    assert model.compare("py_obj_lt", cls("a"), cls("b")) == 0
    assert model.error is pending


def test_exhaustion_uses_original_descriptor_snapshot_after_mutation():
    right_cls = type("Right", (str,), {"__gt__": _decline})

    def mutate(self, other):
        del right_cls.__gt__
        return NotImplemented

    left_cls = type("Left", (str,), {"__lt__": mutate})
    model = _OrderingModel()
    assert model.compare("py_obj_lt", left_cls("a"), right_cls("b")) == 0
    assert "__gt__" not in right_cls.__dict__
    assert model.calls == [(left_cls, mutate), (right_cls, _decline)]
    assert model.markers == [2]
    assert isinstance(model.error, TypeError)


@pytest.mark.parametrize("mode,name,rname,attempts", [
    (0, "__add__", "__radd__", 1),
    (1, "__eq__", "__eq__", 2),
])
def test_equality_and_binop_exhaustion_keep_boolean_handled(mode, name, rname, attempts):
    cls = type("Declines", (), {name: _decline, rname: _decline})
    model = _OrderingModel()
    scalar, handled = _Slot(), _Slot()
    assert model._named_binary(cls(), cls(), name, rname, mode, 2, scalar, handled) == 0
    assert _load(handled) == 0
    assert model.markers == [0]
    assert len(model.calls) == attempts
    assert model.adopts == model.conversions == []
    assert model.error is None


@pytest.mark.parametrize("entry,op,oracle,name,rname", OPS)
@pytest.mark.parametrize("left,right", [
    (1, 2.0), (True, 1), ("a", "b"), (b"a", b"b"),
    ((1,), (2,)), ([1], [2]), ({1}, {1, 2}), (float("nan"), 1),
])
def test_builtin_controls_remain_unchanged(entry, op, oracle, name, rname, left, right):
    model = _OrderingModel()
    assert model.compare(entry, left, right) == int(oracle(left, right))
    assert model.error is None
    assert model.modes == []
