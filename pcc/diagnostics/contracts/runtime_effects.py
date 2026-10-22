"""Runtime-effect/resource taxonomy for PCC runtime ABI calls.

This module is intentionally a checker/protocol layer.  It does not change
runtime behavior, emitted IR, GC behavior, scheduler behavior, or GPU behavior.

The model is deliberately small: a runtime ABI symbol is treated as an arrow
from input resources to output resources with a finite effect set.  Later codegen
or IR checkers can compose these arrows over a basic block, function, or
suspend/resume path without knowing collector internals.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Iterable, Mapping, Sequence

from pcc.diagnostics.contracts.category import (
    CategoryArrow,
    CategoryPath,
    EffectQuantale,
    QuantaleViolation,
    SmallCategory,
)


class RuntimeResource(str, Enum):
    """Resources whose ownership/visibility matters to runtime composition."""

    HEAP_OBJECT = "heap_object"
    HEAP_SLOT = "heap_slot"
    ROOT_SLOT = "root_slot"
    FRAME_ROOT = "frame_root"
    CONTINUATION_ROOT = "continuation_root"
    CONTINUATION = "continuation"
    SCHEDULER_ROOT = "scheduler_root"
    SCHEDULER_QUEUE = "scheduler_queue"
    VIRTUAL_THREAD = "virtual_thread"
    HOST_BUFFER = "host_buffer"
    DEVICE_BUFFER = "device_buffer"
    PINNED_REGION = "pinned_region"
    NOGC_REGION = "nogc_region"


class RuntimeEffect(str, Enum):
    """Observable effect classes for runtime ABI composition."""

    ALLOC = "alloc"
    RETAIN = "retain"
    RELEASE = "release"
    READ_BARRIER = "read_barrier"
    WRITE_BARRIER = "write_barrier"
    ROOT_ENTER = "root_enter"
    ROOT_LEAVE = "root_leave"
    CAPTURE_CONTINUATION = "capture_continuation"
    CONTINUATION_ROOT_ENTER = "continuation_root_enter"
    CONTINUATION_ROOT_LEAVE = "continuation_root_leave"
    SCHEDULER_ROOT_ENTER = "scheduler_root_enter"
    SCHEDULER_ROOT_LEAVE = "scheduler_root_leave"
    SAFEPOINT = "safepoint"
    COLLECT = "collect"
    PIN_ENTER = "pin_enter"
    PIN_LEAVE = "pin_leave"
    SCHEDULER_ENQUEUE = "scheduler_enqueue"
    SCHEDULER_DEQUEUE = "scheduler_dequeue"
    VTHREAD_START = "vthread_start"
    VTHREAD_PARK = "vthread_park"
    VTHREAD_UNPARK = "vthread_unpark"
    VTHREAD_SLEEP = "vthread_sleep"
    VTHREAD_BLOCK_IO = "vthread_block_io"
    VTHREAD_RUN = "vthread_run"
    VTHREAD_RESUME = "vthread_resume"
    VTHREAD_CANCEL = "vthread_cancel"
    VTHREAD_COMPLETE = "vthread_complete"
    VTHREAD_FAIL = "vthread_fail"
    HOST_DEVICE_TRANSFER = "host_device_transfer"
    GPU_LAUNCH = "gpu_launch"
    GPU_SYNC = "gpu_sync"


@dataclass(frozen=True)
class RuntimeArrow:
    """A runtime ABI operation viewed as a resource/effect arrow."""

    name: str
    inputs: frozenset[RuntimeResource]
    outputs: frozenset[RuntimeResource]
    effects: frozenset[RuntimeEffect]
    summary: str

    def has_effect(self, effect: RuntimeEffect) -> bool:
        return effect in self.effects

    def touches_resource(self, resource: RuntimeResource) -> bool:
        return resource in self.inputs or resource in self.outputs


@dataclass(frozen=True)
class RuntimeContractViolation:
    """A checker finding for a composed runtime path."""

    message: str
    index: int
    arrow_name: str


class ProductionVThreadEventKind(IntEnum):
    """Numeric event kinds shared with ``py_runtime.h``."""

    ROOT_ENTER = 1
    ROOT_LEAVE = 2
    READY_ENQUEUE = 3
    START = 4
    PARK = 5
    UNPARK = 6
    RESUME = 7
    TIMER_PARK = 8
    TIMER_WAKE = 9
    IO_PARK = 10
    IO_WAKE = 11
    CANCEL_TIMER = 12
    CANCEL_IO = 13
    COMPLETE = 14
    FAIL = 15
    CANCEL_COMPLETE = 16


@dataclass(frozen=True)
class ProductionVThreadEvent:
    """One allocation-free event read from the production scheduler ring."""

    kind: ProductionVThreadEventKind
    detail: int
    root_delta: int
    state: int


@dataclass(frozen=True)
class RuntimeEffectEvent:
    """One recorded occurrence of a runtime-effect arrow.

    ``source`` is intentionally free-form: early users can record values such
    as ``codegen``, ``runtime-audit``, ``gpu-metal``, or a test helper before a
    stricter event schema exists.
    """

    name: str
    index: int
    source: str
    function: str | None = None
    node_kind: str | None = None
    span: str | None = None
    note: str | None = None

    @property
    def arrow(self) -> RuntimeArrow:
        return runtime_arrow(self.name)

    @property
    def effects(self) -> frozenset[RuntimeEffect]:
        return self.arrow.effects

    @property
    def inputs(self) -> frozenset[RuntimeResource]:
        return self.arrow.inputs

    @property
    def outputs(self) -> frozenset[RuntimeResource]:
        return self.arrow.outputs


class RuntimeEffectRecorder:
    """Collect a runtime path as validated runtime-effect events.

    This is the first compiler-visible category interface: codegen, runtime
    audits, or tests can record ABI arrows with source metadata, then compose
    and check the resulting path without needing collector-specific knowledge.
    """

    def __init__(
        self,
        *,
        source: str = "runtime-effect",
        function: str | None = None,
    ) -> None:
        self._source = source
        self._function = function
        self._events: list[RuntimeEffectEvent] = []

    def record(
        self,
        name: str,
        *,
        source: str | None = None,
        function: str | None = None,
        node_kind: str | None = None,
        span: str | None = None,
        note: str | None = None,
    ) -> RuntimeEffectEvent:
        runtime_arrow(name)
        event = RuntimeEffectEvent(
            name=name,
            index=len(self._events),
            source=source if source is not None else self._source,
            function=function if function is not None else self._function,
            node_kind=node_kind,
            span=span,
            note=note,
        )
        self._events.append(event)
        return event

    def extend(
        self,
        names: Iterable[str],
        *,
        source: str | None = None,
        function: str | None = None,
        node_kind: str | None = None,
        span: str | None = None,
        note: str | None = None,
    ) -> tuple[RuntimeEffectEvent, ...]:
        return tuple(
            self.record(
                name,
                source=source,
                function=function,
                node_kind=node_kind,
                span=span,
                note=note,
            )
            for name in names
        )

    def events(self) -> tuple[RuntimeEffectEvent, ...]:
        return tuple(self._events)

    def names(self) -> tuple[str, ...]:
        return tuple(event.name for event in self._events)

    def arrows(self) -> tuple[RuntimeArrow, ...]:
        return tuple(event.arrow for event in self._events)

    def effects(self) -> frozenset[RuntimeEffect]:
        return compose_runtime_effects(self.names())

    def check(self) -> tuple[RuntimeContractViolation, ...]:
        return check_runtime_path(self.names())

    def category_path(self) -> CategoryPath[str]:
        return runtime_effect_category_path(self.names())

    def clear(self) -> None:
        self._events.clear()


def _resources(*values: RuntimeResource) -> frozenset[RuntimeResource]:
    return frozenset(values)


def _effects(*values: RuntimeEffect) -> frozenset[RuntimeEffect]:
    return frozenset(values)


def _arrow(
    name: str,
    inputs: Iterable[RuntimeResource],
    outputs: Iterable[RuntimeResource],
    effects: Iterable[RuntimeEffect],
    summary: str,
) -> RuntimeArrow:
    return RuntimeArrow(
        name=name,
        inputs=frozenset(inputs),
        outputs=frozenset(outputs),
        effects=frozenset(effects),
        summary=summary,
    )


RUNTIME_ABI_ARROWS: dict[str, RuntimeArrow] = {
    "pcc_gc_alloc": _arrow(
        "pcc_gc_alloc",
        (),
        (RuntimeResource.HEAP_OBJECT,),
        (RuntimeEffect.ALLOC, RuntimeEffect.SAFEPOINT),
        "Allocate a heap object through the selected GC backend.",
    ),
    "pcc_gc_retain": _arrow(
        "pcc_gc_retain",
        (RuntimeResource.HEAP_OBJECT,),
        (RuntimeResource.HEAP_OBJECT,),
        (RuntimeEffect.RETAIN,),
        "Retain an owned heap object reference.",
    ),
    "pcc_gc_release": _arrow(
        "pcc_gc_release",
        (RuntimeResource.HEAP_OBJECT,),
        (),
        (RuntimeEffect.RELEASE, RuntimeEffect.READ_BARRIER),
        "Release an owned heap object reference, resolving moving-GC candidates.",
    ),
    "pcc_gc_load_ptr": _arrow(
        "pcc_gc_load_ptr",
        (RuntimeResource.HEAP_OBJECT, RuntimeResource.HEAP_SLOT),
        (RuntimeResource.HEAP_OBJECT,),
        (RuntimeEffect.READ_BARRIER,),
        "Load an owned pointer slot through the collector read barrier.",
    ),
    "pcc_gc_load_borrowed_ptr": _arrow(
        "pcc_gc_load_borrowed_ptr",
        (RuntimeResource.HEAP_OBJECT, RuntimeResource.HEAP_SLOT),
        (RuntimeResource.HEAP_OBJECT,),
        (RuntimeEffect.READ_BARRIER,),
        "Load a borrowed pointer slot through the collector read barrier.",
    ),
    "pcc_gc_store_ptr": _arrow(
        "pcc_gc_store_ptr",
        (RuntimeResource.HEAP_OBJECT, RuntimeResource.HEAP_SLOT),
        (RuntimeResource.HEAP_SLOT,),
        (RuntimeEffect.WRITE_BARRIER, RuntimeEffect.RETAIN, RuntimeEffect.RELEASE),
        "Store a heap pointer slot with write barrier and ref ownership update.",
    ),
    "pcc_gc_store_root": _arrow(
        "pcc_gc_store_root",
        (RuntimeResource.ROOT_SLOT,),
        (RuntimeResource.ROOT_SLOT,),
        (RuntimeEffect.WRITE_BARRIER, RuntimeEffect.RETAIN, RuntimeEffect.RELEASE),
        "Store a root slot with collector-visible write semantics.",
    ),
    "pcc_gc_frame_enter": _arrow(
        "pcc_gc_frame_enter",
        (RuntimeResource.ROOT_SLOT,),
        (RuntimeResource.FRAME_ROOT,),
        (RuntimeEffect.ROOT_ENTER,),
        "Register active frame roots.",
    ),
    "pcc_gc_frame_leave": _arrow(
        "pcc_gc_frame_leave",
        (RuntimeResource.FRAME_ROOT,),
        (),
        (RuntimeEffect.ROOT_LEAVE,),
        "Unregister active frame roots.",
    ),
    "pcc_gc_scheduler_root_register_handle": _arrow(
        "pcc_gc_scheduler_root_register_handle",
        (RuntimeResource.ROOT_SLOT,),
        (RuntimeResource.SCHEDULER_ROOT,),
        (RuntimeEffect.SCHEDULER_ROOT_ENTER,),
        "Register a scheduler-owned runtime root and return an O(1) handle.",
    ),
    "pcc_gc_scheduler_root_unregister_handle": _arrow(
        "pcc_gc_scheduler_root_unregister_handle",
        (RuntimeResource.SCHEDULER_ROOT,),
        (),
        (RuntimeEffect.SCHEDULER_ROOT_LEAVE,),
        "Unregister a scheduler-owned runtime root through its O(1) handle.",
    ),
    "pcc_gc_scheduler_root_register": _arrow(
        "pcc_gc_scheduler_root_register",
        (RuntimeResource.ROOT_SLOT,),
        (RuntimeResource.SCHEDULER_ROOT,),
        (RuntimeEffect.SCHEDULER_ROOT_ENTER,),
        "Register a scheduler-owned runtime root through the legacy slot API.",
    ),
    "pcc_gc_scheduler_root_unregister": _arrow(
        "pcc_gc_scheduler_root_unregister",
        (RuntimeResource.SCHEDULER_ROOT,),
        (),
        (RuntimeEffect.SCHEDULER_ROOT_LEAVE,),
        "Unregister a scheduler-owned runtime root through the legacy slot API.",
    ),
    "pcc_gc_safepoint": _arrow(
        "pcc_gc_safepoint",
        (),
        (),
        (RuntimeEffect.SAFEPOINT,),
        "Poll the runtime/GC/thread safepoint hook.",
    ),
    "pcc_gc_collect": _arrow(
        "pcc_gc_collect",
        (),
        (),
        (RuntimeEffect.COLLECT, RuntimeEffect.SAFEPOINT),
        "Run the selected backend's explicit collection path.",
    ),
    "pcc_gc_pin": _arrow(
        "pcc_gc_pin",
        (RuntimeResource.HEAP_OBJECT,),
        (RuntimeResource.PINNED_REGION,),
        (RuntimeEffect.PIN_ENTER,),
        "Pin an object/region against moving collector relocation.",
    ),
    "pcc_gc_unpin": _arrow(
        "pcc_gc_unpin",
        (RuntimeResource.PINNED_REGION,),
        (),
        (RuntimeEffect.PIN_LEAVE,),
        "Leave a pinned object/region.",
    ),
    "pcc_gc_take_pinned_slot": _arrow(
        "pcc_gc_take_pinned_slot",
        (RuntimeResource.ROOT_SLOT, RuntimeResource.PINNED_REGION),
        (RuntimeResource.HEAP_OBJECT,),
        (RuntimeEffect.PIN_LEAVE,),
        "Transfer an owned pinned value out of stable (possibly unregistered) slot storage; restore its prior pin without parking or changing refcount.",
    ),
    "pcc_gc_register_continuation_root": _arrow(
        "pcc_gc_register_continuation_root",
        (RuntimeResource.ROOT_SLOT,),
        (RuntimeResource.CONTINUATION_ROOT,),
        (RuntimeEffect.CONTINUATION_ROOT_ENTER,),
        "Register suspended continuation roots.",
    ),
    "pcc_gc_unregister_continuation_root": _arrow(
        "pcc_gc_unregister_continuation_root",
        (RuntimeResource.CONTINUATION_ROOT,),
        (),
        (RuntimeEffect.CONTINUATION_ROOT_LEAVE,),
        "Unregister suspended continuation roots.",
    ),
    "pcc_gc_scheduler_queue_push": _arrow(
        "pcc_gc_scheduler_queue_push",
        (RuntimeResource.SCHEDULER_QUEUE, RuntimeResource.HEAP_OBJECT),
        (RuntimeResource.SCHEDULER_QUEUE, RuntimeResource.SCHEDULER_ROOT),
        (
            RuntimeEffect.SCHEDULER_ENQUEUE,
            RuntimeEffect.SCHEDULER_ROOT_ENTER,
            RuntimeEffect.WRITE_BARRIER,
        ),
        "Enqueue a GC-visible scheduler root entry.",
    ),
    "pcc_gc_scheduler_queue_pop_into": _arrow(
        "pcc_gc_scheduler_queue_pop_into",
        (RuntimeResource.SCHEDULER_QUEUE, RuntimeResource.ROOT_SLOT),
        (RuntimeResource.ROOT_SLOT,),
        (
            RuntimeEffect.SCHEDULER_DEQUEUE,
            RuntimeEffect.SCHEDULER_ROOT_LEAVE,
            RuntimeEffect.WRITE_BARRIER,
        ),
        "Pop a scheduler queue entry into a root slot.",
    ),
    "py_continuation_new_typed": _arrow(
        "py_continuation_new_typed",
        (RuntimeResource.ROOT_SLOT,),
        (RuntimeResource.CONTINUATION, RuntimeResource.CONTINUATION_ROOT),
        (
            RuntimeEffect.ALLOC,
            RuntimeEffect.CAPTURE_CONTINUATION,
            RuntimeEffect.CONTINUATION_ROOT_ENTER,
        ),
        "Allocate a typed continuation and register its saved slots as roots.",
    ),
    "py_virtual_thread_new": _arrow(
        "py_virtual_thread_new",
        (RuntimeResource.CONTINUATION,),
        (RuntimeResource.VIRTUAL_THREAD,),
        (RuntimeEffect.ALLOC,),
        "Allocate a virtual-thread object around a captured continuation.",
    ),
    "py_virtual_thread_start": _arrow(
        "py_virtual_thread_start",
        (RuntimeResource.VIRTUAL_THREAD,),
        (RuntimeResource.SCHEDULER_QUEUE, RuntimeResource.SCHEDULER_ROOT),
        (
            RuntimeEffect.VTHREAD_START,
            RuntimeEffect.SCHEDULER_ENQUEUE,
            RuntimeEffect.SCHEDULER_ROOT_ENTER,
        ),
        "Start a virtual thread by making it scheduler-visible.",
    ),
    "py_virtual_thread_park": _arrow(
        "py_virtual_thread_park",
        (RuntimeResource.VIRTUAL_THREAD,),
        (RuntimeResource.VIRTUAL_THREAD,),
        (RuntimeEffect.VTHREAD_PARK,),
        "Park a virtual thread without blocking a carrier thread.",
    ),
    "py_virtual_thread_unpark": _arrow(
        "py_virtual_thread_unpark",
        (RuntimeResource.VIRTUAL_THREAD,),
        (RuntimeResource.SCHEDULER_QUEUE, RuntimeResource.SCHEDULER_ROOT),
        (
            RuntimeEffect.VTHREAD_UNPARK,
            RuntimeEffect.SCHEDULER_ENQUEUE,
            RuntimeEffect.SCHEDULER_ROOT_ENTER,
        ),
        "Unpark a virtual thread by enqueueing it for execution.",
    ),
    "py_virtual_thread_sleep": _arrow(
        "py_virtual_thread_sleep",
        (RuntimeResource.VIRTUAL_THREAD,),
        (RuntimeResource.SCHEDULER_QUEUE, RuntimeResource.SCHEDULER_ROOT),
        (
            RuntimeEffect.VTHREAD_SLEEP,
            RuntimeEffect.VTHREAD_PARK,
            RuntimeEffect.SCHEDULER_ROOT_ENTER,
        ),
        "Park a virtual thread on the timer queue.",
    ),
    "py_virtual_thread_cancel_timer": _arrow(
        "py_virtual_thread_cancel_timer",
        (RuntimeResource.VIRTUAL_THREAD, RuntimeResource.SCHEDULER_ROOT),
        (RuntimeResource.VIRTUAL_THREAD,),
        (
            RuntimeEffect.SCHEDULER_DEQUEUE,
            RuntimeEffect.SCHEDULER_ROOT_LEAVE,
        ),
        "Cancel an active timer and immediately release its scheduler root.",
    ),
    "py_virtual_thread_block_on_fd": _arrow(
        "py_virtual_thread_block_on_fd",
        (RuntimeResource.VIRTUAL_THREAD,),
        (RuntimeResource.SCHEDULER_QUEUE, RuntimeResource.SCHEDULER_ROOT),
        (
            RuntimeEffect.VTHREAD_BLOCK_IO,
            RuntimeEffect.VTHREAD_PARK,
            RuntimeEffect.SCHEDULER_ROOT_ENTER,
        ),
        "Park a virtual thread on the fd poller.",
    ),
    "py_virtual_thread_run_once": _arrow(
        "py_virtual_thread_run_once",
        (RuntimeResource.SCHEDULER_QUEUE,),
        (RuntimeResource.VIRTUAL_THREAD,),
        (
            RuntimeEffect.VTHREAD_RUN,
            RuntimeEffect.VTHREAD_RESUME,
            RuntimeEffect.SCHEDULER_DEQUEUE,
            RuntimeEffect.SCHEDULER_ROOT_LEAVE,
        ),
        "Run one scheduler/carrier step.",
    ),
    "py_virtual_thread_run_until_idle": _arrow(
        "py_virtual_thread_run_until_idle",
        (RuntimeResource.SCHEDULER_QUEUE,),
        (RuntimeResource.SCHEDULER_QUEUE,),
        (
            RuntimeEffect.VTHREAD_RUN,
            RuntimeEffect.VTHREAD_RESUME,
            RuntimeEffect.SCHEDULER_DEQUEUE,
            RuntimeEffect.SCHEDULER_ROOT_LEAVE,
        ),
        "Run bounded scheduler/carrier steps until idle or budget exhausted.",
    ),
}


RUNTIME_EFFECT_CATEGORY_OBJECT = "runtime_state"
_RUNTIME_EFFECT_CATEGORY: SmallCategory[str] | None = None
_RUNTIME_EFFECT_QUANTALE: EffectQuantale[frozenset[RuntimeEffect]] | None = None


CORRECTNESS_CRITICAL_RUNTIME_ABI_NAMES: frozenset[str] = frozenset(
    RUNTIME_ABI_ARROWS.keys()
)


def runtime_arrow(name: str) -> RuntimeArrow:
    """Return the contract arrow for ``name`` or raise a clear ``KeyError``."""

    try:
        return RUNTIME_ABI_ARROWS[name]
    except KeyError as exc:
        raise KeyError(f"no runtime-effect contract for {name!r}") from exc


def known_runtime_arrow_names() -> tuple[str, ...]:
    return tuple(sorted(RUNTIME_ABI_ARROWS))


def runtime_effect_category() -> SmallCategory[str]:
    """Return the runtime-effect category instance.

    Runtime ABI calls are modeled as decorated state transitions
    ``runtime_state -> runtime_state``.  Their resource inputs/outputs remain
    the runtime contract annotations on ``RuntimeArrow`` rather than strict
    categorical domain/codomain objects.
    """

    global _RUNTIME_EFFECT_CATEGORY
    if _RUNTIME_EFFECT_CATEGORY is None:
        _RUNTIME_EFFECT_CATEGORY = SmallCategory(
            "PCCRuntimeEffect",
            objects=(RUNTIME_EFFECT_CATEGORY_OBJECT,),
            arrows=(
                CategoryArrow(
                    name=name,
                    domain=RUNTIME_EFFECT_CATEGORY_OBJECT,
                    codomain=RUNTIME_EFFECT_CATEGORY_OBJECT,
                )
                for name in RUNTIME_ABI_ARROWS
            ),
        )
    return _RUNTIME_EFFECT_CATEGORY


def runtime_effect_category_path(names: Sequence[str]) -> CategoryPath[str]:
    return runtime_effect_category().path(
        names,
        start=RUNTIME_EFFECT_CATEGORY_OBJECT,
    )


def runtime_effect_quantale() -> EffectQuantale[frozenset[RuntimeEffect]]:
    """Return the runtime-effect grade quantale.

    This first instance is the powerset quantale over runtime effects:
    sequential composition and join are both set union, with the empty set as
    unit/bottom.  Ordering is therefore effect inclusion.
    """

    global _RUNTIME_EFFECT_QUANTALE
    if _RUNTIME_EFFECT_QUANTALE is None:
        _RUNTIME_EFFECT_QUANTALE = EffectQuantale(
            "PCCRuntimeEffectGrade",
            unit=frozenset(),
            bottom=frozenset(),
            compose=lambda left, right: frozenset(set(left) | set(right)),
            join=lambda left, right: frozenset(set(left) | set(right)),
        )
    return _RUNTIME_EFFECT_QUANTALE


def runtime_effect_quantale_law_violations() -> tuple[QuantaleViolation, ...]:
    samples = (
        frozenset(),
        runtime_arrow("pcc_gc_alloc").effects,
        runtime_arrow("pcc_gc_store_ptr").effects,
        runtime_arrow("pcc_gc_frame_enter").effects,
        runtime_arrow("pcc_gc_frame_leave").effects,
        runtime_arrow("pcc_gc_collect").effects,
        compose_runtime_effects(["pcc_gc_alloc", "pcc_gc_store_ptr"]),
        compose_runtime_effects(["pcc_gc_frame_enter", "pcc_gc_frame_leave"]),
    )
    return runtime_effect_quantale().check_laws(samples)


def arrows_with_effect(effect: RuntimeEffect) -> tuple[RuntimeArrow, ...]:
    return tuple(
        arrow for arrow in RUNTIME_ABI_ARROWS.values() if effect in arrow.effects
    )


def arrows_touching_resource(resource: RuntimeResource) -> tuple[RuntimeArrow, ...]:
    return tuple(
        arrow
        for arrow in RUNTIME_ABI_ARROWS.values()
        if arrow.touches_resource(resource)
    )


def missing_runtime_effect_contracts(
    runtime_signatures: Mapping[str, object] | Iterable[str],
    required_names: Iterable[str] | None = None,
) -> tuple[str, ...]:
    """Return required ABI names that are missing from the taxonomy.

    ``runtime_signatures`` may be the runtime ABI signature dict or any iterable
    of available runtime symbol names.  Missing symbols in the ABI are reported
    by ``missing_runtime_abi_symbols``; this function reports symbols that exist
    in the ABI but lack a runtime-effect arrow.
    """

    if isinstance(runtime_signatures, Mapping):
        available = set(runtime_signatures.keys())
    else:
        available = set(runtime_signatures)
    required = set(required_names or CORRECTNESS_CRITICAL_RUNTIME_ABI_NAMES)
    return tuple(sorted((required & available) - set(RUNTIME_ABI_ARROWS)))


def missing_runtime_abi_symbols(
    runtime_signatures: Mapping[str, object] | Iterable[str],
    required_names: Iterable[str] | None = None,
) -> tuple[str, ...]:
    """Return required contract names not exported by the runtime ABI table."""

    if isinstance(runtime_signatures, Mapping):
        available = set(runtime_signatures.keys())
    else:
        available = set(runtime_signatures)
    required = set(required_names or CORRECTNESS_CRITICAL_RUNTIME_ABI_NAMES)
    return tuple(sorted(required - available))


def compose_runtime_effects(names: Sequence[str]) -> frozenset[RuntimeEffect]:
    """Union effects for a composed runtime path."""

    quantale = runtime_effect_quantale()
    effects = quantale.unit
    for name in names:
        effects = quantale.compose(effects, runtime_arrow(name).effects)
    return effects


def compose_runtime_event_effects(
    events: Sequence[RuntimeEffectEvent],
) -> frozenset[RuntimeEffect]:
    """Union effects for a recorded runtime-event path."""

    return compose_runtime_effects(tuple(event.name for event in events))


_PRODUCTION_VTHREAD_EVENT_EFFECTS: Mapping[
    ProductionVThreadEventKind, frozenset[RuntimeEffect]
] = {
    ProductionVThreadEventKind.ROOT_ENTER: frozenset(
        (RuntimeEffect.SCHEDULER_ROOT_ENTER,)
    ),
    ProductionVThreadEventKind.ROOT_LEAVE: frozenset(
        (RuntimeEffect.SCHEDULER_ROOT_LEAVE,)
    ),
    ProductionVThreadEventKind.READY_ENQUEUE: frozenset(
        (RuntimeEffect.SCHEDULER_ENQUEUE,)
    ),
    ProductionVThreadEventKind.START: frozenset((RuntimeEffect.VTHREAD_START,)),
    ProductionVThreadEventKind.PARK: frozenset((RuntimeEffect.VTHREAD_PARK,)),
    ProductionVThreadEventKind.UNPARK: frozenset(
        (RuntimeEffect.VTHREAD_UNPARK,)
    ),
    ProductionVThreadEventKind.RESUME: frozenset(
        (
            RuntimeEffect.VTHREAD_RUN,
            RuntimeEffect.VTHREAD_RESUME,
            RuntimeEffect.SCHEDULER_DEQUEUE,
        )
    ),
    ProductionVThreadEventKind.TIMER_PARK: frozenset(
        (RuntimeEffect.VTHREAD_SLEEP, RuntimeEffect.VTHREAD_PARK)
    ),
    ProductionVThreadEventKind.TIMER_WAKE: frozenset(
        (RuntimeEffect.VTHREAD_UNPARK,)
    ),
    ProductionVThreadEventKind.IO_PARK: frozenset(
        (RuntimeEffect.VTHREAD_BLOCK_IO, RuntimeEffect.VTHREAD_PARK)
    ),
    ProductionVThreadEventKind.IO_WAKE: frozenset(
        (RuntimeEffect.VTHREAD_UNPARK,)
    ),
    ProductionVThreadEventKind.CANCEL_TIMER: frozenset(
        (RuntimeEffect.VTHREAD_CANCEL, RuntimeEffect.SCHEDULER_DEQUEUE)
    ),
    ProductionVThreadEventKind.CANCEL_IO: frozenset(
        (RuntimeEffect.VTHREAD_CANCEL, RuntimeEffect.SCHEDULER_DEQUEUE)
    ),
    ProductionVThreadEventKind.COMPLETE: frozenset(
        (RuntimeEffect.VTHREAD_COMPLETE,)
    ),
    ProductionVThreadEventKind.FAIL: frozenset((RuntimeEffect.VTHREAD_FAIL,)),
    ProductionVThreadEventKind.CANCEL_COMPLETE: frozenset(
        (RuntimeEffect.VTHREAD_CANCEL, RuntimeEffect.VTHREAD_COMPLETE)
    ),
}


def production_vthread_event_effects(
    event: ProductionVThreadEvent | ProductionVThreadEventKind,
) -> frozenset[RuntimeEffect]:
    """Map an observed C scheduler event into the shared effect vocabulary."""

    kind = event.kind if isinstance(event, ProductionVThreadEvent) else event
    return _PRODUCTION_VTHREAD_EVENT_EFFECTS[kind]


def compose_production_vthread_event_effects(
    events: Sequence[ProductionVThreadEvent],
) -> frozenset[RuntimeEffect]:
    effects: set[RuntimeEffect] = set()
    for event in events:
        effects.update(production_vthread_event_effects(event))
    return frozenset(effects)


def check_production_vthread_events(
    events: Sequence[ProductionVThreadEvent],
) -> tuple[RuntimeContractViolation, ...]:
    """Check production event schema, states, and scheduler-root balance.

    Root events are recorded at the actual handle register/unregister calls,
    rather than inferred from logical public-ABI calls. This catches failed
    insertion/cancellation paths and root transfers that a logical call trace
    cannot see.
    """

    expected_delta = {
        ProductionVThreadEventKind.ROOT_ENTER: 1,
        ProductionVThreadEventKind.ROOT_LEAVE: -1,
    }
    expected_state = {
        ProductionVThreadEventKind.ROOT_ENTER: -1,
        ProductionVThreadEventKind.ROOT_LEAVE: -1,
        ProductionVThreadEventKind.READY_ENQUEUE: 1,
        ProductionVThreadEventKind.START: 1,
        ProductionVThreadEventKind.PARK: 3,
        ProductionVThreadEventKind.UNPARK: 1,
        ProductionVThreadEventKind.RESUME: 2,
        ProductionVThreadEventKind.TIMER_PARK: 3,
        ProductionVThreadEventKind.TIMER_WAKE: 1,
        ProductionVThreadEventKind.IO_PARK: 3,
        ProductionVThreadEventKind.IO_WAKE: 1,
        ProductionVThreadEventKind.CANCEL_TIMER: 3,
        ProductionVThreadEventKind.CANCEL_IO: 3,
        ProductionVThreadEventKind.COMPLETE: 4,
        ProductionVThreadEventKind.FAIL: 4,
        ProductionVThreadEventKind.CANCEL_COMPLETE: 4,
    }
    root_balance = 0
    violations: list[RuntimeContractViolation] = []
    for index, event in enumerate(events):
        name = event.kind.name.lower()
        delta = expected_delta.get(event.kind, 0)
        if event.root_delta != delta:
            violations.append(
                RuntimeContractViolation(
                    f"production event root_delta {event.root_delta} != {delta}",
                    index,
                    name,
                )
            )
        state = expected_state[event.kind]
        if event.state != state:
            violations.append(
                RuntimeContractViolation(
                    f"production event state {event.state} != {state}",
                    index,
                    name,
                )
            )
        root_balance += event.root_delta
        if root_balance < 0:
            violations.append(
                RuntimeContractViolation(
                    "production scheduler root leave without matching enter",
                    index,
                    name,
                )
            )
            root_balance = 0
        if event.kind in (
            ProductionVThreadEventKind.READY_ENQUEUE,
            ProductionVThreadEventKind.TIMER_PARK,
            ProductionVThreadEventKind.IO_PARK,
        ) and root_balance <= 0:
            violations.append(
                RuntimeContractViolation(
                    "production scheduler visibility without a live root",
                    index,
                    name,
                )
            )
    if root_balance != 0:
        violations.append(
            RuntimeContractViolation(
                "production scheduler root enter without matching leave",
                len(events),
                "<end>",
            )
        )
    return tuple(violations)


def check_runtime_events(
    events: Sequence[RuntimeEffectEvent],
) -> tuple[RuntimeContractViolation, ...]:
    """Check bracket invariants for a recorded runtime-event path."""

    return check_runtime_path(tuple(event.name for event in events))


def check_runtime_path(names: Sequence[str]) -> tuple[RuntimeContractViolation, ...]:
    """Check simple bracket invariants for a composed runtime path.

    This deliberately checks only local syntactic balance.  It does not prove
    full ownership, aliasing, or collector correctness.
    """

    frame_roots = 0
    continuation_roots = 0
    scheduler_roots = 0
    pins = 0
    violations: list[RuntimeContractViolation] = []

    for index, name in enumerate(names):
        arrow = runtime_arrow(name)
        effects = arrow.effects

        if RuntimeEffect.ROOT_ENTER in effects:
            frame_roots += 1
        if RuntimeEffect.ROOT_LEAVE in effects:
            frame_roots -= 1
            if frame_roots < 0:
                violations.append(
                    RuntimeContractViolation(
                        "frame root leave without matching enter",
                        index,
                        name,
                    )
                )
                frame_roots = 0

        if RuntimeEffect.CONTINUATION_ROOT_ENTER in effects:
            continuation_roots += 1
        if RuntimeEffect.CONTINUATION_ROOT_LEAVE in effects:
            continuation_roots -= 1
            if continuation_roots < 0:
                violations.append(
                    RuntimeContractViolation(
                        "continuation root leave without matching enter",
                        index,
                        name,
                    )
                )
                continuation_roots = 0

        if RuntimeEffect.SCHEDULER_ROOT_ENTER in effects:
            scheduler_roots += 1
        if (
            RuntimeEffect.VTHREAD_PARK in effects
            and scheduler_roots <= 0
            and RuntimeEffect.SCHEDULER_ROOT_ENTER not in effects
        ):
            violations.append(
                RuntimeContractViolation(
                    "virtual thread park without scheduler root",
                    index,
                    name,
                )
            )
        if RuntimeEffect.SCHEDULER_ROOT_LEAVE in effects:
            scheduler_roots -= 1
            if scheduler_roots < 0:
                violations.append(
                    RuntimeContractViolation(
                        "scheduler root leave without matching enter",
                        index,
                        name,
                    )
                )
                scheduler_roots = 0

        if RuntimeEffect.PIN_ENTER in effects:
            pins += 1
        if RuntimeEffect.PIN_LEAVE in effects:
            pins -= 1
            if pins < 0:
                violations.append(
                    RuntimeContractViolation(
                        "pin leave without matching enter",
                        index,
                        name,
                    )
                )
                pins = 0

    if frame_roots != 0:
        violations.append(
            RuntimeContractViolation(
                "frame root enter without matching leave",
                len(names),
                "<end>",
            )
        )
    if continuation_roots != 0:
        violations.append(
            RuntimeContractViolation(
                "continuation root enter without matching leave",
                len(names),
                "<end>",
            )
        )
    if scheduler_roots != 0:
        violations.append(
            RuntimeContractViolation(
                "scheduler root enter without matching leave",
                len(names),
                "<end>",
            )
        )
    if pins != 0:
        violations.append(
            RuntimeContractViolation(
                "pin enter without matching leave",
                len(names),
                "<end>",
            )
        )
    return tuple(violations)


__all__ = [
    "CORRECTNESS_CRITICAL_RUNTIME_ABI_NAMES",
    "RUNTIME_ABI_ARROWS",
    "RUNTIME_EFFECT_CATEGORY_OBJECT",
    "ProductionVThreadEvent",
    "ProductionVThreadEventKind",
    "RuntimeArrow",
    "RuntimeContractViolation",
    "RuntimeEffect",
    "RuntimeEffectEvent",
    "RuntimeEffectRecorder",
    "RuntimeResource",
    "arrows_touching_resource",
    "arrows_with_effect",
    "check_runtime_events",
    "check_runtime_path",
    "check_production_vthread_events",
    "compose_production_vthread_event_effects",
    "compose_runtime_event_effects",
    "compose_runtime_effects",
    "known_runtime_arrow_names",
    "missing_runtime_abi_symbols",
    "missing_runtime_effect_contracts",
    "production_vthread_event_effects",
    "runtime_effect_category",
    "runtime_effect_category_path",
    "runtime_effect_quantale",
    "runtime_effect_quantale_law_violations",
    "runtime_arrow",
]
