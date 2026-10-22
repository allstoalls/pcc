"""Frozen workload and performance contract for CPython replacement claims.

This module deliberately does not run the workloads.  It loads and validates
the finite catalogue that says what a Level 1, 2, or 3 product claim must
prove.  Runtime evidence is validated separately; a host-Python or
``cpython-compat`` result can therefore never turn an ``unverified`` catalogue
entry into a pcc-native replacement claim merely by changing a label.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "pcc.cpython-replacement.workloads.v1"
LANGUAGE_BASELINE = "3.13"
ORACLE_BASELINE = "CPython 3.15.0"
TARGETS = ("arm64-apple-darwin", "x86_64-unknown-linux-gnu")
LEVELS = (1, 2, 3)
STATUS_UNVERIFIED = "unverified"
CLAIM_MODE = "pcc1/pcc-native/self/no-libpython"
FORBIDDEN_EVIDENCE_MODES = (
    "host-python",
    "host-pcc",
    "cpython-compat",
    "libpython",
    "llvm-fallback",
)

DEFAULT_CATALOG_PATH = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "compat"
    / "pcc1-cpython-replacement-workloads.json"
)

_REQUIRED_TOP_LEVEL = {
    "schema",
    "python_baseline",
    "targets",
    "claim_contract",
    "levels",
    "workloads",
}
_REQUIRED_LEVEL_FIELDS = {
    "level",
    "name",
    "cumulative_includes",
    "required_categories",
    "required_gates",
    "performance_envelope",
    "unsupported_policy",
    "claim_mode",
    "status",
    "final_product_goal",
}
_REQUIRED_WORKLOAD_FIELDS = {
    "id",
    "minimum_level",
    "category",
    "title",
    "source",
    "targets",
    "lifecycle",
    "oracle",
    "required_gates",
    "performance_envelope",
    "unsupported_policy",
    "claim_mode",
    "status",
}
_REQUIRED_PERF_FIELDS = {
    "duration_seconds",
    "warmup_seconds",
    "sample_interval_seconds",
    "metrics",
    "acceptance",
}
_REQUIRED_ACCEPTANCE_FIELDS = {
    "correctness",
    "resource_growth",
    "comparison",
    "max_error_count",
    "rss_growth_estimator",
    "max_rss_growth_fraction_of_peak_per_hour",
    "max_gc_pause_p95_seconds",
    "throughput_policy",
    "latency_policy",
}
_REQUIRED_METRICS = {
    "throughput",
    "latency_p50",
    "latency_p95",
    "latency_p99",
    "rss_peak_bytes",
    "rss_growth_bytes_per_hour",
    "gc_pause_p95_seconds",
    "error_count",
}
_REQUIRED_LIFECYCLE = {
    "acquire",
    "install",
    "build",
    "start",
    "exercise",
    "shutdown",
    "cleanup",
}


class WorkloadCatalogError(ValueError):
    """A catalogue violates the frozen, fail-closed workload contract."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _fail(code: str, detail: str) -> None:
    raise WorkloadCatalogError(code, detail)


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail("PCC-CPY-WORKLOAD-002", f"{path} must be an object")
    return value


def _nonempty_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail("PCC-CPY-WORKLOAD-002", f"{path} must be a non-empty string")
    return value


def _string_list(value: Any, path: str, *, nonempty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, list) or (nonempty and not value):
        qualifier = "a non-empty" if nonempty else "a"
        _fail("PCC-CPY-WORKLOAD-002", f"{path} must be {qualifier} string list")
    result = tuple(_nonempty_string(item, f"{path}[]") for item in value)
    if len(result) != len(set(result)):
        _fail("PCC-CPY-WORKLOAD-002", f"{path} contains duplicate entries")
    return result


def _exact_fields(value: Mapping[str, Any], required: set[str], path: str) -> None:
    missing = sorted(required - set(value))
    extra = sorted(set(value) - required)
    if missing:
        _fail("PCC-CPY-WORKLOAD-002", f"{path} missing fields: {', '.join(missing)}")
    if extra:
        _fail("PCC-CPY-WORKLOAD-002", f"{path} has unknown fields: {', '.join(extra)}")


def _validate_performance(value: Any, path: str) -> None:
    envelope = _mapping(value, path)
    _exact_fields(envelope, _REQUIRED_PERF_FIELDS, path)
    duration = envelope["duration_seconds"]
    warmup = envelope["warmup_seconds"]
    interval = envelope["sample_interval_seconds"]
    if type(duration) is not int or duration < 60:
        _fail("PCC-CPY-WORKLOAD-006", f"{path}.duration_seconds must be >= 60")
    if type(warmup) is not int or warmup < 0 or warmup >= duration:
        _fail(
            "PCC-CPY-WORKLOAD-006",
            f"{path}.warmup_seconds must be >= 0 and less than duration_seconds",
        )
    if type(interval) is not int or interval <= 0 or interval > duration:
        _fail(
            "PCC-CPY-WORKLOAD-006",
            f"{path}.sample_interval_seconds must be in 1..duration_seconds",
        )
    metrics = set(_string_list(envelope["metrics"], f"{path}.metrics"))
    missing_metrics = sorted(_REQUIRED_METRICS - metrics)
    if missing_metrics:
        _fail(
            "PCC-CPY-WORKLOAD-006",
            f"{path}.metrics missing: {', '.join(missing_metrics)}",
        )
    acceptance = _mapping(envelope["acceptance"], f"{path}.acceptance")
    if set(acceptance) != _REQUIRED_ACCEPTANCE_FIELDS:
        _fail(
            "PCC-CPY-WORKLOAD-006",
            f"{path}.acceptance fields are not the frozen measurement contract",
        )
    for key in ("correctness", "resource_growth", "comparison"):
        rule = acceptance[key]
        _nonempty_string(rule, f"{path}.acceptance.{key}")
    if "machine-labeled" not in str(acceptance["comparison"]):
        _fail(
            "PCC-CPY-WORKLOAD-006",
            f"{path}.acceptance.comparison must require machine-labeled evidence",
        )
    if type(acceptance["max_error_count"]) is not int or acceptance["max_error_count"] != 0:
        _fail(
            "PCC-CPY-WORKLOAD-006",
            f"{path}.acceptance.max_error_count must be zero",
        )
    if acceptance["rss_growth_estimator"] != "theil-sen-after-warmup":
        _fail(
            "PCC-CPY-WORKLOAD-006",
            f"{path}.acceptance.rss_growth_estimator is not the frozen estimator",
        )
    rss_fraction = acceptance["max_rss_growth_fraction_of_peak_per_hour"]
    if (
        isinstance(rss_fraction, bool)
        or not isinstance(rss_fraction, (int, float))
        or not 0.0 <= rss_fraction <= 0.10
    ):
        _fail(
            "PCC-CPY-WORKLOAD-006",
            f"{path}.acceptance.max_rss_growth_fraction_of_peak_per_hour "
            "must be in 0.0..0.10",
        )
    pause_ceiling = acceptance["max_gc_pause_p95_seconds"]
    if (
        isinstance(pause_ceiling, bool)
        or not isinstance(pause_ceiling, (int, float))
        or pause_ceiling <= 0.0
        or pause_ceiling > interval
    ):
        _fail(
            "PCC-CPY-WORKLOAD-006",
            f"{path}.acceptance.max_gc_pause_p95_seconds must be in "
            "0..sample_interval_seconds",
        )
    if acceptance["throughput_policy"] != "report-machine-labeled-no-universal-floor":
        _fail(
            "PCC-CPY-WORKLOAD-006",
            f"{path}.acceptance.throughput_policy is not the frozen policy",
        )
    if acceptance["latency_policy"] != "report-machine-labeled-no-universal-ceiling":
        _fail(
            "PCC-CPY-WORKLOAD-006",
            f"{path}.acceptance.latency_policy is not the frozen policy",
        )


def _validate_unsupported_policy(value: Any, path: str) -> None:
    policy = _mapping(value, path)
    if set(policy) != {"behavior", "diagnostic_prefix", "publication"}:
        _fail(
            "PCC-CPY-WORKLOAD-007",
            f"{path} must define behavior, diagnostic_prefix, publication",
        )
    if policy["behavior"] != "fail-closed-before-artifact-publication":
        _fail(
            "PCC-CPY-WORKLOAD-007",
            f"{path}.behavior must fail closed before artifact publication",
        )
    prefix = _nonempty_string(policy["diagnostic_prefix"], f"{path}.diagnostic_prefix")
    if not prefix.startswith("PCC-CPY-UNSUPPORTED-"):
        _fail(
            "PCC-CPY-WORKLOAD-007",
            f"{path}.diagnostic_prefix must start PCC-CPY-UNSUPPORTED-",
        )
    if policy["publication"] != "forbidden":
        _fail("PCC-CPY-WORKLOAD-007", f"{path}.publication must be forbidden")


def _validate_source(value: Any, path: str) -> None:
    source = _mapping(value, path)
    if set(source) != {"kind", "name", "version", "digest_policy"}:
        _fail(
            "PCC-CPY-WORKLOAD-002",
            f"{path} must define kind, name, version, digest_policy",
        )
    _nonempty_string(source["kind"], f"{path}.kind")
    _nonempty_string(source["name"], f"{path}.name")
    _nonempty_string(source["version"], f"{path}.version")
    if source["digest_policy"] not in {
        "sha256-pinned-release-artifact",
        "git-tree-pinned-before-gate",
        "repository-tree-pinned-before-gate",
        "generated-corpus-canonical-sha256",
    }:
        _fail("PCC-CPY-WORKLOAD-002", f"{path}.digest_policy is not recognized")


def validate_workload_catalog(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Validate *payload* and return it unchanged.

    Validation is intentionally stricter than a JSON schema: it verifies the
    cumulative product meaning, target-specific libc boundary, claim mode,
    lifecycle, performance, and unsupported-surface rules.
    """

    root = _mapping(payload, "catalog")
    _exact_fields(root, _REQUIRED_TOP_LEVEL, "catalog")
    if root["schema"] != SCHEMA:
        _fail("PCC-CPY-WORKLOAD-001", f"expected schema {SCHEMA!r}")

    baseline = _mapping(root["python_baseline"], "python_baseline")
    if set(baseline) != {"language", "oracle"}:
        _fail("PCC-CPY-WORKLOAD-003", "python_baseline must define language and oracle")
    if baseline["language"] != LANGUAGE_BASELINE or baseline["oracle"] != ORACLE_BASELINE:
        _fail(
            "PCC-CPY-WORKLOAD-003",
            f"baseline must be language {LANGUAGE_BASELINE} / {ORACLE_BASELINE}",
        )

    targets = root["targets"]
    if not isinstance(targets, list) or len(targets) != len(TARGETS):
        _fail("PCC-CPY-WORKLOAD-004", "catalog must define both frozen targets")
    target_names: list[str] = []
    for index, raw_target in enumerate(targets):
        target = _mapping(raw_target, f"targets[{index}]")
        if set(target) != {"triple", "runtime_boundary", "status"}:
            _fail(
                "PCC-CPY-WORKLOAD-004",
                f"targets[{index}] must define triple, runtime_boundary, status",
            )
        triple = _nonempty_string(target["triple"], f"targets[{index}].triple")
        target_names.append(triple)
        if target["status"] != STATUS_UNVERIFIED:
            _fail("PCC-CPY-WORKLOAD-005", f"target {triple} must remain unverified")
        boundary = target["runtime_boundary"]
        if triple == "arm64-apple-darwin" and boundary != "named-libSystem-abi":
            _fail(
                "PCC-CPY-WORKLOAD-004",
                "Darwin must use the named-libSystem ABI boundary, never zero-libc",
            )
        if triple == "x86_64-unknown-linux-gnu" and boundary != "static-zero-libc-required":
            _fail(
                "PCC-CPY-WORKLOAD-004",
                "Linux must require the static zero-libc boundary",
            )
    if tuple(target_names) != TARGETS:
        _fail("PCC-CPY-WORKLOAD-004", f"target order must be {TARGETS!r}")

    claim = _mapping(root["claim_contract"], "claim_contract")
    if set(claim) != {
        "required_mode",
        "forbidden_evidence_modes",
        "gc_backends",
        "fixed_point",
        "historical_evidence_satisfies_current_claim",
    }:
        _fail("PCC-CPY-WORKLOAD-008", "claim_contract fields are not frozen")
    if claim["required_mode"] != CLAIM_MODE:
        _fail("PCC-CPY-WORKLOAD-008", f"required_mode must be {CLAIM_MODE}")
    forbidden_modes = _string_list(
        claim["forbidden_evidence_modes"],
        "claim_contract.forbidden_evidence_modes",
    )
    if forbidden_modes != FORBIDDEN_EVIDENCE_MODES:
        _fail("PCC-CPY-WORKLOAD-008", "forbidden evidence modes are incomplete")
    if claim["gc_backends"] != [0, 1, 2, 3, 4]:
        _fail("PCC-CPY-WORKLOAD-008", "all GC0..4 backends are mandatory")
    if claim["fixed_point"] != "current-pcc1-to-pcc2-to-pcc3":
        _fail("PCC-CPY-WORKLOAD-008", "current pcc1->pcc2->pcc3 is mandatory")
    if claim["historical_evidence_satisfies_current_claim"] is not False:
        _fail("PCC-CPY-WORKLOAD-008", "historical evidence cannot satisfy a current claim")

    levels = root["levels"]
    if not isinstance(levels, list) or len(levels) != 3:
        _fail("PCC-CPY-WORKLOAD-009", "catalog must define exactly Levels 1, 2, and 3")
    level_categories: dict[int, set[str]] = {}
    for index, raw_level in enumerate(levels):
        level = _mapping(raw_level, f"levels[{index}]")
        _exact_fields(level, _REQUIRED_LEVEL_FIELDS, f"levels[{index}]")
        number = level["level"]
        if type(number) is not int or number != index + 1:
            _fail("PCC-CPY-WORKLOAD-009", "levels must be ordered 1, 2, 3")
        expected_includes = list(range(1, number + 1))
        if level["cumulative_includes"] != expected_includes:
            _fail(
                "PCC-CPY-WORKLOAD-009",
                f"Level {number} must cumulatively include {expected_includes}",
            )
        categories = set(
            _string_list(level["required_categories"], f"levels[{index}].required_categories")
        )
        if number > 1 and not level_categories[number - 1].issubset(categories):
            _fail(
                "PCC-CPY-WORKLOAD-009",
                f"Level {number} categories must include every Level {number - 1} category",
            )
        level_categories[number] = categories
        _string_list(level["required_gates"], f"levels[{index}].required_gates")
        _validate_performance(level["performance_envelope"], f"levels[{index}].performance_envelope")
        _validate_unsupported_policy(level["unsupported_policy"], f"levels[{index}].unsupported_policy")
        if level["claim_mode"] != CLAIM_MODE:
            _fail("PCC-CPY-WORKLOAD-008", f"Level {number} claim mode is not pcc-native")
        if level["status"] != STATUS_UNVERIFIED:
            _fail("PCC-CPY-WORKLOAD-005", f"Level {number} must remain unverified")
        if level["final_product_goal"] is not (number == 3):
            _fail(
                "PCC-CPY-WORKLOAD-009",
                "only Level 3 must be marked as the final product goal",
            )

    workloads = root["workloads"]
    if not isinstance(workloads, list) or not workloads:
        _fail("PCC-CPY-WORKLOAD-010", "catalog must contain workloads")
    ids: set[str] = set()
    seen_levels: set[int] = set()
    categories_by_level: dict[int, set[str]] = {1: set(), 2: set(), 3: set()}
    for index, raw_workload in enumerate(workloads):
        workload = _mapping(raw_workload, f"workloads[{index}]")
        _exact_fields(workload, _REQUIRED_WORKLOAD_FIELDS, f"workloads[{index}]")
        workload_id = _nonempty_string(workload["id"], f"workloads[{index}].id")
        if workload_id in ids:
            _fail("PCC-CPY-WORKLOAD-010", f"duplicate workload id {workload_id!r}")
        ids.add(workload_id)
        if not workload_id.startswith("cpy-l"):
            _fail("PCC-CPY-WORKLOAD-010", f"workload id {workload_id!r} is not namespaced")
        minimum_level = workload["minimum_level"]
        if type(minimum_level) is not int or minimum_level not in LEVELS:
            _fail("PCC-CPY-WORKLOAD-010", f"{workload_id} minimum_level must be 1..3")
        seen_levels.add(minimum_level)
        category = _nonempty_string(workload["category"], f"workloads[{index}].category")
        if category not in level_categories[minimum_level]:
            _fail(
                "PCC-CPY-WORKLOAD-010",
                f"{workload_id} category {category!r} is not required at Level {minimum_level}",
            )
        categories_by_level[minimum_level].add(category)
        _nonempty_string(workload["title"], f"workloads[{index}].title")
        _validate_source(workload["source"], f"workloads[{index}].source")
        if tuple(_string_list(workload["targets"], f"workloads[{index}].targets")) != TARGETS:
            _fail("PCC-CPY-WORKLOAD-004", f"{workload_id} must cover both frozen targets")
        lifecycle = set(_string_list(workload["lifecycle"], f"workloads[{index}].lifecycle"))
        if lifecycle != _REQUIRED_LIFECYCLE:
            _fail("PCC-CPY-WORKLOAD-011", f"{workload_id} lifecycle is incomplete")
        oracle = _mapping(workload["oracle"], f"workloads[{index}].oracle")
        if set(oracle) != {"implementation", "role", "execution_is_separate"}:
            _fail("PCC-CPY-WORKLOAD-012", f"{workload_id} oracle fields are incomplete")
        if oracle["implementation"] != ORACLE_BASELINE:
            _fail("PCC-CPY-WORKLOAD-012", f"{workload_id} oracle must be {ORACLE_BASELINE}")
        if oracle["role"] != "behavioral-reference-only" or oracle["execution_is_separate"] is not True:
            _fail(
                "PCC-CPY-WORKLOAD-012",
                f"{workload_id} must keep CPython outside the pcc execution",
            )
        _string_list(workload["required_gates"], f"workloads[{index}].required_gates")
        _validate_performance(
            workload["performance_envelope"],
            f"workloads[{index}].performance_envelope",
        )
        _validate_unsupported_policy(
            workload["unsupported_policy"],
            f"workloads[{index}].unsupported_policy",
        )
        if workload["claim_mode"] != CLAIM_MODE:
            _fail("PCC-CPY-WORKLOAD-008", f"{workload_id} claim mode is not pcc-native")
        if workload["status"] != STATUS_UNVERIFIED:
            _fail("PCC-CPY-WORKLOAD-005", f"{workload_id} must remain unverified")

    if seen_levels != set(LEVELS):
        _fail("PCC-CPY-WORKLOAD-010", "every product level needs at least one workload")
    for number in LEVELS:
        promised_categories = level_categories[number]
        available_categories = set().union(
            *(categories_by_level[level] for level in range(1, number + 1))
        )
        missing = sorted(promised_categories - available_categories)
        if missing:
            _fail(
                "PCC-CPY-WORKLOAD-010",
                f"Level {number} has no workload for categories: {', '.join(missing)}",
            )
    return payload


def load_workload_catalog(path: str | Path | None = None) -> Mapping[str, Any]:
    """Load and validate the repository catalogue or an explicit JSON file."""

    catalog_path = Path(path) if path is not None else DEFAULT_CATALOG_PATH
    try:
        with catalog_path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        _fail("PCC-CPY-WORKLOAD-001", f"cannot load {catalog_path}: {exc}")
    return validate_workload_catalog(payload)


def workloads_for_level(
    payload: Mapping[str, Any], level: int
) -> tuple[Mapping[str, Any], ...]:
    """Return the cumulative ordered workload set required by *level*."""

    validate_workload_catalog(payload)
    if level not in LEVELS:
        _fail("PCC-CPY-WORKLOAD-009", f"unknown replacement level {level!r}")
    return tuple(
        workload
        for workload in payload["workloads"]
        if workload["minimum_level"] <= level
    )


def catalog_digest(payload: Mapping[str, Any]) -> str:
    """Return the canonical SHA-256 identity used by evidence manifests."""

    validate_workload_catalog(payload)
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


__all__ = [
    "CLAIM_MODE",
    "DEFAULT_CATALOG_PATH",
    "FORBIDDEN_EVIDENCE_MODES",
    "LANGUAGE_BASELINE",
    "LEVELS",
    "ORACLE_BASELINE",
    "SCHEMA",
    "STATUS_UNVERIFIED",
    "TARGETS",
    "WorkloadCatalogError",
    "catalog_digest",
    "load_workload_catalog",
    "validate_workload_catalog",
    "workloads_for_level",
]
