"""PKG-P0-INSTALL-IMPORT-SEPARATION regression gate.

`pcc1 -m pip install` may fetch/place a real package or wheel, but install
success must NEVER imply import success, pcc-native ABI support, or
no-libpython package support. These tests pin the install manifest so that a
reader can tell those claims apart: install_success is recorded separately from
import_attempted / import_success (import is a distinct, un-run gate), and a
CPython-ABI wheel install records its wheel/ABI tags WITHOUT ever setting a
pcc-native package claim.

The install flow is exercised in-process via ``pcc.package.install`` (no real
pcc1 binary shell-out) with tiny wheel fixtures built in a tmp dir. The point is
the honesty of the manifest fields, not any single package.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from pcc.package.install import install_package
from pcc.package.schema import PACKAGE_MANIFEST_SCHEMA


def _write_pure_python_wheel(path: Path) -> Path:
    """A trivial pure-Python wheel: py3-none-any, only .py payloads."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("purepkg/__init__.py", "VALUE = 1\n")
        zf.writestr(
            "purepkg/core.py",
            "def answer() -> int:\n    return 42\n",
        )
        zf.writestr("purepkg-0.1.dist-info/METADATA", "Name: purepkg\n")
    return path


def _write_cpython_extension_wheel(path: Path) -> Path:
    """A wheel whose filename + payload declare a CPython extension ABI.

    The filename tag is ``cp313-cp313-...`` and the payload carries a
    ``.cpython-313-...so`` native module. The .so content is benign (no
    libpython string) so the only ABI signal is the CPython extension ABI name,
    which cpython-compat mode accepts but which must NOT read as pcc-native
    support.
    """
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("extpkg/__init__.py", "VALUE = 1\n")
        zf.writestr(
            "extpkg/_ext.cpython-313-x86_64-linux-gnu.so",
            "pcc-compat cpython extension placeholder\n",
        )
        zf.writestr("extpkg-0.1.dist-info/METADATA", "Name: extpkg\n")
    return path


def _write_pcc_native_like_extension_wheel(path: Path) -> Path:
    """A wheel with a native artifact that does not declare CPython ABI.

    This is not an import proof; it only gives ``linkage_report`` a real native
    artifact scan with no libpython / CPython-ABI edge, so the install manifest
    can keep install-vs-linkage claim fields distinct.
    """
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("nativepkg/__init__.py", "VALUE = 1\n")
        zf.writestr(
            "nativepkg/_native.pcc.so",
            "pcc-native extension placeholder without libpython edge\n",
        )
        zf.writestr("nativepkg-0.1.dist-info/METADATA", "Name: nativepkg\n")
    return path


def test_pcc1_pip_install_pure_py_fixture_records_install_without_import(tmp_path):
    wheel = _write_pure_python_wheel(tmp_path / "purepkg-0.1-py3-none-any.whl")

    result = install_package(
        str(wheel),
        target_dir=tmp_path / "site",
        cache_dir=tmp_path / "cache",
        abi="pcc-native",
    )

    # The install itself succeeded (files placed).
    assert result["install_success"] is True
    manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))

    # Install success is recorded, but import was neither attempted nor claimed.
    assert manifest["install_success"] is True
    assert manifest["import_attempted"] is False
    assert manifest["import_success"] is None
    # Install success is never a pcc-native package support claim.
    assert manifest["install_native_package_claim"] is False
    assert manifest["linkage_native_package_claim"] is False
    assert manifest["native_package_claim"] is False
    assert manifest["abi_mode"] == "pcc-native"
    assert manifest["manifest_schema"] == PACKAGE_MANIFEST_SCHEMA
    assert manifest["capability_profile"]["execution_mode"] == "pcc-native"

    # The payload was actually placed on disk (a real local install skeleton).
    assert (tmp_path / "site" / "purepkg" / "__init__.py").exists()


def test_pcc1_pip_install_cpython_extension_fixture_records_wheel_tags_without_native_claim(
    tmp_path,
):
    wheel = _write_cpython_extension_wheel(
        tmp_path / "extpkg-0.1-cp313-cp313-linux_x86_64.whl"
    )

    # cpython-compat mode accepts a CPython-ABI wheel; that acceptance must not
    # leak into a pcc-native support claim.
    result = install_package(
        str(wheel),
        target_dir=tmp_path / "site",
        cache_dir=tmp_path / "cache",
        abi="cpython-compat",
    )
    manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))

    # The wheel/ABI tags are recorded honestly...
    assert manifest["wheel_tags"]["python_tag"] == "cp313"
    assert manifest["wheel_tags"]["abi_tag"] == "cp313"
    assert manifest["wheel_tags"]["platform_tag"] == "linux_x86_64"
    # ...and the CPython extension ABI usage is surfaced by linkage.
    assert manifest["linkage"]["uses_cpython_extension_abi"] is True
    assert manifest["abi_mode"] == "cpython-compat"
    assert manifest["manifest_schema"] == PACKAGE_MANIFEST_SCHEMA
    assert manifest["capability_profile"]["execution_mode"] == "cpython-compat"

    # But a CPython-ABI (cpython-compat) install is NEVER a pcc-native claim,
    # and import was not attempted, so no import success is implied.
    assert manifest["native_package_claim"] is False
    assert manifest["install_native_package_claim"] is False
    assert manifest["linkage_native_package_claim"] is False
    assert manifest["import_attempted"] is False
    assert manifest["import_success"] is None


def test_install_manifest_separates_install_claim_from_linkage_claim(tmp_path):
    wheel = _write_pcc_native_like_extension_wheel(
        tmp_path / "nativepkg-0.1-py3-none-macosx.whl"
    )

    result = install_package(
        str(wheel),
        target_dir=tmp_path / "site",
        cache_dir=tmp_path / "cache",
        abi="pcc-native",
    )
    manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))

    assert manifest["install_success"] is True
    assert manifest["linkage"]["native_package_claim"] is True
    assert manifest["linkage_native_package_claim"] is True
    # Installing/scanning a compatible native artifact is still not an import
    # or package-support proof. The top-level install claim stays false.
    assert manifest["install_native_package_claim"] is False
    assert manifest["native_package_claim"] is False
    assert manifest["import_attempted"] is False
    assert manifest["import_success"] is None
