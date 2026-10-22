from __future__ import annotations

from contextlib import contextmanager
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import threading
import zipfile

import pytest

from pcc.package.acquire import acquire_requirement
from pcc.package.pip_shim import pip_install_plan


def _write_wheel(path: Path, *, requires: tuple[str, ...] = ()) -> Path:
    metadata = "Name: demo-pkg\nVersion: 1.2\n"
    for requirement in requires:
        metadata += f"Requires-Dist: {requirement}\n"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("demo_pkg/__init__.py", "VALUE = 42\n")
        zf.writestr("demo_pkg-1.2.dist-info/METADATA", metadata)
    return path


@contextmanager
def _serve_directory(root: Path):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def log_message(self, format, *args):  # noqa: A002
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _write_simple_index(
    root: Path, wheel: Path, *, digest: str, requires_python: str = ""
) -> None:
    project = root / "simple" / "demo-pkg"
    project.mkdir(parents=True)
    requires_attr = (
        ' data-requires-python="' + requires_python.replace(">", "&gt;") + '"'
        if requires_python
        else ""
    )
    (project / "index.html").write_text(
        '<a href="/packages/'
        + wheel.name
        + "#sha256="
        + digest
        + '"'
        + requires_attr
        + ">wheel</a>\n",
        encoding="utf-8",
    )


def test_owned_acquisition_downloads_and_verifies_without_host_python(
    tmp_path, monkeypatch
):
    index = tmp_path / "index"
    packages = index / "packages"
    packages.mkdir(parents=True)
    wheel = _write_wheel(packages / "demo_pkg-1.2-py3-none-any.whl")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    _write_simple_index(index, wheel, digest=digest)
    monkeypatch.setenv("PCC_HOST_PYTHON", "/definitely/not/available")

    with _serve_directory(index) as base_url:
        result = acquire_requirement(
            "demo_pkg==1.2",
            mode="owned",
            cache_dir=tmp_path / "cache",
            index_urls=[base_url + "/simple"],
        )

    assert result["ok"] is True, json.dumps(result, sort_keys=True)
    assert result["acquire_mode"] == "owned"
    assert result["host_assisted"] is False
    assert result["hash_verified"] is True
    assert result["sha256"] == digest
    assert result["resolved_version"] == "1.2"
    artifact = Path(str(result["artifact_path"]))
    assert artifact.read_bytes() == wheel.read_bytes()
    assert artifact.parent.name == digest


def test_owned_acquisition_rejects_missing_repository_hash(tmp_path):
    index = tmp_path / "index"
    packages = index / "packages"
    packages.mkdir(parents=True)
    wheel = _write_wheel(packages / "demo_pkg-1.2-py3-none-any.whl")
    _write_simple_index(index, wheel, digest="missing")

    with _serve_directory(index) as base_url:
        result = acquire_requirement(
            "demo_pkg",
            mode="owned",
            cache_dir=tmp_path / "cache",
            index_urls=[base_url + "/simple"],
        )

    assert result["ok"] is False
    assert result["diagnostic"] == "PCC-PKG-ACQUIRE-HASH-REQUIRED"


def test_owned_acquisition_respects_target_python_metadata(tmp_path):
    index = tmp_path / "index"
    packages = index / "packages"
    packages.mkdir(parents=True)
    compatible = _write_wheel(packages / "demo_pkg-1.2-py3-none-any.whl")
    incompatible = _write_wheel(packages / "demo_pkg-1.3-py3-none-any.whl")
    compatible_hash = hashlib.sha256(compatible.read_bytes()).hexdigest()
    incompatible_hash = hashlib.sha256(incompatible.read_bytes()).hexdigest()
    project = index / "simple" / "demo-pkg"
    project.mkdir(parents=True)
    (project / "index.html").write_text(
        '<a href="/packages/'
        + compatible.name
        + "#sha256="
        + compatible_hash
        + '" data-requires-python="&gt;=3.11">compatible</a>\n'
        + '<a href="/packages/'
        + incompatible.name
        + "#sha256="
        + incompatible_hash
        + '" data-requires-python="&gt;=3.12">incompatible</a>\n',
        encoding="utf-8",
    )

    with _serve_directory(index) as base_url:
        result = acquire_requirement(
            "demo_pkg",
            mode="owned",
            cache_dir=tmp_path / "cache",
            index_urls=[base_url + "/simple"],
            target_python="3.11",
        )

    assert result["ok"] is True, json.dumps(result, sort_keys=True)
    assert result["resolved_version"] == "1.2"
    assert result["target_python"] == "3.11"


@pytest.mark.parametrize(
    "configured, expected_version, expected_target",
    [
        (None, "1.3", "3.15"),
        ("3.11", "1.2", "3.11"),
    ],
)
def test_default_and_overridden_targets_select_requires_python_artifacts(
    tmp_path, monkeypatch, configured, expected_version, expected_target
):
    from pcc.driver import cli_bootstrap
    from pcc.package.acquire import requires_python_allows

    monkeypatch.delenv("PCC_PACKAGE_TARGET_PYTHON", raising=False)
    if configured is not None:
        monkeypatch.setenv("PCC_PACKAGE_TARGET_PYTHON", configured)
    index = tmp_path / "index"
    packages = index / "packages"
    packages.mkdir(parents=True)
    project = index / "simple/demo-pkg"
    project.mkdir(parents=True)
    links = []
    for version, requires in (("1.2", ">=3.11,<3.15"), ("1.3", ">=3.15,<3.16")):
        artifact = _write_wheel(
            packages / ("demo_pkg-" + version + "-py3-none-any.whl")
        )
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        links.append(
            '<a href="/packages/'
            + artifact.name
            + "#sha256="
            + digest
            + '" data-requires-python="'
            + requires.replace("<", "&lt;").replace(">", "&gt;")
            + '">artifact</a>\n'
        )
        expected = version == expected_version
        assert requires_python_allows(requires, expected_target) is expected
        assert (
            cli_bootstrap._native_requires_python_allows(requires, expected_target)
            is expected
        )
    (project / "index.html").write_text("".join(links), encoding="utf-8")
    with _serve_directory(index) as base_url:
        result = acquire_requirement(
            "demo_pkg",
            mode="owned",
            cache_dir=tmp_path / "cache",
            index_urls=[base_url + "/simple"],
        )
    assert result["ok"] is True, result
    assert result["resolved_version"] == expected_version
    assert result["target_python"] == expected_target


def test_owned_acquisition_fails_closed_on_runtime_dependency_resolution(tmp_path):
    index = tmp_path / "index"
    packages = index / "packages"
    packages.mkdir(parents=True)
    wheel = _write_wheel(
        packages / "demo_pkg-1.2-py3-none-any.whl", requires=("helper-pkg",)
    )
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    _write_simple_index(index, wheel, digest=digest)

    with _serve_directory(index) as base_url:
        result = acquire_requirement(
            "demo_pkg",
            mode="owned",
            cache_dir=tmp_path / "cache",
            index_urls=[base_url + "/simple"],
        )

    assert result["ok"] is False
    assert result["diagnostic"] == "PCC-PKG-ACQUIRE-DEPENDENCY-RESOLUTION-UNSUPPORTED"
    assert result["dependencies"] == ["helper-pkg"]
    assert result["hash_verified"] is True


def test_owned_acquisition_fails_closed_on_build_isolation(tmp_path):
    index = tmp_path / "index"
    packages = index / "packages"
    packages.mkdir(parents=True)
    tree = tmp_path / "demo_pkg-1.2"
    tree.mkdir()
    (tree / "pyproject.toml").write_text(
        "[build-system]\nrequires = ['setuptools']\nbuild-backend = 'setuptools.build_meta'\n",
        encoding="utf-8",
    )
    artifact = packages / "demo_pkg-1.2.tar.gz"
    with tarfile.open(artifact, "w:gz") as tf:
        tf.add(tree, arcname=tree.name)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    _write_simple_index(index, artifact, digest=digest)

    with _serve_directory(index) as base_url:
        result = acquire_requirement(
            "demo_pkg",
            mode="owned",
            cache_dir=tmp_path / "cache",
            index_urls=[base_url + "/simple"],
        )

    assert result["ok"] is False
    assert result["diagnostic"] == "PCC-PKG-ACQUIRE-BUILD-ISOLATION-UNSUPPORTED"
    assert result["hash_verified"] is True


def test_auto_owned_acquisition_delegates_supported_source_build_to_pcc(tmp_path):
    index = tmp_path / "index"
    packages = index / "packages"
    packages.mkdir(parents=True)
    tree = tmp_path / "demo_pkg-1.2"
    tree.mkdir()
    (tree / "pyproject.toml").write_text(
        "[build-system]\nrequires = ['setuptools']\n"
        "build-backend = 'setuptools.build_meta'\n",
        encoding="utf-8",
    )
    artifact = packages / "demo_pkg-1.2.tar.gz"
    with tarfile.open(artifact, "w:gz") as tf:
        tf.add(tree, arcname=tree.name)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    _write_simple_index(index, artifact, digest=digest)

    with _serve_directory(index) as base_url:
        result = acquire_requirement(
            "demo_pkg",
            mode="auto",
            cache_dir=tmp_path / "cache",
            index_urls=[base_url + "/simple"],
        )

    assert result["ok"] is True, json.dumps(result, sort_keys=True)
    assert result["acquire_mode_requested"] == "auto"
    assert result["acquire_mode"] == "owned"
    assert result["host_assisted"] is False
    assert result["hash_verified"] is True
    assert result["build_isolation"] == "delegated-to-pcc-native-builder"


def test_host_acquisition_is_labeled_and_publishes_immutable_artifact(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("PCC_PACKAGE_TARGET_PYTHON", raising=False)
    source = _write_wheel(tmp_path / "demo_pkg-1.2-py3-none-any.whl")
    fake_python = tmp_path / "fake-host-python"
    fake_python.write_text(
        "#!/usr/bin/env python3\n"
        "import os, shutil, sys\n"
        "dest = sys.argv[sys.argv.index('--dest') + 1]\n"
        "open(os.environ['PCC_TEST_ACQUIRE_ARGV'], 'w').write('\\n'.join(sys.argv))\n"
        "shutil.copy2(os.environ['PCC_TEST_ACQUIRE_ARTIFACT'], dest)\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    old = os.environ.get("PCC_TEST_ACQUIRE_ARTIFACT")
    old_argv = os.environ.get("PCC_TEST_ACQUIRE_ARGV")
    argv_path = tmp_path / "argv.txt"
    os.environ["PCC_TEST_ACQUIRE_ARTIFACT"] = str(source)
    os.environ["PCC_TEST_ACQUIRE_ARGV"] = str(argv_path)
    try:
        result = acquire_requirement(
            "demo_pkg",
            mode="host",
            cache_dir=tmp_path / "cache",
            index_urls=["https://example.invalid/simple"],
            host_python=str(fake_python),
            timeout=10,
        )
    finally:
        if old is None:
            os.environ.pop("PCC_TEST_ACQUIRE_ARTIFACT", None)
        else:
            os.environ["PCC_TEST_ACQUIRE_ARTIFACT"] = old
        if old_argv is None:
            os.environ.pop("PCC_TEST_ACQUIRE_ARGV", None)
        else:
            os.environ["PCC_TEST_ACQUIRE_ARGV"] = old_argv

    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    assert result["ok"] is True
    assert result["acquire_mode"] == "host"
    assert result["host_assisted"] is True
    assert result["hash_verified"] is False
    assert result["sha256"] == digest
    assert result["artifact_origin"] == "host-pip"
    assert result["target_python"] == "3.15"
    argv = argv_path.read_text(encoding="utf-8").splitlines()
    assert argv[argv.index("--python-version") + 1] == "3.15"
    assert "--no-binary=:all:" in argv
    assert Path(str(result["artifact_path"])).parent.name == digest


def test_host_acquisition_skips_path_python_without_pip(tmp_path, monkeypatch):
    source = _write_wheel(tmp_path / "demo_pkg-1.2-py3-none-any.whl")
    bad_dir = tmp_path / "bad"
    good_dir = tmp_path / "good"
    bad_dir.mkdir()
    good_dir.mkdir()
    bad = bad_dir / "python3"
    bad.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    bad.chmod(0o755)
    good = good_dir / "python3"
    good.write_text(
        "#!" + sys.executable + "\n"
        "import os, shutil, sys\n"
        "dest = sys.argv[sys.argv.index('--dest') + 1]\n"
        "shutil.copy2(os.environ['PCC_TEST_ACQUIRE_ARTIFACT'], dest)\n",
        encoding="utf-8",
    )
    good.chmod(0o755)
    monkeypatch.delenv("PCC_HOST_PYTHON", raising=False)
    monkeypatch.setenv("PCC_TEST_ACQUIRE_ARTIFACT", str(source))
    monkeypatch.setenv("PATH", os.pathsep.join((str(bad_dir), str(good_dir))))

    result = acquire_requirement(
        "demo_pkg",
        mode="host",
        cache_dir=tmp_path / "cache",
        index_urls=["https://example.invalid/simple"],
        timeout=10,
    )

    assert result["ok"] is True, json.dumps(result, sort_keys=True)
    assert result["host_python"] == str(good)


def test_pip_install_owned_acquires_then_installs_generic_package(tmp_path):
    index = tmp_path / "index"
    packages = index / "packages"
    packages.mkdir(parents=True)
    wheel = _write_wheel(packages / "demo_pkg-1.2-py3-none-any.whl")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    _write_simple_index(index, wheel, digest=digest)

    with _serve_directory(index) as base_url:
        plan = pip_install_plan(
            [
                "install",
                "demo_pkg==1.2",
                "--acquire=owned",
                "--index-url",
                base_url + "/simple",
                "--cache-dir",
                str(tmp_path / "cache"),
                "--target",
                str(tmp_path / "site"),
            ]
        )

    assert plan["ok"] is True, json.dumps(plan, sort_keys=True)
    assert plan["packages"] == ["demo_pkg==1.2"]
    assert plan["acquisitions"][0]["acquire_mode"] == "owned"
    assert plan["acquisitions"][0]["hash_verified"] is True
    assert plan["installs"][0]["install_success"] is True
    assert plan["installs"][0]["resolved_from"] == "index-url"
    assert (tmp_path / "site" / "demo_pkg" / "__init__.py").exists()


def test_pip_install_auto_selects_owned_acquisition(tmp_path, monkeypatch):
    index = tmp_path / "index"
    packages = index / "packages"
    packages.mkdir(parents=True)
    source = _write_wheel(packages / "demo_pkg-1.2-py3-none-any.whl")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    _write_simple_index(index, source, digest=digest)
    monkeypatch.setenv("PCC_HOST_PYTHON", "/definitely/not/available")

    with _serve_directory(index) as base_url:
        plan = pip_install_plan(
            [
                "install",
                "demo_pkg",
                "--index-url",
                base_url + "/simple",
                "--cache-dir",
                str(tmp_path / "cache"),
                "--target",
                str(tmp_path / "site"),
            ]
        )

    assert plan["ok"] is True, json.dumps(plan, sort_keys=True)
    assert plan["acquire_mode_requested"] == "auto"
    assert plan["acquisitions"][0]["acquire_mode"] == "owned"
    assert plan["acquisitions"][0]["host_assisted"] is False
    assert plan["acquisitions"][0]["hash_verified"] is True
    assert (tmp_path / "site" / "demo_pkg" / "__init__.py").exists()


def test_online_bare_name_is_not_shadowed_by_repository_projects(tmp_path, monkeypatch):
    index = tmp_path / "index"
    packages = index / "packages"
    packages.mkdir(parents=True)
    source = _write_wheel(packages / "numpy-1.2-py3-none-any.whl")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    project = index / "simple" / "numpy"
    project.mkdir(parents=True)
    (project / "index.html").write_text(
        f'<a href="/packages/{source.name}#sha256={digest}">numpy</a>\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("PCC_HOST_PYTHON", "/definitely/not/available")

    with _serve_directory(index) as base_url:
        plan = pip_install_plan(
            [
                "install",
                "numpy",
                "--index-url",
                base_url + "/simple",
                "--cache-dir",
                str(tmp_path / "cache"),
                "--target",
                str(tmp_path / "site"),
            ]
        )

    assert plan["ok"] is True, json.dumps(plan, sort_keys=True)
    assert plan["acquisitions"][0]["artifact_origin"] == "simple-repository"
    assert plan["installs"][0]["resolved_from"] == "index-url"
    assert (
        plan["installs"][0]["source_path"] == plan["acquisitions"][0]["artifact_path"]
    )


def test_acquisition_rejects_resolver_shapes_it_does_not_own(tmp_path):
    result = acquire_requirement(
        "demo_pkg>=1",
        mode="owned",
        cache_dir=tmp_path / "cache",
        index_urls=["https://example.invalid/simple"],
    )
    assert result["ok"] is False
    assert result["diagnostic"] == "PCC-PKG-ACQUIRE-REQUIREMENT-UNSUPPORTED"
    assert result["dependency_resolution"] == "not_attempted"


def test_acquisition_report_is_json_serializable(tmp_path):
    result = acquire_requirement(
        "demo_pkg", mode="offline", cache_dir=tmp_path / "cache"
    )
    assert result["diagnostic"] == "PCC-PKG-ACQUIRE-OFFLINE"
    json.dumps(result)


def test_acquisition_rejects_invalid_target_python(tmp_path):
    result = acquire_requirement(
        "demo_pkg",
        mode="owned",
        cache_dir=tmp_path / "cache",
        target_python="latest",
    )
    assert result["ok"] is False
    assert result["diagnostic"] == "PCC-PKG-ACQUIRE-TARGET-PYTHON-INVALID"


def test_self_backend_transport_and_sha256_kernel_primitives(tmp_path):
    served = tmp_path / "served"
    served.mkdir()
    payload = served / "payload.bin"
    payload.write_bytes(b"pcc-owned-acquisition\x00payload\n")
    expected = hashlib.sha256(payload.read_bytes()).hexdigest()
    empty = served / "empty.bin"
    empty.write_bytes(b"")
    empty_expected = hashlib.sha256(b"").hexdigest()
    multiblock = served / "multiblock.bin"
    multiblock.write_bytes(bytes(range(256)) * 257)
    multiblock_expected = hashlib.sha256(multiblock.read_bytes()).hexdigest()
    downloaded = tmp_path / "downloaded.bin"
    source = tmp_path / "probe.py"
    exe = tmp_path / "probe"

    with _serve_directory(served) as base_url:
        source.write_text(
            "import os\n"
            + "print(os._pcc_http_download_to_file("
            + repr(base_url + "/payload.bin")
            + ", "
            + repr(str(downloaded))
            + "))\n"
            + "print(os._pcc_sha256_file_hex("
            + repr(str(empty))
            + "))\n"
            + "print(os._pcc_sha256_file_hex("
            + repr(str(multiblock))
            + "))\n"
            + "print(os._pcc_sha256_file_hex("
            + repr(str(served / "missing.bin"))
            + "))\n"
            + "print(os._pcc_sha256_file_hex("
            + repr(str(downloaded))
            + "))\n"
            + "print(os._pcc_sha256_file_hex_bounded("
            + repr(str(multiblock))
            + ", "
            + repr(multiblock.stat().st_size)
            + "))\n"
            + "print(os._pcc_sha256_file_hex_bounded("
            + repr(str(multiblock))
            + ", "
            + repr(multiblock.stat().st_size - 1)
            + "))\n",
            encoding="utf-8",
        )
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pcc.driver.cli_launcher",
                "--backend",
                "self",
                "--python-libpython=off",
                "--ir-scaffold=on",
                str(source),
                "-o",
                str(exe),
            ],
            check=True,
            text=True,
            capture_output=True,
            timeout=120,
        )
        proc = subprocess.run(
            [str(exe)],
            check=True,
            text=True,
            capture_output=True,
            timeout=30,
        )

    assert proc.stdout.splitlines() == [
        "0",
        empty_expected,
        multiblock_expected,
        "",
        expected,
        multiblock_expected,
        "",
    ]
    assert downloaded.read_bytes() == payload.read_bytes()


def _network_test_pcc1() -> Path:
    raw = os.environ.get("PCC_ACQUIRE_TEST_PCC1")
    if not raw:
        pytest.fail("PCC_ACQUIRE_TEST_PCC1 must be a current pcc1 binary when this gate is selected")
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        pytest.fail(f"PCC_ACQUIRE_TEST_PCC1 does not exist: {path}")
    return path


@pytest.mark.pcc_gate(env="PCC_ACQUIRE_TEST_PCC1")
def test_pcc1_owned_acquisition_uses_native_simple_api_and_hash(tmp_path):
    pcc1 = _network_test_pcc1()
    index = tmp_path / "index"
    packages = index / "packages"
    packages.mkdir(parents=True)
    wheel = _write_wheel(packages / "demo_pkg-1.2-py3-none-any.whl")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    incompatible = _write_wheel(packages / "demo_pkg-1.3-py3-none-any.whl")
    incompatible_digest = hashlib.sha256(incompatible.read_bytes()).hexdigest()
    project = index / "simple" / "demo-pkg"
    project.mkdir(parents=True)
    (project / "index.html").write_text(
        '<a href="/packages/'
        + wheel.name
        + "#sha256="
        + digest
        + '" data-requires-python="&gt;=3.11">compatible</a>\n'
        + '<a href="/packages/'
        + incompatible.name
        + "#sha256="
        + incompatible_digest
        + '" data-requires-python="&gt;=3.12">incompatible</a>\n',
        encoding="utf-8",
    )
    env = os.environ.copy()
    # This case brackets the historical 3.11/3.12 selection boundary explicitly.
    env["PCC_PACKAGE_TARGET_PYTHON"] = "3.11"
    env["PCC_HOST_PYTHON"] = "/definitely/not/available"

    with _serve_directory(index) as base_url:
        proc = subprocess.run(
            [
                str(pcc1),
                "-m",
                "pip",
                "install",
                "demo_pkg",
                "--acquire=owned",
                "--index-url",
                base_url + "/simple",
                "--cache-dir",
                str(tmp_path / "cache"),
                "--target",
                str(tmp_path / "site"),
            ],
            check=False,
            text=True,
            capture_output=True,
            timeout=60,
            env=env,
        )

    assert proc.returncode == 0, proc.stderr + proc.stdout
    plan = json.loads(proc.stdout)
    acquisition = plan["acquisitions"][0]
    assert acquisition["acquire_mode"] == "owned"
    assert acquisition["host_assisted"] is False
    assert acquisition["hash_verified"] is True
    assert acquisition["sha256"] == digest
    assert acquisition["resolved_version"] == "1.2"
    assert acquisition["target_python"] == "3.11"
    assert (tmp_path / "site" / "demo_pkg" / "__init__.py").exists()


@pytest.mark.pcc_gate(env="PCC_ACQUIRE_TEST_PCC1")
def test_pcc1_auto_owned_acquires_and_native_pcc1_installs(tmp_path):
    pcc1 = _network_test_pcc1()
    index = tmp_path / "index"
    packages = index / "packages"
    packages.mkdir(parents=True)
    wheel = _write_wheel(packages / "demo_pkg-1.2-py3-none-any.whl")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    _write_simple_index(index, wheel, digest=digest)
    env = os.environ.copy()
    env["PCC_HOST_PYTHON"] = "/definitely/not/available"

    with _serve_directory(index) as base_url:
        proc = subprocess.run(
            [
                str(pcc1),
                "-m",
                "pip",
                "install",
                "demo_pkg",
                "--index-url",
                base_url + "/simple",
                "--cache-dir",
                str(tmp_path / "cache"),
                "--target",
                str(tmp_path / "site"),
            ],
            check=False,
            text=True,
            capture_output=True,
            timeout=60,
            env=env,
        )

    assert proc.returncode == 0, proc.stderr + proc.stdout
    plan = json.loads(proc.stdout)
    acquisition = plan["acquisitions"][0]
    assert acquisition["acquire_mode_requested"] == "auto"
    assert acquisition["acquire_mode"] == "owned"
    assert acquisition["host_assisted"] is False
    assert acquisition["hash_verified"] is True
    assert plan["installs"][0]["install_success"] is True
    assert (tmp_path / "site" / "demo_pkg" / "__init__.py").exists()
