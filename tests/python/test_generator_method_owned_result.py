"""Proven generator intrinsics publish results before checks or cleanup.

Source-extracted emitter models do not import the compiler. IR and native
qualification remain separate from those host-model checks.
"""
from __future__ import annotations

import ast
import copy
from pathlib import Path
import re
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
LOWERING = ROOT / "pcc/frontends/python/codegen/method_call_expression_lowering.py"


class _DynType:
    def __init__(self, name):
        self.name = name


def _host_branch():
    tree = ast.parse(LOWERING.read_text())
    method = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.FunctionDef) and node.name == "_emit_method_call")
    start = next(index for index, node in enumerate(method.body)
                 if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == "gen_intrinsic_ok"
                         for target in node.targets))
    end = next(index for index in range(start, len(method.body))
               if isinstance(method.body[index], ast.If)
               and isinstance(method.body[index].test, ast.Name)
               and method.body[index].test.id == "gen_runtime_name")
    wrapper = ast.parse("def emit(self, expr):\n    attr = expr.func\n").body[0]
    wrapper.body.extend(copy.deepcopy(method.body[start:end + 1]))
    namespace = {"DynType": _DynType}
    extracted = ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[]))
    exec(compile(extracted, "real_generator_intrinsic_branch", "exec"), namespace)
    return namespace["emit"]


def _expression(method="send", receiver_kind="generator", kwargs=(), argc=None):
    receiver = SimpleNamespace(ty=_DynType(receiver_kind), label="receiver")
    count = (0 if method == "close" else 1) if argc is None else argc
    arguments = tuple(SimpleNamespace(label="argument") for _ in range(count))
    return SimpleNamespace(func=SimpleNamespace(obj=receiver, name=method),
                           args=arguments, kwargs=kwargs, span="source-span")


class _Host:
    def __init__(self, sink=None, failure=None, outer="outer-error"):
        self.sink = sink
        self.failure = failure
        self._try_err_block = outer
        self._cpy_operand_cleanup_block = "outer-cpy"
        self.events = []
        self.slots = {} if sink is None else {sink: None}
        self.result = object()
        self.builder = SimpleNamespace(load=self._load)

    def _current_try_err_block(self):
        return self._try_err_block

    def _ensure_fn_err_exit(self):
        return "function-error"

    def _slot_call_result_sink(self, expr):
        return self.sink

    def _new_slot_call_root(self, label):
        root = object()
        self.slots[root] = None
        self.events.append(("root", root, label))
        return root

    def _slot_call_cleanup_block(self, roots, target):
        return (roots, target)

    def _emit_slot_call_operand(self, expr, label):
        self.events.append(("evaluate", expr.label, self._try_err_block))
        if self.failure == expr.label:
            raise ValueError("operand failed")
        root = self._new_slot_call_root(label)
        self.slots[root] = object()
        self.events.append(("operand", expr.label, root))
        return root

    def _slot_call_runtime_call(self, name, operands, result_slot, span):
        assert span == "source-span"
        assert all(self.slots[root] is not None for root in operands)
        assert self.slots[result_slot] is None
        self.events.append(("runtime", name, operands, result_slot, self._try_err_block))
        # The existing shared helper owns immediate publication, counted
        # leases and TLS preservation. This model checks its call contract.
        self.slots[result_slot] = self.result
        if self.failure == "runtime":
            raise ValueError("original runtime error")

    def _release_slot_call_roots(self, roots):
        self.events.append(("release", roots))
        for root in roots:
            self.slots[root] = None

    def _take_slot_call_root(self, root):
        self.events.append(("take", root))
        result = self.slots[root]
        self.slots[root] = None
        return result

    def _load(self, root, name):
        self.events.append(("load", root))
        return self.slots[root]

    def _fresh(self, name):
        return name


@pytest.mark.parametrize("method", ("send", "throw", "close"))
@pytest.mark.parametrize("with_sink", (False, True))
@pytest.mark.parametrize("outer", (None, "outer-error"))
def test_generator_method_host_model_owns_operands_and_result(method, with_sink, outer):
    host = _Host(object() if with_sink else None, outer=outer)
    result = _host_branch()(host, _expression(method))
    assert result is host.result
    evaluations = [row[1] for row in host.events if row[0] == "evaluate"]
    assert evaluations == (["receiver"] if method == "close" else ["receiver", "argument"])
    invocation = next(row for row in host.events if row[0] == "runtime")
    _, name, operands, output, cleanup = invocation
    assert name == "py_gen_" + method
    assert len(set(operands)) == len(operands)
    assert output not in operands
    assert cleanup[0] == ((output,) if not with_sink else ()) + operands
    assert cleanup[1] == ("function-error" if outer is None else outer)
    assert host.events[host.events.index(invocation) + 1] == ("release", operands)
    assert host.events[-1] == (("load", output) if with_sink else ("take", output))
    assert all(host.slots[root] is None for root in operands)
    assert host._try_err_block == outer
    assert host._cpy_operand_cleanup_block == "outer-cpy"


@pytest.mark.parametrize("method", ("send", "throw", "close"))
@pytest.mark.parametrize("with_sink", (False, True))
@pytest.mark.parametrize("failure", ("receiver", "runtime"))
def test_generator_method_host_model_failure_keeps_cleanup(method, with_sink, failure):
    host = _Host(object() if with_sink else None, failure=failure)
    with pytest.raises(ValueError):
        _host_branch()(host, _expression(method))
    assert not any(row[0] in ("release", "load", "take") for row in host.events)
    failing = next(row for row in host.events
                   if row[0] == ("runtime" if failure == "runtime" else "evaluate"))
    cleanup = failing[-1]
    expected = (0 if with_sink else 1) + (len(failing[2]) if failure == "runtime" else 0)
    assert len(cleanup[0]) == expected
    assert host._try_err_block == "outer-error"
    assert host._cpy_operand_cleanup_block == "outer-cpy"


@pytest.mark.parametrize("method", ("send", "throw"))
@pytest.mark.parametrize("with_sink", (False, True))
def test_generator_method_host_model_later_operand_error_roots_receiver(method, with_sink):
    host = _Host(object() if with_sink else None, failure="argument")
    with pytest.raises(ValueError, match="operand failed"):
        _host_branch()(host, _expression(method))
    receiver = next(row[2] for row in host.events if row[:2] == ("operand", "receiver"))
    failure = next(row for row in host.events if row[:2] == ("evaluate", "argument"))
    assert failure[2][0][-1] is receiver
    assert len(failure[2][0]) == (1 if with_sink else 2)
    assert not any(row[0] in ("runtime", "release", "take") for row in host.events)
    assert host._try_err_block == "outer-error"
    assert host._cpy_operand_cleanup_block == "outer-cpy"


@pytest.mark.parametrize("method", ("send", "throw", "close"))
@pytest.mark.parametrize("shape", ("dynamic", "keyword", "wrong_arity"))
def test_generator_method_host_model_preserves_dispatch_guard(method, shape):
    host = _Host()
    options = {}
    if shape == "dynamic":
        options["receiver_kind"] = "dyn"
    elif shape == "keyword":
        options["kwargs"] = (("value", object()),)
    else:
        options["argc"] = 1 if method == "close" else 0
    assert _host_branch()(host, _expression(method, **options)) is None
    assert host.events == []


PROGRAM = '''import gc
def sending():
    yield 0
    yield 73
def sending_heap():
    yield 0
    yield [73]
def throwing():
    try:
        yield 0
    except ValueError:
        yield 73
def throwing_heap():
    try:
        yield 0
    except ValueError:
        yield [73]
def closing():
    try:
        yield 0
    except GeneratorExit:
        return [73]
def take(first, second, third=("omitted",)):
    # Omitting the optional formal selects the runtime binder's owning sink:
    # a tuple default's identity belongs to the function, not the call site.
    return first
def later():
    gc.collect()
    return None
def compare_send():
    generator = sending()
    assert next(generator) == 0
    for expected in range(73, 74):
        assert generator.send(None) == expected
    generator.close()
def compare_throw():
    generator = throwing()
    assert next(generator) == 0
    for expected in range(73, 74):
        assert generator.throw(ValueError("sentinel")) == expected
    generator.close()
def close_value():
    generator = closing()
    assert next(generator) == 0
    value = take(generator.close(), later())
    assert value[0] == 73
def heap_send():
    generator = sending_heap()
    next(generator)
    value = take(generator.send(None), later())
    assert value[0] == 73
    generator.close()
def heap_throw():
    generator = throwing_heap()
    next(generator)
    value = take(generator.throw(ValueError("sentinel")), later())
    assert value[0] == 73
    generator.close()
def discard_send():
    generator = sending()
    assert next(generator) == 0
    generator.send(None)
    gc.collect()
    generator.close()
def original_exception():
    generator = sending()
    next(generator)
    generator.send(None)
    try:
        generator.send(None)
    except StopIteration:
        pass
    else:
        raise AssertionError("StopIteration lost")
def dynamic_send(receiver):
    return receiver.send(None)
def dynamic_throw(receiver, error):
    return receiver.throw(error)
def dynamic_close(receiver):
    return receiver.close()
'''


def test_generator_method_host_reference_protocol():
    namespace = {}
    exec(PROGRAM, namespace)
    for name in ("compare_send", "compare_throw", "close_value", "heap_send", "heap_throw",
                 "discard_send", "original_exception"):
        namespace[name]()


def _function(text, name):
    match = re.search(r"(?ms)^define [^\n]*@user_[^\n(]*_" + re.escape(name)
                      + r"\([^\n]*\n.*?^\}", text)
    assert match is not None, name
    return match.group(0)


def _assert_generator_function_slots(module, function, symbol, mode, mutation=None):
    from pcc.backend.self_backend_kernel import get_indexed_function_kernel
    from tests.python.root_slot_contract import RootSlotContract

    original = get_indexed_function_kernel(function).materialize_legacy_blocks(function)
    blocks = [SimpleNamespace(name=block.name, instructions=list(block.instructions),
                              terminator=block.terminator) for block in original]
    if mutation is not None:
        block, index, invoke = next((block, index, ins) for block in blocks
                                    for index, ins in enumerate(block.instructions)
                                    if ins.kind == "call" and ins.data[2] == symbol)
        published = block.instructions[index + 1]
        assert published.kind == "store" and published.data[1] == invoke.data[0]
        if mutation == "missing-publication":
            block.instructions.pop(index + 1)
        elif mutation in ("premature-result-clear", "one-success-edge-clears-result"):
            clear = next(ins for owner in blocks for ins in owner.instructions
                         if ins.kind == "call" and ins.data[2] == "pcc_gc_store_root")
            arguments = ((clear.data[4][0][0], published.data[3]),
                         (clear.data[4][1][0], "null"))
            damaged = SimpleNamespace(kind="call", data=clear.data[:4] + (arguments,) + clear.data[5:])
            if mutation == "premature-result-clear":
                block.instructions.insert(index + 2, damaged)
            else:
                # Both branches now reach the real consumer. One retains the
                # owner and one clears it; finding just one good path is wrong.
                term = block.terminator
                assert term.kind == "br_cond"
                name = "mutated.success.with.cleared.result"
                assert all(owner.name != name for owner in blocks)
                blocks.append(SimpleNamespace(name=name, instructions=[damaged],
                                              terminator=SimpleNamespace(kind="br", data=(term.data[2],))))
                block.terminator = SimpleNamespace(kind="br_cond", data=(term.data[0], name, term.data[2]))
        else:
            assert mutation == "premature-result-retirement"
            leave = next(ins for owner in blocks for ins in owner.instructions
                         if ins.kind == "call" and ins.data[2] == "pcc_gc_frame_leave_lifo")
            arguments = ((leave.data[4][0][0], published.data[3]),)
            block.instructions.insert(index + 2, SimpleNamespace(
                kind="call", data=leave.data[:4] + (arguments,) + leave.data[5:]))

    addresses = RootSlotContract(blocks, module.globals_)
    rows = addresses.rows
    slot, called, args = addresses.slot, addresses.called, addresses.args
    loads = {ins.data[0]: ins.data[3] for _, _, ins in rows if ins.kind == "load"}
    by_name = {block.name: block for block in blocks}
    invocations = [(block, index, ins) for block, index, ins in rows if called(ins, symbol)]
    assert invocations
    for block, index, invoke in invocations:
        published = block.instructions[index + 1]
        assert published.kind == "store" and published.data[1] == invoke.data[0], (
            "generator result needs immediate publication"
        )
        output = slot(published.data[3])
        operands = tuple(slot(loads[value]) for value in args(invoke))
        assert len(set(operands + (output,))) == len(operands) + 1
        for root in operands + (output,):
            addresses.require_owning(root, allow_lifo=True)
        output_frame = addresses.require_owning(output, allow_lifo=True)[:2]

        def is_consumer(ins):
            if mode == "take":
                return called(ins, "pcc_gc_take_pinned_slot") and slot(args(ins)[0]) == output
            return called(ins, mode) and any(
                value in loads and slot(loads[value]) == output for value in args(ins)
            )

        # Boundness guards use true=success, unlike negative-status/TLS
        # guards. Derive the paths reaching this exact consumer from the CFG;
        # never infer success from a fixed branch index or block spelling.
        successors = {}
        for owner in blocks:
            term = owner.terminator
            if term.kind == "br":
                successors[owner.name] = tuple(term.data[:1])
            elif term.kind == "br_cond":
                successors[owner.name] = tuple(term.data[1:])
            elif term.kind == "switch":
                successors[owner.name] = (term.data[2],) + tuple(target for _, target in term.data[3])
            else:
                successors[owner.name] = ()
        reaching = {owner.name for owner in blocks if any(is_consumer(ins) for ins in owner.instructions)}
        assert reaching, "exact generator output slot has no consumer"
        changed = True
        while changed:
            changed = False
            for name, targets in successors.items():
                # A later loop iteration is a different producer execution.
                if name == block.name or name in reaching:
                    continue
                if any(target in reaching for target in targets):
                    reaching.add(name)
                    changed = True

        paths = []
        pending = [(block, index + 2, [], frozenset(), False)]
        while pending:
            current, offset, path, seen, output_retired = pending.pop()
            assert current.name not in seen, "consumer path loops before its handoff"
            seen = seen | {current.name}
            consumed = False
            for ins in current.instructions[offset:]:
                path.append(ins)
                if called(ins, "pcc_gc_store_root") and args(ins)[1] == "null":
                    assert slot(args(ins)[0]) != output, "result cleared before consumption"
                if ins.kind == "store" and ins.data[1] == "null":
                    assert slot(ins.data[3]) != output, "result cleared before consumption"
                if called(ins, "pcc_gc_frame_leave") or called(ins, "pcc_gc_frame_leave_lifo"):
                    if slot(args(ins)[0]) == output_frame:
                        assert mode == "take", "result frame retired before sink consumption"
                        assert not output_retired, "result frame retired twice"
                        output_retired = True
                        continue
                if output_retired and ins.kind == "call":
                    assert is_consumer(ins), "result frame retired before its final take handoff"
                consumed = is_consumer(ins)
                if consumed:
                    if mode != "take":
                        for value in args(ins):
                            if value not in loads or slot(loads[value]) != output:
                                continue
                            positions = [position for position, earlier in enumerate(path)
                                         if earlier.kind == "load" and earlier.data[0] == value]
                            assert positions, "consumer must reload the published result on this path"
                            assert not any(earlier.kind == "call" for earlier in path[positions[-1] + 1:-1]), (
                                "consumer carries a raw result across a possible safepoint"
                            )
                    paths.append(path)
                    break
            if consumed:
                continue
            targets = [name for name in successors[current.name] if name in reaching and name != block.name]
            assert targets, "exact generator output slot never reaches its consumer"
            for name in targets:
                pending.append((by_name[name], 0, list(path), seen, output_retired))
        assert paths

        for path in paths:
            releases = [ins for ins in path if called(ins, "pcc_gc_foreign_lease_release")
                        and slot(args(ins)[0]) in operands]
            assert [slot(args(ins)[0]) for ins in releases[:len(operands)]] == list(reversed(operands))
            for release in releases[:len(operands)]:
                token = args(release)[1]
                acquisitions = [ins for _, _, ins in rows if called(ins, "pcc_gc_foreign_lease_acquire")
                                and ins.data[0] == token]
                assert len(acquisitions) == 1
                assert slot(args(acquisitions[0])[0]) == slot(args(release)[0])
            clears = [slot(args(ins)[0]) for ins in path
                      if called(ins, "pcc_gc_store_root") and args(ins)[1] == "null"
                      and slot(args(ins)[0]) in operands]
            assert clears[:len(operands)] == list(reversed(operands)), (
                "input roots must retire before the result's consumer"
            )
    addresses.assert_frame_exits()


@pytest.mark.parametrize("target", ("arm64-apple-darwin", "x86_64-unknown-linux-gnu"))
def test_generator_method_owned_result_ir(tmp_path, monkeypatch, target):
    from pcc.frontends.python.pipeline import compile_python
    from tests.owned_ir_validation import verify_ir_text

    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "generator_method_owner.py"
    source.write_text(PROGRAM)
    output = source.with_suffix(".ll")
    compile_python(str(source), str(output), emit_llvm_only=True, python_library=True,
                   libpython_mode="off", ir_scaffold_mode="on", backend="self", target_triple=target)
    text = output.read_text()
    module = verify_ir_text(text)
    for name, symbol, mode in (
        ("compare_send", "py_gen_send", "py_obj_eq_value"),
        ("compare_throw", "py_gen_throw", "py_obj_eq_value"),
        ("close_value", "py_gen_close", "py_list_append"),
        ("heap_send", "py_gen_send", "py_list_append"),
        ("heap_throw", "py_gen_throw", "py_list_append"),
        ("discard_send", "py_gen_send", "take"),
        ("original_exception", "py_gen_send", "take"),
    ):
        function = next(fn for fn in module.functions if fn.name.endswith("_" + name)
                        and fn.name.startswith("user_"))
        _assert_generator_function_slots(module, function, symbol, mode)
        for mutation in ("missing-publication", "premature-result-clear",
                         "premature-result-retirement", "one-success-edge-clears-result"):
            with pytest.raises(AssertionError):
                _assert_generator_function_slots(module, function, symbol, mode, mutation)
    for name, symbol in (("dynamic_send", "py_gen_send"), ("dynamic_throw", "py_gen_throw"),
                         ("dynamic_close", "py_gen_close")):
        assert not re.search(r"\bcall\b[^\n]*@" + symbol + r"\(", _function(text, name))
