"""Bounded host controls for the owned x86 emission's globals index.

These execute production emission and object encoding, not native programs.
"""

import ast
from dataclasses import replace
import hashlib
from pathlib import Path

import pytest

from pcc.backend import BackendUnavailable
from pcc.backend import indexed_phase_timing as timing_module
from pcc.backend import self_backend_precise_stackmaps as planner
from pcc.backend import self_backend_prepare as preparation
from pcc.backend import self_backend_x86_64_linux as emitter
from pcc.backend.indexed_phase_timing import ModulePhaseTiming
from pcc.backend.self_backend_ir import GlobalDef
from pcc.backend.self_backend_parse import parse_self_backend_module
from pcc.backend.target_objects import emit_indexed_assembly, encode_assembly_object
from tests.python.test_stackmap_global_index import source_text


TARGETS = ("x86_64-linux-gnu", "x86_64-pc-windows-msvc")
BASELINE_PLANNER_AST_SHA256 = "b6158e0a1b8f7d34bb76b4bfa541b99c1e990fc587bca22d4ac009c7763cba9a"
# ast.dump's text changed in CPython 3.15; ast.unparse gives the same text on
# 3.14 and 3.15, so either hash identifies the unchanged baseline body.
BASELINE_PLANNER_UNPARSE_SHA256 = "edc2ef8324602596e51a8254588ecc2465d6570a02ef782d9f688da8d5cda654"


@pytest.fixture(autouse=True)
def fixed_emission_configuration(monkeypatch):
    monkeypatch.delenv("PCC_CODE_PROFILE", raising=False)
    monkeypatch.setenv("PCC_SELF_TARGET_PASSES", "none")
    monkeypatch.setenv("PCC_SELF_TARGET_PASS_TRANSPORT", "text")


def original_planner():
    """Restore only this change and require the exact baseline function AST."""
    source = Path(planner.__file__).read_text()
    function = next(node for node in ast.parse(source).body
                    if isinstance(node, ast.FunctionDef)
                    and node.name == "build_function_stack_map_plan")
    assert function.args.kwonlyargs[-1].arg == "_globals_index"
    function.args.kwonlyargs.pop()
    function.args.kw_defaults.pop()
    assert isinstance(function.body[3], ast.If)
    function.body[3] = ast.parse(
        "globals_by_name = {global_.name: global_ for global_ in globals_}"
    ).body[0]
    assert (
        hashlib.sha256(ast.dump(function).encode()).hexdigest() == BASELINE_PLANNER_AST_SHA256
        or hashlib.sha256(ast.unparse(function).encode()).hexdigest()
        == BASELINE_PLANNER_UNPARSE_SHA256
    )
    namespace = dict(vars(planner))
    module = ast.Module(body=[ast.ImportFrom(module="__future__",
        names=[ast.alias(name="annotations")], level=0), function], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "<5014e0-x86-planner>", "exec"), namespace)
    return namespace[function.name]


def prepared(source):
    return preparation.prepare_module_for_target(
        source, aggregate_returned_indirect=lambda _ty: False,
    )


def select_original(monkeypatch):
    original = original_planner()

    def plan(*args, **kwargs):
        kwargs.pop("_globals_index", None)
        return original(*args, **kwargs)

    monkeypatch.setattr(emitter, "build_function_stack_map_plan", plan)


def test_original_control_is_exact_baseline():
    assert original_planner().__name__ == "build_function_stack_map_plan"


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("timed", (False, True))
def test_production_index_name_reads_scale_linearly(monkeypatch, target, timed):
    counts = []
    for size in (4, 8):
        source = 'target triple = "' + target + '"\n'
        source += "\n".join("@g%d = internal constant i64 %d" % (i, i)
                            for i in range(size))
        source += "\n" + "\n".join(
            "define i64 @f%d() {\nentry:\n ret i64 %d\n}" % (i, i)
            for i in range(size)
        )
        outputs, visits = [], []
        for original in (True, False):
            state = {"index": False, "reads": 0}
            attribute = GlobalDef.__getattribute__
            aliases = planner._pointer_aliases
            current = planner.build_function_stack_map_plan

            def read_name(global_, name):
                if state["index"] and name == "name":
                    state["reads"] += 1
                return attribute(global_, name)

            def end_index(func):
                state["index"] = False
                return aliases(func)

            with monkeypatch.context() as patch:
                patch.setattr(GlobalDef, "__getattribute__", read_name)
                patch.setattr(planner, "_pointer_aliases", end_index)
                control = original_planner() if original else current

                def observe(*args, **kwargs):
                    assert type(args[1]) is list
                    if original:
                        kwargs.pop("_globals_index", None)
                    else:
                        assert (kwargs["_globals_index"] is None) is timed
                    state["index"] = True
                    try:
                        return control(*args, **kwargs)
                    finally:
                        state["index"] = False

                patch.setattr(emitter, "build_function_stack_map_plan", observe)
                timing = ModulePhaseTiming("index-count") if timed else None
                outputs.append(emit_indexed_assembly(
                    parse_self_backend_module(source), phase_timing=timing,
                ))
            visits.append(state["reads"])
        assert outputs[0] == outputs[1]
        assert visits == [size * size, size * size if timed else size]
        counts.append(visits)
    assert counts[1][0] == counts[0][0] * 4
    assert counts[1][1] == counts[0][1] * (4 if timed else 2)
    print("index-name-reads", target, "timed=" + str(timed), counts)


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("shape", ("derived", "all-record-kinds", "tail-call"))
@pytest.mark.parametrize("route", ("assembly", "object"))
def test_production_outputs_match_exact_original(monkeypatch, target, shape, route):
    source = source_text(shape, triple=target, functions=3)
    outputs = []
    for original in (True, False):
        with monkeypatch.context() as patch:
            if original:
                select_original(patch)
            plans = [] if route == "object" else None
            assembly = emit_indexed_assembly(
                parse_self_backend_module(source), stack_map_plans_out=plans,
            )
            output = (assembly.encode() if plans is None else encode_assembly_object(
                assembly, target, stack_map_plans=plans, consume_stack_map_plans=True,
            ))
            outputs.append(output)
    assert outputs[0] == outputs[1]
    print("exact-output", target, shape, route, len(outputs[1]),
          hashlib.sha256(outputs[1]).hexdigest())


@pytest.mark.parametrize("kind", ("container", "record", "name"))
def test_custom_globals_keep_per_function_mutations(kind):
    module = prepared(source_text(triple=TARGETS[0], functions=2))
    globals_ = module.globals_
    if kind == "container":
        class Globals(list):
            pass
        globals_ = Globals(globals_)
    elif kind == "record":
        class CustomGlobal(GlobalDef):
            pass
        globals_[0] = CustomGlobal(**vars(globals_[0]))
    else:
        class Name(str):
            pass
        globals_[0] = replace(globals_[0], name=Name(globals_[0].name))
    holder = []
    planner.build_function_stack_map_plan(
        module.functions[0], globals_, target="x86_64-linux", _globals_index=holder,
    )
    assert holder == []
    globals_[0] = replace(globals_[0], initializer="65536")
    with pytest.raises(BackendUnavailable, match="exceeds the ABI location bound"):
        planner.build_function_stack_map_plan(
            module.functions[1], globals_, target="x86_64-linux", _globals_index=holder,
        )
    assert holder == []


@pytest.mark.parametrize("last_valid", (False, True))
def test_duplicate_global_names_keep_last_definition(last_valid):
    module = prepared(source_text(triple=TARGETS[0], functions=1))
    valid = module.globals_[0]
    invalid = replace(valid, initializer="65536")
    globals_ = [invalid, valid] if last_valid else [valid, invalid]
    holder = []
    if last_valid:
        planner.build_function_stack_map_plan(
            module.functions[0], globals_, target="x86_64-linux", _globals_index=holder,
        )
        assert holder[0][valid.name] is valid
    else:
        with pytest.raises(BackendUnavailable, match="exceeds the ABI location bound"):
            planner.build_function_stack_map_plan(
                module.functions[0], globals_, target="x86_64-linux", _globals_index=holder,
            )


def test_kernel_failure_and_empty_batch_do_not_read_globals(monkeypatch):
    module = prepared(source_text(triple=TARGETS[0], functions=1))

    class Globals(list):
        def __iter__(self):
            raise AssertionError("global index was read")

    def fail(_func):
        raise ValueError("kernel failure")

    monkeypatch.setattr(planner, "get_indexed_function_kernel", fail)
    globals_ = Globals(module.globals_)
    assert planner.build_stack_map_plans([], globals_, target="unsupported",
                                        _share_globals_index=True) == ()
    holder = []
    with pytest.raises(ValueError, match="kernel failure"):
        planner.build_function_stack_map_plan(
            module.functions[0], globals_, target="x86_64-linux", _globals_index=holder,
        )
    assert holder == []


def test_direct_prepared_call_keeps_cross_function_mutation(monkeypatch):
    module = prepared(source_text(triple=TARGETS[0], functions=2))
    emit = emitter._emit_function
    seen = []

    def mutate(func, *args, **kwargs):
        result = emit(func, *args, **kwargs)
        seen.append(func.name)
        module.globals_[0] = replace(module.globals_[0], initializer="65536")
        return result

    monkeypatch.setattr(emitter, "_emit_function", mutate)
    with pytest.raises(BackendUnavailable, match="exceeds the ABI location bound"):
        emitter._emit_prepared_x86_64_module(module, "")
    assert len(seen) == 1


def test_custom_timing_keeps_cross_function_mutation(monkeypatch):
    prepare = preparation.prepare_parsed_module_for_target
    captured = []
    holders = []
    plan = emitter.build_function_stack_map_plan

    def capture(*args, **kwargs):
        result = prepare(*args, **kwargs)
        captured.append(result)
        return result

    def observe(*args, **kwargs):
        holders.append(kwargs["_globals_index"])
        return plan(*args, **kwargs)

    class Timing:
        def start(self):
            return 1

        def add(self, phase, started):
            assert started == 1
            if phase == 5:
                globals_ = captured[0].globals_
                globals_[0] = replace(globals_[0], initializer="65536")

    monkeypatch.setattr(preparation, "prepare_parsed_module_for_target", capture)
    monkeypatch.setattr(emitter, "build_function_stack_map_plan", observe)
    with pytest.raises(BackendUnavailable, match="exceeds the ABI location bound"):
        emit_indexed_assembly(parse_self_backend_module(
            source_text(triple=TARGETS[1], functions=2)), phase_timing=Timing())
    assert holders == [None, None]


@pytest.mark.parametrize("callback", (
    "instance-start", "instance-add", "class-start", "class-add", "clock",
))
def test_exact_owned_timing_callbacks_keep_original_mutation(monkeypatch, callback):
    outcomes = []
    for original in (True, False):
        prepare = preparation.prepare_parsed_module_for_target
        captured, holders, mutations = [], [], []
        control = original_planner() if original else planner.build_function_stack_map_plan
        timing = ModulePhaseTiming("mutable-timing")

        def capture(*args, **kwargs):
            result = prepare(*args, **kwargs)
            captured.append(result)
            return result

        def observe(*args, **kwargs):
            holders.append(kwargs["_globals_index"])
            if original:
                kwargs.pop("_globals_index")
            return control(*args, **kwargs)

        def mutate():
            if captured and len(holders) == 1 and not mutations:
                globals_ = captured[0].globals_
                globals_[0] = replace(globals_[0], initializer="65536")
                mutations.append(True)

        start = ModulePhaseTiming.start
        add = ModulePhaseTiming.add
        clock = timing_module.time.perf_counter_ns

        def replaced_start(self):
            mutate()
            return start(self)

        def replaced_add(self, phase, started):
            mutate()
            return add(self, phase, started)

        def replaced_clock():
            mutate()
            return clock()

        with monkeypatch.context() as patch:
            patch.setattr(preparation, "prepare_parsed_module_for_target", capture)
            patch.setattr(emitter, "build_function_stack_map_plan", observe)
            if callback == "instance-start":
                timing.start = lambda: replaced_start(timing)
            elif callback == "instance-add":
                timing.add = lambda phase, started: replaced_add(timing, phase, started)
            elif callback == "class-start":
                patch.setattr(ModulePhaseTiming, "start", replaced_start)
            elif callback == "class-add":
                patch.setattr(ModulePhaseTiming, "add", replaced_add)
            else:
                patch.setattr(timing_module.time, "perf_counter_ns", replaced_clock)
            with pytest.raises(BackendUnavailable, match="exceeds the ABI location bound") as error:
                emit_indexed_assembly(parse_self_backend_module(
                    source_text(triple=TARGETS[1], functions=2)), phase_timing=timing)
            outcomes.append(str(error.value))
        assert mutations == [True]
        assert holders == [None, None]
    assert outcomes[0] == outcomes[1]


@pytest.mark.parametrize("failure", (None, "plan-first", "plan-later", "emit"))
def test_production_index_is_shared_then_cleared_even_with_saved_exception(monkeypatch, failure):
    entries = planner._block_entry_states
    emit = emitter._emit_function
    seen, snapshots, holders, saved = [], [], [], []
    plan = emitter.build_function_stack_map_plan

    def observe(*args, **kwargs):
        holders.append(kwargs["_globals_index"])
        return plan(*args, **kwargs)

    def roots(func, index, aliases):
        seen.append(index)
        snapshots.append(tuple(index.items()))
        if failure == "plan-first" or (failure == "plan-later" and len(seen) == 2):
            raise ValueError("planning failure")
        return entries(func, index, aliases)

    def body(*args, **kwargs):
        if failure == "emit":
            raise ValueError("emission failure")
        return emit(*args, **kwargs)

    monkeypatch.setattr(emitter, "build_function_stack_map_plan", observe)
    monkeypatch.setattr(planner, "_block_entry_states", roots)
    monkeypatch.setattr(emitter, "_emit_function", body)
    try:
        emit_indexed_assembly(parse_self_backend_module(source_text(triple=TARGETS[1])))
        assert failure is None
    except ValueError as error:
        assert failure is not None
        saved.append(error)
    assert seen and all(index is seen[0] for index in seen)
    assert snapshots and all(snapshot == snapshots[0] for snapshot in snapshots)
    assert all(not index for index in seen)
    assert holders and all(holder is holders[0] for holder in holders)
    assert holders[0] == []


def test_next_production_call_rebuilds_from_current_globals(monkeypatch):
    plan = emitter.build_function_stack_map_plan
    holders = []

    def observe(*args, **kwargs):
        holders.append(kwargs["_globals_index"])
        return plan(*args, **kwargs)

    monkeypatch.setattr(emitter, "build_function_stack_map_plan", observe)
    source = source_text(triple=TARGETS[1], functions=1)
    emit_indexed_assembly(parse_self_backend_module(source))
    module = parse_self_backend_module(source)
    module = replace(module, globals_=(
        replace(module.globals_[0], initializer="65536"), *module.globals_[1:],
    ))
    with pytest.raises(BackendUnavailable, match="exceeds the ABI location bound"):
        emit_indexed_assembly(module)
    assert len(holders) == 2 and holders[0] is not holders[1]
    assert holders == [[], []]
