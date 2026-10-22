"""Handled-state links borrow already registered exception slots per thread."""

import ast
from pathlib import Path
import threading


SOURCE = Path(__file__).resolve().parents[2] / "pcc/runtime/py/freestanding_gc_root_operations.py"


def _primitives():
    tls = threading.local()
    namespace = {
        "c_ptr": object,
        "i64": int,
        "c_abi_export": lambda _name: lambda function: function,
        "null": lambda: None,
        "ptr_is_null": lambda value: value is None,
        "ptr_eq": lambda lhs, rhs: lhs is rhs,
        "load_ptr": lambda value, offset: value[offset // 8],
        "store_ptr": lambda value, offset, item: value.__setitem__(offset // 8, item),
        "global_load_ptr": lambda _name: getattr(tls, "current", None),
        "global_store_ptr": lambda _name, value: setattr(tls, "current", value),
    }
    tree = ast.parse(SOURCE.read_text())
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name.startswith("py_handled_")]
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace


def test_handled_context_nested_root_relocation_and_detachment():
    runtime = _primitives()
    push, pop = runtime["py_handled_context_push"], runtime["py_handled_context_pop"]
    current, swap = runtime["py_handled_exception_slot"], runtime["py_handled_context_swap"]
    outer, inner = [None, None], [None, None]
    outer_slot, inner_slot = [object()], [object()]
    assert current() is None
    push(outer, outer_slot)
    moved = object()
    outer_slot[0] = moved
    assert current() is outer_slot and current()[0] is moved
    push(inner, inner_slot)
    assert current() is inner_slot
    assert pop(inner) == 1
    assert pop(inner) == 0
    assert current() is outer_slot
    checkpoint = swap(None)
    assert current() is None
    assert swap(checkpoint) is None
    assert current() is outer_slot
    assert pop(outer) == 1
    assert current() is None


def test_handled_context_thread_local_isolation():
    runtime = _primitives()
    barrier = threading.Barrier(3)
    results = []
    main_record, main_slot = [None, None], [object()]
    runtime["py_handled_context_push"](main_record, main_slot)
    def worker():
        record, slot = [None, None], [object()]
        results.append(runtime["py_handled_exception_slot"]() is None)
        runtime["py_handled_context_push"](record, slot)
        barrier.wait(timeout=5)
        results.append(runtime["py_handled_exception_slot"]() is slot)
        barrier.wait(timeout=5)
        runtime["py_handled_context_pop"](record)
        results.append(runtime["py_handled_exception_slot"]() is None)
    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=5)
    assert runtime["py_handled_exception_slot"]() is main_slot
    barrier.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert results == [True] * 6
    runtime["py_handled_context_pop"](main_record)


def _implicit_chainer(on_lock=None):
    state = {"locked": False, "finishes": 0}
    def load(pointer):
        if isinstance(pointer, tuple):
            obj, offset = pointer
            return obj.get(offset)
        return pointer[0]
    def commit(plan, owner, pointer, value):
        assert state["locked"]
        obj, offset = pointer
        plan["old"] = obj.get(offset)
        obj[offset] = value
        return 1
    def finish(plan):
        assert not state["locked"]
        state["finishes"] += 1
    def lock():
        assert not state["locked"]
        if on_lock is not None:
            on_lock()
        state["locked"] = True
    namespace = {
        "c_abi_export": lambda _name: lambda function: function,
        "PY_TYPE_EXC": 8,
        "null": lambda: None,
        "ptr_is_null": lambda value: value is None,
        "ptr_eq": lambda lhs, rhs: lhs is rhs,
        "ptr_add": lambda obj, offset: (obj, offset),
        "is_tagged_int": lambda value: isinstance(value, int),
        "load_i32": lambda obj, offset: obj[offset],
        "stack_alloc": lambda _size: {},
        "pcc_gc_backend": lambda: 0,
        "pcc_gc_load_ptr": lambda _owner, pointer: load(pointer),
        "pcc_gc_store_ptr_plan_init": lambda plan, _owner, _backend: plan.clear(),
        "pcc_gc_store_ptr_plan_commit_locked": commit,
        "pcc_gc_store_ptr_plan_finish": finish,
        "pcc_py_gc_minor_graph_lock": lock,
        "pcc_py_gc_minor_graph_unlock": lambda: state.__setitem__("locked", False),
    }
    source = SOURCE.with_name("py_exc_objects.py")
    tree = ast.parse(source.read_text())
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name == "py_exc_set_implicit_context_slots"]
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), namespace)
    return namespace["py_exc_set_implicit_context_slots"], state


def test_implicit_context_overwrite_self_and_indirect_cycles():
    chain, state = _implicit_chainer()
    first, second, error = [{8: 8, 40: None} for _ in range(3)]
    explicit_cause = object()
    error[32] = explicit_cause
    chain([error], [first])
    chain([error], [second])
    assert error[40] is second and error[32] is explicit_cause
    chain([error], [error])
    assert error[40] is second
    first[40] = second
    second[40] = error
    chain([error], [first])
    assert error[40] is first and first[40] is second and second[40] is None
    # Pre-existing explicit cycles do not hang and need not be destroyed.
    first[40], second[40] = second, first
    fresh = {8: 8, 40: None}
    chain([fresh], [first])
    assert fresh[40] is first and second[40] is first
    assert not state["locked"] and state["finishes"] == 10


def test_implicit_context_reloads_roots_after_graph_lock_parks():
    old_error, old_context = {8: 8, 40: None}, {8: 8, 40: None}
    error_slot, context_slot = [old_error], [old_context]
    moved_error, moved_context = {8: 8, 40: None}, {8: 8, 40: None}
    def relocate():
        error_slot[0], context_slot[0] = moved_error, moved_context
        old_error.clear()
        old_context.clear()
    chain, state = _implicit_chainer(relocate)
    chain(error_slot, context_slot)
    assert moved_error[40] is moved_context
    assert not state["locked"] and state["finishes"] == 2


def test_handled_cleanup_helpers_have_native_static_method_exports():
    from pcc.frontends.python.codegen.host_contract import L1_CODEGEN_HOST_METHODS
    from pcc.frontends.python.codegen._l1_codegen_static_methods import L1_CODEGEN_STATIC_METHODS
    from pcc.frontends.python.codegen.layer1_support import _default_native_module_exports

    helpers = (
        "_begin_handled_exception_scope", "_emit_handled_exception_scope_exit",
        "_emit_suspend_handled_exception_scopes", "_emit_resume_handled_exception_scopes",
        "_emit_chain_pending_handled_exception", "_exception_selection_owner_slot",
        "_clear_exception_selection_owner_slot", "_emit_exception_class_match",
        "_generator_tuple_build_name", "_generator_expr_may_suspend",
        "_emit_suspending_tuple_literal", "_emit_cancel_pending_return_roots",
        "_emit_owned_return_through_finally", "_emit_generator_yield_expr",
    )
    static = {entry["name"]: entry for entry in L1_CODEGEN_STATIC_METHODS}
    exports = _default_native_module_exports("pcc.frontends.python.codegen.layer1")
    native = {entry["name"]: entry for entry in exports["pcc.frontends.python.codegen.layer1"]["L1CodeGen"]["methods"]}
    for name in helpers:
        assert name in L1_CODEGEN_HOST_METHODS
        assert static[name] == native[name]
    assert tuple(parameter["name"] for parameter in static["_emit_cancel_pending_return_roots"]["call_sig"]) == (
        "self", "loop_exit", "root_base", "only_slot",
    )
    assert tuple(parameter["has_default"] for parameter in static["_emit_cancel_pending_return_roots"]["call_sig"]) == (False, True, True, True)
    assert tuple(parameter["name"] for parameter in static["_emit_owned_return_through_finally"]["call_sig"]) == (
        "self", "value", "stmt", "source_slot",
    )
    assert tuple(parameter["has_default"] for parameter in static["_emit_owned_return_through_finally"]["call_sig"]) == (False, False, False, True)


def test_exception_suppression_flag_has_one_c_python_export_identity():
    import re
    from pcc.runtime.py.py_abi_constants import PY_FLAG_EXC_SUPPRESS_CONTEXT
    from pcc.frontends.python.codegen.port_abi_exports import PORT_ABI_NATIVE_EXPORTS

    root = SOURCE.parents[3]
    header = (root / "pcc/runtime/src/py_internal.h").read_text()
    flags = {name: int(value, 0) for name, value in re.findall(
        r"^#define (PY_FLAG_\w+)\s+(0x[0-9a-fA-F]+|[0-9]+)$", header, re.MULTILINE,
    )}
    flag = PY_FLAG_EXC_SUPPRESS_CONTEXT
    assert flag == flags["PY_FLAG_EXC_SUPPRESS_CONTEXT"] == 0x08000000
    assert all(value & flag == 0 for name, value in flags.items() if name != "PY_FLAG_EXC_SUPPRESS_CONTEXT")
    assert PORT_ABI_NATIVE_EXPORTS["pcc.runtime.py.py_abi_constants"]["PY_FLAG_EXC_SUPPRESS_CONTEXT"]["value"] == flag
    generator = ast.parse((root / "scripts/gen_port_abi_constants.py").read_text())
    inventory = next(node.value for node in generator.body if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "FLAGS")
    assert "PY_FLAG_EXC_SUPPRESS_CONTEXT" in ast.literal_eval(inventory)


def test_c_api_explicit_cause_sets_suppression_preserving_gc_bits():
    from pcc.runtime.py.py_abi_constants import PY_FLAG_EXC_SUPPRESS_CONTEXT, PY_TYPE_EXC

    def atomic_or(operation, obj, offset, value, order):
        assert (operation, offset, order) == ("or", 12, "relaxed")
        obj[offset] |= value
    namespace = {
        "c_abi_export": lambda _name: lambda function: function,
        "c_abi_typed_export": lambda *_args: lambda function: function,
        "ptr_is_null": lambda value: value is None,
        "_type_of": lambda value: value[8],
        "PY_TYPE_EXC": PY_TYPE_EXC,
        "PY_FLAG_EXC_SUPPRESS_CONTEXT": PY_FLAG_EXC_SUPPRESS_CONTEXT,
        "pcc_gc_note_relocation_read": lambda value: value,
        "atomic_rmw_i32": atomic_or,
        "ptr_add": lambda owner, offset: offset,
        "pcc_gc_store_ptr": lambda owner, offset, value: owner.__setitem__(offset, value),
        "pcc_diagnostics_runtime_log_event_code": lambda *_args: None,
    }
    for filename, name in (("py_exc_objects.py", "py_exc_set_cause"), ("py_capi_misc_runtime.py", "PyException_SetCause")):
        path = SOURCE.with_name(filename)
        tree = ast.parse(path.read_text())
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)
    flags = 1 | 2 | 4 | 8 | 64 | 128 | 256 | 262144
    error = {8: PY_TYPE_EXC, 12: flags}
    cause = object()
    for value in (cause, None):
        error[12] = flags
        namespace["PyException_SetCause"](error, value)
        assert error[32] is value
        assert error[12] == flags | PY_FLAG_EXC_SUPPRESS_CONTEXT


def test_generator_close_result_reload_is_locked_before_pinning():
    """Exercise the actual close handoff with movement on graph-lock entry."""
    import copy
    import pytest

    source = SOURCE.with_name("py_gen.py")
    tree = ast.parse(source.read_text())
    close = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name == "py_gen_close")
    stopped = next(node for node in ast.walk(close) if isinstance(node, ast.If)
                   and isinstance(node.test, ast.Compare)
                   and isinstance(node.test.left, ast.Name)
                   and node.test.left.id == "stopped")
    clear_index = next(index for index, node in enumerate(stopped.body)
                       if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                       and isinstance(node.value.func, ast.Name)
                       and node.value.func.id == "py_clear_exception")
    handoff = stopped.body[clear_index + 1:]

    def execute(body, prior_pin):
        old = {12: prior_pin}
        moved = {12: prior_pin}
        slot = [old]
        state = {"locked": False, "pins": 0}

        def lock():
            assert not state["locked"]
            # The lock's entry safepoint moves the traced root first.
            slot[0] = moved
            state["locked"] = True

        def unlock():
            assert state["locked"] and moved[12] & 64
            state["locked"] = False

        def load(_owner, incoming):
            value = incoming[0]
            if not state["locked"]:
                # GC4 read-barrier root introspection may park on unlock.
                incoming[0] = moved
            return value

        def header(value, offset):
            assert state["locked"] and value is moved, "stale close result"
            return value[offset]

        def pin(value):
            assert state["locked"] and value is moved
            value[12] |= 64
            state["pins"] += 1

        def cleanup(*_args):
            assert not state["locked"] and slot[0] is moved and moved[12] & 64

        def take(incoming, saved_pin):
            cleanup()
            assert saved_pin == prior_pin
            value = incoming[0]
            value[12] = (value[12] & ~64) | saved_pin
            incoming[0] = None
            return value

        namespace = {
            "exc_slot": slot, "gen_slot": [object()], "gen_root": object(),
            "exc_root": object(), "null": lambda: None,
            "pcc_py_gc_minor_graph_lock": lock, "pcc_py_gc_minor_graph_unlock": unlock,
            "pcc_gc_load_ptr": load, "load_i32": header, "pcc_gc_pin": pin,
            "store_ptr": lambda target, offset, value: target.__setitem__(offset // 8, value),
            "is_tagged_int": lambda _value: False,
            "pcc_gc_store_root": cleanup,
            "pcc_gc_scheduler_root_unregister_handle": cleanup,
            "pcc_gc_take_pinned_slot": take,
        }
        function = ast.FunctionDef(name="handoff", args=ast.arguments(
            posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
            body=copy.deepcopy(body), decorator_list=[])
        unit = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
        exec(compile(unit, str(source), "exec"), namespace)
        assert namespace["handoff"]() is moved
        assert state == {"locked": False, "pins": 1}
        assert moved[12] == prior_pin and old[12] == prior_pin and slot[0] is None

    for prior_pin in (0, 64):
        execute(handoff, prior_pin)
    unlocked = [node for node in handoff if not (
        isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id in {"pcc_py_gc_minor_graph_lock", "pcc_py_gc_minor_graph_unlock"}
    )]
    with pytest.raises(AssertionError, match="stale close result"):
        execute(unlocked, 0)
