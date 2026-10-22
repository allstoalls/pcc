"""Level-2 pcc1 replacement gate for the frozen scientific/build corpus.

The pcc side builds unmodified NumPy 2.4.4 and simplejson 4.1.1 sources
offline with the current pcc1.  It then compiles one closed-world application
and executes that artifact under GC0..GC4 with host Python and libpython
unavailable.  CPython 3.15.0 is a separate behavioral oracle; its package site
is never exposed to the pcc compiler or the produced executable.

The required 30-minute resource envelope is a release gate.  This bounded test
records per-GC wall time, sampled RSS and runtime GC telemetry without claiming
that those short samples prove the long-run acceptance thresholds.
"""

from __future__ import annotations

import ast
import bz2
import gzip
import hashlib
import io
import json
import lzma
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import time
import warnings
import zipfile
import zlib

import pytest

from tests.python.pcc1_gate import find_current_pcc1


ROOT = Path(__file__).resolve().parents[2]
NUMPY_SOURCE = ROOT / "projects" / "numpy-2.4.4"
NUMPY_TREE_SHA256 = "3ab6d97b34440c2e5d02ed5458068533dfb72ac9372030cdd8daa0b55ce17525"
SIMPLEJSON_SDIST_SHA256 = (
    "c08eb9f7a90f77ae470e19a07472e9a79ebc0d1c2315d86a72767665bd5ba79f"
)
_TREE_IGNORED_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "build",
}
_TREE_IGNORED_FILES = {"uv.lock"}

pytestmark = [pytest.mark.integration, pytest.mark.pcc_gate(probe="pcc1")]


def _clean_tree_files(root: Path) -> list[Path]:
    files = [
        path
        for path in root.rglob("*")
        if path.is_file()
        and not any(part in _TREE_IGNORED_DIRS for part in path.relative_to(root).parts)
        and path.name not in _TREE_IGNORED_FILES
        and path.suffix not in (".pyc", ".pyo")
    ]
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def _tree_sha256(root: Path) -> str:
    """Hash the release source tree, excluding local build/tool state."""
    digest = hashlib.sha256()
    for path in _clean_tree_files(root):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_clean_tree(source: Path, destination: Path) -> None:
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(
            ".git",
            ".mypy_cache",
            ".pytest_cache",
            ".venv",
            "__pycache__",
            "*.pyc",
            "*.pyo",
            "build",
            "uv.lock",
        ),
    )


def _last_json_object(output: str) -> dict[str, object]:
    marker = '{"command": "install"'
    start = output.find(marker)
    assert start >= 0, "pcc1 install emitted no JSON report:\n" + output[-8000:]
    value = json.loads(output[start:])
    assert isinstance(value, dict)
    return value


def _deny_host_tools(root: Path) -> tuple[Path, Path]:
    deny = root / "deny-host-tools"
    deny.mkdir()
    log = root / "forbidden-host-command.log"
    body = (
        "#!/bin/sh\n"
        'printf "%s\\n" "$0 $*" >> "$PCC_FORBIDDEN_HOST_LOG"\n'
        "exit 97\n"
    )
    for name in (
        "pcc",
        "pip",
        "pip3",
        "python",
        "python3",
        "python3.13",
        "python3-config",
        "uv",
        "uvx",
    ):
        path = deny / name
        path.write_text(body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return deny, log


def _hostless_env(
    root: Path,
    *,
    runtime_archive: Path,
    package_site: Path | None = None,
) -> tuple[dict[str, str], Path]:
    deny, log = _deny_host_tools(root)
    env = os.environ.copy()
    for name in (
        "LC_ALL",
        "PYTHONHOME",
        "PYTHONPATH",
        "PCC_DATA_HOME",
        "PCC_PACKAGE_SITE",
    ):
        env.pop(name, None)
    env.update(
        {
            "HOME": str(root / "home"),
            "PCC_COMPAT_PYTHON": str(deny / "python3"),
            "PCC_FORBIDDEN_HOST_LOG": str(log),
            "PCC_HOST_PCC": str(deny / "pcc"),
            "PCC_HOST_PYTHON": str(deny / "python3"),
            "PCC_PACKAGE_BUILD_JOBS": "2",
            "PCC_PACKAGE_BUILD_TIMEOUT": "900",
            "PCC_REPO_ROOT": str(ROOT),
            "PCC_RUNTIME_ARCHIVE": str(runtime_archive),
            "PCC_RUNTIME_CC": "pcc",
            "PCC_RUNTIME_HIGH": "py",
            "PATH": str(deny) + os.pathsep + env.get("PATH", ""),
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": "",
        }
    )
    if package_site is not None:
        env["PCC_PACKAGE_SITE"] = str(package_site)
    return env, log


def _install_owned(
    pcc1: Path,
    source: Path,
    *,
    site: Path,
    cache: Path,
    env: dict[str, str],
    timeout: int,
) -> dict[str, object]:
    process = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--abi",
            "pcc-native",
            "--build=owned",
            "--target",
            str(site),
            "--cache-dir",
            str(cache),
            str(source),
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    report = _last_json_object(process.stdout)
    assert process.returncode == 0 and report.get("ok") is True, (
        process.stdout + process.stderr
    )
    installs = report.get("installs")
    assert isinstance(installs, list) and len(installs) == 1, report
    manifest = installs[0]
    assert isinstance(manifest, dict)
    assert manifest.get("manifest_schema") == "pcc.package-manifest.v1"
    assert manifest.get("abi_mode") == "pcc-native"
    assert manifest.get("install_success") is True
    assert manifest.get("no_libpython_runtime") is True
    assert manifest.get("links_libpython") is False
    assert manifest.get("uses_cpython_extension_abi") is False
    build = manifest.get("build_report")
    assert isinstance(build, dict) and build.get("ok") is True
    assert build.get("build_mode_requested") == "owned"
    assert build.get("build_ownership") in ("owned", "not-required")
    assert build.get("host_assisted") is False
    assert build.get("host_python") is None
    assert build.get("host_free_build_claim") is True
    return manifest


def _assert_owned_numpy_build(manifest: dict[str, object]) -> None:
    build = manifest["build_report"]
    assert isinstance(build, dict)
    assert build.get("build_backend") == "pcc-native-meson"
    assert build.get("build_ownership") == "owned"
    kinds = [action.get("kind") for action in build.get("actions", [])]
    assert kinds == [
        "owned_meson_compile",
        "owned_meson_setup",
        "owned_build_exec_compile",
        "owned_meson_target_replay",
    ]


def _assert_no_package_name_mechanism_branch() -> None:
    """Keep the mechanism generic without rejecting package-owned source."""
    roots = (
        ROOT / "pcc" / "package",
        ROOT / "pcc" / "frontends" / "python",
        ROOT / "pcc" / "runtime" / "py",
    )
    forbidden: list[str] = []
    for root in roots:
        for path in root.rglob("*.py"):
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", SyntaxWarning)
                    tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Compare):
                    continue
                operands = (node.left, *node.comparators)
                literals = {
                    value.value.lower()
                    for value in operands
                    if isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                }
                if literals & {"numpy", "simplejson"}:
                    forbidden.append(
                        f"{path.relative_to(ROOT)}:{node.lineno}:"
                        + ast.unparse(node)
                    )
    assert forbidden == [], "package-name mechanism branches:\n" + "\n".join(
        forbidden
    )


def _assert_no_libpython(path: Path) -> None:
    if sys.platform == "darwin":
        command = ["otool", "-L", str(path)]
    elif sys.platform.startswith("linux"):
        command = ["readelf", "-d", str(path)]
    else:
        pytest.fail("Level-2 replacement supports Darwin and Linux")
    result = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    lowered = (result.stdout + result.stderr).lower()
    assert "libpython" not in lowered
    assert "python.framework" not in lowered


def _write_archive_inputs(root: Path, payload: bytes) -> dict[str, Path]:
    paths = {
        "gzip_in": root / "oracle-input.gz",
        "bz2_in": root / "oracle-input.bz2",
        "lzma_in": root / "oracle-input.xz",
        "zip_in": root / "oracle-input.zip",
        "tar_in": root / "oracle-input.tar.gz",
        "gzip_out": root / "pcc-output.gz",
        "bz2_out": root / "pcc-output.bz2",
        "lzma_out": root / "pcc-output.xz",
        "zlib_out": root / "pcc-output.zlib",
    }
    paths["gzip_in"].write_bytes(gzip.compress(payload, mtime=0))
    paths["bz2_in"].write_bytes(bz2.compress(payload))
    paths["lzma_in"].write_bytes(lzma.compress(payload))
    with zipfile.ZipFile(paths["zip_in"], "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("payload/data.bin", payload)
    info = tarfile.TarInfo("payload/data.bin")
    info.size = len(payload)
    info.mtime = 0
    with tarfile.open(paths["tar_in"], "w:gz") as archive:
        archive.addfile(info, io.BytesIO(payload))
    return paths


def _workload_source(paths: dict[str, Path], *, telemetry: bool) -> str:
    literals = {name: repr(str(path)) for name, path in paths.items()}
    source = f'''\
import bz2
import gc
import gzip
import lzma
import simplejson
import simplejson.decoder
import simplejson.encoder
import simplejson.scanner
import tarfile
import zipfile
import zlib
import numpy as np

payload = (b"level2-scientific-archive-payload\\n" * 257) + bytes(range(64))
with gzip.open({literals["gzip_in"]}, "rb") as stream:
    assert stream.read() == payload
with bz2.open({literals["bz2_in"]}, "rb") as stream:
    assert stream.read() == payload
with lzma.open({literals["lzma_in"]}, "rb") as stream:
    assert stream.read() == payload
with zipfile.ZipFile({literals["zip_in"]}, "r") as archive:
    zip_names = archive.namelist()
    zip_payload = archive.read("payload/data.bin")
with tarfile.open({literals["tar_in"]}, "r:gz") as archive:
    tar_names = archive.getnames()
    tar_payload = archive.extractfile("payload/data.bin").read()
assert zip_payload == payload
assert tar_payload == payload

with gzip.open({literals["gzip_out"]}, "wb", compresslevel=6) as stream:
    assert stream.write(payload[:333]) == 333
    stream.flush()
    assert stream.write(payload[333:]) == len(payload) - 333
with bz2.open({literals["bz2_out"]}, "wb", compresslevel=7) as stream:
    assert stream.write(payload[:511]) == 511
    stream.flush()
    assert stream.write(payload[511:]) == len(payload) - 511
with lzma.open({literals["lzma_out"]}, "wb", preset=4) as stream:
    assert stream.write(payload[:777]) == 777
    stream.flush()
    assert stream.write(payload[777:]) == len(payload) - 777
compressor = zlib.compressobj(6)
with open({literals["zlib_out"]}, "wb") as stream:
    stream.write(compressor.compress(payload[:1024]))
    stream.write(compressor.compress(payload[1024:]))
    stream.write(compressor.flush())

last = None
for index in range(32):
    matrix = np.array([[1, 2], [3, 4]])
    shifted = matrix + np.array([10, 20])
    product = matrix @ np.array([[2], [1]])
    record = {{
        "broadcast": shifted.tolist(),
        "index": int(matrix[1, 0]),
        "matmul": product.tolist(),
        "serialized": matrix.tobytes().hex(),
        "sum": int(matrix.sum()),
    }}
    encoded = simplejson.dumps(record, separators=(",", ":"), sort_keys=True)
    assert simplejson.loads(encoded) == record
    last = encoded
    del matrix, shifted, product, record, encoded
    if index % 4 == 0:
        gc.collect()
gc.collect()
native = (
    simplejson.scanner.c_make_scanner is not None
    and simplejson.decoder.c_scanstring is not None
    and simplejson.encoder.c_make_encoder is not None
)
result = {{
    "archives": [zip_names, tar_names],
    "native_simplejson": native,
    "numpy": np.__version__,
    "payload_size": len(payload),
    "scientific": simplejson.loads(last),
    "simplejson": simplejson.__version__,
}}
print("RESULT " + simplejson.dumps(result, separators=(",", ":"), sort_keys=True))
'''
    if telemetry:
        source += '''\
from pcc.unsafe import c_int64, extern
pcc_gc_telemetry = extern("pcc_gc_telemetry", (c_int64,), c_int64)
print(
    "TELEMETRY",
    pcc_gc_telemetry(0),
    pcc_gc_telemetry(4),
    pcc_gc_telemetry(7),
    pcc_gc_telemetry(32),
    pcc_gc_telemetry(38),
    pcc_gc_telemetry(39),
)
'''
    return source


def _sample_rss_kib(pid: int) -> int:
    sample = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(pid)],
        text=True,
        capture_output=True,
        timeout=5,
    )
    if sample.returncode != 0 or not sample.stdout.strip():
        return 0
    try:
        return int(sample.stdout.strip().splitlines()[0])
    except ValueError:
        return 0


def _run_measured(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    peak_rss_kib = 0
    try:
        while process.poll() is None:
            if time.monotonic() - started >= timeout:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
                raise AssertionError(
                    f"timed out after {timeout:.1f}s: {' '.join(command)}"
                )
            peak_rss_kib = max(peak_rss_kib, _sample_rss_kib(process.pid))
            time.sleep(0.02)
        stdout, stderr = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
    elapsed = time.monotonic() - started
    completed = subprocess.CompletedProcess(
        command,
        int(process.returncode),
        stdout,
        stderr,
    )
    return completed, {
        "elapsed_seconds": round(elapsed, 6),
        "rss_peak_bytes": peak_rss_kib * 1024,
        "rss_sampling": "ps-rss-kib",
    }


def _result_and_telemetry(output: str) -> tuple[dict[str, object], dict[str, int]]:
    result_lines = [line for line in output.splitlines() if line.startswith("RESULT ")]
    assert len(result_lines) == 1, output
    result = json.loads(result_lines[0][len("RESULT ") :])
    telemetry_lines = [
        line for line in output.splitlines() if line.startswith("TELEMETRY ")
    ]
    telemetry: dict[str, int] = {}
    if telemetry_lines:
        assert len(telemetry_lines) == 1, output
        values = [int(value) for value in telemetry_lines[0].split()[1:]]
        assert len(values) == 6
        telemetry = dict(
            zip(
                (
                    "allocations",
                    "pin_balance",
                    "max_pause_us",
                    "pause_count",
                    "scheduler_roots",
                    "frame_root_slots",
                ),
                values,
                strict=True,
            )
        )
    return result, telemetry


def _oracle_result(oracle: str, source: Path, oracle_site: str) -> dict[str, object]:
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONPATH"] = oracle_site
    version = subprocess.run(
        [oracle, "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert version.returncode == 0, version.stdout + version.stderr
    assert version.stdout.strip() == "3.15.0"
    probe = subprocess.run(
        [
            oracle,
            "-c",
            "import numpy,simplejson;print(numpy.__version__);print(simplejson.__version__)",
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert probe.returncode == 0, (
        "CPython 3.15.0 Level-2 oracle site must contain NumPy 2.4.4 and "
        "simplejson 4.1.1:\n" + probe.stdout + probe.stderr
    )
    assert probe.stdout.splitlines() == ["2.4.4", "4.1.1"]
    ran = subprocess.run(
        [oracle, str(source)],
        cwd=source.parent,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr
    result, telemetry = _result_and_telemetry(ran.stdout)
    assert telemetry == {}
    return result


def _write_report(path: Path, report: dict[str, object]) -> None:
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    path.write_text(payload, encoding="utf-8")
    requested = os.environ.get("PCC_LEVEL2_REPLACEMENT_REPORT")
    if requested:
        destination = Path(requested).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(payload, encoding="utf-8")


def test_current_pcc1_replaces_cpython_for_frozen_scientific_build_corpus(
    tmp_path: Path,
    pcc_runtime_archive: Path,
) -> None:
    assert NUMPY_SOURCE.is_dir(), f"missing pinned NumPy source: {NUMPY_SOURCE}"
    assert _tree_sha256(NUMPY_SOURCE) == NUMPY_TREE_SHA256
    simplejson_text = os.environ.get("PCC_SIMPLEJSON_411_SDIST", "").strip()
    assert simplejson_text, (
        "PCC_SIMPLEJSON_411_SDIST must name the offline canonical "
        "simplejson-4.1.1 sdist"
    )
    simplejson_sdist = Path(simplejson_text).expanduser().resolve()
    assert simplejson_sdist.is_file(), simplejson_sdist
    assert _file_sha256(simplejson_sdist) == SIMPLEJSON_SDIST_SHA256
    _assert_no_package_name_mechanism_branch()

    pcc1 = find_current_pcc1(ROOT)
    assert pcc1 is not None, "receipt-current pcc1 is required"
    numpy_copy = tmp_path / "numpy-2.4.4"
    _copy_clean_tree(NUMPY_SOURCE, numpy_copy)
    assert _tree_sha256(numpy_copy) == NUMPY_TREE_SHA256

    package_root = tmp_path / "packages"
    package_root.mkdir()
    site = package_root / "site"
    cache = package_root / "cache"
    install_env, forbidden_log = _hostless_env(
        package_root,
        runtime_archive=pcc_runtime_archive,
    )
    numpy_manifest = _install_owned(
        pcc1,
        numpy_copy,
        site=site,
        cache=cache,
        env=install_env,
        timeout=1500,
    )
    _assert_owned_numpy_build(numpy_manifest)
    assert numpy_manifest.get("linkage_native_package_claim") is True
    simplejson_manifest = _install_owned(
        pcc1,
        simplejson_sdist,
        site=site,
        cache=cache,
        env=install_env,
        timeout=600,
    )
    assert simplejson_manifest.get("linkage_native_package_claim") is True
    assert simplejson_manifest.get("artifact_sha256") == SIMPLEJSON_SDIST_SHA256
    if forbidden_log.exists():
        assert forbidden_log.read_text(encoding="utf-8") == ""
    assert list(site.glob("numpy/**/*.pcc*-pcc_native-*.so"))
    assert list(site.glob("simplejson/**/*.pcc*-pcc_native-*.so"))
    assert not list(site.glob("**/*.cpython-*.so"))

    payload = (b"level2-scientific-archive-payload\n" * 257) + bytes(range(64))
    paths = _write_archive_inputs(tmp_path, payload)
    pcc_source = tmp_path / "level2_pcc.py"
    pcc_source.write_text(_workload_source(paths, telemetry=True), encoding="utf-8")
    oracle_source = tmp_path / "level2_oracle.py"
    oracle_source.write_text(
        _workload_source(paths, telemetry=False), encoding="utf-8"
    )

    oracle = os.environ.get("PCC_CPYTHON_3132_ORACLE", sys.executable)
    oracle_site = os.environ.get("PCC_CPYTHON_3132_LEVEL2_SITE", "")
    expected = _oracle_result(oracle, oracle_source, oracle_site)

    compile_root = tmp_path / "compile"
    compile_root.mkdir()
    compile_env, compile_forbidden_log = _hostless_env(
        compile_root,
        runtime_archive=pcc_runtime_archive,
        package_site=site,
    )
    executable = compile_root / "level2-app"
    compiled = subprocess.run(
        [
            str(pcc1),
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(pcc_source),
            "-o",
            str(executable),
        ],
        cwd=ROOT,
        env=compile_env,
        text=True,
        capture_output=True,
        timeout=1200,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    assert executable.is_file() and os.access(executable, os.X_OK)
    if compile_forbidden_log.exists():
        assert compile_forbidden_log.read_text(encoding="utf-8") == ""
    _assert_no_libpython(executable)

    gc_reports: dict[str, object] = {}
    for backend in range(5):
        run_root = tmp_path / f"gc{backend}"
        run_root.mkdir()
        run_env, run_forbidden_log = _hostless_env(
            run_root,
            runtime_archive=pcc_runtime_archive,
        )
        run_env["PCC_GC_BACKEND"] = str(backend)
        ran, measurement = _run_measured(
            [str(executable)],
            cwd=run_root,
            env=run_env,
            timeout=180,
        )
        assert ran.returncode == 0, f"GC{backend}:\n{ran.stdout}\n{ran.stderr}"
        assert ran.stderr == "", f"GC{backend}: {ran.stderr}"
        actual, telemetry = _result_and_telemetry(ran.stdout)
        assert actual == expected
        assert telemetry["allocations"] > 0
        assert telemetry["pin_balance"] == 0
        assert telemetry["max_pause_us"] >= 0
        assert telemetry["pause_count"] >= 0
        if run_forbidden_log.exists():
            assert run_forbidden_log.read_text(encoding="utf-8") == ""
        assert gzip.decompress(paths["gzip_out"].read_bytes()) == payload
        assert bz2.decompress(paths["bz2_out"].read_bytes()) == payload
        assert lzma.decompress(paths["lzma_out"].read_bytes()) == payload
        assert zlib.decompress(paths["zlib_out"].read_bytes()) == payload
        gc_reports[str(backend)] = {
            **measurement,
            "returncode": ran.returncode,
            "telemetry": telemetry,
        }

    report = {
        "claim_mode": "pcc1/pcc-native/self/no-libpython",
        "cpython_oracle": "3.15.0",
        "gc": gc_reports,
        "numpy_tree_sha256": NUMPY_TREE_SHA256,
        "numpy_version": "2.4.4",
        "result": expected,
        "schema": "pcc.cpython-replacement.level2-report.v1",
        "simplejson_sdist_sha256": SIMPLEJSON_SDIST_SHA256,
        "simplejson_version": "4.1.1",
        "bounded_sample_only": True,
    }
    _write_report(tmp_path / "level2-replacement-report.json", report)
