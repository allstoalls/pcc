"""Strict release-evidence bundle for a pcc1 CPython-replacement claim."""

from __future__ import annotations

import hashlib
import json

from .contract import (
    contract_digest,
    load_contract,
    surfaces_for_level,
    validate_contract,
)
from .workloads import (
    catalog_digest,
    load_workload_catalog,
    validate_workload_catalog,
    workloads_for_level,
)


EVIDENCE_SCHEMA_VERSION = "pcc.cpython-replacement.evidence.v1"

_ROOT_FIELDS = {
    "schema_version",
    "contract_digest",
    "workload_catalog_digest",
    "claim",
    "source",
    "compiler_artifacts",
    "application_artifacts",
    "runs",
    "package_artifacts",
    "oracle",
}
_CLAIM_FIELDS = {
    "profile", "level", "surface_ids", "workload_ids", "targets",
    "gc_backends", "status",
}
_SOURCE_FIELDS = {"repository", "revision", "tree_sha256", "clean"}
_COMPILER_FIELDS = {
    "source_tree_sha256", "stages", "normalized_pcc2_pcc3_sha256",
    "fixed_point",
}
_STAGE_FIELDS = {"stage", "sha256", "normalized_sha256"}
_APPLICATION_FIELDS = {"id", "sha256", "source_tree_sha256"}
_RUN_FIELDS = {
    "workload_id", "target_triple", "gc_backend", "modes",
    "application_sha256",
    "process_tree", "binary_linkage", "runtime_archive",
    "package_environment_sha256", "performance_result_sha256", "status",
}
_PROCESS_FIELDS = {
    "sha256", "execution_owners", "forbidden_owners_observed",
}
_LINKAGE_FIELDS = {
    "sha256", "needed_libraries", "undefined_symbols", "static",
    "zero_libc", "libpython", "llvm_runtime_fallback",
    "cpython_extension_abi",
}
_RUNTIME_FIELDS = {
    "archive_sha256", "manifest_sha256", "inventory_sha256", "source_kind",
    "producer_kind", "uses_host_cc",
}
_PACKAGE_FIELDS = {"name", "version", "sha256", "abi_mode", "build_owner"}
_ORACLE_FIELDS = {
    "implementation", "version", "build", "execution_is_separate",
    "result_sha256",
}
_ALLOWED_PROCESS_OWNERS = {
    "pcc1", "pcc-application", "pcc-owned-tool", "os",
}
_HEX = set("0123456789abcdef")


class ReplacementEvidenceError(ValueError):
    """A release bundle violates the frozen replacement evidence schema."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(code + ": " + detail)


def _fail(code: str, path: str, detail: str) -> None:
    raise ReplacementEvidenceError(code, path + ": " + detail)


def _object(value: object, fields: set[str], path: str) -> dict[str, object]:
    if not isinstance(value, dict):
        _fail("PCC-CPY-EVIDENCE-TYPE", path, "expected object")
    actual = set(value)
    missing = sorted(fields - actual)
    extra = sorted(actual - fields)
    if missing or extra:
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if extra:
            detail.append("unknown " + ", ".join(extra))
        _fail("PCC-CPY-EVIDENCE-FIELDS", path, "; ".join(detail))
    return value


def _array(value: object, path: str, *, nonempty: bool = False) -> list[object]:
    if not isinstance(value, list) or (nonempty and not value):
        _fail("PCC-CPY-EVIDENCE-TYPE", path, "expected array")
    return value


def _string(value: object, path: str) -> str:
    if not isinstance(value, str) or not value:
        _fail("PCC-CPY-EVIDENCE-TYPE", path, "expected non-empty string")
    return value


def _boolean(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        _fail("PCC-CPY-EVIDENCE-TYPE", path, "expected boolean")
    return value


def _integer(value: object, path: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        _fail("PCC-CPY-EVIDENCE-TYPE", path, "expected integer")
    return value


def _sha256(value: object, path: str) -> str:
    digest = _string(value, path)
    if len(digest) != 64 or any(character not in _HEX for character in digest):
        _fail("PCC-CPY-EVIDENCE-IDENTITY", path, "expected lowercase SHA-256")
    return digest


def _strings(value: object, path: str) -> list[str]:
    rows = [_string(item, path + "[]") for item in _array(value, path)]
    if len(rows) != len(set(rows)):
        _fail("PCC-CPY-EVIDENCE-IDENTITY", path, "duplicate value")
    return rows


def _integers(value: object, path: str) -> list[int]:
    rows = [_integer(item, path + "[]") for item in _array(value, path)]
    if len(rows) != len(set(rows)):
        _fail("PCC-CPY-EVIDENCE-IDENTITY", path, "duplicate value")
    return rows


def validate_evidence_bundle(
    payload: object,
    contract: object | None = None,
    catalog: object | None = None,
) -> dict[str, object]:
    """Validate one complete target×GC replacement-release evidence bundle."""

    frozen_contract = validate_contract(
        load_contract() if contract is None else contract
    )
    frozen_catalog = validate_workload_catalog(
        load_workload_catalog() if catalog is None else catalog
    )
    bundle = _object(payload, _ROOT_FIELDS, "$")
    if bundle["schema_version"] != EVIDENCE_SCHEMA_VERSION:
        _fail("PCC-CPY-EVIDENCE-SCHEMA", "$.schema_version", "unknown schema")
    if _sha256(bundle["contract_digest"], "$.contract_digest") != contract_digest(
        frozen_contract
    ):
        _fail("PCC-CPY-EVIDENCE-IDENTITY", "$.contract_digest", "contract drift")
    if _sha256(
        bundle["workload_catalog_digest"], "$.workload_catalog_digest"
    ) != catalog_digest(frozen_catalog):
        _fail(
            "PCC-CPY-EVIDENCE-IDENTITY",
            "$.workload_catalog_digest",
            "workload catalog drift",
        )

    claim = _object(bundle["claim"], _CLAIM_FIELDS, "$.claim")
    if claim["profile"] != frozen_contract["evidence_policy"]["claimable_profile"]:
        _fail("PCC-CPY-EVIDENCE-CLAIM", "$.claim.profile", "wrong profile")
    level = _integer(claim["level"], "$.claim.level")
    if level not in (1, 2, 3):
        _fail("PCC-CPY-EVIDENCE-CLAIM", "$.claim.level", "expected 1, 2 or 3")
    if claim["status"] != "passed":
        _fail("PCC-CPY-EVIDENCE-CLAIM", "$.claim.status", "claim did not pass")
    expected_surfaces = [
        row["id"] for row in surfaces_for_level(
            frozen_contract, level, include_unsupported=False,
        )
    ]
    if _strings(claim["surface_ids"], "$.claim.surface_ids") != expected_surfaces:
        _fail(
            "PCC-CPY-EVIDENCE-CLAIM",
            "$.claim.surface_ids",
            "surface coverage is not exact and cumulative",
        )
    expected_workloads = [
        row["id"] for row in workloads_for_level(frozen_catalog, level)
    ]
    if _strings(claim["workload_ids"], "$.claim.workload_ids") != expected_workloads:
        _fail(
            "PCC-CPY-EVIDENCE-CLAIM",
            "$.claim.workload_ids",
            "workload coverage is not exact and cumulative",
        )
    expected_targets = list(frozen_contract["evidence_policy"]["required_targets"])
    if _strings(claim["targets"], "$.claim.targets") != expected_targets:
        _fail("PCC-CPY-EVIDENCE-CLAIM", "$.claim.targets", "target coverage drift")
    expected_backends = list(
        frozen_contract["evidence_policy"]["required_gc_backends"]
    )
    if _integers(claim["gc_backends"], "$.claim.gc_backends") != expected_backends:
        _fail("PCC-CPY-EVIDENCE-CLAIM", "$.claim.gc_backends", "GC coverage drift")

    source = _object(bundle["source"], _SOURCE_FIELDS, "$.source")
    _string(source["repository"], "$.source.repository")
    _string(source["revision"], "$.source.revision")
    source_digest = _sha256(source["tree_sha256"], "$.source.tree_sha256")
    if _boolean(source["clean"], "$.source.clean") is not True:
        _fail("PCC-CPY-EVIDENCE-SOURCE", "$.source.clean", "dirty source")

    compiler = _object(
        bundle["compiler_artifacts"], _COMPILER_FIELDS, "$.compiler_artifacts"
    )
    if _sha256(
        compiler["source_tree_sha256"], "$.compiler_artifacts.source_tree_sha256"
    ) != source_digest:
        _fail(
            "PCC-CPY-EVIDENCE-IDENTITY",
            "$.compiler_artifacts.source_tree_sha256",
            "compiler source does not match claim source",
        )
    stages = _array(compiler["stages"], "$.compiler_artifacts.stages", nonempty=True)
    stage_names = []
    raw_stage_digests: dict[str, str] = {}
    normalized_stage_digests: dict[str, str] = {}
    for index, value in enumerate(stages):
        path = "$.compiler_artifacts.stages[" + str(index) + "]"
        stage = _object(value, _STAGE_FIELDS, path)
        stage_name = _string(stage["stage"], path + ".stage")
        stage_names.append(stage_name)
        raw_stage_digests[stage_name] = _sha256(stage["sha256"], path + ".sha256")
        normalized_stage_digests[stage_name] = _sha256(
            stage["normalized_sha256"], path + ".normalized_sha256"
        )
    expected_stage_names = ["pcc0"] + list(
        frozen_contract["evidence_policy"]["required_fixed_point_stages"]
    )
    if stage_names != expected_stage_names:
        _fail("PCC-CPY-EVIDENCE-FIXED-POINT", "$.compiler_artifacts.stages", "stage drift")
    if raw_stage_digests.get("pcc2") != raw_stage_digests.get("pcc3"):
        _fail("PCC-CPY-EVIDENCE-FIXED-POINT", "$.compiler_artifacts.stages", "pcc2/pcc3 raw identities do not match")
    fixed_point_digest = _sha256(
        compiler["normalized_pcc2_pcc3_sha256"],
        "$.compiler_artifacts.normalized_pcc2_pcc3_sha256",
    )
    if (
        normalized_stage_digests.get("pcc2") != fixed_point_digest
        or normalized_stage_digests.get("pcc3") != fixed_point_digest
    ):
        _fail(
            "PCC-CPY-EVIDENCE-FIXED-POINT",
            "$.compiler_artifacts.stages",
            "pcc2/pcc3 normalized identities do not match",
        )
    if _boolean(compiler["fixed_point"], "$.compiler_artifacts.fixed_point") is not True:
        _fail("PCC-CPY-EVIDENCE-FIXED-POINT", "$.compiler_artifacts.fixed_point", "not fixed")

    applications = _array(
        bundle["application_artifacts"], "$.application_artifacts", nonempty=True
    )
    application_ids = set()
    application_digests = set()
    application_digest_by_id: dict[str, str] = {}
    for index, value in enumerate(applications):
        path = "$.application_artifacts[" + str(index) + "]"
        artifact = _object(value, _APPLICATION_FIELDS, path)
        identifier = _string(artifact["id"], path + ".id")
        digest = _sha256(artifact["sha256"], path + ".sha256")
        if identifier in application_ids or digest in application_digests:
            _fail("PCC-CPY-EVIDENCE-IDENTITY", path, "duplicate application artifact")
        application_ids.add(identifier)
        application_digests.add(digest)
        application_digest_by_id[identifier] = digest
        if _sha256(
            artifact["source_tree_sha256"], path + ".source_tree_sha256"
        ) != source_digest:
            _fail("PCC-CPY-EVIDENCE-IDENTITY", path, "application source drift")
    if application_ids != set(expected_workloads):
        _fail(
            "PCC-CPY-EVIDENCE-IDENTITY",
            "$.application_artifacts",
            "application artifacts do not cover every claimed workload",
        )

    packages = _array(bundle["package_artifacts"], "$.package_artifacts", nonempty=True)
    package_ids = set()
    for index, value in enumerate(packages):
        path = "$.package_artifacts[" + str(index) + "]"
        package = _object(value, _PACKAGE_FIELDS, path)
        package_id = (
            _string(package["name"], path + ".name"),
            _string(package["version"], path + ".version"),
        )
        if package_id in package_ids:
            _fail("PCC-CPY-EVIDENCE-IDENTITY", path, "duplicate package artifact")
        package_ids.add(package_id)
        _sha256(package["sha256"], path + ".sha256")
        if package["abi_mode"] != "pcc-native" or package["build_owner"] != "pcc1":
            _fail("PCC-CPY-EVIDENCE-PACKAGE", path, "host/CPython package artifact")
    required_packages = {
        (row["source"]["name"], row["source"]["version"])
        for row in workloads_for_level(frozen_catalog, level)
        if row["source"]["kind"] in {
            "vendored-upstream-source", "upstream-release-sdist",
        }
    }
    if package_ids != required_packages:
        _fail(
            "PCC-CPY-EVIDENCE-PACKAGE",
            "$.package_artifacts",
            "package artifacts do not match claimed upstream workloads",
        )

    oracle = _object(bundle["oracle"], _ORACLE_FIELDS, "$.oracle")
    baseline = frozen_contract["baseline"]
    if (
        oracle["implementation"] != baseline["oracle_implementation"]
        or oracle["version"] != baseline["oracle_version"]
        or oracle["build"] != baseline["oracle_build"]
    ):
        _fail("PCC-CPY-EVIDENCE-ORACLE", "$.oracle", "oracle baseline drift")
    if _boolean(
        oracle["execution_is_separate"], "$.oracle.execution_is_separate"
    ) is not True:
        _fail("PCC-CPY-EVIDENCE-ORACLE", "$.oracle", "oracle entered pcc run")
    _sha256(oracle["result_sha256"], "$.oracle.result_sha256")

    expected_modes = frozen_contract["evidence_policy"]["required_modes"]
    forbidden_owners = set(
        frozen_contract["evidence_policy"]["forbidden_execution_owners"]
    )
    expected_pairs = {
        (workload_id, target, backend)
        for workload_id in expected_workloads
        for target in expected_targets
        for backend in expected_backends
    }
    observed_pairs = set()
    runs = _array(bundle["runs"], "$.runs", nonempty=True)
    for index, value in enumerate(runs):
        path = "$.runs[" + str(index) + "]"
        run = _object(value, _RUN_FIELDS, path)
        workload_id = _string(run["workload_id"], path + ".workload_id")
        target = _string(run["target_triple"], path + ".target_triple")
        backend = _integer(run["gc_backend"], path + ".gc_backend")
        pair = (workload_id, target, backend)
        if pair in observed_pairs:
            _fail(
                "PCC-CPY-EVIDENCE-RUNS",
                path,
                "duplicate workload/target/GC run",
            )
        observed_pairs.add(pair)
        modes = _object(run["modes"], set(expected_modes), path + ".modes")
        if modes != expected_modes:
            _fail("PCC-CPY-EVIDENCE-MODE", path + ".modes", "not pcc1 native/self")
        application_digest = _sha256(
            run["application_sha256"], path + ".application_sha256"
        )
        if application_digest_by_id.get(workload_id) != application_digest:
            _fail(
                "PCC-CPY-EVIDENCE-IDENTITY",
                path,
                "run does not bind its claimed workload artifact",
            )

        process = _object(run["process_tree"], _PROCESS_FIELDS, path + ".process_tree")
        _sha256(process["sha256"], path + ".process_tree.sha256")
        owners = set(_strings(
            process["execution_owners"], path + ".process_tree.execution_owners"
        ))
        forbidden_seen = _strings(
            process["forbidden_owners_observed"],
            path + ".process_tree.forbidden_owners_observed",
        )
        if forbidden_seen or owners & forbidden_owners or not owners <= _ALLOWED_PROCESS_OWNERS:
            _fail("PCC-CPY-EVIDENCE-OWNER", path + ".process_tree", "host/CPython owner observed")
        if "pcc1" not in owners or "pcc-application" not in owners:
            _fail("PCC-CPY-EVIDENCE-OWNER", path + ".process_tree", "pcc owner chain incomplete")

        linkage = _object(
            run["binary_linkage"], _LINKAGE_FIELDS, path + ".binary_linkage"
        )
        _sha256(linkage["sha256"], path + ".binary_linkage.sha256")
        needed = _strings(
            linkage["needed_libraries"], path + ".binary_linkage.needed_libraries"
        )
        undefined = _strings(
            linkage["undefined_symbols"], path + ".binary_linkage.undefined_symbols"
        )
        for field in (
            "libpython", "llvm_runtime_fallback", "cpython_extension_abi"
        ):
            if _boolean(linkage[field], path + ".binary_linkage." + field):
                _fail("PCC-CPY-EVIDENCE-LINKAGE", path + ".binary_linkage", field)
        is_static = _boolean(linkage["static"], path + ".binary_linkage.static")
        zero_libc = _boolean(linkage["zero_libc"], path + ".binary_linkage.zero_libc")
        if target == "arm64-apple-darwin":
            if needed != ["/usr/lib/libSystem.B.dylib"] or is_static or zero_libc:
                _fail("PCC-CPY-EVIDENCE-LINKAGE", path, "false Darwin boundary")
        elif target == "x86_64-unknown-linux-gnu":
            if needed or undefined or not is_static or not zero_libc:
                _fail("PCC-CPY-EVIDENCE-LINKAGE", path, "false Linux zero-libc boundary")
        else:
            _fail("PCC-CPY-EVIDENCE-RUNS", path, "target outside frozen matrix")

        runtime = _object(
            run["runtime_archive"], _RUNTIME_FIELDS, path + ".runtime_archive"
        )
        for field in ("archive_sha256", "manifest_sha256", "inventory_sha256"):
            _sha256(runtime[field], path + ".runtime_archive." + field)
        if (
            runtime["source_kind"] != "pcc-python"
            or runtime["producer_kind"] != "pcc-python-library-ir-to-obj"
            or _boolean(runtime["uses_host_cc"], path + ".runtime_archive.uses_host_cc")
        ):
            _fail("PCC-CPY-EVIDENCE-RUNTIME", path + ".runtime_archive", "runtime owner drift")
        _sha256(
            run["package_environment_sha256"],
            path + ".package_environment_sha256",
        )
        _sha256(
            run["performance_result_sha256"],
            path + ".performance_result_sha256",
        )
        if run["status"] != "passed":
            _fail("PCC-CPY-EVIDENCE-RUNS", path + ".status", "run did not pass")

    if observed_pairs != expected_pairs:
        _fail(
            "PCC-CPY-EVIDENCE-RUNS",
            "$.runs",
            "workload/target/GC matrix incomplete",
        )
    return bundle


def canonical_evidence_json(payload: object) -> str:
    bundle = validate_evidence_bundle(payload)
    return json.dumps(
        bundle, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )


def evidence_digest(payload: object) -> str:
    return hashlib.sha256(canonical_evidence_json(payload).encode("utf-8")).hexdigest()


__all__ = [
    "EVIDENCE_SCHEMA_VERSION",
    "ReplacementEvidenceError",
    "canonical_evidence_json",
    "evidence_digest",
    "validate_evidence_bundle",
]
