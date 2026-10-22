"""Deterministic production index-slot bodies and fixture-free owned IR.

These checks do not qualify native/threaded movement. The binder itself is the
production slot binder loaded by SpecialCallModel, not a repeated lookup stub.
"""
import ast
from pathlib import Path

import pytest

from test_foreign_address_leases import _functions
from test_runtime_entry_handoff import SpecialCallModel


PORT = Path(__file__).resolve().parents[2] / "pcc/runtime/py/py_protocol_runtime.py"


EXPECTED_SCRATCH_LAYOUT = {
    '_INDEX_OLD_EXCEPTION_SLOT': 0,
    '_INDEX_RECEIVER_SLOT': 1,
    '_INDEX_RESULT_SLOT': 2,
    '_INDEX_ERROR_SLOT': 3,
    '_INDEX_SLOT_COUNT': 4,
}


def _scratch_layout(tree):
    values = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in EXPECTED_SCRATCH_LAYOUT):
            value = ast.literal_eval(node.value)
            assert type(value) is int
            values[node.targets[0].id] = value
    assert values == EXPECTED_SCRATCH_LAYOUT
    return values


class IndexModel(SpecialCallModel):
    def __init__(self, *, legacy=False):
        super().__init__()
        self.dead = set()
        self.converted = []
        self.after_conversion = None
        self.true = self.make(self.abi.PY_TYPE_BOOL)
        self.false = self.make(self.abi.PY_TYPE_BOOL)
        self.ns.update(
            global_load_ptr=lambda name: self.true if name == "py_True" else self.false if name == "py_False" else self.none,
            py_int_from_i64=self.integer,
            py_int_to_i64=self.convert,
            PyNumber_Index=self.extension_index,
            py_incref=lambda value: self.references.__setitem__(value, self.references[value] + 1),
            py_decref=self.drop_reference,
            py_class_lookup=lambda cls, name: 0,
            py_exc_new=self.new_error,
        )
        names = {"_type_of", "_is_user_instance", "_instance_class", "_lookup_dunder", "py_obj_index"}
        if not legacy:
            names |= {node.name for node in ast.parse(PORT.read_text()).body
                      if isinstance(node, ast.FunctionDef) and node.name.startswith("_index_slot_")}
            names |= {"py_obj_index_slots", "py_obj_index_i64_slots", "py_index_i64_checked_slots"}
        self.ns.update(_scratch_layout(ast.parse(PORT.read_text())))
        _functions(PORT, names, self.ns)

    def load(self, base, offset=0):
        assert base not in getattr(self, "dead", ()), "stale raw index pointer"
        return super().load(base, offset)

    def integer(self, value):
        result = self.make(self.abi.PY_TYPE_INT)
        self.store(result, 24, value)
        return result

    def new_error(self, kind, message):
        result = self.exception(message)
        self.store(result, 32, kind)
        return result

    def convert(self, value, overflow):
        assert value & 1 or self.count(value) > 0, "integer result needs its own counted lease"
        number = value >> 1 if value & 1 else self.load(value, 24)
        self.converted.append(number)
        self.store(overflow, 0, int(not -(1 << 63) <= number < (1 << 63)))
        if self.after_conversion:
            callback, self.after_conversion = self.after_conversion, None
            callback(value)
        return number if -(1 << 63) <= number < (1 << 63) else 0

    def extension_index(self, receiver):
        assert self.count(receiver) > 0
        return self.integer(23)

    def relocate(self, old):
        """Move a selected unleased object and update authoritative slots."""
        assert self.count(old) == 0
        new = self.make(self.load(old, 8))
        for (base, offset), value in list(self.memory.items()):
            if isinstance(base, int) and old <= base < old + 8192:
                self.store(new + base - old, offset, value)
        self.references[new], self.references[old] = self.references[old], 0
        for key, value in list(self.memory.items()):
            if value == old:
                self.memory[key] = new
        if old in self.callbacks:
            self.callbacks[new] = self.callbacks.pop(old)
        self.dead.add(old)
        return new

    def index(self, receiver, *, checked=False):
        source, result = self.own(receiver), self.own(0)
        if checked:
            value = self.ns["py_index_i64_checked_slots"](source)
        else:
            status = self.ns["py_obj_index_slots"](source, result)
            value = status, self.load(result)
        assert not self.registered and self.active() == 0
        return value


def test_legacy_raw_entry_has_no_pre_body_root_owner():
    model = IndexModel(legacy=True)
    old = model.integer(12)
    source = model.own(old)
    current = model.relocate(old)
    assert model.load(source) == current
    with pytest.raises(AssertionError, match="stale raw index pointer"):
        model.ns["py_obj_index"](old)


@pytest.mark.parametrize("phase", ["entry", "register", "lock"])
def test_index_slots_reload_actual_caller_owner_after_movement(phase):
    model = IndexModel()
    old = model.integer(42)
    source, result = model.own(old), model.own(0)
    move = lambda: model.relocate(old)
    if phase == "entry":
        move()
    elif phase == "register":
        model.after_registration = move
    else:
        model.on_lock = move
    assert model.ns["py_obj_index_slots"](source, result) == 0
    assert model.load(result) == model.load(source) != old
    assert model.load(model.load(result), 24) == 42
    assert model.references[model.load(source)] == 2
    assert not model.registered and model.active() == 0


@pytest.mark.parametrize("number", [0, 7, -(1 << 63), (1 << 63) - 1, 1 << 180])
def test_index_keeps_arbitrary_precision_and_checked_conversion_distinct(number):
    model = IndexModel()
    value = model.integer(number)
    callback = model.function(lambda receiver: value)
    receiver = model.instance(model.cls(namespace={"__index__": callback}))
    status, result = model.index(receiver)
    assert status == 0 and result == value and model.load(result, 24) == number
    assert len(model.calls) == 1
    converted = model.index(receiver, checked=True)
    assert len(model.calls) == 2
    if -(1 << 63) <= number < 1 << 63:
        assert converted == number and not model.error
    else:
        assert converted == 0 and "too large" in model.load(model.error, 24)


@pytest.mark.parametrize("binding", ["function", "staticmethod", "classmethod", "descriptor", "native"])
def test_index_uses_single_named_binding_and_no_instance_attribute(binding):
    model = IndexModel()
    seen = []
    result = model.integer(19)
    callback = model.function(lambda *args: seen.append(("call", args)) or result)
    descriptor = callback
    if binding == "staticmethod":
        descriptor = model.descriptor(model.abi.PY_TYPE_STATICMETHOD, callback, model.abi.PYSTATICMETHODOBJECT_FUNC_OFFSET)
    elif binding == "classmethod":
        descriptor = model.descriptor(model.abi.PY_TYPE_CLASSMETHOD, callback, model.abi.PYCLASSMETHODOBJECT_FUNC_OFFSET)
    elif binding == "descriptor":
        getter = model.function(lambda obj, receiver, owner: seen.append(("get", receiver, owner)) or callback)
        descriptor = model.instance(model.cls(namespace={"__get__": getter}))
    if binding == "native":
        descriptor = 0x70000000
        model.callbacks[descriptor] = lambda receiver: result
        base = model.cls(methods={"__index__": descriptor})
    else:
        base = model.cls(namespace={"__index__": descriptor})
    cls = model.cls(bases=(base,))
    receiver = model.instance(cls)
    assert model.index(receiver) == (0, result)
    if binding == "descriptor":
        assert seen == [("get", receiver, cls), ("call", ())]
    elif binding != "native":
        assert seen == [("call", () if binding == "staticmethod" else (cls if binding == "classmethod" else receiver,))]


@pytest.mark.parametrize("failure", ["absent", "null", "raised", "nonint", "bool"])
def test_index_failure_preserves_selected_error_and_cleans_roots(failure):
    model = IndexModel()
    old = model.exception("old pending")
    raised = model.exception("selected index failure")
    invalid = model.make(model.abi.PY_TYPE_STR)
    def callback(receiver):
        assert model.error == 0
        if failure == "raised":
            model.error = raised
        return invalid if failure == "nonint" else model.true if failure == "bool" else 0
    namespace = {} if failure == "absent" else {"__index__": model.function(callback)}
    model.error = old
    assert model.index(model.instance(model.cls(namespace=namespace))) == (-1, 0)
    assert model.error not in (0, old)
    if failure == "raised":
        assert model.error == raised
    if failure == "bool":
        assert "warning emission" in model.load(model.error, 24)


@pytest.mark.parametrize("registration", [1, 2, 3, 4])
def test_index_partial_registration_failure_is_balanced(registration):
    model = IndexModel()
    model.fail_registration = registration
    assert model.index(model.integer(42)) == (-1, 0)


def test_index_bool_and_cext_paths_and_old_tls_success():
    model = IndexModel()
    old = model.exception("pending")
    model.error = old
    status, value = model.index(model.true)
    assert status == 0 and model.load(value, 24) == 1 and model.error == old
    assert model.index(model.false, checked=True) == 0 and model.error == old
    assert model.index(model.make(model.abi.PY_TYPE_CEXT_TAG_BASE), checked=True) == 23
    assert model.error == old and not model.calls


def test_checked_result_lease_survives_nested_legacy_pin_unpin():
    model = IndexModel()
    value = model.integer(37)
    source = model.own(value)
    alias = model.acquire(source)
    def observe(current):
        assert model.count(current) >= 2
        model.store(current, 12, 64)
        model.store(current, 12, 0)
        assert model.pinned(current) == 1
    model.after_conversion = observe
    assert model.ns["py_index_i64_checked_slots"](source) == 37
    assert model.count(model.load(source)) == 1
    assert model.release(source, alias) == 0
    assert not model.registered and model.active() == 0


@pytest.mark.parametrize("container", [False, True])
@pytest.mark.parametrize("callback_overflow", [False, True])
def test_conversion_overflow_kind_does_not_rewrite_callback_overflow(container, callback_overflow):
    model = IndexModel()
    raised = model.new_error(15, "callback overflow")
    wide = model.integer(1 << 180)
    def callback(receiver):
        if callback_overflow:
            model.error = raised
            return 0
        return wide
    receiver = model.instance(model.cls(namespace={"__index__": model.function(callback)}))
    source = model.own(receiver)
    helper = "py_obj_index_i64_slots" if container else "py_index_i64_checked_slots"
    assert model.ns[helper](source) == 0
    if callback_overflow:
        assert model.error == raised and model.converted == []
    else:
        assert model.load(model.error, 32) == (5 if container else 15)
        assert model.converted == [1 << 180]
    assert not model.registered and model.active() == 0


def test_callback_result_can_move_between_binder_and_index_validation():
    model = IndexModel()
    result = model.integer(17)
    receiver = model.instance(model.cls(namespace={"__index__": model.function(lambda _: result)}))
    release = model.ns["pcc_gc_foreign_lease_release"]
    moved = []
    def release_and_move(slot, token):
        value = model.load(slot)
        status = release(slot, token)
        if value == result and token and not model.count(result) and not moved:
            moved.append(model.relocate(result))
        return status
    model.ns["pcc_gc_foreign_lease_release"] = release_and_move
    assert model.index(receiver) == (0, moved[0])
    assert model.load(moved[0], 24) == 17


@pytest.mark.parametrize("failed", [False, True])
def test_index_cleanup_preserves_old_or_selected_tls_over_finalizer_error(failed):
    model = IndexModel()
    old = model.exception("old pending")
    selected = model.exception("selected callback")
    finalizer = model.exception("cleanup finalizer")
    integer = model.integer(31)
    def callback(receiver):
        if failed:
            model.error = selected
            return 0
        return integer
    receiver = model.instance(model.cls(namespace={"__index__": model.function(callback)}))
    model.error = old
    store = model.ns["pcc_gc_store_root"]
    def drop_with_error(slot, value):
        previous = model.load(slot)
        store(slot, value)
        if previous == receiver and value == 0:
            model.error = finalizer
    model.ns["pcc_gc_store_root"] = drop_with_error
    assert model.index(receiver) == ((-1, 0) if failed else (0, integer))
    assert model.error == (selected if failed else old)


@pytest.mark.parametrize("invalid", ["copy_failure", "occupied", "alias"])
def test_index_rejects_bad_destination_and_copy_failure_without_losing_owner(invalid):
    model = IndexModel()
    value = model.integer(13)
    source, output = model.own(value), model.own(0)
    if invalid == "copy_failure":
        model.fail_commit = True
    elif invalid == "occupied":
        model.root_store(output, value)
    else:
        output = source
    before = model.references[value]
    assert model.ns["py_obj_index_slots"](source, output) == -1
    assert model.references[value] == before
    assert model.load(source) == value
    assert not model.registered and model.active() == 0


def _emit_index_probe(source, *, cpython=False):
    from pcc.frontends.python import type_infer
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.py_ast import Name
    from pcc.frontends.python.py_lift import parse_and_lift
    from tests.owned_ir_validation import verify_ir_text

    class IndexProbeCodegen(L1CodeGen):
        def _emit_call(self, expr):
            if isinstance(expr.func, Name) and expr.func.ident == "index_probe":
                if cpython:
                    self._cpy_env_flags[expr.args[0].ident] = True
                return self._emit_index_expr_as_i64(expr.args[0])
            return super()._emit_call(expr)

    module = type_infer.infer_module(parse_and_lift(source, "index_probe.py", "index_probe"))
    codegen = IndexProbeCodegen(module, ir_scaffold_mode="on")
    codegen._strict_no_libpython = not cpython
    text = str(codegen.generate(module))
    verify_ir_text(text)
    return text


def test_compiler_dynamic_index_copies_borrowed_parameter_slot_before_entry():
    from test_slot_call_operand_roots import _calls, _with_slot_origins
    text = _emit_index_probe("def run(index):\n    return index_probe(index)\n")
    copies = _calls(text, "pcc_gc_root_copy_borrowed_lease")
    assert len(copies) == 1 and "%index.addr" in _with_slot_origins(text, copies[0])
    calls = _calls(text, "py_obj_index_i64_slots")
    assert len(calls) == 1 and ".operand" in _with_slot_origins(text, calls[0])
    assert not _calls(text, "py_obj_index_i64")
    assert text.index(copies[0]) < text.index(calls[0])
    assert _calls(text, "pcc_gc_store_root")


def test_compiler_index_retains_machine_and_cpython_lanes():
    from test_slot_call_operand_roots import _calls
    machine = _emit_index_probe("from pcc import i64\ndef run(index: i64):\n    index_probe(index)\n")
    assert not _calls(machine, "py_obj_index_i64_slots")
    cpython = _emit_index_probe("def run(index):\n    return index_probe(index)\n", cpython=True)
    assert _calls(cpython, "py_cpy_to_i64")
    assert not _calls(cpython, "py_obj_index_i64_slots")


def test_index_scratch_layout_preserves_slot_numbers():
    layout = _scratch_layout(ast.parse(PORT.read_text()))
    assert layout["_INDEX_SLOT_COUNT"] == 4
    assert layout["_INDEX_ERROR_SLOT"] == layout["_INDEX_SLOT_COUNT"] - 1
