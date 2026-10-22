from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts.qualification.cpython_replacement.workloads import (
    CLAIM_MODE,
    FORBIDDEN_EVIDENCE_MODES,
    ORACLE_BASELINE,
    SCHEMA,
    STATUS_UNVERIFIED,
    TARGETS,
    WorkloadCatalogError,
    catalog_digest,
    load_workload_catalog,
    validate_workload_catalog,
    workloads_for_level,
)


def _catalog():
    return copy.deepcopy(load_workload_catalog())


def _assert_error(code: str, callback) -> WorkloadCatalogError:
    with pytest.raises(WorkloadCatalogError) as raised:
        callback()
    assert raised.value.code == code
    assert str(raised.value).startswith(f"{code}: ")
    return raised.value


def _workload(payload, workload_id: str):
    return next(item for item in payload["workloads"] if item["id"] == workload_id)


def test_repository_workload_catalog_is_frozen_unverified_and_mode_labeled():
    payload = load_workload_catalog()

    assert payload["schema"] == SCHEMA
    assert payload["python_baseline"] == {
        "language": "3.13",
        "oracle": ORACLE_BASELINE,
    }
    assert tuple(target["triple"] for target in payload["targets"]) == TARGETS
    assert payload["claim_contract"]["required_mode"] == CLAIM_MODE
    assert tuple(payload["claim_contract"]["forbidden_evidence_modes"]) == (
        FORBIDDEN_EVIDENCE_MODES
    )
    assert payload["claim_contract"]["gc_backends"] == [0, 1, 2, 3, 4]
    assert payload["claim_contract"]["historical_evidence_satisfies_current_claim"] is False
    assert {level["status"] for level in payload["levels"]} == {STATUS_UNVERIFIED}
    assert {workload["status"] for workload in payload["workloads"]} == {
        STATUS_UNVERIFIED
    }


def test_levels_are_strictly_cumulative_and_level_three_is_final_goal():
    payload = load_workload_catalog()
    levels = payload["levels"]

    assert [item["cumulative_includes"] for item in levels] == [
        [1],
        [1, 2],
        [1, 2, 3],
    ]
    assert [item["final_product_goal"] for item in levels] == [False, False, True]

    l1 = workloads_for_level(payload, 1)
    l2 = workloads_for_level(payload, 2)
    l3 = workloads_for_level(payload, 3)
    assert l1
    assert len(l1) < len(l2) < len(l3)
    assert {item["id"] for item in l1} < {item["id"] for item in l2}
    assert {item["id"] for item in l2} < {item["id"] for item in l3}
    assert all(item["minimum_level"] <= 2 for item in l2)
    assert tuple(l3) == tuple(payload["workloads"])


def test_level_one_pins_an_unchanged_pure_python_service_lifecycle():
    payload = load_workload_catalog()
    workload = _workload(payload, "cpy-l1-pproxy-service-1-9-5")

    assert workload["minimum_level"] == 1
    assert workload["category"] == "pure-python-service"
    assert workload["source"]["name"] == "pproxy"
    assert workload["source"]["version"] == "1.9.5"
    assert set(workload["lifecycle"]) == {
        "acquire",
        "install",
        "build",
        "start",
        "exercise",
        "shutdown",
        "cleanup",
    }
    assert "gc0-through-gc4-thirty-minute-load" in workload["required_gates"]


def test_level_two_pins_numpy_simplejson_build_and_archive_workloads():
    payload = load_workload_catalog()
    level_two = {
        item["id"]: item
        for item in payload["workloads"]
        if item["minimum_level"] == 2
    }

    assert set(level_two) == {
        "cpy-l2-numpy-2-4-4",
        "cpy-l2-build-tool-closure-v1",
        "cpy-l2-compression-archive-corpus-v1",
        "cpy-l2-simplejson-4-1-1",
    }
    assert level_two["cpy-l2-numpy-2-4-4"]["source"]["version"] == "2.4.4"
    assert level_two["cpy-l2-simplejson-4-1-1"]["source"]["version"] == "4.1.1"
    assert any(
        gate.startswith("sdist-sha256-")
        for gate in level_two["cpy-l2-simplejson-4-1-1"]["required_gates"]
    )
    assert {item["category"] for item in level_two.values()} == {
        "scientific-array",
        "build-tool",
        "compression-archive",
        "native-extension-family",
    }


def test_level_two_gate_is_one_hostless_owned_build_and_five_gc_chain():
    root = Path(__file__).resolve().parents[2]
    gate = root / "tests" / "integration" / "test_pcc1_scientific_build_replacement.py"
    source = gate.read_text(encoding="utf-8")

    assert "3ab6d97b34440c2e5d02ed5458068533dfb72ac9372030cdd8daa0b55ce17525" in source
    assert "c08eb9f7a90f77ae470e19a07472e9a79ebc0d1c2315d86a72767665bd5ba79f" in source
    assert '"--no-index"' in source
    assert '"--build=owned"' in source
    assert '"PCC_HOST_PYTHON": str(deny / "python3")' in source
    assert '"--python-libpython=off"' in source
    assert '"--backend",\n            "self"' in source
    assert "for backend in range(5):" in source
    assert 'assert actual == expected' in source
    assert 'assert telemetry["pin_balance"] == 0' in source
    assert "PCC_CPYTHON_3132_ORACLE" in source
    assert "PCC_CPYTHON_3132_LEVEL2_SITE" in source
    assert "_assert_no_package_name_mechanism_branch()" in source
    assert "gzip.decompress" in source
    assert "bz2.decompress" in source
    assert "lzma.decompress" in source
    assert "zlib.decompress" in source
    assert "bounded_sample_only" in source


def test_level_three_finitely_covers_dropin_product_categories():
    payload = load_workload_catalog()
    level_three_categories = {
        item["category"]
        for item in payload["workloads"]
        if item["minimum_level"] == 3
    }

    assert level_three_categories == {
        "language-compatibility",
        "stdlib-compatibility",
        "cli-import-tooling",
        "package-extension-compatibility",
        "operations-concurrency",
    }
    assert payload["levels"][2]["name"] == "supported python3 drop-in replacement"
    assert "signed-clean-release-provenance-bundle" in payload["levels"][2][
        "required_gates"
    ]


def test_every_workload_uses_separate_cpython_oracle_and_both_targets():
    payload = load_workload_catalog()
    for workload in payload["workloads"]:
        assert workload["claim_mode"] == CLAIM_MODE
        assert workload["oracle"] == {
            "implementation": ORACLE_BASELINE,
            "role": "behavioral-reference-only",
            "execution_is_separate": True,
        }
        assert tuple(workload["targets"]) == TARGETS


def test_darwin_and_linux_runtime_boundaries_are_not_conflated():
    payload = load_workload_catalog()
    boundaries = {
        target["triple"]: target["runtime_boundary"]
        for target in payload["targets"]
    }

    assert boundaries == {
        "arm64-apple-darwin": "named-libSystem-abi",
        "x86_64-unknown-linux-gnu": "static-zero-libc-required",
    }
    assert {target["status"] for target in payload["targets"]} == {"unverified"}


def test_performance_contract_measures_behavior_and_long_running_resources():
    payload = load_workload_catalog()
    required_metrics = {
        "throughput",
        "latency_p50",
        "latency_p95",
        "latency_p99",
        "rss_peak_bytes",
        "rss_growth_bytes_per_hour",
        "gc_pause_p95_seconds",
        "error_count",
    }

    for item in [*payload["levels"], *payload["workloads"]]:
        envelope = item["performance_envelope"]
        assert required_metrics <= set(envelope["metrics"])
        assert "machine-labeled" in envelope["acceptance"]["comparison"]
        assert envelope["duration_seconds"] >= 60
        assert envelope["warmup_seconds"] < envelope["duration_seconds"]
        assert envelope["acceptance"]["max_error_count"] == 0
        assert envelope["acceptance"]["rss_growth_estimator"] == (
            "theil-sen-after-warmup"
        )
        assert 0.0 <= envelope["acceptance"][
            "max_rss_growth_fraction_of_peak_per_hour"
        ] <= 0.10
        assert envelope["acceptance"]["max_gc_pause_p95_seconds"] <= envelope[
            "sample_interval_seconds"
        ]
        assert envelope["acceptance"]["throughput_policy"] == (
            "report-machine-labeled-no-universal-floor"
        )
        assert envelope["acceptance"]["latency_policy"] == (
            "report-machine-labeled-no-universal-ceiling"
        )


def test_catalog_digest_is_canonical_and_changes_with_contract_content():
    first = _catalog()
    reordered_text = json.dumps(first, sort_keys=True)
    reordered = json.loads(reordered_text)
    changed = copy.deepcopy(first)
    changed["workloads"][0]["title"] += " changed"

    assert catalog_digest(first) == catalog_digest(reordered)
    assert len(catalog_digest(first)) == 64
    assert catalog_digest(changed) != catalog_digest(first)


@pytest.mark.parametrize("mode", FORBIDDEN_EVIDENCE_MODES)
def test_forbidden_evidence_mode_cannot_become_required_claim_mode(mode):
    payload = _catalog()
    payload["claim_contract"]["required_mode"] = mode

    error = _assert_error(
        "PCC-CPY-WORKLOAD-008",
        lambda: validate_workload_catalog(payload),
    )
    assert "required_mode" in error.detail


def test_cpython_compat_workload_label_is_rejected():
    payload = _catalog()
    payload["workloads"][0]["claim_mode"] = "pcc1/cpython-compat/self/libpython"

    error = _assert_error(
        "PCC-CPY-WORKLOAD-008",
        lambda: validate_workload_catalog(payload),
    )
    assert "not pcc-native" in error.detail


def test_host_assisted_oracle_inside_pcc_execution_is_rejected():
    payload = _catalog()
    payload["workloads"][0]["oracle"]["execution_is_separate"] = False

    error = _assert_error(
        "PCC-CPY-WORKLOAD-012",
        lambda: validate_workload_catalog(payload),
    )
    assert "outside the pcc execution" in error.detail


def test_historical_results_cannot_satisfy_current_pcc1_claim():
    payload = _catalog()
    payload["claim_contract"]["historical_evidence_satisfies_current_claim"] = True

    error = _assert_error(
        "PCC-CPY-WORKLOAD-008",
        lambda: validate_workload_catalog(payload),
    )
    assert "historical evidence" in error.detail


def test_claim_status_cannot_be_predeclared_supported():
    payload = _catalog()
    payload["workloads"][0]["status"] = "supported"

    error = _assert_error(
        "PCC-CPY-WORKLOAD-005",
        lambda: validate_workload_catalog(payload),
    )
    assert "must remain unverified" in error.detail


def test_level_two_cannot_drop_level_one():
    payload = _catalog()
    payload["levels"][1]["cumulative_includes"] = [2]

    error = _assert_error(
        "PCC-CPY-WORKLOAD-009",
        lambda: validate_workload_catalog(payload),
    )
    assert "cumulatively include" in error.detail


def test_level_three_cannot_be_demoted_from_final_product_goal():
    payload = _catalog()
    payload["levels"][2]["final_product_goal"] = False

    error = _assert_error(
        "PCC-CPY-WORKLOAD-009",
        lambda: validate_workload_catalog(payload),
    )
    assert "final product goal" in error.detail


def test_darwin_cannot_be_claimed_as_zero_libc():
    payload = _catalog()
    payload["targets"][0]["runtime_boundary"] = "static-zero-libc-required"

    error = _assert_error(
        "PCC-CPY-WORKLOAD-004",
        lambda: validate_workload_catalog(payload),
    )
    assert "never zero-libc" in error.detail


def test_linux_cannot_drop_static_zero_libc_requirement():
    payload = _catalog()
    payload["targets"][1]["runtime_boundary"] = "named-libSystem-abi"

    error = _assert_error(
        "PCC-CPY-WORKLOAD-004",
        lambda: validate_workload_catalog(payload),
    )
    assert "static zero-libc" in error.detail


def test_missing_long_running_metric_is_rejected():
    payload = _catalog()
    payload["levels"][0]["performance_envelope"]["metrics"].remove(
        "rss_growth_bytes_per_hour"
    )

    error = _assert_error(
        "PCC-CPY-WORKLOAD-006",
        lambda: validate_workload_catalog(payload),
    )
    assert "rss_growth_bytes_per_hour" in error.detail


def test_performance_comparison_must_be_machine_labeled():
    payload = _catalog()
    payload["workloads"][0]["performance_envelope"]["acceptance"][
        "comparison"
    ] = "pcc1 is always faster"

    error = _assert_error(
        "PCC-CPY-WORKLOAD-006",
        lambda: validate_workload_catalog(payload),
    )
    assert "machine-labeled" in error.detail


def test_performance_error_and_rss_bounds_are_machine_judged():
    payload = _catalog()
    acceptance = payload["workloads"][0]["performance_envelope"]["acceptance"]
    acceptance["max_error_count"] = 1

    error = _assert_error(
        "PCC-CPY-WORKLOAD-006",
        lambda: validate_workload_catalog(payload),
    )
    assert "max_error_count must be zero" in error.detail

    payload = _catalog()
    acceptance = payload["workloads"][0]["performance_envelope"]["acceptance"]
    acceptance["max_rss_growth_fraction_of_peak_per_hour"] = 0.11

    error = _assert_error(
        "PCC-CPY-WORKLOAD-006",
        lambda: validate_workload_catalog(payload),
    )
    assert "must be in 0.0..0.10" in error.detail


def test_unsupported_surface_cannot_publish_partial_artifact():
    payload = _catalog()
    payload["workloads"][0]["unsupported_policy"]["publication"] = "allowed"

    error = _assert_error(
        "PCC-CPY-WORKLOAD-007",
        lambda: validate_workload_catalog(payload),
    )
    assert "publication must be forbidden" in error.detail


def test_workload_lifecycle_cannot_omit_cleanup():
    payload = _catalog()
    payload["workloads"][0]["lifecycle"].remove("cleanup")

    error = _assert_error(
        "PCC-CPY-WORKLOAD-011",
        lambda: validate_workload_catalog(payload),
    )
    assert "lifecycle is incomplete" in error.detail


def test_level_category_needs_a_concrete_cumulative_workload():
    payload = _catalog()
    payload["levels"][1]["required_categories"].append("missing-category")
    payload["levels"][2]["required_categories"].append("missing-category")

    error = _assert_error(
        "PCC-CPY-WORKLOAD-010",
        lambda: validate_workload_catalog(payload),
    )
    assert "no workload" in error.detail


def test_unknown_level_is_rejected_after_catalog_validation():
    payload = load_workload_catalog()

    error = _assert_error(
        "PCC-CPY-WORKLOAD-009",
        lambda: workloads_for_level(payload, 4),
    )
    assert "unknown replacement level" in error.detail


def test_load_rejects_invalid_json_with_stable_code(tmp_path):
    path = tmp_path / "bad-workloads.json"
    path.write_text("{not json", encoding="utf-8")

    error = _assert_error(
        "PCC-CPY-WORKLOAD-001",
        lambda: load_workload_catalog(path),
    )
    assert "cannot load" in error.detail
