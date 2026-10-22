"""Error cleanup keeps the selecting exception across reentrant disposal.

These run production lowering against a moving-root model, including the real
weakref callback boundary. Host IR and stack-map checks are separate from the
still-required native qualification with a source-matched runtime.
"""

import os
from pathlib import Path
import re
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest

from pcc.frontends.python.codegen.call_object_lowering import CallObjectLoweringMixin
from tests.python.owned_regression_support import (
    assert_owned_program,
    assert_reference_program,
    explicit_owned_runtime,
)
from tests.python.test_foreign_address_leases import _functions


class CleanupModel(CallObjectLoweringMixin):
    def __init__(self, owners, callback, move, pending=True):
        self.pending = 900 if pending else 0
        self.original = self.pending
        self.tls_handle = int(pending)
        self.saved = None
        self.roots = {name: 100 + index * 2 for index, name in enumerate(owners)}
        self.registered = list(owners)
        self.retired = []
        self.flags = {}
        self.events = []
        self.callback = callback
        self.move = move
        self.callback_count = 0
        self.builder = SimpleNamespace(
            _block="entry", position_at_end=self.position, call=self.call,
            branch=lambda target: self.events.append(("branch", target)),
            store=self.store_flag,
        )
        self.current_function = SimpleNamespace(append_basic_block=lambda label: label)
        self.module = SimpleNamespace(globals={"py_tls_exc_swap_slot": "py_tls_exc_swap_slot"})
        self.runtime = {name: name for name in (
            "pcc_gc_store_root", "py_current_exception", "py_clear_exception",
            "pcc_gc_load_ptr", "py_raise", "pcc_gc_foreign_lease_release",
            "py_cleanup_one_root_preserving_exception",
            "py_cleanup_two_roots_preserving_exception",
            "py_cleanup_one_lease_preserving_exception",
        )}
        self._slot_call_root_records = [(name, None, True) for name in owners]
        self.weakref_memory = {}
        self.weakref_ns = dict(
            ptr_is_null=lambda value: int(value == 0), is_tagged_int=lambda value: 0,
            null=lambda: 0, ptr_eq=lambda left, right: int(left == right),
            global_load_ptr=lambda name: 10000,
            load_ptr=lambda base, offset: self.weakref_memory.get((base, offset), 0),
            load_i32=lambda *_: 104, store_ptr=self.weakref_store,
            pcc_gc_note_relocation_read=lambda value: value,
            pcc_diagnostics_runtime_log_event_code=lambda *_: None,
            ptr_add=lambda base, offset: base + offset,
            pcc_gc_load_ptr=lambda base, slot: 30000,
            py_tuple_new=lambda size: 20000, py_tuple_set_item=lambda *_: None,
            _py_none=lambda: 0, py_obj_call=self.weakref_callback,
            py_decref=lambda *_: None, py_clear_exception=self.clear,
        )
        _functions(Path(__file__).resolve().parents[2] / "pcc/runtime/py/py_weakref.py",
                   {"py_weakref_invalidate"}, self.weakref_ns)
        self.swap_ns = dict(
            global_load_ptr=lambda name: self.tls_handle,
            global_store_ptr=self.tls_store, global_addr=lambda name: name,
            null=lambda: 0, ptr_is_null=lambda value: int(value == 0),
            pcc_gc_load_ptr=lambda owner, slot: (
                self.pending if slot == "py_tls_current_exc_storage" else self.roots[slot]
            ),
            pcc_gc_scheduler_root_register_handle=self.register_tls,
            pcc_gc_scheduler_root_unregister_handle=lambda handle: None,
            pcc_platform_abort=lambda: pytest.fail("TLS root registration failed"),
            pcc_py_gc_minor_graph_lock=lambda: None,
            pcc_py_gc_minor_graph_unlock=lambda: None,
            pcc_gc_note_slot_write_barrier=lambda *_: None,
            store_ptr=lambda slot, offset, value: self.roots.__setitem__(slot, value),
        )
        _functions(Path(__file__).resolve().parents[2] / "pcc/runtime/py/py_substrate.py",
                   {"py_tls_exc_swap_slot"}, self.swap_ns)

    def position(self, block):
        self.builder._block = block

    def _fresh(self, label):
        return label

    def _as_gc_ptr(self, slot):
        return slot

    def _alloca_in_entry(self, _type, *, name, init_null):
        assert init_null
        self.saved = name
        self.roots[name] = 0
        return name

    def _gc_one_slot_frame_map(self):
        return "one owned slot"

    def _emit_current_gc_frame_enter_lifo(self, frame_map, slot):
        assert not self.roots[slot]
        self.registered.append(slot)
        self.collect()
        self.events.append(("register", slot))

    def _emit_gc_frame_leave_lifo_for_slot(self, slot):
        assert self.roots[slot] == 0
        assert self.registered and self.registered[-1] == slot, (slot, self.registered)
        self.registered.pop()
        if slot == self.saved:
            assert self.pending == self.original
            self.events.append(("retire-exception", slot))
        else:
            self.retired.append(slot)
            self.events.append(("retire-owner", slot))

    def store_flag(self, value, flag):
        self.flags[flag] = value.value
        self.events.append(("flag", flag))

    def tls_store(self, name, value):
        if name == "py_tls_current_exc_storage":
            self.pending = value
        else:
            assert name == "py_tls_current_exc_root_handle"
            self.tls_handle = value

    def register_tls(self, slot):
        assert slot == "py_tls_current_exc_storage"
        self.collect()
        return 1

    def clear(self):
        self.pending = 0
        self.events.append(("clear",))

    def weakref_store(self, base, offset, value):
        self.weakref_memory[base, offset] = value

    def collect(self):
        if not self.move:
            return
        # Rewrite authoritative roots after a callback. Restoring a borrowed
        # pre-disposal exception or releasing a stale raw operand would fail.
        old = self.original
        for name, value in self.roots.items():
            if value:
                self.roots[name] = value + 1000
        if self.pending:
            self.pending += 1000
        self.original = old + 1000 if old else 0
        self.events.append(("collect",))

    def weakref_callback(self, *_args):
        self.callback_count += 1
        self.collect()
        self.pending = -1  # The real weakref boundary clears an unraisable error.
        return 0

    def call(self, runtime, arguments, **_kwargs):
        if runtime.startswith("py_cleanup_"):
            from pcc.ir.compat import ir
            # Execute the production helper against the same moving-root model.
            namespace = dict(
                C_POINTER_SIZE=8, null=lambda: 0,
                stack_alloc=lambda size: self._alloca_in_entry(None, name="helper.exception", init_null=True),
                store_ptr=lambda slot, offset, value: self.roots.__setitem__(slot, value),
                global_addr=lambda name: name,
                pcc_gc_frame_enter_lifo=self._emit_current_gc_frame_enter_lifo,
                pcc_gc_frame_leave_lifo=self._emit_gc_frame_leave_lifo_for_slot,
                py_tls_exc_swap_slot=lambda slot: self.call("py_tls_exc_swap_slot", [slot]),
                py_clear_exception=self.clear,
                pcc_gc_store_root=lambda slot, value: self.call("pcc_gc_store_root", [slot, ir.Constant(ir.IntType(8).as_pointer(), None)]),
                pcc_gc_foreign_lease_release=lambda slot, token: self.call("pcc_gc_foreign_lease_release", [slot, token]),
            )
            _functions(Path(__file__).resolve().parents[2] / "pcc/runtime/py/py_cleanup_runtime.py", {runtime}, namespace)
            return namespace[runtime](*arguments)
        if runtime == "py_tls_exc_swap_slot":
            self.swap_ns[runtime](arguments[0])
            self.events.append(("swap", arguments[0]))
            return
        if runtime == "py_current_exception":
            return self.pending
        if runtime == "py_clear_exception":
            return self.clear()
        if runtime == "pcc_gc_load_ptr":
            return self.roots[arguments[1]]
        if runtime == "py_raise":
            assert arguments[0] == self.original
            self.pending = arguments[0]
            self.events.append(("restore", self.pending))
            return
        if runtime == "pcc_gc_foreign_lease_release":
            self.events.append(("lease", arguments[0]))
            return 0
        assert runtime == "pcc_gc_store_root"
        slot, value = arguments
        assert value.value is None
        old = self.roots[slot]
        assert slot in self.registered
        self.roots[slot] = 0  # Publication precedes finalizers and collection.
        self.events.append(("dispose", slot))
        if not old or self.callback_count:
            return
        if self.callback == "weakref":
            self.weakref_memory[10000, 16] = old
            self.weakref_ns["py_weakref_invalidate"](old)
        else:
            self.callback_count += 1
            saved = self.pending
            self.collect()
            self.pending = -1  # __del__ raises after collection.
            # The production __del__ boundary preserves caller TLS.
            self.pending = saved + (1000 if self.move and saved else 0)


@pytest.mark.parametrize("owners", [
    ("output", "argument"),
    ("output", "source", "keys"),
    ("output", "source", "keys", "key", "value"),
    ("output", "callee", "argument"),
], ids=("two-root-error", "dict-keys-error", "dict-item-error", "generic-call-error"))
@pytest.mark.parametrize("callback", ["weakref", "finalizer"])
@pytest.mark.parametrize("move", [False, True])
@pytest.mark.parametrize("pending", [False, True])
def test_real_cleanup_preserves_exception_and_retires_owners(owners, callback, move, pending):
    model = CleanupModel(owners, callback, move, pending)
    model._slot_call_cleanup_block(owners, "handler")
    assert model.pending == model.original, "root disposal lost the selecting exception"
    assert model.callback_count == 1
    assert model.retired == list(reversed(owners))
    kinds = [event[0] for event in model.events]
    assert max(i for i, kind in enumerate(kinds) if kind == "dispose") < kinds.index("retire-exception")
    assert kinds.index("retire-exception") < kinds.index("retire-owner")
    assert model.registered == [] and not any(model.roots.values())
    assert model.events[-1] == ("branch", "handler")
    assert model.builder._block == "entry"


def test_lease_only_cleanup_preserves_exception_with_balanced_helper_owner():
    model = CleanupModel((), "weakref", False)
    model._slot_call_cleanup_block((), "handler", (("argument", 1),))
    assert model.pending == model.original
    assert model.callback_count == 0 and model.registered == []
    assert [event[0] for event in model.events] == [
        "register", "swap", "lease", "clear", "swap", "retire-exception", "branch",
    ]


@pytest.mark.parametrize("pending", [False, True])
def test_empty_output_root_still_balances_cleanup_without_callback(pending):
    model = CleanupModel(("output",), "weakref", True, pending)
    model.roots["output"] = 0
    model._slot_call_cleanup_block(("output",), "handler")
    assert model.pending == model.original
    assert model.callback_count == 0
    assert model.registered == [] and not any(model.roots.values())


def test_combined_cleanup_saves_exception_before_releasing_leases_and_roots():
    model = CleanupModel(("output", "argument"), "weakref", True)
    model._slot_call_cleanup_block(("output", "argument"), "handler", (("argument", 1),))
    events = [event[0] for event in model.events]
    assert events.index("swap") < events.index("lease") < events.index("dispose")
    assert model.pending == model.original and model.registered == []


def test_generator_cleanup_clears_flags_but_retains_function_root_frames():
    owners = ("output", "argument")
    model = CleanupModel(owners, "weakref", True)
    model._slot_call_root_records = [(name, name + ".owned", False) for name in owners]
    model.flags = {name + ".owned": 1 for name in owners}
    model._slot_call_cleanup_block(owners, "handler")
    assert model.pending == model.original
    assert model.registered == list(owners) and model.retired == []
    assert not any(model.roots.values()) and not any(model.flags.values())
    for name in owners:
        assert model.events.index(("flag", name + ".owned")) < model.events.index(("dispose", name))


def assert_dict_copy_error_cleanup(body):
    """Check the keys-only and per-item CFG edges, owner sets and TLS scope."""
    aliases = dict(re.findall(r"(%[\w.]+) = bitcast ptr (%[\w.]+) to ptr", body))

    def original(slot):
        while slot in aliases:
            slot = aliases[slot]
        return slot

    blocks = dict(re.findall(r"^([\w.]+):\n(.*?)(?=^[\w.]+:|\Z)", body, re.M | re.S))

    def reachable(start):
        visited, queue = set(), [start]
        while queue:
            name = queue.pop()
            if name in visited:
                continue
            visited.add(name)
            queue.extend(re.findall(r"label %([\w.]+)", blocks.get(name, "")))
        return visited

    cleanup_by_owners = {}
    for name, block in blocks.items():
        if not name.startswith("call.slot.cleanup."):
            continue
        stores = list(re.finditer(r"@pcc_gc_store_root\(ptr (%[\w.]+), ptr null\)", block))
        if not stores:
            continue  # Lease-only cleanup cannot finalize an operand.
        owners = frozenset(original(store[1]).split(".operand")[0].lstrip("%") for store in stores)
        # Each disposing block owns a private exception slot only on this
        # error path. No borrowed current-exception pointer is ever needed.
        assert "@py_current_exception" not in block
        swaps = list(re.finditer(r"@py_tls_exc_swap_slot\(ptr (%[\w.]+)\)", block))
        assert len(swaps) == 2, block
        saved = original(swaps[0][1])
        assert saved == original(swaps[1][1]) and saved.startswith("%call.slot.exception.")
        # The frame map is passed directly (@global) or through an identity cast.
        enters = list(re.finditer(r"@pcc_gc_frame_enter_lifo\(ptr [%@][^,]+, ptr (%[\w.]+)\)", block))
        assert len(enters) == 1 and original(enters[0][1]) == saved, block
        clear = block.index("@py_clear_exception(")
        assert enters[0].start() < swaps[0].start() < stores[0].start()
        assert stores[-1].start() < clear < swaps[1].start(), block
        leaves = list(re.finditer(r"@pcc_gc_frame_leave_lifo\(ptr (%[\w.]+)\)", block))
        assert original(leaves[0][1]) == saved and swaps[1].start() < leaves[0].start()
        assert [original(leave[1]) for leave in leaves[1:]] == [original(store[1]) for store in stores]
        assert "@pcc_gc_store_root" not in block[swaps[1].end():], block
        assert "err.exit" in reachable(name), block
        cleanup_by_owners.setdefault(owners, set()).add(name)

    shared = {"dict.constructor.result", "dict.constructor.source", "dict.copy.keys"}
    keys_cleanup = cleanup_by_owners[frozenset(shared)]
    item_cleanup = cleanup_by_owners[frozenset(shared | {"dict.copy.key", "dict.copy.value"})]
    for helper, targets in (("py_obj_len", keys_cleanup), ("py_dict_get", item_cleanup),
                            ("py_dict_set_slots", item_cleanup)):
        calls = [name for name, block in blocks.items() if "@" + helper + "(" in block]
        assert calls and all(reachable(name) & targets for name in calls), helper


def test_cleanup_scopes_do_not_enroll_function_owners():
    from tests.python.test_slot_call_lexical_roots import _emit

    codegen, text = _emit("def probe(source: dict):\n    return dict(source)\n")
    assert "call.slot.exception" in text
    assert not any("call.slot.exception" in name for name in codegen._owned_local_names)
    assert not any("call.slot.exception" in name for name in codegen.env)


@pytest.mark.parametrize("source", [
    "source = {'a': 1}\ncopy = dict(source)\n",
    "def probe(source):\n    try:\n        return dict(source)\n    except Exception:\n        return None\n",
    "def probe(source):\n    try:\n        yield dict(source)\n    except Exception:\n        yield None\n",
])
def test_cleanup_local_frames_balance_at_module_function_and_generator_errors(source):
    from pcc.backend.self_backend_precise_stackmaps import build_stack_map_plans
    from pcc.backend.self_backend_prepare import prepare_module_for_target
    from pcc.backend.self_backend_x86_64_linux import _aggregate_returned_indirect
    from tests.python.test_slot_call_lexical_roots import _emit

    _codegen, text = _emit(source)
    assert "@py_tls_exc_swap_slot(" in text
    prepared = prepare_module_for_target(text, aggregate_returned_indirect=_aggregate_returned_indirect)
    plans = build_stack_map_plans(prepared.functions, prepared.globals_, target="x86_64-linux")
    assert len(plans) == len(prepared.functions)


PROGRAM = textwrap.dedent('''\
    import gc
    import weakref

    events = []
    references = []
    selected = ValueError("selected")

    class Token:
        def __init__(self, raises):
            self.raises = raises
        def __del__(self):
            events.append("finalizer")
            gc.collect()
            if self.raises:
                raise RuntimeError("unraisable finalizer")

    def callback(reference):
        events.append("weakref")
        gc.collect()

    def make(raises):
        token = Token(raises)
        references.append(weakref.ref(token, callback))
        return token

    def fail():
        raise selected

    def consume(*, value, later, **extras):
        raise AssertionError("entered callee")

    def main():
        for raises in (False, True):
            events.clear()
            try:
                consume(value=make(raises), later=fail())
            except ValueError as caught:
                assert caught is selected
            else:
                raise AssertionError("lost selecting exception")
            gc.collect()
            assert events == ["finalizer", "weakref"]
            assert references[-1]() is None
        print("SLOT_CLEANUP_EXCEPTION_OK")

    main()
''')


TWO_ROOT_PROGRAM = textwrap.dedent('''\
    import gc
    import weakref

    events = []
    references = []
    selected = ValueError("two-root selected")

    class Token:
        def __del__(self):
            events.append("finalizer")
            gc.collect()

    def callback(reference):
        events.append("weakref")
        gc.collect()

    def make():
        token = Token()
        references.append(weakref.ref(token, callback))
        return token

    def fail():
        raise selected

    def consume(*arguments):
        raise AssertionError("entered callee")

    def probe(callee):
        return callee(make(), fail())

    def main():
        try:
            probe(consume)
        except ValueError as caught:
            assert caught is selected
            # The argument list's owner must have been released by the
            # two-root error edge before control reaches this handler.
            assert events == ["finalizer", "weakref"]
            assert references[-1]() is None
        else:
            raise AssertionError("lost selecting exception")
        gc.collect()
        assert events == ["finalizer", "weakref"]
        print("TWO_ROOT_CLEANUP_EXCEPTION_OK")

    main()
''')


def _assert_two_root_program_error_edge():
    from tests.python.test_owned_cleanup_helper_ir import verify_stackmaps
    from tests.python.test_shared_call_binding import _function
    from tests.python.test_slot_call_lexical_roots import _emit

    _codegen, text = _emit(TWO_ROOT_PROGRAM, "two_root_cleanup")
    verify_stackmaps(text)
    body = _function(text, "user_two_root_cleanup_probe")
    blocks = dict(re.findall(r"^([\w.]+):\n(.*?)(?=^[\w.]+:|^})", body, re.M | re.S))
    aliases = dict(re.findall(r"(%[\w.]+) = bitcast ptr (%[\w.]+) to ptr", body))

    def original(slot):
        while slot in aliases:
            slot = aliases[slot]
        return slot

    exact_edges = set()
    for name, block in blocks.items():
        call = re.search(
            r"@py_cleanup_two_roots_preserving_exception\(ptr (%[\w.]+), ptr (%[\w.]+)\)",
            block,
        )
        if call is None:
            continue
        roots = tuple(original(slot) for slot in call.groups())
        if not (".args.operand." in roots[0] and ".args.list.operand." in roots[1]):
            continue
        leaves = re.findall(r"@pcc_gc_frame_leave_lifo\(ptr (%[\w.]+)\)", block)
        assert tuple(original(slot) for slot in leaves) == tuple(reversed(roots))
        assert "@py_tls_exc_swap_slot(" not in block and "@pcc_gc_store_root(" not in block
        exact_edges.add(name)
    assert len(exact_edges) == 1
    failures = [name for name, block in blocks.items()
                if re.search(r"\bcall\b[^\n]*@user_two_root_cleanup_fail\(", block)]
    assert len(failures) == 1
    # Follow the successful publication/lease-status checks to the pending-TLS
    # check after fail(). A lease-failure edge alone does not witness this case.
    name = failures[0]
    visited = set()
    while True:
        assert name not in visited
        visited.add(name)
        block = blocks[name]
        branch = re.search(
            r"br i1 (%[\w.]+), label %([\w.]+), label %([\w.]+)", block,
        )
        assert branch is not None
        flag = re.search(r"(%[\w.]+) = call i64 \(\) @py_err_occurred\(\)", block)
        if flag is not None:
            assert re.search(
                re.escape(branch[1]) + r" = icmp ne i64 " + re.escape(flag[1]) + r", 0", block,
            )
            name = branch[2]
            break
        assert re.search(re.escape(branch[1]) + r" = icmp slt i64 %[^,]+, 0", block)
        name = branch[3]
    # This pending-error edge must reach the exact helper through unconditional
    # cleanup branches, rather than merely being able to reach it in the CFG.
    while name not in exact_edges:
        assert name not in visited
        visited.add(name)
        branch = re.search(r"br label %([\w.]+)", blocks[name])
        assert branch is not None and "br i1 " not in blocks[name]
        name = branch[1]
    return text


def test_two_root_cleanup_program_reference_and_error_edge(tmp_path):
    assert_reference_program(TWO_ROOT_PROGRAM, "TWO_ROOT_CLEANUP_EXCEPTION_OK\n", tmp_path)
    (tmp_path / "two-root-cleanup.ll").write_text(_assert_two_root_program_error_edge())


@pytest.mark.integration
@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_two_root_cleanup_error_path_native_five_gc(
    python_program_compiler, request, explicit_owned_runtime, tmp_path, capfd,
):
    # Native execution under all five modes is separate from the deterministic
    # moving-root and hostile intermediate-TLS witnesses in helper-body tests.
    (tmp_path / "two-root-cleanup.ll").write_text(_assert_two_root_program_error_edge())
    mode = request.node.callspec.params["python_program_compiler"]
    assert_owned_program(
        TWO_ROOT_PROGRAM, "TWO_ROOT_CLEANUP_EXCEPTION_OK\n", tmp_path,
        python_program_compiler, mode, explicit_owned_runtime, capfd,
        provenance_probe="2",
    )


def test_cleanup_callback_program_reference(tmp_path):
    source = tmp_path / "reference.py"
    source.write_text(PROGRAM)
    result = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "SLOT_CLEANUP_EXCEPTION_OK\n"


@pytest.mark.integration
def test_cleanup_callback_program_native_five_gc(tmp_path):
    from tests.python.test_native_container_builtin_error_paths import _run_native

    assert os.environ.get("PCC_RUNTIME_ARCHIVE"), "Select an explicit source-matched runtime archive"
    result = _run_native(tmp_path, PROGRAM)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "SLOT_CLEANUP_EXCEPTION_OK\n"
    for backend in range(5):
        result = subprocess.run(
            [str(tmp_path / "prog.out")],
            env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stdout == "SLOT_CLEANUP_EXCEPTION_OK\n", backend
