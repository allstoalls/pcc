from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from pcc1_gate import repo_root

import pytest

from pcc1_gate import find_current_pcc1, skip_or_fail_no_current_pcc1

from pcc.package.campaign import (
    CampaignRecord,
    campaign_dashboard,
    campaign_selection,
    compatibility_matrix,
    select_test_files,
)

REPO = repo_root()


def _find_current_pcc1() -> Path | None:
    return find_current_pcc1(REPO)


def test_campaign_dashboard_counts_status_area_and_xfail_taxonomy(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_core.py").write_text("def test_x(): pass\n", encoding="utf-8")
    (tests / "helper.py").write_text("", encoding="utf-8")
    assert select_test_files(tests) == (str(tests / "test_core.py"),)

    dashboard = campaign_dashboard(
        [
            CampaignRecord("core", "test_core.py::test_a", "pass"),
            CampaignRecord("core", "test_core.py::test_b", "xfail", "buffer-protocol"),
            CampaignRecord("linalg", "test_linalg.py::test_c", "fail"),
            CampaignRecord("typing", "test_typing.py::test_d", "skip"),
            CampaignRecord("core", "test_core.py::test_e", "xfail", "buffer-protocol"),
        ]
    )
    assert dashboard["total"] == 5
    assert dashboard["by_status"]["pass"] == 1
    assert dashboard["by_area"]["core"]["xfail"] == 2
    assert dashboard["xfail_taxonomy"] == {"buffer-protocol": 2}


def test_campaign_dashboard_rejects_unknown_status():
    with pytest.raises(ValueError):
        campaign_dashboard([CampaignRecord("core", "x", "unknown")])


def test_compatibility_matrix_is_stable_and_json_ready():
    matrix = compatibility_matrix(
        {
            "scipy": {"status": "audit", "first_smoke": "import scipy"},
            "pandas": {"status": "blocked", "first_smoke": "import pandas"},
        }
    )
    assert matrix["count"] == 2
    assert [row["name"] for row in matrix["packages"]] == ["pandas", "scipy"]


def test_campaign_selection_filters_and_marks_xfail(tmp_path):
    tests = tmp_path / "numpy" / "_core" / "tests"
    tests.mkdir(parents=True)
    (tests / "test_multiarray.py").write_text(
        "def test_shape(): pass\n", encoding="utf-8"
    )
    (tests / "test_umath.py").write_text("def test_add(): pass\n", encoding="utf-8")
    (tests / "test_object.py").write_text("def test_object(): pass\n", encoding="utf-8")
    report = campaign_selection(
        tests,
        area="core",
        include=["test_"],
        exclude=["object"],
        xfail_rules=["umath=ufunc-baseline"],
    )
    assert [Path(path).name for path in report["selected"]] == [
        "test_multiarray.py",
        "test_umath.py",
    ]
    assert report["dashboard"]["by_status"]["selected"] == 1
    assert report["dashboard"]["by_status"]["xfail"] == 1
    assert report["dashboard"]["xfail_taxonomy"] == {"ufunc-baseline": 1}


def test_numpy_core_l6_profile_selects_documented_subset(tmp_path):
    tests = tmp_path / "numpy" / "_core" / "tests"
    tests.mkdir(parents=True)
    for name in (
        "test_multiarray.py",
        "test_umath.py",
        "test_arrayprint.py",
        "test_linalg.py",
        "test_object_arrays.py",
    ):
        (tests / name).write_text("def test_placeholder(): pass\n", encoding="utf-8")

    report = campaign_selection(tmp_path, profile="numpy-core-l6")
    selected_names = [Path(path).name for path in report["selected"]]
    assert selected_names == [
        "test_arrayprint.py",
        "test_multiarray.py",
        "test_umath.py",
    ]
    assert report["area"] == "numpy-core"
    assert report["profile"] == "numpy-core-l6"
    assert report["scan_root"] == str(tests)
    assert report["task_counts"] == {"L6.2": 1, "L6.5": 1, "L6.6": 1}
    first = report["records"][0]
    assert first["profile"] == "numpy-core-l6"
    assert first["task"] == "L6.6"
    assert first["feature"] == "array-repr-print"
    assert report["dashboard"]["by_status"]["selected"] == 3

    import pcc.driver.cli_bootstrap as cb

    native = json.loads(
        cb._native_campaign_json(
            str(tmp_path), "test_*.py", "core", [], [], [], "numpy-core-l6"
        )
    )
    assert native["area"] == report["area"]
    assert native["profile_description"] == report["profile_description"]
    assert native["selection_rule"] == report["selection_rule"]
    assert native["task_counts"] == report["task_counts"]


def test_pcc_package_campaign_cli_writes_report(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_core.py").write_text("def test_core(): pass\n", encoding="utf-8")
    (tests / "test_ufunc.py").write_text("def test_ufunc(): pass\n", encoding="utf-8")
    out = tmp_path / "campaign.json"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    proc = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "campaign",
            "--root",
            str(tests),
            "--area",
            "core",
            "--xfail",
            "ufunc=ufunc-baseline",
            "--out",
            str(out),
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    report = json.loads(proc.stdout)
    written = json.loads(out.read_text(encoding="utf-8"))
    assert report["dashboard"]["total"] == 2
    assert written["dashboard"]["xfail_taxonomy"] == {"ufunc-baseline": 1}


def test_pcc_package_campaign_cli_numpy_core_l6_profile(tmp_path):
    tests = tmp_path / "numpy" / "_core" / "tests"
    tests.mkdir(parents=True)
    (tests / "test_indexing.py").write_text(
        "def test_indexing(): pass\n", encoding="utf-8"
    )
    (tests / "test_umath.py").write_text("def test_umath(): pass\n", encoding="utf-8")
    (tests / "test_linalg.py").write_text("def test_linalg(): pass\n", encoding="utf-8")
    out = tmp_path / "numpy-l6-campaign.json"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    proc = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "campaign",
            "--root",
            str(tmp_path),
            "--profile",
            "numpy-core-l6",
            "--xfail",
            "umath=ufunc-baseline",
            "--out",
            str(out),
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    report = json.loads(proc.stdout)
    written = json.loads(out.read_text(encoding="utf-8"))
    assert [Path(path).name for path in report["selected"]] == [
        "test_indexing.py",
        "test_umath.py",
    ]
    assert report["task_counts"] == {"L6.4": 1, "L6.5": 1}
    assert report["dashboard"]["by_status"]["xfail"] == 1
    assert written["selection_rule"].startswith("fixed NumPy L6")


def test_pcc1_campaign_cli_does_not_need_host_python(tmp_path):
    pcc1 = _find_current_pcc1()
    if pcc1 is None:
        skip_or_fail_no_current_pcc1("no current pcc1 binary with native campaign shim")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_core.py").write_text("def test_core(): pass\n", encoding="utf-8")
    (tests / "test_ufunc.py").write_text("def test_ufunc(): pass\n", encoding="utf-8")
    out = tmp_path / "campaign.json"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    proc = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "campaign",
            "--root",
            str(tests),
            "--area",
            "core",
            "--xfail",
            "ufunc=ufunc-baseline",
            "--out",
            str(out),
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    report = json.loads(proc.stdout)
    written = json.loads(out.read_text(encoding="utf-8"))
    assert report["dashboard"]["total"] == 2
    assert report["dashboard"]["by_status"]["xfail"] == 1
    assert written["dashboard"]["xfail_taxonomy"] == {"ufunc-baseline": 1}

    numpy_tests = tmp_path / "numpy" / "_core" / "tests"
    numpy_tests.mkdir(parents=True)
    (numpy_tests / "test_multiarray.py").write_text(
        "def test_shape(): pass\n", encoding="utf-8"
    )
    (numpy_tests / "test_linalg.py").write_text(
        "def test_linalg(): pass\n", encoding="utf-8"
    )
    proc = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "campaign",
            "--root",
            str(tmp_path),
            "--profile",
            "numpy-core-l6",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    numpy_report = json.loads(proc.stdout)
    assert numpy_report["profile"] == "numpy-core-l6"
    assert numpy_report["task_counts"] == {"L6.2": 1}
    assert Path(numpy_report["selected"][0]).name == "test_multiarray.py"
