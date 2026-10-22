from __future__ import annotations

from pcc.frontends.python.codegen.runtime_abi import RUNTIME_SIGNATURES
from pcc.diagnostics.contracts.runtime_effects import (
    CORRECTNESS_CRITICAL_RUNTIME_ABI_NAMES,
    RUNTIME_EFFECT_CATEGORY_OBJECT,
    RuntimeEffect,
    RuntimeEffectRecorder,
    RuntimeResource,
    arrows_touching_resource,
    arrows_with_effect,
    check_runtime_events,
    check_runtime_path,
    compose_runtime_event_effects,
    compose_runtime_effects,
    known_runtime_arrow_names,
    missing_runtime_abi_symbols,
    missing_runtime_effect_contracts,
    runtime_effect_category,
    runtime_effect_category_path,
    runtime_effect_quantale,
    runtime_effect_quantale_law_violations,
    runtime_arrow,
)


def test_correctness_critical_runtime_abi_symbols_exist_and_are_classified() -> None:
    assert missing_runtime_abi_symbols(RUNTIME_SIGNATURES) == ()
    assert missing_runtime_effect_contracts(RUNTIME_SIGNATURES) == ()
    assert set(CORRECTNESS_CRITICAL_RUNTIME_ABI_NAMES).issubset(
        set(known_runtime_arrow_names())
    )


def test_gc_slot_operations_encode_barrier_contracts() -> None:
    load = runtime_arrow("pcc_gc_load_ptr")
    borrowed_load = runtime_arrow("pcc_gc_load_borrowed_ptr")
    store = runtime_arrow("pcc_gc_store_ptr")
    root_store = runtime_arrow("pcc_gc_store_root")

    assert RuntimeEffect.READ_BARRIER in load.effects
    assert RuntimeEffect.READ_BARRIER in borrowed_load.effects
    assert RuntimeEffect.WRITE_BARRIER in store.effects
    assert RuntimeEffect.RETAIN in store.effects
    assert RuntimeEffect.RELEASE in store.effects
    assert RuntimeEffect.WRITE_BARRIER in root_store.effects
    assert RuntimeResource.HEAP_SLOT in store.inputs
    assert RuntimeResource.ROOT_SLOT in root_store.inputs


def test_gc_root_and_pin_brackets_are_checkable() -> None:
    assert (
        check_runtime_path(
            [
                "pcc_gc_frame_enter",
                "pcc_gc_store_root",
                "pcc_gc_pin",
                "pcc_gc_unpin",
                "pcc_gc_frame_leave",
            ]
        )
        == ()
    )

    frame_violations = check_runtime_path(["pcc_gc_frame_leave"])
    assert len(frame_violations) == 1
    assert frame_violations[0].message == "frame root leave without matching enter"

    pin_violations = check_runtime_path(["pcc_gc_pin"])
    assert len(pin_violations) == 1
    assert pin_violations[0].message == "pin enter without matching leave"


def test_continuation_roots_are_explicit_resources() -> None:
    new_cont = runtime_arrow("py_continuation_new_typed")
    register = runtime_arrow("pcc_gc_register_continuation_root")
    unregister = runtime_arrow("pcc_gc_unregister_continuation_root")

    assert RuntimeResource.CONTINUATION in new_cont.outputs
    assert RuntimeResource.CONTINUATION_ROOT in new_cont.outputs
    assert RuntimeEffect.CAPTURE_CONTINUATION in new_cont.effects
    assert RuntimeEffect.CONTINUATION_ROOT_ENTER in register.effects
    assert RuntimeEffect.CONTINUATION_ROOT_LEAVE in unregister.effects

    assert (
        check_runtime_path(
            [
                "pcc_gc_register_continuation_root",
                "pcc_gc_unregister_continuation_root",
            ]
        )
        == ()
    )


def test_virtual_thread_scheduler_effects_are_visible() -> None:
    new_vthread = runtime_arrow("py_virtual_thread_new")
    start = runtime_arrow("py_virtual_thread_start")
    park = runtime_arrow("py_virtual_thread_park")
    unpark = runtime_arrow("py_virtual_thread_unpark")
    sleep = runtime_arrow("py_virtual_thread_sleep")
    cancel_timer = runtime_arrow("py_virtual_thread_cancel_timer")
    run_once = runtime_arrow("py_virtual_thread_run_once")

    assert RuntimeResource.CONTINUATION in new_vthread.inputs
    assert RuntimeResource.VIRTUAL_THREAD in new_vthread.outputs
    assert RuntimeEffect.VTHREAD_START in start.effects
    assert RuntimeEffect.SCHEDULER_ENQUEUE in start.effects
    assert RuntimeEffect.SCHEDULER_ROOT_ENTER in start.effects
    assert RuntimeEffect.VTHREAD_PARK in park.effects
    assert RuntimeEffect.VTHREAD_UNPARK in unpark.effects
    assert RuntimeEffect.SCHEDULER_ENQUEUE in unpark.effects
    assert RuntimeEffect.SCHEDULER_ROOT_ENTER in sleep.effects
    assert RuntimeEffect.SCHEDULER_DEQUEUE in cancel_timer.effects
    assert RuntimeEffect.SCHEDULER_ROOT_LEAVE in cancel_timer.effects
    assert RuntimeEffect.VTHREAD_RUN in run_once.effects
    assert RuntimeEffect.VTHREAD_RESUME in run_once.effects
    assert RuntimeEffect.SCHEDULER_DEQUEUE in run_once.effects
    assert RuntimeEffect.SCHEDULER_ROOT_LEAVE in run_once.effects


def test_scheduler_root_handle_effects_are_visible_and_checkable() -> None:
    register = runtime_arrow("pcc_gc_scheduler_root_register_handle")
    unregister = runtime_arrow("pcc_gc_scheduler_root_unregister_handle")

    assert RuntimeResource.SCHEDULER_ROOT in register.outputs
    assert RuntimeEffect.SCHEDULER_ROOT_ENTER in register.effects
    assert RuntimeResource.SCHEDULER_ROOT in unregister.inputs
    assert RuntimeEffect.SCHEDULER_ROOT_LEAVE in unregister.effects

    assert (
        check_runtime_path(
            [
                "pcc_gc_scheduler_root_register_handle",
                "pcc_gc_scheduler_root_unregister_handle",
            ]
        )
        == ()
    )

    violations = check_runtime_path(["pcc_gc_scheduler_root_unregister_handle"])
    assert len(violations) == 1
    assert violations[0].message == "scheduler root leave without matching enter"


def test_virtual_thread_lifecycle_checker_enforces_rooted_park_resume() -> None:
    assert (
        check_runtime_path(
            [
                "py_continuation_new_typed",
                "py_virtual_thread_new",
                "py_virtual_thread_start",
                "py_virtual_thread_run_once",
                "pcc_gc_unregister_continuation_root",
            ]
        )
        == ()
    )

    park_violations = check_runtime_path(["py_virtual_thread_park"])
    assert len(park_violations) == 1
    assert park_violations[0].message == "virtual thread park without scheduler root"

    leak_violations = check_runtime_path(["py_virtual_thread_start"])
    assert len(leak_violations) == 1
    assert leak_violations[0].message == "scheduler root enter without matching leave"


def test_effect_queries_and_composition_are_deterministic() -> None:
    barrier_names = {
        arrow.name for arrow in arrows_with_effect(RuntimeEffect.WRITE_BARRIER)
    }
    root_names = {
        arrow.name for arrow in arrows_touching_resource(RuntimeResource.ROOT_SLOT)
    }
    effects = compose_runtime_effects(["pcc_gc_alloc", "pcc_gc_store_ptr"])

    assert "pcc_gc_store_ptr" in barrier_names
    assert "pcc_gc_store_root" in barrier_names
    assert "pcc_gc_store_root" in root_names
    assert RuntimeEffect.ALLOC in effects
    assert RuntimeEffect.WRITE_BARRIER in effects
    assert RuntimeEffect.SAFEPOINT in effects


def test_runtime_effect_quantale_composes_and_satisfies_sampled_laws() -> None:
    quantale = runtime_effect_quantale()
    alloc = runtime_arrow("pcc_gc_alloc").effects
    store = runtime_arrow("pcc_gc_store_ptr").effects

    assert quantale.unit == frozenset()
    assert quantale.bottom == frozenset()
    assert quantale.compose(alloc, store) == compose_runtime_effects(
        ["pcc_gc_alloc", "pcc_gc_store_ptr"]
    )
    assert RuntimeEffect.ALLOC in quantale.compose(alloc, store)
    assert RuntimeEffect.WRITE_BARRIER in quantale.compose(alloc, store)
    assert quantale.leq(alloc, quantale.compose(alloc, store))
    assert runtime_effect_quantale_law_violations() == ()


def test_runtime_effect_recorder_records_metadata_and_composes_path() -> None:
    recorder = RuntimeEffectRecorder(source="codegen", function="user_f")

    first = recorder.record(
        "pcc_gc_frame_enter",
        node_kind="FunctionDef",
        span="1:0",
        note="function entry roots",
    )
    recorder.extend(
        ["pcc_gc_store_root", "pcc_gc_frame_leave"],
        node_kind="Return",
        span="3:4",
    )

    events = recorder.events()
    assert recorder.names() == (
        "pcc_gc_frame_enter",
        "pcc_gc_store_root",
        "pcc_gc_frame_leave",
    )
    assert [event.index for event in events] == [0, 1, 2]
    assert first.source == "codegen"
    assert first.function == "user_f"
    assert first.node_kind == "FunctionDef"
    assert first.span == "1:0"
    assert first.note == "function entry roots"
    assert first.arrow.name == "pcc_gc_frame_enter"

    assert recorder.check() == ()
    assert recorder.category_path().factors == recorder.names()
    assert check_runtime_events(events) == ()
    effects = recorder.effects()
    assert effects == compose_runtime_event_effects(events)
    assert RuntimeEffect.ROOT_ENTER in effects
    assert RuntimeEffect.WRITE_BARRIER in effects
    assert RuntimeEffect.ROOT_LEAVE in effects


def test_runtime_effect_recorder_rejects_unknown_arrows_without_mutating() -> None:
    recorder = RuntimeEffectRecorder(source="codegen")

    try:
        recorder.record("not_a_runtime_abi_symbol")
    except KeyError as exc:
        assert "no runtime-effect contract" in str(exc)
    else:
        raise AssertionError("unknown runtime ABI symbol should be rejected")

    assert recorder.events() == ()


def test_runtime_effect_recorder_reports_bracket_violations() -> None:
    recorder = RuntimeEffectRecorder(source="runtime-audit")
    recorder.record("pcc_gc_pin", function="store_device_buffer")

    violations = recorder.check()
    assert len(violations) == 1
    assert violations[0].message == "pin enter without matching leave"
    assert violations[0].index == 1
    assert violations[0].arrow_name == "<end>"


def test_runtime_effect_category_is_lawful_state_transition_instance() -> None:
    category = runtime_effect_category()
    alloc = category.arrow("pcc_gc_alloc")
    store = category.arrow("pcc_gc_store_ptr")
    collect = category.arrow("pcc_gc_collect")

    assert category.objects() == (RUNTIME_EFFECT_CATEGORY_OBJECT,)
    assert "pcc_gc_store_ptr" in category.arrow_names()
    assert alloc.domain == RUNTIME_EFFECT_CATEGORY_OBJECT
    assert alloc.codomain == RUNTIME_EFFECT_CATEGORY_OBJECT
    assert category.identity_laws_hold(alloc)
    assert category.associativity_holds(alloc, store, collect)

    path = runtime_effect_category_path(
        ["pcc_gc_alloc", "pcc_gc_store_ptr", "pcc_gc_collect"]
    )
    composed = category.compose_path(path, name="alloc_store_collect")
    assert composed.domain == RUNTIME_EFFECT_CATEGORY_OBJECT
    assert composed.codomain == RUNTIME_EFFECT_CATEGORY_OBJECT
    assert composed.factors == (
        "pcc_gc_alloc",
        "pcc_gc_store_ptr",
        "pcc_gc_collect",
    )
