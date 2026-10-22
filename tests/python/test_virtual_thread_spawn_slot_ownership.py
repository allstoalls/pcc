"""Spawn transfers NEW owners before cleanup and keeps the real call sink live."""

import os
import subprocess
from types import SimpleNamespace

import pytest

from pcc.backend.self_backend_kernel import get_indexed_function_kernel
from pcc.backend.self_backend_parse import parse_self_backend_module
from pcc.backend.self_backend_verify import verify_parsed_module
from pcc.diagnostics.gc_log import parse_log_lines
from tests.python.root_slot_contract import RootSlotContract
from tests.python.test_slot_call_operand_roots import (
    _emit,
    _probe_function,
)


def _spawn_source(generator=False, sink=True, module_scope=False, resume=False):
    source = "import pcc.virtual_thread as vt\ndef worker(value: int):\n"
    if generator:
        source += "    vt.yield_now()\n"
    source += "    return value\n"
    expression = "vt.spawn(worker, 42)"
    if sink:
        expression = "slot_operand_probe(" + expression + ")"
    if module_scope:
        return source + expression + "\n"
    return source + "def probe():\n" + ("    vt.yield_now()\n" if resume else "") + "    return " + expression + "\n"


def _assert_singleton_root_cleanup(block, addresses):
    """Recognize the audited helper without hiding caller-slot retirement.

    The production helper saves/restores the selecting exception internally;
    tests in test_owned_cleanup_helpers execute that body with hostile moving
    callbacks. Here the exact caller owner must remain live through the call
    and retire immediately afterward on this real error CFG.
    """
    calls = [ins for ins in block.instructions if ins.kind == "call"]
    assert (len(calls) == 2
            and addresses.called(calls[0], "py_cleanup_one_root_preserving_exception")
            and not calls[0].data[3]
            and addresses.called(calls[1], "pcc_gc_frame_leave_lifo")
            and not calls[1].data[3]), (
            "singleton cleanup needs helper before its sole caller retirement"
        )
    assert all(ins.kind == "call" or (ins.kind == "cast" and ins.data[0] == "bitcast")
               for ins in block.instructions), "singleton cleanup cannot overwrite its owner"
    helper_args, leave_args = addresses.args(calls[0]), addresses.args(calls[1])
    assert len(helper_args) == len(leave_args) == 1
    owner = addresses.slot(helper_args[0])
    assert owner == addresses.slot(leave_args[0]), "singleton helper must clear the retired owner"
    addresses.require_owning(owner, allow_lifo=True)
    assert any(addresses.called(ins, "pcc_gc_frame_enter_lifo")
               and not ins.data[3]
               and addresses.slot(addresses.args(ins)[1]) == owner for _, _, ins in addresses.rows), (
        "singleton helper requires the caller's lexical registration"
    )
    assert block.terminator.kind == "br", "singleton cleanup must branch to its error target"
    assert block.terminator.data[0] != block.name, "singleton cleanup cannot loop before its error target"
    return owner


def _assert_spawn_contract(text, mutate_cleanup=None):
    module = parse_self_backend_module(text)
    verify_parsed_module(module)
    functions = []
    for fn in module.functions:
        blocks = get_indexed_function_kernel(fn).materialize_legacy_blocks(fn)
        rows = [(block, index, ins) for block in blocks
                for index, ins in enumerate(block.instructions)]
        if any(ins.kind == "call" and ins.data[2] == "py_virtual_thread_new"
               for _, _, ins in rows):
            functions.append((blocks, rows))
    assert len(functions) == 1
    blocks, rows = functions[0]
    if mutate_cleanup is not None:
        blocks = mutate_cleanup(blocks)
        rows = [(block, index, ins) for block in blocks
                for index, ins in enumerate(block.instructions)]
    addresses = RootSlotContract(blocks, module.globals_)
    helper_clears = {block.name: _assert_singleton_root_cleanup(block, addresses)
                     for block in blocks if any(addresses.called(ins, "py_cleanup_one_root_preserving_exception")
                                                for ins in block.instructions)}
    # Local helper-before-leave shape plus the complete frame-state proof
    # establishes that its caller owner is live throughout the helper call.
    addresses.assert_frame_exits()
    slot = addresses.slot
    loads = {ins.data[0]: ins.data[3] for _, _, ins in rows if ins.kind == "load"}

    def called(ins, name):
        return ins.kind == "call" and ins.data[2] == name

    def args(ins):
        return [value for _, value in ins.data[4]]

    allocations = [(block, index, ins) for block, index, ins in rows
                   if called(ins, "py_virtual_thread_new")
                   or called(ins, "py_continuation_new_typed")
                   or (ins.kind == "call" and ins.data[2] == "user_slot_operand_worker")]
    by_name = {block.name: block for block in blocks}
    assert "err.exit" in by_name, "fixture needs its independently named enclosing error exit"
    thread_root = None
    allocated_roots = []
    for block, index, ins in allocations:
        published = block.instructions[index + 1]
        assert published.kind == "store" and published.data[1] == ins.data[0]
        root = slot(published.data[3])
        addresses.require_owning(root, allow_lifo=True)
        assert root not in allocated_roots, "different allocations alias the same physical root cell"
        allocated_roots.append(root)
        null_checks = [row for _, _, row in rows if row.kind == "icmp"
                       and row.data[0] == "eq" and row.data[4] == "null"
                       and row.data[3] in loads and slot(loads[row.data[3]]) == root]
        assert null_checks, "each allocated owner needs an explicit NULL rejection"
        statuses = [row for _, _, row in rows if row.kind == "select"
                    and any(row.data[2] == check.data[1] for check in null_checks)
                    and row.data[3:] == ("-1", "0")]
        assert statuses
        for status in statuses:
            check_block, check = next((owner, row) for owner, _, row in rows
                                      if row.kind == "icmp" and row.data[0] == "slt"
                                      and row.data[3:] == (status.data[0], "0"))
            assert check_block.terminator.data[0] == check.data[1]
            error = by_name[check_block.terminator.data[1]]
            seen, cleared = set(), []
            while error.name not in seen:
                seen.add(error.name)
                assert not any(called(row, "py_virtual_thread_start") for row in error.instructions)
                cleared.extend(slot(args(row)[0]) for row in error.instructions
                               if called(row, "pcc_gc_store_root") and args(row)[1] == "null")
                if error.name in helper_clears:
                    cleared.append(helper_clears[error.name])
                term = error.terminator
                if term.kind == "br":
                    error = by_name[term.data[0]]
                elif term.kind == "br_cond":
                    error = by_name[term.data[1]]
                else:
                    break
            assert root in cleared, "allocated owner must be cleared on its NULL error path"
            assert "err.exit" in seen, "NULL error cleanup must reach the enclosing error exit"
        if called(ins, "py_virtual_thread_new"):
            thread_root = root
            thread_ssa = ins.data[0]
    assert thread_root is not None
    start = next(ins for _, _, ins in rows if called(ins, "py_virtual_thread_start"))
    assert slot(loads[args(start)[0]]) == thread_root
    assert any(called(ins, "pcc_gc_foreign_lease_acquire")
               and slot(args(ins)[0]) == thread_root for _, _, ins in rows)
    failure = next(block for block in blocks if block.name.startswith("vthread.start.fail"))
    clear = next(i for i, ins in enumerate(failure.instructions)
                 if called(ins, "pcc_gc_store_root") and args(ins)[1] == "null"
                 and slot(args(ins)[0]) == thread_root)
    allocate = next(i for i, ins in enumerate(failure.instructions) if called(ins, "py_exc_new"))
    raised = next(i for i, ins in enumerate(failure.instructions) if called(ins, "py_raise"))
    assert clear < allocate < raised
    # Returning the producer's old SSA after start would evade relocation.
    assert not any(block.terminator.kind == "ret" and thread_ssa in block.terminator.data
                   for block in blocks)
    return text


@pytest.mark.parametrize("generator", (False, True))
@pytest.mark.parametrize("direct", ("0", "1"))
@pytest.mark.parametrize("sink", (False, True))
@pytest.mark.parametrize("scope", ("function", "module", "resume"))
def test_spawn_publishes_all_owners_before_cleanup(generator, direct, sink, scope, monkeypatch):
    monkeypatch.setenv("PCC_DIRECT_GENERATOR_TASKS", direct)
    text = _assert_spawn_contract(_emit(_spawn_source(generator, sink, scope == "module", scope == "resume")))
    if sink:
        assert "probe.operand" in text


def test_spawn_allocation_errors_keep_cleanup_and_strict_stale_rejection():
    from pcc.frontends.python.codegen.errors import L1CodegenError
    text = _assert_spawn_contract(_emit(_spawn_source()))
    assert "@py_tls_exc_swap_slot(" in text
    with pytest.raises(L1CodegenError, match="lacks an immediate owned-result handoff"):
        _emit("def probe():\n    return slot_stale_probe()\n")


@pytest.fixture(scope="module")
def singleton_spawn_ir():
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("PCC_DIRECT_GENERATOR_TASKS", "0")
        return _emit(_spawn_source(generator=False, sink=True))


def test_spawn_singleton_root_cleanup_contract(singleton_spawn_ir):
    _assert_spawn_contract(singleton_spawn_ir)


def _mutate_singleton_spawn_cleanup(blocks, mutation, applied):
    copied = [SimpleNamespace(name=block.name, instructions=list(block.instructions),
                              terminator=block.terminator) for block in blocks]
    addresses = RootSlotContract(copied)
    publications = [(ins.data[2], block.instructions[index + 1].data[3])
                    for block, index, ins in addresses.rows
                    if ins.kind == "call" and ins.data[2] in (
                        "py_virtual_thread_new", "py_continuation_new_typed")]
    root_value = next(value for name, value in publications if name == "py_virtual_thread_new")
    root = addresses.slot(root_value)
    # This other SSA is a real, distinct, positively registered NEW owner in
    # the unmodified positive, not an invalid/unbound pointer spelling.
    other = next(value for _, value in publications if addresses.slot(value) != root)
    target, index, helper = next((block, index, ins) for block, index, ins in addresses.rows
                                if addresses.called(ins, "py_cleanup_one_root_preserving_exception")
                                and addresses.slot(addresses.args(ins)[0]) == root)
    instructions = target.instructions

    def with_args(ins, arguments):
        return SimpleNamespace(kind=ins.kind, data=ins.data[:4] + (arguments,) + ins.data[5:])

    if mutation == "missing-helper":
        instructions.pop(index)
    elif mutation in ("wrong-valid-owner", "null-owner"):
        value = other if mutation == "wrong-valid-owner" else "null"
        instructions[index] = with_args(helper, ((helper.data[4][0][0], value),))
    elif mutation in ("missing-retirement", "retire-before-helper"):
        leave_index = next(i for i, ins in enumerate(instructions)
                           if addresses.called(ins, "pcc_gc_frame_leave_lifo"))
        leave = instructions.pop(leave_index)
        if mutation == "retire-before-helper":
            # Reuse the already-defined helper address to preserve valid SSA
            # while moving retirement ahead of the potentially reentrant drop.
            instructions.insert(index, with_args(leave, helper.data[4]))
    else:
        assert mutation == "bypass-cleanup"
        predecessor = next(block for block in copied if block.terminator.kind == "br"
                           and block.terminator.data[0] == target.name)
        assert target.terminator.kind == "br"
        predecessor.terminator = SimpleNamespace(kind="br", data=target.terminator.data)
    applied.append(mutation)
    return copied


@pytest.mark.parametrize("mutation, diagnostic", [
    ("missing-helper", "allocated owner must be cleared"),
    ("wrong-valid-owner", "singleton helper must clear the retired owner"),
    ("null-owner", "singleton helper must clear the retired owner"),
    ("missing-retirement", "singleton cleanup needs helper before"),
    ("retire-before-helper", "singleton cleanup needs helper before"),
    ("bypass-cleanup", "inconsistent live root frames|active root frames"),
])
def test_spawn_singleton_cleanup_rejects_wrong_owner_or_retirement(
    singleton_spawn_ir, mutation, diagnostic,
):
    _assert_spawn_contract(singleton_spawn_ir)
    applied = []
    with pytest.raises(AssertionError, match=diagnostic):
        _assert_spawn_contract(singleton_spawn_ir, mutate_cleanup=lambda blocks:
                               _mutate_singleton_spawn_cleanup(blocks, mutation, applied))
    assert applied == [mutation], "the negative case must reach the singleton ownership validator"


@pytest.mark.parametrize("runtime, diagnostic", [
    ("py_cleanup_one_root_preserving_exception", "singleton cleanup needs helper before"),
    ("pcc_gc_frame_enter_lifo", "singleton helper requires the caller's lexical registration"),
    ("pcc_gc_frame_leave_lifo", "singleton cleanup needs helper before"),
])
def test_spawn_singleton_cleanup_rejects_same_name_indirect_calls(
    singleton_spawn_ir, runtime, diagnostic,
):
    _assert_spawn_contract(singleton_spawn_ir)
    body = _probe_function(singleton_spawn_ir)
    direct, indirect = "@" + runtime + "(", "%" + runtime + "("
    line = next(line for line in body.splitlines(keepends=True)
                if "call " in line and direct in line)
    assert "%" + runtime not in body, "indirect control needs an unused local SSA name"
    # Give the local function pointer a real dominating definition, so this
    # is valid IR with the same decoded callee name, not an undefined SSA.
    replacement = ("  %" + runtime + " = bitcast ptr @" + runtime + " to ptr\n"
                   + line.replace(direct, indirect, 1))
    changed = body.replace(line, replacement, 1)
    assert changed != body and changed.count(indirect) == 1, "indirect mutation must apply"
    mutated = singleton_spawn_ir.replace(body, changed, 1)
    module = parse_self_backend_module(mutated)
    verify_parsed_module(module)
    calls = [ins for fn in module.functions
             for block in get_indexed_function_kernel(fn).materialize_legacy_blocks(fn)
             for ins in block.instructions
             if ins.kind == "call" and ins.data[2] == runtime and ins.data[3]]
    assert len(calls) == 1, "control must preserve the name while changing directness"
    with pytest.raises(AssertionError, match=diagnostic):
        _assert_spawn_contract(mutated)


NATIVE_SOURCE = '''import gc
import pcc.virtual_thread as vt
from pcc.extern import c_int64, extern
actual_backend = extern("pcc_gc_backend", (), c_int64)
events = []
class Token:
    def __init__(self, value):
        self.value = value
    def __del__(self):
        events.append(self.value)
class Sink:
    def keep(self, task, checked):
        assert checked == 7
        return task
def collect_later():
    gc.collect()
    assert events == []
    return 7
def ordinary(token):
    gc.collect()
    return token.value
def yielding(token):
    vt.yield_now()
    gc.collect()
    return token.value
def failing(token):
    vt.yield_now()
    raise ValueError("spawn child failed")
def main():
    print(actual_backend())
    sink = Sink()
    first = sink.keep(vt.spawn(ordinary, Token(41)), collect_later())
    second = sink.keep(vt.spawn(yielding, Token(42)), collect_later())
    third = sink.keep(vt.spawn(failing, Token(43)), collect_later())
    vt.run(1, 128)
    assert vt.result(first) == 41
    assert vt.result(second) == 42
    assert vt.outcome(third) == vt.OUTCOME_RAISED
    assert str(vt.exception(third)) == "spawn child failed"
    first = None
    second = None
    third = None
    gc.collect()
    assert sorted(events) == [41, 42, 43]
    print("PCC_SPAWN_SLOT_OWNERSHIP_OK")
main()
'''


def test_native_spawn_regression_emits_complete_owned_ir():
    text = _emit(NATIVE_SOURCE)
    assert "strict.nolib.stub:" not in text
    verify_parsed_module(parse_self_backend_module(text))


@pytest.mark.integration
@pytest.mark.parametrize("direct", ("0", "1"))
def test_spawn_slot_ownership_native_all_gc(tmp_path, pcc_runtime_archive, monkeypatch, direct):
    from pcc.frontends.python.pipeline import compile_python
    monkeypatch.setenv("PCC_DIRECT_GENERATOR_TASKS", direct)
    source = tmp_path / "spawn_slot.py"
    executable = tmp_path / "spawn_slot"
    source.write_text(NATIVE_SOURCE, encoding="utf-8")
    compile_python(str(source), str(executable), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        log = tmp_path / ("gc" + str(backend) + ".jsonl")
        ran = subprocess.run(
            [str(executable)], capture_output=True, text=True, timeout=20,
            env=dict(os.environ, PATH="/nonexistent", PCC_GC_BACKEND=str(backend),
                     PCC_LOG="gc", PCC_LOG_FORMAT="json", PCC_LOG_FILE=str(log)),
        )
        assert ran.returncode == 0, (backend, ran.stdout, ran.stderr)
        assert ran.stdout.splitlines() == [str(backend), "PCC_SPAWN_SLOT_OWNERSHIP_OK"]
        assert ran.stderr == ""
        assert log.is_file()
        observed = {event.fields["value1"] for event in parse_log_lines(log.read_text().splitlines())
                    if event.fields.get("category") == "gc"
                    and event.event in ("collect_start", "collect_stop", "collect_end")}
        assert observed == {backend}
