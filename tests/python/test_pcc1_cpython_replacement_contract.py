"""Control-plane contract for the finite pcc1 CPython replacement promise.

These tests validate declarations and fail-closed claim policy.  They do not
claim that a workload, target, GC backend, or pcc1 fixed point has passed.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from scripts.qualification.cpython_replacement.contract import (
    ReplacementContractError,
    contract_digest,
    load_contract,
    validate_contract,
    surfaces_for_level,
)
from scripts.qualification.cpython_replacement.evidence import (
    EVIDENCE_SCHEMA_VERSION,
    ReplacementEvidenceError,
    evidence_digest,
    validate_evidence_bundle,
)
from scripts.qualification.cpython_replacement.workloads import (
    WorkloadCatalogError,
    catalog_digest,
    load_workload_catalog,
    validate_workload_catalog,
    workloads_for_level,
)


ROOT = Path(__file__).resolve().parents[2]
MATRIX = ROOT / "docs" / "compat" / "pcc1-cpython-replacement-matrix.json"
WORKLOADS = ROOT / "docs" / "compat" / "pcc1-cpython-replacement-workloads.json"


def test_frozen_contract_has_one_exact_oracle_and_two_honest_platform_boundaries():
    contract = load_contract(MATRIX)

    assert contract["baseline"] == {
        "language_version": "3.13",
        "oracle_implementation": "CPython",
        "oracle_version": "3.15.0",
        "oracle_build": "standard-gil",
        "version_policy": "exact-oracle-version",
    }
    targets = {row["target_triple"]: row for row in contract["targets"]}
    assert set(targets) == {
        "arm64-apple-darwin",
        "x86_64-unknown-linux-gnu",
    }
    assert targets["arm64-apple-darwin"]["runtime_boundary"] == "named-libSystem"
    assert targets["arm64-apple-darwin"]["zero_libc"] is False
    assert targets["x86_64-unknown-linux-gnu"]["runtime_boundary"] == "static-zero-libc"
    assert targets["x86_64-unknown-linux-gnu"]["zero_libc"] is True
    assert contract["product"]["evidence_schema"] == EVIDENCE_SCHEMA_VERSION


def test_levels_are_cumulative_and_level_three_is_the_only_final_product_goal():
    levels = load_contract(MATRIX)["levels"]

    assert [row["level"] for row in levels] == [1, 2, 3]
    assert [row["inherits"] for row in levels] == [[], [1], [1, 2]]
    assert [row["final_goal"] for row in levels] == [False, False, True]


def test_every_contract_surface_has_owner_oracle_gate_verdict_and_stable_diagnostic():
    surfaces = load_contract(MATRIX)["surfaces"]

    assert {row["category"] for row in surfaces} >= {
        "language",
        "stdlib",
        "cli",
        "import",
        "package",
        "tooling",
        "runtime",
        "platform",
    }
    assert len({row["id"] for row in surfaces}) == len(surfaces)
    for row in surfaces:
        assert row["owner"]
        assert row["oracle"]
        assert row["gate"]
        assert row["verdict"] in {"supported", "unsupported"}
        assert row["diagnostic"].startswith("PCC-CPY-")
        if row["verdict"] == "supported":
            assert row["implementation_status"] == "unverified"
            assert row["diagnostic"].endswith("-UNPROVEN")
        else:
            assert row["implementation_status"] == "excluded"
            assert row["diagnostic"].startswith("PCC-CPY-UNSUPPORTED-")


def test_supported_verdict_is_a_promise_not_current_proof():
    contract = load_contract(MATRIX)

    assert contract["product"]["current_implementation_status"] == "unverified"
    assert contract["unsupported_policy"]["supported_verdict_is_current_proof"] is False
    assert all(
        row["implementation_status"] == "unverified"
        for row in contract["targets"]
        if row["verdict"] == "supported"
    )


def test_contract_identity_is_canonical_and_changes_when_the_promise_changes():
    contract = load_contract(MATRIX)
    first = contract_digest(contract)
    second = contract_digest(deepcopy(contract))
    changed = deepcopy(contract)
    changed["baseline"]["oracle_version"] = "3.13.3"

    assert len(first) == 64
    assert first == second
    with pytest.raises(ReplacementContractError):
        validate_contract(changed)


def test_contract_rejects_false_darwin_zero_libc_and_non_cumulative_levels():
    contract = load_contract(MATRIX)
    wrong_boundary = deepcopy(contract)
    wrong_boundary["targets"][0]["zero_libc"] = True
    with pytest.raises(ReplacementContractError) as boundary_error:
        validate_contract(wrong_boundary)
    assert boundary_error.value.code.startswith("PCC-CPY-CONTRACT-")

    wrong_levels = deepcopy(contract)
    wrong_levels["levels"][2]["inherits"] = [2]
    with pytest.raises(ReplacementContractError) as levels_error:
        validate_contract(wrong_levels)
    assert levels_error.value.code.startswith("PCC-CPY-CONTRACT-")


def test_workload_catalog_is_unverified_cumulative_and_bound_to_the_same_matrix():
    contract = load_contract(MATRIX)
    catalog = load_workload_catalog(WORKLOADS)

    assert validate_workload_catalog(catalog) is catalog
    assert catalog["python_baseline"] == {
        "language": "3.13",
        "oracle": "CPython 3.15.0",
    }
    assert [row["level"] for row in catalog["levels"]] == [1, 2, 3]
    assert [row["final_product_goal"] for row in catalog["levels"]] == [
        False,
        False,
        True,
    ]
    assert all(row["status"] == "unverified" for row in catalog["levels"])
    assert all(row["status"] == "unverified" for row in catalog["workloads"])
    assert {row["triple"] for row in catalog["targets"]} == {
        row["target_triple"] for row in contract["targets"]
    }
    assert contract["product"]["workload_catalog"] == WORKLOADS.relative_to(ROOT).as_posix()


def test_workloads_for_higher_levels_include_every_lower_level_workload():
    catalog = load_workload_catalog(WORKLOADS)
    level1 = {row["id"] for row in workloads_for_level(catalog, 1)}
    level2 = {row["id"] for row in workloads_for_level(catalog, 2)}
    level3 = {row["id"] for row in workloads_for_level(catalog, 3)}

    assert level1
    assert level1 < level2
    assert level2 < level3
    assert {row["minimum_level"] for row in workloads_for_level(catalog, 3)} == {
        1,
        2,
        3,
    }


def test_workload_identity_is_canonical_and_host_assisted_modes_are_forbidden():
    catalog = load_workload_catalog(WORKLOADS)

    assert len(catalog_digest(catalog)) == 64
    assert catalog_digest(catalog) == catalog_digest(deepcopy(catalog))
    assert set(catalog["claim_contract"]["forbidden_evidence_modes"]) >= {
        "host-python",
        "host-pcc",
        "cpython-compat",
        "libpython",
        "llvm-fallback",
    }

    assisted = deepcopy(catalog)
    assisted["claim_contract"]["required_mode"] = "host-python"
    with pytest.raises(WorkloadCatalogError) as error:
        validate_workload_catalog(assisted)
    assert error.value.code == "PCC-CPY-WORKLOAD-008"


def _sha(character: str) -> str:
    return character * 64


def _valid_evidence_bundle(level: int = 1):
    contract = load_contract(MATRIX)
    catalog = load_workload_catalog(WORKLOADS)
    workloads = list(workloads_for_level(catalog, level))
    digest_characters = "abcdef0123456789"
    application_artifacts = []
    application_sha_by_id = {}
    for index, workload in enumerate(workloads):
        digest = _sha(digest_characters[index])
        application_sha_by_id[workload["id"]] = digest
        application_artifacts.append({
            "id": workload["id"],
            "sha256": digest,
            "source_tree_sha256": _sha("3"),
        })
    runs = []
    for workload in workloads:
        for target in contract["evidence_policy"]["required_targets"]:
            for backend in contract["evidence_policy"]["required_gc_backends"]:
                darwin = target == "arm64-apple-darwin"
                runs.append({
                    "workload_id": workload["id"],
                    "target_triple": target,
                    "gc_backend": backend,
                    "modes": dict(
                        contract["evidence_policy"]["required_modes"]
                    ),
                    "application_sha256": application_sha_by_id[workload["id"]],
                    "process_tree": {
                        "sha256": _sha("b"),
                        "execution_owners": ["pcc1", "pcc-application"],
                        "forbidden_owners_observed": [],
                    },
                    "binary_linkage": {
                        "sha256": _sha("c"),
                        "needed_libraries": (
                            ["/usr/lib/libSystem.B.dylib"] if darwin else []
                        ),
                        "undefined_symbols": [] if not darwin else ["_write"],
                        "static": not darwin,
                        "zero_libc": not darwin,
                        "libpython": False,
                        "llvm_runtime_fallback": False,
                        "cpython_extension_abi": False,
                    },
                    "runtime_archive": {
                        "archive_sha256": _sha("d"),
                        "manifest_sha256": _sha("e"),
                        "inventory_sha256": _sha("f"),
                        "source_kind": "pcc-python",
                        "producer_kind": "pcc-python-library-ir-to-obj",
                        "uses_host_cc": False,
                    },
                    "package_environment_sha256": _sha("1"),
                    "performance_result_sha256": _sha("2"),
                    "status": "passed",
                })
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "contract_digest": contract_digest(contract),
        "workload_catalog_digest": catalog_digest(catalog),
        "claim": {
            "profile": "replacement-release",
            "level": level,
            "surface_ids": [
                row["id"] for row in surfaces_for_level(
                    contract, level, include_unsupported=False,
                )
            ],
            "workload_ids": [
                row["id"] for row in workloads_for_level(catalog, level)
            ],
            "targets": list(contract["evidence_policy"]["required_targets"]),
            "gc_backends": list(
                contract["evidence_policy"]["required_gc_backends"]
            ),
            "status": "passed",
        },
        "source": {
            "repository": "pcc",
            "revision": "current-clean-revision",
            "tree_sha256": _sha("3"),
            "clean": True,
        },
        "compiler_artifacts": {
            "source_tree_sha256": _sha("3"),
            "stages": [
                {
                    "stage": "pcc0",
                    "sha256": _sha("4"),
                    "normalized_sha256": _sha("a"),
                },
                {
                    "stage": "pcc1",
                    "sha256": _sha("5"),
                    "normalized_sha256": _sha("b"),
                },
                {
                    "stage": "pcc2",
                    "sha256": _sha("6"),
                    "normalized_sha256": _sha("7"),
                },
                {
                    "stage": "pcc3",
                    "sha256": _sha("6"),
                    "normalized_sha256": _sha("7"),
                },
            ],
            "normalized_pcc2_pcc3_sha256": _sha("7"),
            "fixed_point": True,
        },
        "application_artifacts": application_artifacts,
        "runs": runs,
        "package_artifacts": [
            {
                "name": workload["source"]["name"],
                "version": workload["source"]["version"],
                "sha256": _sha(digest_characters[(8 + index) % 16]),
                "abi_mode": "pcc-native",
                "build_owner": "pcc1",
            }
            for index, workload in enumerate(workloads)
            if workload["source"]["kind"] in {
                "vendored-upstream-source", "upstream-release-sdist",
            }
        ],
        "oracle": {
            "implementation": "CPython",
            "version": "3.15.0",
            "build": "standard-gil",
            "execution_is_separate": True,
            "result_sha256": _sha("9"),
        },
    }


def test_replacement_evidence_binds_every_identity_and_is_canonical():
    evidence = _valid_evidence_bundle()

    assert validate_evidence_bundle(evidence) is evidence
    assert evidence_digest(evidence) == evidence_digest(deepcopy(evidence))
    assert len(evidence_digest(evidence)) == 64


def test_replacement_evidence_rejects_host_owner_and_libpython_linkage():
    host = _valid_evidence_bundle()
    host["runs"][0]["process_tree"]["execution_owners"].append("host-python")
    with pytest.raises(ReplacementEvidenceError) as host_error:
        validate_evidence_bundle(host)
    assert host_error.value.code == "PCC-CPY-EVIDENCE-OWNER"

    linked = _valid_evidence_bundle()
    linked["runs"][0]["binary_linkage"]["libpython"] = True
    with pytest.raises(ReplacementEvidenceError) as linkage_error:
        validate_evidence_bundle(linked)
    assert linkage_error.value.code == "PCC-CPY-EVIDENCE-LINKAGE"


def test_replacement_evidence_requires_every_target_gc_pair():
    evidence = _valid_evidence_bundle()
    evidence["runs"].pop()

    with pytest.raises(ReplacementEvidenceError) as error:
        validate_evidence_bundle(evidence)
    assert error.value.code == "PCC-CPY-EVIDENCE-RUNS"


def test_replacement_evidence_requires_every_level_two_workload_matrix():
    evidence = _valid_evidence_bundle(level=2)

    assert validate_evidence_bundle(evidence) is evidence
    removed_workload = evidence["claim"]["workload_ids"][-1]
    evidence["application_artifacts"] = [
        artifact
        for artifact in evidence["application_artifacts"]
        if artifact["id"] != removed_workload
    ]
    evidence["runs"] = [
        run for run in evidence["runs"]
        if run["workload_id"] != removed_workload
    ]

    with pytest.raises(ReplacementEvidenceError) as error:
        validate_evidence_bundle(evidence)
    assert error.value.code == "PCC-CPY-EVIDENCE-IDENTITY"


def test_replacement_evidence_rejects_host_built_package_artifact():
    evidence = _valid_evidence_bundle(level=2)
    evidence["package_artifacts"][0]["build_owner"] = "host-python"

    with pytest.raises(ReplacementEvidenceError) as error:
        validate_evidence_bundle(evidence)
    assert error.value.code == "PCC-CPY-EVIDENCE-PACKAGE"


def test_replacement_evidence_is_bound_to_contract_and_workload_digests():
    evidence = _valid_evidence_bundle()
    evidence["contract_digest"] = _sha("0")

    with pytest.raises(ReplacementEvidenceError) as error:
        validate_evidence_bundle(evidence)
    assert error.value.code == "PCC-CPY-EVIDENCE-IDENTITY"


def test_replacement_evidence_requires_pcc2_pcc3_normalized_fixed_point():
    evidence = _valid_evidence_bundle()
    evidence["compiler_artifacts"]["stages"][-1]["normalized_sha256"] = _sha("8")

    with pytest.raises(ReplacementEvidenceError) as error:
        validate_evidence_bundle(evidence)
    assert error.value.code == "PCC-CPY-EVIDENCE-FIXED-POINT"


def test_replacement_evidence_rejects_raw_drift_even_with_equal_normalized_images():
    evidence = _valid_evidence_bundle()
    evidence["compiler_artifacts"]["stages"][-1]["sha256"] = _sha("9")
    with pytest.raises(ReplacementEvidenceError, match="raw identities do not match"):
        validate_evidence_bundle(evidence)
