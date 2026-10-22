from __future__ import annotations

import json
import os
from pathlib import Path

from pcc1_gate import repo_root
import subprocess

import pytest

from pcc1_gate import find_current_pcc1, skip_or_fail_no_current_pcc1

REPO = repo_root()


def _find_current_pcc1() -> Path | None:
    return find_current_pcc1(REPO)


def test_package_inspect_counts_generic_source_tree(tmp_path):
    pkg = tmp_path / "demo_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("x = 1\n", encoding="utf-8")
    (pkg / "mod.c").write_text("int demo(void) { return 1; }\n", encoding="utf-8")
    (pkg / "mod.hpp").write_text("#pragma once\n", encoding="utf-8")

    env = os.environ.copy()
    env.pop("LC_ALL", None)
    proc = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "inspect",
            "demo-pkg",
            "--path",
            str(pkg),
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    report = json.loads(proc.stdout)
    assert report["name"] == "demo-pkg"
    assert report["files"] == 3
    assert report["python_files"] == 1
    assert report["c_files"] == 1
    assert report["header_files"] == 1
    assert "numpy_level" not in report


def test_package_inspect_accepts_positional_local_source_path(tmp_path):
    pkg = tmp_path / "demo_pkg-0.1"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("x = 1\n", encoding="utf-8")
    (pkg / "pyproject.toml").write_text(
        "[build-system]\nrequires = ['demo-backend']\nbuild-backend = 'demo.backend'\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("LC_ALL", None)
    proc = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "inspect",
            str(pkg),
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    report = json.loads(proc.stdout)
    assert report["name"] == "demo_pkg"
    assert report["path"] == str(pkg)
    assert report["files"] == 2
    assert report["artifact_metadata"]["source_kind"] == "local_source"
    assert report["artifact_metadata"]["pyproject_build_backend"] == "demo.backend"


def test_pcc_module_pip_dry_run_shim_is_generic():
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    proc = subprocess.run(
        ["uv", "run", "pcc", "-m", "pip", "install", "requests", "--dry-run"],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    plan = json.loads(proc.stdout)
    assert plan["ok"] is True
    assert plan["packages"] == ["requests"]
    inspection = plan["inspections"][0]
    assert inspection["name"] == "requests"
    assert inspection["package_level"] == "nolibpython_python"
    assert "numpy_level" not in inspection


def test_package_inspect_reports_mlx_as_extension_abi_target():
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    proc = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "inspect",
            "mlx",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    report = json.loads(proc.stdout)
    assert report["name"] == "mlx"
    assert report["package_level"] == "c_extension_abi"
    assert "MLX" in report["package_summary"]
    assert "numpy_level" not in report


def test_package_inspect_reports_vllm_as_extension_abi_target():
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    proc = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "inspect",
            "vllm",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    report = json.loads(proc.stdout)
    assert report["name"] == "vllm"
    assert report["package_level"] == "c_extension_abi"
    assert "vLLM" in report["package_summary"]
    assert "numpy_level" not in report


def test_package_inspect_reports_tilelang_as_extension_abi_target():
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    proc = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "inspect",
            "tilelang",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    report = json.loads(proc.stdout)
    assert report["name"] == "tilelang"
    assert report["package_level"] == "c_extension_abi"
    assert "TileLang" in report["package_summary"]
    assert "numpy_level" not in report


def test_package_inspect_reports_vllm_metal_as_extension_abi_target():
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    proc = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "inspect",
            "vllm-metal",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    report = json.loads(proc.stdout)
    assert report["name"] == "vllm-metal"
    assert report["package_level"] == "c_extension_abi"
    assert "Metal" in report["package_summary"]
    assert "numpy_level" not in report


def test_scientific_package_support_has_no_runtime_or_codegen_special_case():
    forbidden = (
        "PY_TYPE_" + "NUMPY_ARRAY",
        "Py" + "NumpyArrayObject",
        "py_" + "numpy",
        "numpy_" + "compat",
        "numpy_" + "smoke_plan",
        "pcc-" + "numpy-l5",
    )
    deleted_paths = (
        REPO / "pcc" / "runtime" / "src" / ("py_" + "numpy.c"),
        REPO / "pcc" / ("numpy_" + "compat.py"),
        REPO / "pcc" / ("numpy_" + "smoke_plan.py"),
    )
    for path in deleted_paths:
        assert not path.exists(), str(path)
    checked_roots = (
        REPO / "pcc" / "frontends" / "python",
        REPO / "pcc" / "runtime" / "src",
        REPO / "pcc" / "runtime" / "py",
    )
    for root in checked_roots:
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in {".py", ".c", ".h"}:
                text = path.read_text(errors="ignore")
                for token in forbidden:
                    assert token not in text, str(path)


def test_pcc1_package_inspect_does_not_need_host_python(tmp_path):
    pcc1 = _find_current_pcc1()
    if pcc1 is None:
        skip_or_fail_no_current_pcc1(
            "no current pcc1 binary with native package module shim"
        )
    pkg = tmp_path / "demo_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("x = 1\n", encoding="utf-8")
    (pkg / "mod.c").write_text("int demo(void) { return 1; }\n", encoding="utf-8")
    (pkg / "mod.hpp").write_text("#pragma once\n", encoding="utf-8")
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    proc = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package.inspect",
            "demo-pkg",
            "--path",
            str(pkg),
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    report = json.loads(proc.stdout)
    assert report["name"] == "demo-pkg"
    assert report["files"] == 3
    assert report["python_files"] == 1
    assert report["c_files"] == 1
    assert report["header_files"] == 1


def test_pcc1_package_inspect_accepts_positional_local_source_path(tmp_path):
    pcc1 = _find_current_pcc1()
    if pcc1 is None:
        skip_or_fail_no_current_pcc1(
            "no current pcc1 binary with native package module shim"
        )
    pkg = tmp_path / "demo_pkg-0.1"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("x = 1\n", encoding="utf-8")
    (pkg / "pyproject.toml").write_text(
        "[build-system]\nrequires = ['demo-backend']\nbuild-backend = 'demo.backend'\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    proc = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package.inspect",
            str(pkg),
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    report = json.loads(proc.stdout)
    assert report["name"] == "demo_pkg"
    assert report["path"] == str(pkg)
    assert report["files"] == 2
    assert report["artifact_metadata"]["source_kind"] == "local_source"
    assert report["artifact_metadata"]["meson_build"] is False


def test_pcc1_pip_dry_run_does_not_need_host_python():
    pcc1 = _find_current_pcc1()
    if pcc1 is None:
        skip_or_fail_no_current_pcc1(
            "no current pcc1 binary with native package module shim"
        )
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    proc = subprocess.run(
        [str(pcc1), "-m", "pip", "install", "requests", "--dry-run"],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    plan = json.loads(proc.stdout)
    assert plan["ok"] is True, proc.stdout
    assert plan["packages"] == ["requests"], proc.stdout
    inspection = plan["inspections"][0]
    assert inspection["name"] == "requests", proc.stdout
    assert inspection["artifact_metadata"]["name"] == "requests", proc.stdout
    assert inspection["package_level"] == "nolibpython_python", proc.stdout


def test_pcc1_package_inspect_reports_mlx_without_host_python():
    pcc1 = _find_current_pcc1()
    if pcc1 is None:
        skip_or_fail_no_current_pcc1(
            "no current pcc1 binary with native package module shim"
        )
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    proc = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package.inspect",
            "mlx",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    report = json.loads(proc.stdout)
    assert report["name"] == "mlx"
    assert report["package_level"] == "c_extension_abi"
    assert "MLX" in report["package_summary"]


def test_pcc1_package_inspect_reports_vllm_without_host_python():
    pcc1 = _find_current_pcc1()
    if pcc1 is None:
        skip_or_fail_no_current_pcc1(
            "no current pcc1 binary with native package module shim"
        )
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    proc = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package.inspect",
            "vllm",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    report = json.loads(proc.stdout)
    assert report["name"] == "vllm"
    assert report["package_level"] == "c_extension_abi"
    assert "vLLM" in report["package_summary"]


def test_pcc1_package_inspect_reports_tilelang_without_host_python():
    pcc1 = _find_current_pcc1()
    if pcc1 is None:
        skip_or_fail_no_current_pcc1(
            "no current pcc1 binary with native package module shim"
        )
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    proc = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package.inspect",
            "tilelang",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    report = json.loads(proc.stdout)
    assert report["name"] == "tilelang"
    assert report["package_level"] == "c_extension_abi"
    assert "TileLang" in report["package_summary"]


def test_pcc1_package_inspect_reports_vllm_metal_without_host_python():
    pcc1 = _find_current_pcc1()
    if pcc1 is None:
        skip_or_fail_no_current_pcc1(
            "no current pcc1 binary with native package module shim"
        )
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    proc = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package.inspect",
            "vllm-metal",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    report = json.loads(proc.stdout)
    assert report["name"] == "vllm-metal"
    assert report["package_level"] == "c_extension_abi"
    assert "Metal" in report["package_summary"]
