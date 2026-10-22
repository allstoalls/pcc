"""Gate for PKG-P0-PCC1-COMPAT-RUNNER.

`pcc1 --python-libpython=auto -m <module>` is the compatibility runner:
cpython-compat, ecosystem-compatibility, pcc1-like-Python execution. It is NOT
a pcc-native / no-libpython package support claim. `--python-libpython=off`
(also the plain `-m` form) is the strict research default and must stay
unchanged (no manifest line).

This file has two layers:
  * fast, in-process unit tests over the pure helpers (no build, no pcc1);
  * the required binary gate that runs a real pcc1 and inspects stderr.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from pcc1_gate import find_current_pcc1, skip_or_fail_no_current_pcc1

import pcc.driver.cli_bootstrap as cb


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "AGENTS.md").exists():
            return parent
    # Fallback: tests/python/<file> -> repo root is two levels up.
    return here.parents[2]


REPO_ROOT = _repo_root()

# A native module handler that pcc1 supports, exits 0, and needs no network.
_MODULE = "pcc.package.inspect"
_MODULE_ARGS = ["mlx", "--json"]

_MANIFEST_PREFIX = "PCC1_COMPAT_RUNNER_MANIFEST:"
_EXPECTED_AUTO_JSON = (
    '{"requested_execution_mode": "cpython-compat", '
    '"execution_mode": "cpython-compat", "python_libpython_mode": "auto", '
    '"allows_libpython_fallback": true, '
    '"links_libpython": false, "native_package_claim": false}'
)
_EXPECTED_ON_JSON = (
    '{"requested_execution_mode": "cpython-compat", '
    '"execution_mode": "cpython-compat", "python_libpython_mode": "on", '
    '"allows_libpython_fallback": true, '
    '"links_libpython": false, "native_package_claim": false}'
)
_EXPECTED_OFF_JSON = (
    '{"requested_execution_mode": "pcc-native", '
    '"execution_mode": "pcc-native", "python_libpython_mode": "off", '
    '"allows_libpython_fallback": false, '
    '"links_libpython": false, "native_package_claim": false}'
)


# --------------------------------------------------------------------------
# Fast in-process unit tests (no build, no pcc1).
# --------------------------------------------------------------------------


def test_compat_runner_manifest_auto():
    assert cb.compat_runner_manifest("auto") == {
        "requested_execution_mode": "cpython-compat",
        "execution_mode": "cpython-compat",
        "python_libpython_mode": "auto",
        "allows_libpython_fallback": True,
        "links_libpython": False,
        "native_package_claim": False,
    }


def test_compat_runner_manifest_on_records_requested_mode():
    assert cb.compat_runner_manifest("on") == {
        "requested_execution_mode": "cpython-compat",
        "execution_mode": "cpython-compat",
        "python_libpython_mode": "on",
        "allows_libpython_fallback": True,
        "links_libpython": False,
        "native_package_claim": False,
    }


def test_compat_runner_manifest_off_is_pcc_native():
    assert cb.compat_runner_manifest("off") == {
        "requested_execution_mode": "pcc-native",
        "execution_mode": "pcc-native",
        "python_libpython_mode": "off",
        "allows_libpython_fallback": False,
        "links_libpython": False,
        "native_package_claim": False,
    }


def test_compat_runner_manifest_never_claims_native_package_support():
    for mode in ("off", "auto", "on"):
        assert cb.compat_runner_manifest(mode)["native_package_claim"] is False


def test_compat_runner_manifest_json_exact_strings():
    assert cb.compat_runner_manifest_json("auto") == _EXPECTED_AUTO_JSON
    assert cb.compat_runner_manifest_json("on") == _EXPECTED_ON_JSON
    assert cb.compat_runner_manifest_json("off") == _EXPECTED_OFF_JSON


def test_module_request_parse_plain_dash_m_defaults_off():
    is_req, mode, module_argv = cb._module_request_libpython_mode(
        ["-m", "pcc.package.inspect", "mlx"]
    )
    assert is_req is True
    assert mode == "off"
    assert module_argv == ["-m", "pcc.package.inspect", "mlx"]


def test_module_request_parse_flag_equals_form():
    is_req, mode, module_argv = cb._module_request_libpython_mode(
        ["--python-libpython=auto", "-m", "pcc.package.inspect", "mlx"]
    )
    assert is_req is True
    assert mode == "auto"
    assert module_argv == ["-m", "pcc.package.inspect", "mlx"]


def test_module_request_parse_flag_space_form():
    is_req, mode, module_argv = cb._module_request_libpython_mode(
        ["--python-libpython", "on", "-m", "pcc.package.inspect"]
    )
    assert is_req is True
    assert mode == "on"
    assert module_argv == ["-m", "pcc.package.inspect"]


def test_module_request_parse_off_form_is_module_but_off():
    is_req, mode, module_argv = cb._module_request_libpython_mode(
        ["--python-libpython=off", "-m", "pcc.package.inspect"]
    )
    assert is_req is True
    assert mode == "off"
    assert module_argv == ["-m", "pcc.package.inspect"]


def test_module_request_parse_non_module_is_not_a_request():
    # A compile invocation (no -m) must not be treated as a module run.
    is_req, mode, module_argv = cb._module_request_libpython_mode(
        ["--python-libpython=auto", "pcc/__main__.py", "-o", "out"]
    )
    assert is_req is False
    assert mode == "off"
    assert module_argv == []


def test_module_request_parse_unknown_mode_is_not_a_request():
    is_req, mode, module_argv = cb._module_request_libpython_mode(
        ["--python-libpython=weird", "-m", "pcc.package.inspect"]
    )
    assert is_req is False
    assert mode == "off"
    assert module_argv == []


# --------------------------------------------------------------------------
# Required binary gate.
# --------------------------------------------------------------------------


def _pcc1_env() -> dict:
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    # The dedicated compatibility owner wins over unrelated host-query config.
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    env["PCC_COMPAT_PYTHON"] = sys.executable
    return env


def _manifest_lines(stderr: str) -> list:
    return [
        line
        for line in stderr.splitlines()
        if line.startswith(_MANIFEST_PREFIX)
    ]


def test_pcc1_python_libpython_auto_module_runs_through_compatibility_path(
    tmp_path: Path,
):
    pcc1 = find_current_pcc1(REPO_ROOT)
    if pcc1 is None:
        skip_or_fail_no_current_pcc1(
            "no current pcc1 binary for the compat-runner gate"
        )

    env = _pcc1_env()

    # A generic CPython module (including a C-extension import) proves this is
    # no longer the pcc1 native package-handler path.
    probe_module = "pcc_compat_runner_probe"
    marker = tmp_path / "compat-marker.txt"
    (tmp_path / (probe_module + ".py")).write_text(
        "import math\n"
        "import pathlib\n"
        "import sys\n"
        "pathlib.Path(sys.argv[1]).write_text("
        "sys.implementation.name + ':' + math.__name__)\n",
        encoding="utf-8",
    )
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(tmp_path)
    if existing_pythonpath:
        env["PYTHONPATH"] += os.pathsep + existing_pythonpath

    # auto: emits exactly one compat-runner manifest line, then runs the module.
    auto = subprocess.run(
        [
            str(pcc1),
            "--python-libpython=auto",
            "-m",
            probe_module,
            str(marker),
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    auto_lines = _manifest_lines(auto.stderr)
    assert len(auto_lines) == 1, (
        "expected exactly one compat-runner manifest line on stderr; got: "
        + repr(auto.stderr)
    )
    manifest_line = auto_lines[0]
    assert manifest_line == _MANIFEST_PREFIX + " " + _EXPECTED_AUTO_JSON
    assert '"requested_execution_mode": "cpython-compat"' in manifest_line
    assert '"execution_mode": "cpython-compat"' in manifest_line
    assert '"python_libpython_mode": "auto"' in manifest_line
    assert '"allows_libpython_fallback": true' in manifest_line
    assert '"links_libpython": false' in manifest_line
    assert '"native_package_claim": false' in manifest_line
    assert marker.read_text(encoding="utf-8") == "cpython:math"

    # off: strict research default, unchanged -- no manifest line at all.
    off = subprocess.run(
        [str(pcc1), "--python-libpython=off", "-m", _MODULE] + _MODULE_ARGS,
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert _manifest_lines(off.stderr) == [], (
        "strict --python-libpython=off must not emit the compat manifest; got: "
        + repr(off.stderr)
    )

    # plain -m (no flag): also strict default, unchanged -- no manifest line.
    plain = subprocess.run(
        [str(pcc1), "-m", _MODULE] + _MODULE_ARGS,
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert _manifest_lines(plain.stderr) == [], (
        "plain `-m` must not emit the compat manifest; got: " + repr(plain.stderr)
    )
