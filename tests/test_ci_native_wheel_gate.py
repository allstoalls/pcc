"""Installed-wheel qualification cannot silently select the source checkout."""

import json
import os
from pathlib import Path
import subprocess
import venv

import pytest

from scripts import ci_native_wheel_gate as gate


def test_install_environment_removes_checkout_and_pip_routes_without_mutation():
    original = {
        "PATH": "/system/bin",
        "SystemRoot": "C:\\Windows",
        "HOME": "/home/user",
        "PCC_CURRENT_PCC1": "/checkout/pcc1",
        "PCC_RUNTIME_ARCHIVE": "/checkout/runtime.a",
        "PCC_RUNTIME_DIR": "/checkout/runtime",
        "PYTHONPATH": "/checkout",
        "PYTHONHOME": "/other/python",
        "PIP_TARGET": "/outside/venv",
        "PIP_CONFIG_FILE": "/user/pip.conf",
        "VIRTUAL_ENV": "/outer/venv",
    }
    before = dict(original)
    assert gate._isolated_environment(original) == {
        "PATH": "/system/bin",
        "SystemRoot": "C:\\Windows",
        "HOME": "/home/user",
        "PYTHONNOUSERSITE": "1",
        "PIP_CONFIG_FILE": os.devnull,
    }
    assert original == before


@pytest.fixture
def installed_identity(tmp_path, monkeypatch):
    root = tmp_path / "venv"
    identity = {
        "package": str(root / "site-packages/pcc/__init__.py"),
        "target_module": str(root / "site-packages/pcc/driver/python_target.py"),
        "distribution_root": str(root / "site-packages"),
        "version": "1.2.3",
        "target": [3, 15, 0],
    }

    def run(command, *, cwd, env, timeout):
        assert command[:3] == [str(root / "python"), "-I", "-c"]
        assert cwd == tmp_path
        assert timeout == 30
        return json.dumps(identity)

    monkeypatch.setattr(gate, "_run", run)

    def probe():
        return gate._installed_identity(
            root / "python", root, cwd=tmp_path, env={}, wheel_version="1.2.3",
        )

    return identity, probe


def test_installed_identity_uses_installed_target(installed_identity):
    identity, probe = installed_identity
    identity["target"] = [3, 27, 4]
    assert probe() == (3, 27, 4)


@pytest.mark.parametrize("field", ["package", "target_module", "distribution_root"])
def test_installed_identity_rejects_checkout_or_external_modules(installed_identity, field):
    identity, probe = installed_identity
    identity[field] = str(Path(identity["distribution_root"]).parent.parent / "outside")
    with pytest.raises(RuntimeError, match="escaped its venv"):
        probe()


def test_installed_identity_rejects_distribution_version_mismatch(installed_identity):
    identity, probe = installed_identity
    identity["version"] = "1.2.2"
    with pytest.raises(RuntimeError, match="does not match wheel"):
        probe()


@pytest.mark.parametrize("target", [None, "3.15.0", [], [3, 15], [3, 15, 0, 0], [3, "15", 0], [3, True, 0], [3, -1, 0]])
def test_installed_identity_rejects_malformed_target(installed_identity, target):
    identity, probe = installed_identity
    identity["target"] = target
    with pytest.raises(RuntimeError, match="invalid installed Python target"):
        probe()


def test_real_isolated_probe_ignores_checkout_pythonpath(tmp_path):
    """Real interpreter/import proof; the fixture package is not a native wheel."""
    root = tmp_path / "venv"
    venv.EnvBuilder(with_pip=False).create(root)
    python = gate._installed_command(root, "python")
    purelib = Path(subprocess.check_output(
        [str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        text=True,
        timeout=30,
    ).strip())
    package = purelib / "pcc"
    driver = package / "driver"
    driver.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (driver / "__init__.py").write_text("")
    (driver / "python_target.py").write_text("PYTHON_TARGET_VERSION_INFO = (3, 27, 4)\n")
    metadata = purelib / "python_cc-1.2.3.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Metadata-Version: 2.1\nName: python-cc\nVersion: 1.2.3\n")
    checkout = tmp_path / "checkout"
    (checkout / "pcc").mkdir(parents=True)
    (checkout / "pcc/__init__.py").write_text("raise RuntimeError('checkout selected')\n")
    env = dict(os.environ, PYTHONPATH=str(checkout))
    assert gate._installed_identity(
        python, root, cwd=checkout, env=env, wheel_version="1.2.3",
    ) == (3, 27, 4)
