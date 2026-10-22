"""Call temporaries balance lexical roots without multiplying return cleanup."""

import pytest

from pcc.backend.self_backend_precise_stackmaps import build_stack_map_plans
from pcc.backend.self_backend_prepare import prepare_module_for_target
from pcc.backend.self_backend_x86_64_linux import _aggregate_returned_indirect
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.py_lift import parse_and_lift
from pcc.frontends.python.type_infer import infer_module


def _emit(source, name="lexical"):
    module = infer_module(parse_and_lift(source, name + ".py", name))
    codegen = L1CodeGen(module, ir_scaffold_mode="on")
    codegen._target_triple = "x86_64-unknown-linux-gnu"
    codegen._strict_no_libpython = True
    codegen._prefer_native_callable_values = True
    text = str(codegen.generate(module))
    return codegen, 'target triple = "x86_64-unknown-linux-gnu"\n' + text


def test_explicit_linux_target_keeps_registered_frames_on_a_darwin_host(monkeypatch):
    from pcc.backend.precise_stackmap import precise_stackmap_tables_enabled
    from pcc.frontends.python import pipeline_targets

    monkeypatch.delenv("PCC_PRECISE_STACKMAP_TABLES", raising=False)
    monkeypatch.setattr(pipeline_targets, "host_target_triple", lambda: "arm64-apple-darwin")
    assert precise_stackmap_tables_enabled() is True
    assert precise_stackmap_tables_enabled("x86_64-unknown-linux-gnu") is False
    codegen, text = _emit("def probe(f, x):\n    return f(x)\n")
    calls = [line for line in text.splitlines() if "call " in line]
    assert any("@pcc_gc_frame_enter_lifo(" in line for line in calls)
    assert any("@pcc_gc_frame_leave_lifo(" in line for line in calls)
    assert not any("@pcc_gc_frame_" in line and "_mapped(" in line for line in calls)
    assert codegen._target_triple == "x86_64-unknown-linux-gnu"


@pytest.mark.parametrize("source", (
    "def target(*, value, later=None):\n    return value\n"
    "def probe(factory, value):\n"
    "    return target(value=factory()(value), later=[1, 2])\n",
    "def target(*, value, later=None):\n    return value\n"
    "def fail():\n    raise ValueError('later')\n"
    "def probe(factory, value):\n"
    "    try:\n        return target(value=factory()(value), later=fail())\n"
    "    except ValueError:\n        return None\n",
    "def probe(factory, values):\n    result = None\n"
    "    for value in values:\n        result = factory()(value)\n"
    "    return result\n",
    "def probe(factory, value):\n"
    "    def target(item=factory()(value)):\n        return item\n"
    "    return target()\n",
), ids=("callee-and-arguments", "later-argument-error", "loop", "default"))
def test_lexical_call_roots_balance_precise_stackmap_joins(source):
    codegen, text = _emit(source)
    records = codegen._slot_call_root_records
    assert records and all(flag is None and lifo for _slot, flag, lifo in records)
    # This is the emitter's real verification and stack-slot preparation, then
    # its precise GC analysis. Any missed exceptional leave disagrees at a
    # handler/error join; a root left early cannot cover a later managed use.
    prepared = prepare_module_for_target(
        text, aggregate_returned_indirect=_aggregate_returned_indirect,
    )
    plans = build_stack_map_plans(
        prepared.functions, prepared.globals_, target="x86_64-linux",
    )
    assert len(plans) == len(prepared.functions)


def test_generator_resume_keeps_function_owned_operand_roots():
    codegen, text = _emit(
        "def probe(factory, value):\n"
        "    yield factory()(value)\n"
        "    yield factory()(value)\n",
    )
    assert any(flag is not None and not lifo
               for _slot, flag, lifo in codegen._slot_call_root_records)
    prepared = prepare_module_for_target(
        text, aggregate_returned_indirect=_aggregate_returned_indirect,
    )
    build_stack_map_plans(
        prepared.functions, prepared.globals_, target="x86_64-linux",
    )


def test_call_temporaries_do_not_multiply_early_return_ir():
    counts = []
    for size in (8, 16):
        source = "def probe(callee, value, branch):\n"
        for index in range(size):
            source += ("    if branch == " + str(index) + ":\n"
                       "        return callee(value=value)\n")
        source += "    return None\n"
        codegen, _text = _emit(source, "growth")
        function = next(fn for fn in codegen.module.functions
                        if fn.name == "user_growth_probe")
        counts.append(sum(len(block._instrs) for block in function.blocks))
    # Repeating the same branch should grow linearly. Before lexical roots,
    # every later temporary patched earlier returns and each later return
    # also re-emitted cleanup for all prior temporary slots (11,568 -> 28,832).
    assert counts[1] < counts[0] * 2.25, counts
