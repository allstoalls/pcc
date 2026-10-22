"""Owner-neutral legality and guard contract for one typed buffer loop.

The first deliberately bounded plan is a dot product over two read-only,
signed-i64 buffers.  It is not a licence to specialize arbitrary Python
iteration.  The generic scalar loop remains part of every accepted plan and
executes from index zero on a guard miss.  Its operation order is

``left[i] -> right[i] -> multiply -> accumulate``

and its integer operations use Python promotion semantics.  The fast loop may
use raw i64 operations only after all guards have completed without invoking
user code.  Its multiply and add remain checked; an unexpected overflow also
restarts the scalar loop before a result is published.

Both supported self target emitters consume the same sequence returned by
``owner_lowering_contract``.  Target adapters may select instructions, but may
not delete/reorder guards or replace the scalar fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Iterable, Mapping


SCHEMA = "pcc.guarded-loop-plan.v1"
LOOP_KIND = "read-only-i64-buffer-dot"
SUPPORTED_OWNERS = (
    "self-aarch64-darwin",
    "self-x86_64-linux",
)
EXPECTED_EFFECTS = ("read:left", "read:right")
EXPECTED_EXCEPTION_ORDER = (
    "left-load",
    "right-load",
    "multiply",
    "accumulate",
)
GUARD_ORDER = (
    "left-exact-type",
    "right-exact-type",
    "left-layout-version",
    "right-layout-version",
    "function-version",
    "globals-version",
    "left-buffer-version",
    "right-buffer-version",
    "trip-count",
    "no-alias",
    "left-unit-stride",
    "right-unit-stride",
    "left-alignment",
    "right-alignment",
    "left-integer-range",
    "right-integer-range",
)
FAST_OPERATIONS = (
    "fast.accumulator.i64.zero",
    "fast.loop.left-load.i64",
    "fast.loop.right-load.i64",
    "fast.loop.multiply.checked-i64",
    "fast.loop.accumulate.checked-i64",
    "fast.overflow.restart-scalar-at-zero",
    "fast.result.box-python-int",
)
SCALAR_OPERATIONS = (
    "scalar.index.zero",
    "scalar.loop.left-getitem",
    "scalar.loop.right-getitem",
    "scalar.loop.python-int-multiply-promote",
    "scalar.loop.python-int-add-promote",
    "scalar.loop.check-exception-before-next-index",
    "scalar.result.return-python-int",
)

_I64_MIN = -(1 << 63)
_I64_MAX = (1 << 63) - 1
_DIGEST_HEX = frozenset("0123456789abcdef")


def _clean_text(value: object, field: str) -> str:
    text = str(value)
    if not text or "\x00" in text or "\n" in text or "\r" in text:
        raise ValueError("invalid " + field)
    return text


def _clean_digest(value: object, field: str) -> str:
    text = _clean_text(value, field)
    if len(text) != 64:
        raise ValueError("invalid " + field + " digest")
    for char in text:
        if char not in _DIGEST_HEX:
            raise ValueError("invalid " + field + " digest")
    return text


def _clean_range(value: Iterable[int], field: str) -> tuple[int, int]:
    pair = tuple(int(item) for item in value)
    if len(pair) != 2 or pair[0] > pair[1]:
        raise ValueError("invalid " + field + " range")
    if pair[0] < _I64_MIN or pair[1] > _I64_MAX:
        raise ValueError(field + " range is outside signed i64")
    return pair[0], pair[1]


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


@dataclass(frozen=True)
class Guard:
    kind: str
    subject: str
    expected: object
    miss_reason: str

    def payload(self) -> dict[str, object]:
        return {
            "expected": self.expected,
            "kind": self.kind,
            "miss_reason": self.miss_reason,
            "subject": self.subject,
        }


@dataclass(frozen=True)
class DotLoopCandidate:
    """Facts frozen by frontend analysis for the one supported loop."""

    source_id: str
    function_id: str
    left_type_id: str
    right_type_id: str
    left_layout_version: str
    right_layout_version: str
    function_version: str
    globals_version: str
    left_buffer_version: int
    right_buffer_version: int
    trip_count: int
    left_stride_bytes: int
    right_stride_bytes: int
    left_alignment: int
    right_alignment: int
    left_integer_range: tuple[int, int]
    right_integer_range: tuple[int, int]
    effects: tuple[str, ...] = EXPECTED_EFFECTS
    exception_order: tuple[str, ...] = EXPECTED_EXCEPTION_ORDER
    exact_builtin_buffers: bool = True
    readonly: bool = True
    scalar_oracle_id: str = "python-int-dot.v1"

    @classmethod
    def create(
        cls,
        *,
        source_id: str,
        function_id: str,
        left_type_id: str,
        right_type_id: str,
        left_layout_version: str,
        right_layout_version: str,
        function_version: str,
        globals_version: str,
        left_buffer_version: int,
        right_buffer_version: int,
        trip_count: int,
        left_stride_bytes: int,
        right_stride_bytes: int,
        left_alignment: int,
        right_alignment: int,
        left_integer_range: Iterable[int],
        right_integer_range: Iterable[int],
        effects: Iterable[str] = EXPECTED_EFFECTS,
        exception_order: Iterable[str] = EXPECTED_EXCEPTION_ORDER,
        exact_builtin_buffers: bool = True,
        readonly: bool = True,
        scalar_oracle_id: str = "python-int-dot.v1",
    ) -> "DotLoopCandidate":
        return cls(
            source_id=_clean_digest(source_id, "source"),
            function_id=_clean_text(function_id, "function id"),
            left_type_id=_clean_text(left_type_id, "left type"),
            right_type_id=_clean_text(right_type_id, "right type"),
            left_layout_version=_clean_digest(
                left_layout_version, "left layout"
            ),
            right_layout_version=_clean_digest(
                right_layout_version, "right layout"
            ),
            function_version=_clean_digest(function_version, "function"),
            globals_version=_clean_digest(globals_version, "globals"),
            left_buffer_version=int(left_buffer_version),
            right_buffer_version=int(right_buffer_version),
            trip_count=int(trip_count),
            left_stride_bytes=int(left_stride_bytes),
            right_stride_bytes=int(right_stride_bytes),
            left_alignment=int(left_alignment),
            right_alignment=int(right_alignment),
            left_integer_range=_clean_range(left_integer_range, "left integer"),
            right_integer_range=_clean_range(
                right_integer_range, "right integer"
            ),
            effects=tuple(str(item) for item in effects),
            exception_order=tuple(str(item) for item in exception_order),
            exact_builtin_buffers=bool(exact_builtin_buffers),
            readonly=bool(readonly),
            scalar_oracle_id=_clean_text(scalar_oracle_id, "scalar oracle"),
        )


@dataclass(frozen=True)
class TargetCost:
    target: str
    vector_lanes: int
    scalar_cost: int
    fast_cost: int
    guard_cost: int
    minimum_speedup_basis_points: int = 500

    @classmethod
    def create(
        cls,
        *,
        target: str,
        vector_lanes: int,
        scalar_cost: int,
        fast_cost: int,
        guard_cost: int,
        minimum_speedup_basis_points: int = 500,
    ) -> "TargetCost":
        clean_target = _clean_text(target, "target")
        if clean_target not in SUPPORTED_OWNERS:
            raise ValueError("unsupported loop-plan target " + clean_target)
        values = (
            int(vector_lanes),
            int(scalar_cost),
            int(fast_cost),
            int(guard_cost),
        )
        if any(value <= 0 for value in values):
            raise ValueError("loop-plan costs and lanes must be positive")
        threshold = int(minimum_speedup_basis_points)
        if threshold < 0 or threshold >= 10_000:
            raise ValueError("invalid minimum speedup threshold")
        return cls(
            target=clean_target,
            vector_lanes=values[0],
            scalar_cost=values[1],
            fast_cost=values[2],
            guard_cost=values[3],
            minimum_speedup_basis_points=threshold,
        )


@dataclass(frozen=True)
class GuardedLoopPlan:
    candidate_id: str
    accepted: bool
    rejection_reasons: tuple[str, ...]
    guards: tuple[Guard, ...]
    fast_operations: tuple[str, ...]
    scalar_operations: tuple[str, ...]
    target_cost: TargetCost
    scalar_oracle_id: str

    def payload(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "candidate_id": self.candidate_id,
            "fast_operations": list(self.fast_operations),
            "guards": [guard.payload() for guard in self.guards],
            "loop_kind": LOOP_KIND,
            "rejection_reasons": list(self.rejection_reasons),
            "scalar_operations": list(self.scalar_operations),
            "scalar_oracle_id": self.scalar_oracle_id,
            "schema": SCHEMA,
            "target_cost": {
                "fast_cost": self.target_cost.fast_cost,
                "guard_cost": self.target_cost.guard_cost,
                "minimum_speedup_basis_points": (
                    self.target_cost.minimum_speedup_basis_points
                ),
                "scalar_cost": self.target_cost.scalar_cost,
                "target": self.target_cost.target,
                "vector_lanes": self.target_cost.vector_lanes,
            },
        }

    def digest(self) -> str:
        return hashlib.sha256(_canonical_json(self.payload())).hexdigest()


@dataclass(frozen=True)
class RuntimeObservation:
    left_type_id: str
    right_type_id: str
    left_layout_version: str
    right_layout_version: str
    function_version: str
    globals_version: str
    left_buffer_version: int
    right_buffer_version: int
    trip_count: int
    aliases: bool
    left_stride_bytes: int
    right_stride_bytes: int
    left_alignment: int
    right_alignment: int
    left_integer_range: tuple[int, int]
    right_integer_range: tuple[int, int]


@dataclass(frozen=True)
class GuardEvaluation:
    hit: bool
    miss_guard: str
    counters: tuple[tuple[str, int], ...]


def _range_product_bound(
    left_range: tuple[int, int],
    right_range: tuple[int, int],
    trip_count: int,
) -> int:
    left_abs = max(abs(left_range[0]), abs(left_range[1]))
    right_abs = max(abs(right_range[0]), abs(right_range[1]))
    return left_abs * right_abs * trip_count


def _candidate_id(candidate: DotLoopCandidate) -> str:
    material = {
        "function_id": candidate.function_id,
        "loop_kind": LOOP_KIND,
        "scalar_oracle_id": candidate.scalar_oracle_id,
        "source_id": candidate.source_id,
    }
    return hashlib.sha256(_canonical_json(material)).hexdigest()


def _guards(candidate: DotLoopCandidate) -> tuple[Guard, ...]:
    guards = (
        Guard("left-exact-type", "left", candidate.left_type_id, "type-miss"),
        Guard("right-exact-type", "right", candidate.right_type_id, "type-miss"),
        Guard(
            "left-layout-version",
            "left",
            candidate.left_layout_version,
            "layout-miss",
        ),
        Guard(
            "right-layout-version",
            "right",
            candidate.right_layout_version,
            "layout-miss",
        ),
        Guard(
            "function-version",
            "function",
            candidate.function_version,
            "function-version-miss",
        ),
        Guard(
            "globals-version",
            "globals",
            candidate.globals_version,
            "globals-version-miss",
        ),
        Guard(
            "left-buffer-version",
            "left",
            candidate.left_buffer_version,
            "buffer-version-miss",
        ),
        Guard(
            "right-buffer-version",
            "right",
            candidate.right_buffer_version,
            "buffer-version-miss",
        ),
        Guard("trip-count", "buffers", candidate.trip_count, "length-miss"),
        Guard("no-alias", "left,right", False, "alias-miss"),
        Guard(
            "left-unit-stride",
            "left",
            candidate.left_stride_bytes,
            "stride-miss",
        ),
        Guard(
            "right-unit-stride",
            "right",
            candidate.right_stride_bytes,
            "stride-miss",
        ),
        Guard(
            "left-alignment",
            "left",
            candidate.left_alignment,
            "alignment-miss",
        ),
        Guard(
            "right-alignment",
            "right",
            candidate.right_alignment,
            "alignment-miss",
        ),
        Guard(
            "left-integer-range",
            "left",
            list(candidate.left_integer_range),
            "integer-range-miss",
        ),
        Guard(
            "right-integer-range",
            "right",
            list(candidate.right_integer_range),
            "integer-range-miss",
        ),
    )
    if tuple(guard.kind for guard in guards) != GUARD_ORDER:
        raise AssertionError("guard order drift")
    return guards


def build_dot_loop_plan(
    candidate: DotLoopCandidate,
    target_cost: TargetCost,
) -> GuardedLoopPlan:
    """Build the plan or an explicit rejection retaining only the slow path."""

    reasons: list[str] = []
    if not candidate.exact_builtin_buffers:
        reasons.append("not-exact-builtin-buffers")
    if not candidate.readonly:
        reasons.append("observable-store-or-mutation")
    if candidate.effects != EXPECTED_EFFECTS:
        reasons.append("unproved-or-observable-effect")
    if candidate.exception_order != EXPECTED_EXCEPTION_ORDER:
        reasons.append("exception-order-mismatch")
    if candidate.trip_count <= 0:
        reasons.append("empty-loop")
    if candidate.left_stride_bytes != 8 or candidate.right_stride_bytes != 8:
        reasons.append("non-unit-stride")
    if candidate.left_alignment < 8 or candidate.right_alignment < 8:
        reasons.append("insufficient-alignment")
    if candidate.trip_count < target_cost.vector_lanes:
        reasons.append("trip-count-below-target-lanes")
    if _range_product_bound(
        candidate.left_integer_range,
        candidate.right_integer_range,
        max(candidate.trip_count, 0),
    ) > _I64_MAX:
        reasons.append("integer-range-needs-python-promotion")

    guarded_cost = target_cost.fast_cost + target_cost.guard_cost
    allowed_cost = (
        target_cost.scalar_cost
        * (10_000 - target_cost.minimum_speedup_basis_points)
    ) // 10_000
    if guarded_cost > allowed_cost:
        reasons.append("target-cost-not-profitable")

    accepted = not reasons
    return GuardedLoopPlan(
        candidate_id=_candidate_id(candidate),
        accepted=accepted,
        rejection_reasons=tuple(reasons),
        guards=_guards(candidate) if accepted else (),
        fast_operations=FAST_OPERATIONS if accepted else (),
        scalar_operations=SCALAR_OPERATIONS,
        target_cost=target_cost,
        scalar_oracle_id=candidate.scalar_oracle_id,
    )


def _observed_values(observation: RuntimeObservation) -> dict[str, object]:
    return {
        "left-exact-type": observation.left_type_id,
        "right-exact-type": observation.right_type_id,
        "left-layout-version": observation.left_layout_version,
        "right-layout-version": observation.right_layout_version,
        "function-version": observation.function_version,
        "globals-version": observation.globals_version,
        "left-buffer-version": observation.left_buffer_version,
        "right-buffer-version": observation.right_buffer_version,
        "trip-count": observation.trip_count,
        "no-alias": observation.aliases,
        "left-unit-stride": observation.left_stride_bytes,
        "right-unit-stride": observation.right_stride_bytes,
        "left-alignment": observation.left_alignment,
        "right-alignment": observation.right_alignment,
        "left-integer-range": list(observation.left_integer_range),
        "right-integer-range": list(observation.right_integer_range),
    }


def evaluate_guards(
    plan: GuardedLoopPlan,
    observation: RuntimeObservation,
) -> GuardEvaluation:
    """Evaluate in semantic order and identify the exact scalar-path edge."""

    if not plan.accepted:
        return GuardEvaluation(
            hit=False,
            miss_guard="plan-rejected",
            counters=(("candidate", 1), ("rejected", 1), ("slow-path", 1)),
        )
    observed = _observed_values(observation)
    for guard in plan.guards:
        if observed[guard.kind] != guard.expected:
            return GuardEvaluation(
                hit=False,
                miss_guard=guard.kind,
                counters=(
                    ("candidate", 1),
                    ("guard-miss", 1),
                    ("slow-path", 1),
                ),
            )
    return GuardEvaluation(
        hit=True,
        miss_guard="",
        counters=(("candidate", 1), ("guard-hit", 1), ("fast-path", 1)),
    )


def owner_lowering_contract(
    owner: str,
    plan: GuardedLoopPlan,
) -> tuple[str, ...]:
    """Return the exact target-neutral operation order an owner must lower."""

    clean_owner = _clean_text(owner, "owner")
    if clean_owner not in SUPPORTED_OWNERS:
        raise ValueError("unsupported guarded-loop owner " + clean_owner)
    if not plan.accepted:
        return plan.scalar_operations
    guard_ops = tuple("guard." + guard.kind for guard in plan.guards)
    return (
        *guard_ops,
        "guard.miss.branch-scalar-at-zero",
        *plan.fast_operations,
        *plan.scalar_operations,
    )


def plan_from_payload(payload: Mapping[str, Any]) -> GuardedLoopPlan:
    """Strict artifact reader; unknown or reordered fields fail closed."""

    expected_keys = {
        "accepted",
        "candidate_id",
        "fast_operations",
        "guards",
        "loop_kind",
        "rejection_reasons",
        "scalar_operations",
        "scalar_oracle_id",
        "schema",
        "target_cost",
    }
    if set(payload) != expected_keys:
        raise ValueError("guarded-loop plan fields do not match schema")
    if payload.get("schema") != SCHEMA or payload.get("loop_kind") != LOOP_KIND:
        raise ValueError("unsupported guarded-loop plan schema")
    cost_payload = payload.get("target_cost")
    if not isinstance(cost_payload, Mapping):
        raise ValueError("invalid guarded-loop target cost")
    if set(cost_payload) != {
        "fast_cost",
        "guard_cost",
        "minimum_speedup_basis_points",
        "scalar_cost",
        "target",
        "vector_lanes",
    }:
        raise ValueError("guarded-loop target cost fields do not match schema")
    cost = TargetCost.create(**dict(cost_payload))
    raw_guards = payload.get("guards")
    if not isinstance(raw_guards, list):
        raise ValueError("invalid guarded-loop guard list")
    guards: list[Guard] = []
    for raw in raw_guards:
        if not isinstance(raw, Mapping) or set(raw) != {
            "expected",
            "kind",
            "miss_reason",
            "subject",
        }:
            raise ValueError("invalid guarded-loop guard")
        guards.append(
            Guard(
                kind=_clean_text(raw["kind"], "guard kind"),
                subject=_clean_text(raw["subject"], "guard subject"),
                expected=raw["expected"],
                miss_reason=_clean_text(raw["miss_reason"], "guard reason"),
            )
        )
    accepted = bool(payload.get("accepted"))
    if accepted and tuple(guard.kind for guard in guards) != GUARD_ORDER:
        raise ValueError("guarded-loop guard order mismatch")
    if not accepted and guards:
        raise ValueError("rejected guarded-loop plan carries guards")
    fast_operations = tuple(str(item) for item in payload.get("fast_operations", ()))
    scalar_operations = tuple(
        str(item) for item in payload.get("scalar_operations", ())
    )
    if accepted and fast_operations != FAST_OPERATIONS:
        raise ValueError("guarded-loop fast operation contract mismatch")
    if not accepted and fast_operations:
        raise ValueError("rejected guarded-loop plan carries fast operations")
    if scalar_operations != SCALAR_OPERATIONS:
        raise ValueError("guarded-loop scalar fallback contract mismatch")
    candidate_id = _clean_digest(payload.get("candidate_id"), "candidate")
    scalar_oracle_id = _clean_text(
        payload.get("scalar_oracle_id"), "scalar oracle"
    )
    reasons = tuple(str(item) for item in payload.get("rejection_reasons", ()))
    if accepted == bool(reasons):
        raise ValueError("guarded-loop acceptance/rejection mismatch")
    return GuardedLoopPlan(
        candidate_id=candidate_id,
        accepted=accepted,
        rejection_reasons=reasons,
        guards=tuple(guards),
        fast_operations=fast_operations,
        scalar_operations=scalar_operations,
        target_cost=cost,
        scalar_oracle_id=scalar_oracle_id,
    )
