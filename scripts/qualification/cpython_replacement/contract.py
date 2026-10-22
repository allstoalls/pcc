"""Machine-readable product contract for replacing CPython with ``pcc1``.

This module intentionally validates a small, versioned JSON format instead of
using a permissive schema library.  A replacement claim is a release claim, so
unknown fields, duplicate identifiers, an unfrozen oracle, a weakened target
boundary, or a relabelled execution owner must fail closed.

``supported`` in the matrix means "inside the release obligation".  It is not
evidence that the current source implements the surface: that is recorded
separately by ``implementation_status`` and by a release evidence manifest.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


CONTRACT_SCHEMA_VERSION = "pcc.cpython-replacement.contract.v1"
CONTRACT_FILENAME = "pcc1-cpython-replacement-matrix.json"

_ROOT_FIELDS = {
    "schema_version",
    "product",
    "baseline",
    "levels",
    "targets",
    "evidence_policy",
    "unsupported_policy",
    "surfaces",
}
_PRODUCT_FIELDS = {
    "name",
    "execution_root",
    "claim_mode",
    "final_level",
    "current_implementation_status",
    "workload_catalog",
    "evidence_schema",
}
_BASELINE_FIELDS = {
    "language_version",
    "oracle_implementation",
    "oracle_version",
    "oracle_build",
    "version_policy",
}
_LEVEL_FIELDS = {"level", "id", "inherits", "final_goal", "description"}
_TARGET_FIELDS = {
    "id",
    "target_triple",
    "minimum_level",
    "runtime_boundary",
    "zero_libc",
    "gate",
    "verdict",
    "implementation_status",
    "diagnostic",
}
_EVIDENCE_FIELDS = {
    "claimable_profile",
    "required_modes",
    "required_gc_backends",
    "required_fixed_point_stages",
    "required_clean_source_levels",
    "required_targets",
    "forbidden_execution_owners",
    "forbidden_linkage",
    "required_provenance",
}
_MODE_FIELDS = {
    "execution_root",
    "execution_mode",
    "backend",
    "python_libpython",
    "runtime_owner",
    "fallback",
}
_UNSUPPORTED_POLICY_FIELDS = {
    "unknown_surface",
    "publication_behavior",
    "execution_behavior",
    "diagnostic_prefix",
    "host_assistance_counts_as_replacement",
    "cpython_compat_counts_as_replacement",
    "supported_verdict_is_current_proof",
}
_SURFACE_FIELDS = {
    "id",
    "category",
    "title",
    "owner",
    "oracle",
    "minimum_level",
    "gate",
    "verdict",
    "implementation_status",
    "diagnostic",
}

_CATEGORIES = {
    "language",
    "stdlib",
    "cli",
    "import",
    "package",
    "tooling",
    "runtime",
    "platform",
}
_VERDICTS = {"supported", "unsupported"}
_IMPLEMENTATION_STATUSES = {"unverified", "proven", "excluded"}
_EXPECTED_TARGETS = {
    "darwin-arm64": {
        "target_triple": "arm64-apple-darwin",
        "runtime_boundary": "named-libSystem",
        "zero_libc": False,
    },
    "linux-x86_64": {
        "target_triple": "x86_64-unknown-linux-gnu",
        "runtime_boundary": "static-zero-libc",
        "zero_libc": True,
    },
}
_EXPECTED_MODES = {
    "execution_root": "pcc1",
    "execution_mode": "pcc-native",
    "backend": "self",
    "python_libpython": "off",
    "runtime_owner": "pcc-python",
    "fallback": "forbidden",
}
_EXPECTED_LEVEL_IDS = [
    "pcc-native-pure-python-service",
    "scientific-build-ecosystem",
    "python3-drop-in",
]
_EXPECTED_FORBIDDEN_OWNERS = [
    "host-python",
    "host-pcc",
    "cpython",
    "libpython",
]
_EXPECTED_FORBIDDEN_LINKAGE = [
    "libpython",
    "llvm-runtime-fallback",
    "cpython-extension-abi",
]
_EXPECTED_PROVENANCE = [
    "source",
    "compiler-artifacts",
    "application-artifacts",
    "process-tree",
    "binary-linkage",
    "runtime-archive",
    "gc",
    "packages",
    "oracle",
    "performance",
]


class ReplacementContractError(ValueError):
    """A stable, fail-closed replacement-contract diagnostic."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code + ": " + message)
        self.code = code


def _fail(code: str, path: str, message: str) -> None:
    raise ReplacementContractError(code, path + ": " + message)


def _require_object(value: object, path: str) -> dict[str, object]:
    if not isinstance(value, dict):
        _fail(
            "PCC-CPY-CONTRACT-TYPE",
            path,
            "expected object, got " + type(value).__name__,
        )
    return value


def _require_list(value: object, path: str) -> list[object]:
    if not isinstance(value, list):
        _fail(
            "PCC-CPY-CONTRACT-TYPE",
            path,
            "expected array, got " + type(value).__name__,
        )
    return value


def _require_string(value: object, path: str) -> str:
    if not isinstance(value, str) or not value:
        _fail("PCC-CPY-CONTRACT-TYPE", path, "expected non-empty string")
    return value


def _require_integer(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail("PCC-CPY-CONTRACT-TYPE", path, "expected integer")
    return value


def _require_boolean(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        _fail("PCC-CPY-CONTRACT-TYPE", path, "expected boolean")
    return value


def _require_fields(
    value: object,
    expected: set[str],
    path: str,
) -> dict[str, object]:
    row = _require_object(value, path)
    actual = set(row)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing or unknown:
        parts = []
        if missing:
            parts.append("missing " + ", ".join(missing))
        if unknown:
            parts.append("unknown " + ", ".join(unknown))
        _fail("PCC-CPY-CONTRACT-FIELDS", path, "; ".join(parts))
    return row


def _require_exact(value: object, expected: object, path: str, code: str) -> None:
    if value != expected:
        _fail(code, path, "expected " + repr(expected) + ", got " + repr(value))


def _require_unique_strings(values: object, path: str) -> list[str]:
    rows = _require_list(values, path)
    out = []
    seen = set()
    for index, value in enumerate(rows):
        item = _require_string(value, path + "[" + str(index) + "]")
        if item in seen:
            _fail(
                "PCC-CPY-CONTRACT-DUPLICATE",
                path + "[" + str(index) + "]",
                "duplicate value " + repr(item),
            )
        seen.add(item)
        out.append(item)
    return out


def _require_exact_integers(
    values: object,
    expected: list[int],
    path: str,
    code: str,
) -> None:
    rows = _require_list(values, path)
    normalized = []
    for index, value in enumerate(rows):
        normalized.append(_require_integer(value, path + "[" + str(index) + "]"))
    _require_exact(normalized, expected, path, code)


def _validate_product(value: object) -> None:
    product = _require_fields(value, _PRODUCT_FIELDS, "$.product")
    _require_exact(
        product["name"],
        "pcc1-cpython-replacement",
        "$.product.name",
        "PCC-CPY-CONTRACT-PRODUCT",
    )
    _require_exact(
        product["execution_root"],
        "pcc1",
        "$.product.execution_root",
        "PCC-CPY-CONTRACT-PRODUCT",
    )
    _require_exact(
        product["claim_mode"],
        "pcc-native",
        "$.product.claim_mode",
        "PCC-CPY-CONTRACT-PRODUCT",
    )
    _require_exact(
        _require_integer(product["final_level"], "$.product.final_level"),
        3,
        "$.product.final_level",
        "PCC-CPY-CONTRACT-LEVELS",
    )
    status = _require_string(
        product["current_implementation_status"],
        "$.product.current_implementation_status",
    )
    if status not in {"unverified", "proven"}:
        _fail(
            "PCC-CPY-CONTRACT-PRODUCT",
            "$.product.current_implementation_status",
            "expected unverified or proven",
        )
    _require_string(product["workload_catalog"], "$.product.workload_catalog")
    _require_exact(
        product["evidence_schema"],
        "pcc.cpython-replacement.evidence.v1",
        "$.product.evidence_schema",
        "PCC-CPY-CONTRACT-EVIDENCE",
    )


def _validate_baseline(value: object) -> None:
    baseline = _require_fields(value, _BASELINE_FIELDS, "$.baseline")
    expected = {
        "language_version": "3.13",
        "oracle_implementation": "CPython",
        "oracle_version": "3.15.0",
        "oracle_build": "standard-gil",
        "version_policy": "exact-oracle-version",
    }
    for field, frozen in expected.items():
        _require_exact(
            baseline[field],
            frozen,
            "$.baseline." + field,
            "PCC-CPY-CONTRACT-BASELINE",
        )


def _validate_levels(value: object) -> None:
    levels = _require_list(value, "$.levels")
    if len(levels) != 3:
        _fail(
            "PCC-CPY-CONTRACT-LEVELS",
            "$.levels",
            "expected exactly three cumulative product levels",
        )
    ids = set()
    for index, value in enumerate(levels):
        path = "$.levels[" + str(index) + "]"
        row = _require_fields(value, _LEVEL_FIELDS, path)
        level = _require_integer(row["level"], path + ".level")
        expected_level = index + 1
        if level != expected_level:
            _fail(
                "PCC-CPY-CONTRACT-LEVELS",
                path + ".level",
                "levels must be ordered 1, 2, 3",
            )
        identifier = _require_string(row["id"], path + ".id")
        if identifier in ids:
            _fail(
                "PCC-CPY-CONTRACT-DUPLICATE",
                path + ".id",
                "duplicate level id " + repr(identifier),
            )
        ids.add(identifier)
        _require_exact(
            identifier,
            _EXPECTED_LEVEL_IDS[index],
            path + ".id",
            "PCC-CPY-CONTRACT-LEVELS",
        )
        inherits = _require_list(row["inherits"], path + ".inherits")
        for inherit_index, inherited_level in enumerate(inherits):
            _require_integer(
                inherited_level,
                path + ".inherits[" + str(inherit_index) + "]",
            )
        expected_inherits = list(range(1, level))
        if inherits != expected_inherits:
            _fail(
                "PCC-CPY-CONTRACT-LEVELS",
                path + ".inherits",
                "level must inherit every prior level in order",
            )
        final_goal = _require_boolean(row["final_goal"], path + ".final_goal")
        if final_goal != (level == 3):
            _fail(
                "PCC-CPY-CONTRACT-LEVELS",
                path + ".final_goal",
                "only Level 3 is the final CPython-replacement goal",
            )
        _require_string(row["description"], path + ".description")


def _validate_targets(value: object) -> None:
    targets = _require_list(value, "$.targets")
    if len(targets) != len(_EXPECTED_TARGETS):
        _fail(
            "PCC-CPY-CONTRACT-TARGETS",
            "$.targets",
            "expected the frozen Darwin arm64 and Linux x86_64 targets",
        )
    seen = set()
    for index, value in enumerate(targets):
        path = "$.targets[" + str(index) + "]"
        row = _require_fields(value, _TARGET_FIELDS, path)
        identifier = _require_string(row["id"], path + ".id")
        if identifier in seen:
            _fail(
                "PCC-CPY-CONTRACT-DUPLICATE",
                path + ".id",
                "duplicate target id " + repr(identifier),
            )
        seen.add(identifier)
        frozen = _EXPECTED_TARGETS.get(identifier)
        if frozen is None:
            _fail(
                "PCC-CPY-CONTRACT-TARGETS",
                path + ".id",
                "target is outside the frozen matrix",
            )
        for field, expected in frozen.items():
            if field == "zero_libc":
                _require_boolean(row[field], path + "." + field)
            _require_exact(
                row[field],
                expected,
                path + "." + field,
                "PCC-CPY-CONTRACT-TARGETS",
            )
        _require_exact(
            _require_integer(row["minimum_level"], path + ".minimum_level"),
            1,
            path + ".minimum_level",
            "PCC-CPY-CONTRACT-TARGETS",
        )
        _require_exact(
            row["verdict"],
            "supported",
            path + ".verdict",
            "PCC-CPY-CONTRACT-TARGETS",
        )
        status = _require_string(row["implementation_status"], path + ".implementation_status")
        if status not in {"unverified", "proven"}:
            _fail(
                "PCC-CPY-CONTRACT-TARGETS",
                path + ".implementation_status",
                "supported target must be unverified or proven",
            )
        _require_string(row["gate"], path + ".gate")
        diagnostic = _require_string(row["diagnostic"], path + ".diagnostic")
        if not diagnostic.startswith("PCC-CPY-TARGET-"):
            _fail(
                "PCC-CPY-CONTRACT-DIAGNOSTIC",
                path + ".diagnostic",
                "target diagnostic must start with PCC-CPY-TARGET-",
            )
    if seen != set(_EXPECTED_TARGETS):
        _fail(
            "PCC-CPY-CONTRACT-TARGETS",
            "$.targets",
            "target ids do not match the frozen matrix",
        )


def _validate_evidence_policy(value: object) -> None:
    policy = _require_fields(value, _EVIDENCE_FIELDS, "$.evidence_policy")
    _require_exact(
        policy["claimable_profile"],
        "replacement-release",
        "$.evidence_policy.claimable_profile",
        "PCC-CPY-CONTRACT-EVIDENCE",
    )
    modes = _require_fields(
        policy["required_modes"],
        _MODE_FIELDS,
        "$.evidence_policy.required_modes",
    )
    for field, expected in _EXPECTED_MODES.items():
        _require_exact(
            modes[field],
            expected,
            "$.evidence_policy.required_modes." + field,
            "PCC-CPY-CONTRACT-EVIDENCE",
        )
    _require_exact_integers(
        policy["required_gc_backends"],
        [0, 1, 2, 3, 4],
        "$.evidence_policy.required_gc_backends",
        "PCC-CPY-CONTRACT-EVIDENCE",
    )
    _require_exact(
        _require_unique_strings(
            policy["required_fixed_point_stages"],
            "$.evidence_policy.required_fixed_point_stages",
        ),
        ["pcc1", "pcc2", "pcc3"],
        "$.evidence_policy.required_fixed_point_stages",
        "PCC-CPY-CONTRACT-EVIDENCE",
    )
    _require_exact_integers(
        policy["required_clean_source_levels"],
        [1, 2, 3],
        "$.evidence_policy.required_clean_source_levels",
        "PCC-CPY-CONTRACT-EVIDENCE",
    )
    _require_exact(
        _require_unique_strings(
            policy["required_targets"],
            "$.evidence_policy.required_targets",
        ),
        ["arm64-apple-darwin", "x86_64-unknown-linux-gnu"],
        "$.evidence_policy.required_targets",
        "PCC-CPY-CONTRACT-EVIDENCE",
    )
    _require_exact(
        _require_unique_strings(
            policy["forbidden_execution_owners"],
            "$.evidence_policy.forbidden_execution_owners",
        ),
        _EXPECTED_FORBIDDEN_OWNERS,
        "$.evidence_policy.forbidden_execution_owners",
        "PCC-CPY-CONTRACT-EVIDENCE",
    )
    _require_exact(
        _require_unique_strings(
            policy["forbidden_linkage"],
            "$.evidence_policy.forbidden_linkage",
        ),
        _EXPECTED_FORBIDDEN_LINKAGE,
        "$.evidence_policy.forbidden_linkage",
        "PCC-CPY-CONTRACT-EVIDENCE",
    )
    _require_exact(
        _require_unique_strings(
            policy["required_provenance"],
            "$.evidence_policy.required_provenance",
        ),
        _EXPECTED_PROVENANCE,
        "$.evidence_policy.required_provenance",
        "PCC-CPY-CONTRACT-EVIDENCE",
    )


def _validate_unsupported_policy(value: object) -> None:
    policy = _require_fields(
        value,
        _UNSUPPORTED_POLICY_FIELDS,
        "$.unsupported_policy",
    )
    expected = {
        "unknown_surface": "reject",
        "publication_behavior": "fail-closed",
        "execution_behavior": "stable-diagnostic-before-fallback",
        "diagnostic_prefix": "PCC-CPY-UNSUPPORTED-",
        "host_assistance_counts_as_replacement": False,
        "cpython_compat_counts_as_replacement": False,
        "supported_verdict_is_current_proof": False,
    }
    for field, frozen in expected.items():
        if isinstance(frozen, bool):
            _require_boolean(policy[field], "$.unsupported_policy." + field)
        _require_exact(
            policy[field],
            frozen,
            "$.unsupported_policy." + field,
            "PCC-CPY-CONTRACT-UNSUPPORTED-POLICY",
        )


def _validate_surfaces(value: object) -> None:
    surfaces = _require_list(value, "$.surfaces")
    if not surfaces:
        _fail(
            "PCC-CPY-CONTRACT-SURFACE",
            "$.surfaces",
            "at least one surface is required",
        )
    ids = set()
    diagnostics = set()
    categories = set()
    supported_levels = set()
    unsupported_count = 0
    for index, value in enumerate(surfaces):
        path = "$.surfaces[" + str(index) + "]"
        row = _require_fields(value, _SURFACE_FIELDS, path)
        identifier = _require_string(row["id"], path + ".id")
        if identifier in ids:
            _fail(
                "PCC-CPY-CONTRACT-DUPLICATE",
                path + ".id",
                "duplicate surface id " + repr(identifier),
            )
        ids.add(identifier)
        category = _require_string(row["category"], path + ".category")
        if category not in _CATEGORIES:
            _fail(
                "PCC-CPY-CONTRACT-SURFACE",
                path + ".category",
                "unknown category " + repr(category),
            )
        if not identifier.startswith(category + "."):
            _fail(
                "PCC-CPY-CONTRACT-SURFACE",
                path + ".id",
                "surface id must start with its category and a dot",
            )
        categories.add(category)
        _require_string(row["title"], path + ".title")
        _require_string(row["owner"], path + ".owner")
        _require_string(row["oracle"], path + ".oracle")
        level = _require_integer(row["minimum_level"], path + ".minimum_level")
        if level not in (1, 2, 3):
            _fail(
                "PCC-CPY-CONTRACT-SURFACE",
                path + ".minimum_level",
                "expected Level 1, 2 or 3",
            )
        _require_string(row["gate"], path + ".gate")
        verdict = _require_string(row["verdict"], path + ".verdict")
        if verdict not in _VERDICTS:
            _fail(
                "PCC-CPY-CONTRACT-SURFACE",
                path + ".verdict",
                "expected supported or unsupported",
            )
        status = _require_string(
            row["implementation_status"], path + ".implementation_status"
        )
        if status not in _IMPLEMENTATION_STATUSES:
            _fail(
                "PCC-CPY-CONTRACT-SURFACE",
                path + ".implementation_status",
                "unknown implementation status " + repr(status),
            )
        if verdict == "unsupported":
            unsupported_count += 1
            if status != "excluded":
                _fail(
                    "PCC-CPY-CONTRACT-SURFACE",
                    path + ".implementation_status",
                    "unsupported surface must be explicitly excluded",
                )
        else:
            supported_levels.add(level)
            if status == "excluded":
                _fail(
                    "PCC-CPY-CONTRACT-SURFACE",
                    path + ".implementation_status",
                    "supported surface cannot be excluded",
                )
        diagnostic = _require_string(row["diagnostic"], path + ".diagnostic")
        if not diagnostic.startswith("PCC-CPY-"):
            _fail(
                "PCC-CPY-CONTRACT-DIAGNOSTIC",
                path + ".diagnostic",
                "diagnostic must start with PCC-CPY-",
            )
        if verdict == "unsupported" and not diagnostic.startswith(
            "PCC-CPY-UNSUPPORTED-"
        ):
            _fail(
                "PCC-CPY-CONTRACT-DIAGNOSTIC",
                path + ".diagnostic",
                "unsupported surface needs the frozen unsupported prefix",
            )
        if diagnostic in diagnostics:
            _fail(
                "PCC-CPY-CONTRACT-DUPLICATE",
                path + ".diagnostic",
                "duplicate diagnostic " + repr(diagnostic),
            )
        diagnostics.add(diagnostic)
    if categories != _CATEGORIES:
        missing = sorted(_CATEGORIES - categories)
        _fail(
            "PCC-CPY-CONTRACT-SURFACE",
            "$.surfaces",
            "missing categories " + ", ".join(missing),
        )
    if supported_levels != {1, 2, 3}:
        _fail(
            "PCC-CPY-CONTRACT-LEVELS",
            "$.surfaces",
            "each product level needs at least one supported obligation",
        )
    if unsupported_count == 0:
        _fail(
            "PCC-CPY-CONTRACT-UNSUPPORTED-POLICY",
            "$.surfaces",
            "the frozen matrix must name explicit unsupported surfaces",
        )


def validate_contract(payload: object) -> dict[str, object]:
    """Validate and return a CPython-replacement contract.

    Validation is deliberately strict: adding a field or weakening a frozen
    mode requires a schema-versioned contract change rather than being ignored.
    """

    contract = _require_fields(payload, _ROOT_FIELDS, "$")
    _require_exact(
        contract["schema_version"],
        CONTRACT_SCHEMA_VERSION,
        "$.schema_version",
        "PCC-CPY-CONTRACT-SCHEMA",
    )
    _validate_product(contract["product"])
    _validate_baseline(contract["baseline"])
    _validate_levels(contract["levels"])
    _validate_targets(contract["targets"])
    _validate_evidence_policy(contract["evidence_policy"])
    _validate_unsupported_policy(contract["unsupported_policy"])
    _validate_surfaces(contract["surfaces"])
    return contract


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReplacementContractError(
                "PCC-CPY-CONTRACT-DUPLICATE-JSON-KEY",
                "duplicate JSON object key " + repr(key),
            )
        result[key] = value
    return result


def default_contract_path() -> Path:
    """Return the source-tree location of the authoritative v1 matrix."""

    return Path(__file__).resolve().parents[3] / "docs" / "compat" / CONTRACT_FILENAME


def load_contract(path: str | Path | None = None) -> dict[str, object]:
    """Read and strictly validate a replacement contract JSON file."""

    contract_path = Path(path) if path is not None else default_contract_path()
    try:
        text = contract_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReplacementContractError(
            "PCC-CPY-CONTRACT-IO",
            "cannot read " + str(contract_path) + ": " + str(exc),
        ) from exc
    try:
        payload = json.loads(text, object_pairs_hook=_unique_object)
    except ReplacementContractError:
        raise
    except (TypeError, ValueError) as exc:
        raise ReplacementContractError(
            "PCC-CPY-CONTRACT-JSON",
            "invalid JSON in " + str(contract_path) + ": " + str(exc),
        ) from exc
    return validate_contract(payload)


def canonical_contract_json(payload: object) -> str:
    """Return the validated contract's deterministic UTF-8 JSON spelling."""

    contract = validate_contract(payload)
    return json.dumps(
        contract,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def contract_digest(payload: object) -> str:
    """Return the SHA-256 identity of the validated canonical contract."""

    canonical = canonical_contract_json(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def surfaces_for_level(
    payload: object,
    level: int,
    include_unsupported: bool = True,
) -> tuple[dict[str, object], ...]:
    """Return the cumulative surface obligations visible at ``level``."""

    contract = validate_contract(payload)
    if isinstance(level, bool) or level not in (1, 2, 3):
        _fail(
            "PCC-CPY-CONTRACT-LEVELS",
            "level",
            "expected Level 1, 2 or 3",
        )
    rows = []
    for row in contract["surfaces"]:
        if row["minimum_level"] <= level:
            if include_unsupported or row["verdict"] == "supported":
                rows.append(row)
    return tuple(rows)


__all__ = [
    "CONTRACT_FILENAME",
    "CONTRACT_SCHEMA_VERSION",
    "ReplacementContractError",
    "canonical_contract_json",
    "contract_digest",
    "default_contract_path",
    "load_contract",
    "surfaces_for_level",
    "validate_contract",
]
