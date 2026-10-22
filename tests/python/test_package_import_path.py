from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import zipfile
from pathlib import Path

import pytest

from process_timeout import run_process_tree_timeout

from pcc1_gate import (
    find_current_pcc1,
    require_current_pcc1_enabled,
)

from pcc.package.install import install_package
from pcc.package.metadata import current_platform_tag
from pcc.frontends.python.pipeline import (
    PyPipelineError,
    _collect_relative_module_closure,
    compile_python,
)


def _repo_root_from_here() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pcc" / "__main__.py").exists() and (parent / "tests").exists():
            return parent
    return here.parents[2]


REPO = _repo_root_from_here()
DEFAULT_NUMPY_ARTIFACT = REPO / "projects" / "numpy-2.4.4"


def _find_current_pcc1() -> Path | None:
    return find_current_pcc1(REPO)


def _assert_skipped_with_reason(reason: str, *tokens: str) -> None:
    assert reason.startswith("SKIPPED_WITH_REASON:")
    lowered = reason.lower()
    for token in tokens:
        assert token.lower() in lowered


def _resolve_real_artifact(
    env_name: str,
    *,
    default: Path | None = None,
) -> Path | None:
    raw = os.environ.get(env_name)
    if raw:
        return Path(raw).expanduser().resolve()
    if default is not None and default.exists():
        return default.resolve()
    return None


def _real_package_gate_or_reason(
    *,
    package: str,
    run_env: str,
    artifact_env: str,
    default_artifact: Path | None = None,
) -> tuple[Path, Path] | str:
    artifact = _resolve_real_artifact(artifact_env, default=default_artifact)
    pcc1 = _find_current_pcc1()
    reasons: list[str] = []
    if os.environ.get(run_env) != "1":
        reasons.append(f"{run_env}=1 is not set")
    if artifact is None:
        if default_artifact is not None:
            reasons.append(
                f"{artifact_env} does not name a {package} artifact; "
                f"default={default_artifact} exists={default_artifact.exists()}"
            )
        else:
            reasons.append(f"{artifact_env} does not name a {package} artifact")
    elif not artifact.exists():
        reasons.append(f"{artifact_env} path does not exist: {artifact}")
    if pcc1 is None:
        if require_current_pcc1_enabled():
            pytest.fail(
                "no current pcc1 binary with native package install shim; "
                "PCC_REQUIRE_CURRENT_PCC1=1 makes pcc1 package parity a hard gate"
            )
        reasons.append("no current pcc1 binary with native package install shim")
    if reasons:
        return "SKIPPED_WITH_REASON: " + "; ".join(reasons)
    assert pcc1 is not None
    assert artifact is not None
    return pcc1, artifact


def _pcc1_package_gate_or_reason(reason: str) -> Path | str:
    pcc1 = _find_current_pcc1()
    if pcc1 is not None:
        return pcc1
    if require_current_pcc1_enabled():
        pytest.fail(
            reason
            + "; PCC_REQUIRE_CURRENT_PCC1=1 makes pcc1 package parity a hard gate"
        )
    return "SKIPPED_WITH_REASON: " + reason


def _current_pcc1_or_verdict(reason: str) -> Path | None:
    gate = _pcc1_package_gate_or_reason(reason)
    if isinstance(gate, str):
        _assert_skipped_with_reason(gate)
        return None
    return gate


def _write_importable_project(root: Path) -> Path:
    root.mkdir()
    pkg = root / "demo_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (pkg / "core.py").write_text(
        "def answer() -> int:\n" "    return 42\n",
        encoding="utf-8",
    )
    return root


def _write_import_main(path: Path) -> None:
    path.write_text(
        "from demo_pkg.core import answer\n"
        "def main():\n"
        "    print(answer())\n"
        "main()\n",
        encoding="utf-8",
    )


def _write_import_package_main(path: Path) -> None:
    path.write_text(
        "import demo_pkg\n"
        "from demo_pkg.core import answer\n"
        "def main():\n"
        "    print(demo_pkg.VALUE + answer())\n"
        "main()\n",
        encoding="utf-8",
    )


def _assert_compiled_import_prints_42(main: Path, exe: Path) -> None:
    compile_python(
        str(main),
        str(exe),
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    proc = subprocess.run(
        [str(exe)],
        check=True,
        text=True,
        capture_output=True,
        timeout=180,
        env=env,
    )
    assert proc.stdout.strip() == "42"


def _assert_pcc_cli_import_prints(
    main: Path, exe: Path, site: Path, expected: str
) -> None:
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = str(site)
    subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(main),
            "-o",
            str(exe),
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=300,
        env=env,
    )
    proc = subprocess.run(
        [str(exe)],
        check=True,
        text=True,
        capture_output=True,
        timeout=180,
        env=env,
    )
    assert proc.stdout.strip() == expected


def _write_importable_wheel(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("demo_pkg/__init__.py", "VALUE = 1\n")
        zf.writestr(
            "demo_pkg/core.py",
            "def answer() -> int:\n" "    return 42\n",
        )
        zf.writestr("demo_pkg-0.1.dist-info/METADATA", "Name: demo-pkg\n")
    return path


def _write_numpy_command_shape_wheel(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("numpy/__init__.py", "VALUE = 3\n")
        zf.writestr(
            "numpy/core.py",
            "def answer() -> int:\n" "    return 39\n",
        )
        zf.writestr("numpy-2.4.4.dist-info/METADATA", "Name: numpy\nVersion: 2.4.4\n")
        zf.writestr(
            "numpy-2.4.4.dist-info/WHEEL",
            "Wheel-Version: 1.0\n"
            "Generator: pcc-test\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n",
        )
    return path


def _write_mlx_command_shape_wheel(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mlx/__init__.py", "VALUE = 5\n")
        zf.writestr(
            "mlx/core.py",
            "def answer() -> int:\n" "    return 37\n",
        )
        zf.writestr("mlx-0.0.0.dist-info/METADATA", "Name: mlx\nVersion: 0.0.0\n")
        zf.writestr(
            "mlx-0.0.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\n"
            "Generator: pcc-test\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n",
        )
    return path


def _write_vllm_command_shape_wheel(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("vllm/__init__.py", "VALUE = 7\n")
        zf.writestr(
            "vllm/entrypoints.py",
            "def answer() -> int:\n" "    return 35\n",
        )
        zf.writestr("vllm-0.0.0.dist-info/METADATA", "Name: vllm\nVersion: 0.0.0\n")
        zf.writestr(
            "vllm-0.0.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\n"
            "Generator: pcc-test\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n",
        )
    return path


def _write_tilelang_command_shape_wheel(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("tilelang/__init__.py", "VALUE = 11\n")
        zf.writestr(
            "tilelang/language.py",
            "def kernel_marker() -> str:\n" "    return 'tilelang'\n",
        )
        zf.writestr(
            "tilelang-0.0.0.dist-info/METADATA",
            "Name: tilelang\nVersion: 0.0.0\n",
        )
        zf.writestr(
            "tilelang-0.0.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\n"
            "Generator: pcc-test\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n",
        )
    return path


def _write_vllm_metal_command_shape_wheel(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("vllm_metal/__init__.py", "READY = True\n")
        zf.writestr(
            "vllm_metal/engine.py",
            "def backend() -> str:\n" "    return 'metal'\n",
        )
        zf.writestr("vllm_metal/kernels/prefill.metallib", b"metallib marker\n")
        zf.writestr(
            "vllm_metal-0.0.0.dist-info/METADATA",
            "Name: vllm-metal\nVersion: 0.0.0\nRequires-Dist: mlx\n",
        )
        zf.writestr(
            "vllm_metal-0.0.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\n"
            "Generator: pcc-test\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n",
        )
    return path


def _write_mlx_namespace_extension_tree(root: Path) -> Path:
    root.mkdir()
    pkg = root / "mlx"
    pkg.mkdir()
    (pkg / "__main__.py").write_text("VALUE = 5\n", encoding="utf-8")
    (pkg / "_os_warning.py").write_text("WARNING = None\n", encoding="utf-8")
    (pkg / "core.cpython-313-darwin.so").write_text(
        "native extension marker\n", encoding="utf-8"
    )
    core = pkg / "core"
    core.mkdir()
    (core / "__init__.pyi").write_text("def array(x): ...\n", encoding="utf-8")
    dist_info = root / "mlx-0.0.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text("Name: mlx\nVersion: 0.0.0\n", encoding="utf-8")
    return root


def _write_vllm_extension_tree(root: Path) -> Path:
    root.mkdir()
    pkg = root / "vllm"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("VALUE = 7\n", encoding="utf-8")
    (pkg / "entrypoints.py").write_text("READY = True\n", encoding="utf-8")
    (pkg / "_C.cpython-313-darwin.so").write_text(
        "native extension marker\n", encoding="utf-8"
    )
    dist_info = root / "vllm-0.0.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Name: vllm\nVersion: 0.0.0\n", encoding="utf-8"
    )
    return root


def _write_tilelang_extension_tree(root: Path) -> Path:
    # The Requires-Dist lines are metadata for diagnostics. pcc1's pip also
    # resolves names against <cwd>/projects, where the repository vendors
    # numpy, so installs of this tree run from tmp_path: from the repo root
    # they would build numpy from source first.
    root.mkdir()
    pkg = root / "tilelang"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("VALUE = 11\n", encoding="utf-8")
    (pkg / "language.py").write_text("KERNEL = 'tilelang'\n", encoding="utf-8")
    (pkg / "_ffi.cpython-313-darwin.so").write_text(
        "native extension marker\n", encoding="utf-8"
    )
    dist_info = root / "tilelang-0.0.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Name: tilelang\n"
        "Version: 0.0.0\n"
        "Requires-Dist: numpy\n"
        "Requires-Dist: torch\n"
        "Requires-Dist: apache-tvm-ffi\n",
        encoding="utf-8",
    )
    return root


def _write_vllm_metal_extension_tree(root: Path) -> Path:
    root.mkdir()
    pkg = root / "vllm_metal"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("READY = True\n", encoding="utf-8")
    (pkg / "engine.py").write_text("BACKEND = 'metal'\n", encoding="utf-8")
    (pkg / "_metal.cpython-313-darwin.so").write_text(
        "native extension marker\n", encoding="utf-8"
    )
    kernels = pkg / "kernels"
    kernels.mkdir()
    (kernels / "prefill.metallib").write_bytes(b"metallib marker\n")
    dist_info = root / "vllm_metal-0.0.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Name: vllm-metal\nVersion: 0.0.0\nRequires-Dist: mlx\n",
        encoding="utf-8",
    )
    return root


def _real_mlx_install_command(
    pcc1: Path,
    artifact_path: Path,
    site: Path,
    cache: Path,
) -> list[str]:
    if artifact_path.is_dir() and (artifact_path / "mlx").is_dir():
        return [
            str(pcc1),
            "-m",
            "pip",
            "install",
            str(artifact_path),
            "--abi",
            "cpython-compat",
            "--target",
            str(site),
            "--cache-dir",
            str(cache),
            "--json",
        ]
    find_links = artifact_path.parent if artifact_path.is_file() else artifact_path
    return [
        str(pcc1),
        "-m",
        "pip",
        "install",
        "mlx",
        "--no-index",
        "--find-links",
        str(find_links),
        "--abi",
        "cpython-compat",
        "--target",
        str(site),
        "--cache-dir",
        str(cache),
        "--json",
    ]


def _real_vllm_metal_install_command(
    pcc1: Path,
    artifact_path: Path,
    site: Path,
    cache: Path,
) -> list[str]:
    return [
        str(pcc1),
        "-m",
        "pip",
        "install",
        str(artifact_path),
        "--abi",
        "cpython-compat",
        "--target",
        str(site),
        "--cache-dir",
        str(cache),
        "--json",
    ]


def _write_real_cpython_extension_wheel(tmp_path: Path) -> Path:
    cc = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
    if cc is None:
        pytest.fail("C compiler required for real CPython extension wheel tracer")
    include = sysconfig.get_paths().get("include")
    if not include or not Path(include).is_dir():
        pytest.fail(
            "Python include directory required for real CPython extension wheel tracer"
        )
    ext_suffix = sysconfig.get_config_var("EXT_SUFFIX")
    if not ext_suffix:
        pytest.fail(
            "Python EXT_SUFFIX required for real CPython extension wheel tracer"
        )

    build = tmp_path / "real_ext_build"
    build.mkdir()
    src = build / "_native.c"
    src.write_text(
        "#include <Python.h>\n"
        "static PyObject *answer(PyObject *self, PyObject *args) {\n"
        "    return PyLong_FromLong(7);\n"
        "}\n"
        "static PyMethodDef Methods[] = {\n"
        '    {"answer", answer, METH_NOARGS, ""},\n'
        "    {NULL, NULL, 0, NULL},\n"
        "};\n"
        "static struct PyModuleDef Module = {\n"
        '    PyModuleDef_HEAD_INIT, "_native", NULL, -1, Methods,\n'
        "};\n"
        "PyMODINIT_FUNC PyInit__native(void) {\n"
        "    return PyModule_Create(&Module);\n"
        "}\n",
        encoding="utf-8",
    )
    ext = build / ("_native" + ext_suffix)
    if sys.platform == "darwin":
        cmd = [
            cc,
            "-bundle",
            "-undefined",
            "dynamic_lookup",
            "-I",
            include,
            str(src),
            "-o",
            str(ext),
        ]
    else:
        cmd = [cc, "-shared", "-fPIC", "-I", include, str(src), "-o", str(ext)]
    subprocess.run(cmd, check=True, text=True, capture_output=True, timeout=60)

    cp_tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    wheel = (
        tmp_path / f"real_ext_pkg-0.1-{cp_tag}-{cp_tag}-{current_platform_tag()}.whl"
    )
    with zipfile.ZipFile(wheel, "w") as zf:
        zf.writestr(
            "real_ext_pkg/__init__.py",
            "from . import _native\nfrom .core import answer\n",
        )
        zf.writestr(
            "real_ext_pkg/core.py",
            "def answer() -> int:\n" "    return 42\n",
        )
        zf.write(ext, "real_ext_pkg/" + ext.name)
        zf.writestr(
            "real_ext_pkg-0.1.dist-info/METADATA", "Name: real-ext-pkg\nVersion: 0.1\n"
        )
        zf.writestr(
            "real_ext_pkg-0.1.dist-info/WHEEL",
            "Wheel-Version: 1.0\n"
            "Generator: pcc-test\n"
            "Root-Is-Purelib: false\n"
            f"Tag: {cp_tag}-{cp_tag}-{current_platform_tag()}\n",
        )
    return wheel


def _write_importable_sdist(path: Path, src_root: Path) -> Path:
    src_root.mkdir()
    project = _write_importable_project(src_root / "demo_pkg-0.1")
    with tarfile.open(path, "w:gz") as tf:
        tf.add(project, arcname="demo_pkg-0.1")
    return path


def test_package_site_participates_in_python_source_closure(tmp_path, monkeypatch):
    project = _write_importable_project(tmp_path / "demo_pkg-0.1")
    site = tmp_path / "site"
    result = install_package(
        str(project), target_dir=site, cache_dir=tmp_path / "cache"
    )
    assert result["ok"] is True
    monkeypatch.setenv("PCC_PACKAGE_SITE", str(site))

    main = tmp_path / "main.py"
    _write_import_main(main)

    srcs, mods = _collect_relative_module_closure(str(main))
    assert "demo_pkg.core" in mods
    assert str(site / "demo_pkg" / "core.py") in srcs


def test_discovered_package_module_closure_skips_function_local_imports(
    tmp_path, monkeypatch
):
    site = tmp_path / "site"
    package = site / "bounded_pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "frontier.py").write_text(
        "from .eager import VALUE\n"
        "def load_later():\n"
        "    from .deep import OTHER\n"
        "    return OTHER\n",
        encoding="utf-8",
    )
    (package / "eager.py").write_text("VALUE = 1\n", encoding="utf-8")
    (package / "deep.py").write_text("OTHER = 2\n", encoding="utf-8")
    monkeypatch.setenv("PCC_PACKAGE_SITE", str(site))

    main = tmp_path / "main.py"
    main.write_text("import bounded_pkg.frontier\n", encoding="utf-8")

    _, mods = _collect_relative_module_closure(str(main))

    assert "bounded_pkg.frontier" in mods
    assert "bounded_pkg.eager" in mods
    assert "bounded_pkg.deep" not in mods


def test_installed_pure_python_package_compiles_without_libpython(
    tmp_path, monkeypatch
):
    project = _write_importable_project(tmp_path / "demo_pkg-0.1")
    site = tmp_path / "site"
    result = install_package(
        str(project), target_dir=site, cache_dir=tmp_path / "cache"
    )
    assert result["ok"] is True
    monkeypatch.setenv("PCC_PACKAGE_SITE", str(site))

    main = tmp_path / "main.py"
    _write_import_main(main)
    _assert_compiled_import_prints_42(main, tmp_path / "main")


def test_installed_pure_python_wheel_compiles_without_libpython(tmp_path, monkeypatch):
    wheel = _write_importable_wheel(tmp_path / "demo_pkg-0.1-py3-none-any.whl")
    site = tmp_path / "site"
    result = install_package(str(wheel), target_dir=site, cache_dir=tmp_path / "cache")
    assert result["ok"] is True
    assert (site / "demo_pkg" / "core.py").exists()
    assert (tmp_path / "cache" / "demo_pkg" / "core.py").exists()
    monkeypatch.setenv("PCC_PACKAGE_SITE", str(site))

    main = tmp_path / "main.py"
    _write_import_main(main)
    _assert_compiled_import_prints_42(main, tmp_path / "main")


def test_installed_namespace_extension_tree_copies_importable_payload(tmp_path):
    source = _write_mlx_namespace_extension_tree(tmp_path / "mlx_tree")
    site = tmp_path / "site"
    result = install_package(
        str(source),
        target_dir=site,
        cache_dir=tmp_path / "cache",
        abi="cpython-compat",
    )
    assert result["ok"] is True
    assert (site / "mlx" / "__main__.py").exists()
    assert (site / "mlx" / "core.cpython-313-darwin.so").exists()
    assert (site / "mlx-0.0.0.dist-info").exists() is False


def test_installed_real_cpython_extension_wheel_rejected_by_no_libpython_import(
    tmp_path, monkeypatch
):
    wheel = _write_real_cpython_extension_wheel(tmp_path)
    site = tmp_path / "site"
    result = install_package(
        str(wheel),
        target_dir=site,
        cache_dir=tmp_path / "cache",
        abi="cpython-compat",
    )
    ext_suffix = sysconfig.get_config_var("EXT_SUFFIX")
    assert (site / "real_ext_pkg" / ("_native" + ext_suffix)).exists()
    assert (site / "real_ext_pkg" / "pcc-package.json").exists()
    assert result["metadata"]["source_kind"] == "wheel"
    monkeypatch.setenv("PCC_PACKAGE_SITE", str(site))

    main = tmp_path / "main.py"
    main.write_text(
        "from real_ext_pkg.core import answer\n"
        "def main():\n"
        "    print(answer())\n"
        "main()\n",
        encoding="utf-8",
    )

    with pytest.raises(PyPipelineError) as exc:
        compile_python(
            str(main),
            str(tmp_path / "main"),
            libpython_mode="off",
            ir_scaffold_mode="on",
            backend="self",
        )

    msg = str(exc.value)
    assert "PCC-PKG-004" in msg
    assert "_native" in msg
    assert "no-libpython import" in msg


def test_cli_import_real_cpython_extension_package_init_rejected_by_no_libpython(
    tmp_path,
):
    wheel = _write_real_cpython_extension_wheel(tmp_path)
    site = tmp_path / "site"
    result = install_package(
        str(wheel),
        target_dir=site,
        cache_dir=tmp_path / "cache",
        abi="cpython-compat",
    )
    assert result["ok"] is True

    main = tmp_path / "main.py"
    main.write_text(
        "import real_ext_pkg\n"
        "def main():\n"
        "    print(real_ext_pkg.answer())\n"
        "main()\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = str(site)
    proc = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(main),
            "-o",
            str(tmp_path / "main"),
        ],
        check=False,
        text=True,
        capture_output=True,
        timeout=300,
        env=env,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert "PCC-PKG-004" in combined
    assert "_native" in combined


def test_installed_pure_python_sdist_compiles_without_libpython(tmp_path, monkeypatch):
    sdist = _write_importable_sdist(tmp_path / "demo_pkg-0.1.tar.gz", tmp_path / "src")
    site = tmp_path / "site"
    result = install_package(str(sdist), target_dir=site, cache_dir=tmp_path / "cache")
    assert result["ok"] is True
    assert (site / "demo_pkg" / "core.py").exists()
    assert (tmp_path / "cache" / "demo_pkg" / "core.py").exists()
    monkeypatch.setenv("PCC_PACKAGE_SITE", str(site))

    main = tmp_path / "main.py"
    _write_import_main(main)
    _assert_compiled_import_prints_42(main, tmp_path / "main")


def test_package_site_rejects_cpython_extension_abi_for_no_libpython(
    tmp_path, monkeypatch
):
    site = tmp_path / "site"
    pkg = site / "demo_pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        "from .core import answer\n",
        encoding="utf-8",
    )
    (pkg / "core.py").write_text(
        "def answer() -> int:\n" "    return 42\n",
        encoding="utf-8",
    )
    (pkg / "_native.cpython-314-darwin.so").write_text(
        "libpcc_runtime\n",
        encoding="utf-8",
    )
    (pkg / "pcc-package.json").write_text(
        json.dumps({"abi_mode": "pcc-native", "ok": True}),
        encoding="utf-8",
    )
    monkeypatch.setenv("PCC_PACKAGE_SITE", str(site))

    main = tmp_path / "main.py"
    _write_import_main(main)

    with pytest.raises(PyPipelineError) as exc:
        compile_python(
            str(main),
            str(tmp_path / "main"),
            libpython_mode="off",
            ir_scaffold_mode="on",
            backend="self",
        )

    assert "PCC-PKG-004" in str(exc.value)
    assert "pcc-native no-libpython" in str(exc.value)


def test_pcc_pip_install_wheel_participates_in_cli_import_path(tmp_path):
    wheel = _write_importable_wheel(tmp_path / "demo_pkg-0.1-py3-none-any.whl")
    site = tmp_path / "site"
    cache = tmp_path / "cache"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    proc = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pip",
            "install",
            str(wheel),
            "--target",
            str(site),
            "--cache-dir",
            str(cache),
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    plan = json.loads(proc.stdout)
    assert plan["ok"] is True
    assert plan["installs"][0]["ok"] is True
    assert (site / "demo_pkg" / "__init__.py").exists()
    assert (site / "demo_pkg" / "core.py").exists()

    main = tmp_path / "main.py"
    _write_import_package_main(main)
    _assert_pcc_cli_import_prints(main, tmp_path / "main", site, "43")


def test_pcc1_pip_install_wheel_participates_in_import_site(tmp_path):
    pcc1 = _current_pcc1_or_verdict(
        "no current pcc1 binary with native package install shim"
    )
    if pcc1 is None:
        return
    wheel = _write_importable_wheel(tmp_path / "demo_pkg-0.1-py3-none-any.whl")
    site = tmp_path / "site"
    cache = tmp_path / "cache"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    env["PCC_PYTHON_IR_PASSES"] = "off"
    env["PCC_PLATFORM_TAG"] = current_platform_tag()
    run_process_tree_timeout(
        [
            str(pcc1),
            "-m",
            "pip",
            "install",
            str(wheel),
            "--target",
            str(site),
            "--cache-dir",
            str(cache),
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=180,
        env=env,
        evidence=tmp_path / "install-process",
    )
    assert (site / "demo_pkg" / "__init__.py").exists()
    assert (site / "demo_pkg" / "core.py").exists()

    env["PCC_PACKAGE_SITE"] = str(site)
    main = tmp_path / "main.py"
    _write_import_package_main(main)
    exe = tmp_path / "main_pcc1"
    subprocess.run(
        [
            str(pcc1),
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(main),
            "-o",
            str(exe),
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=300,
        env=env,
    )
    proc = subprocess.run(
        [str(exe)],
        check=True,
        text=True,
        capture_output=True,
        timeout=180,
        env=env,
    )
    assert proc.stdout.strip() == "43"


def test_pcc1_pip_install_numpy_name_from_find_links_command_shape(tmp_path):
    pcc1 = _current_pcc1_or_verdict(
        "no current pcc1 binary with native package install shim"
    )
    if pcc1 is None:
        return

    wheel = _write_numpy_command_shape_wheel(tmp_path / "numpy-2.4.4-py3-none-any.whl")
    site = tmp_path / "site"
    cache = tmp_path / "cache"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    env["PCC_PYTHON_IR_PASSES"] = "off"
    env["PCC_PLATFORM_TAG"] = current_platform_tag()
    proc = run_process_tree_timeout(
        [
            str(pcc1),
            "-m",
            "pip",
            "install",
            "numpy",
            "--no-index",
            "--find-links",
            str(tmp_path),
            "--target",
            str(site),
            "--cache-dir",
            str(cache),
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=180,
        env=env,
        evidence=tmp_path / "install-process",
    )
    plan = json.loads(proc.stdout)
    assert plan["ok"] is True
    assert plan["packages"] == ["numpy"]
    assert plan["installs"][0]["name"] == "numpy"
    assert plan["installs"][0]["source_path"] == str(wheel.resolve())
    assert (site / "numpy" / "__init__.py").exists()
    assert (site / "numpy" / "core.py").exists()


def test_pcc1_pip_install_mlx_name_from_find_links_command_shape(tmp_path):
    pcc1 = _current_pcc1_or_verdict(
        "no current pcc1 binary with native package install shim"
    )
    if pcc1 is None:
        return

    wheel = _write_mlx_command_shape_wheel(tmp_path / "mlx-0.0.0-py3-none-any.whl")
    site = tmp_path / "site"
    cache = tmp_path / "cache"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    env["PCC_PYTHON_IR_PASSES"] = "off"
    env["PCC_PLATFORM_TAG"] = current_platform_tag()
    proc = run_process_tree_timeout(
        [
            str(pcc1),
            "-m",
            "pip",
            "install",
            "mlx",
            "--no-index",
            "--find-links",
            str(tmp_path),
            "--target",
            str(site),
            "--cache-dir",
            str(cache),
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=180,
        env=env,
        evidence=tmp_path / "install-process",
    )
    plan = json.loads(proc.stdout)
    assert plan["ok"] is True
    assert plan["packages"] == ["mlx"]
    assert plan["installs"][0]["name"] == "mlx"
    assert plan["installs"][0]["source_path"] == str(wheel.resolve())
    assert (site / "mlx" / "__init__.py").exists()
    assert (site / "mlx" / "core.py").exists()


def test_pcc1_pip_install_vllm_name_from_find_links_command_shape(tmp_path):
    pcc1 = _current_pcc1_or_verdict(
        "no current pcc1 binary with native package install shim"
    )
    if pcc1 is None:
        return

    wheel = _write_vllm_command_shape_wheel(tmp_path / "vllm-0.0.0-py3-none-any.whl")
    site = tmp_path / "site"
    cache = tmp_path / "cache"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    env["PCC_PYTHON_IR_PASSES"] = "off"
    env["PCC_PLATFORM_TAG"] = current_platform_tag()
    proc = run_process_tree_timeout(
        [
            str(pcc1),
            "-m",
            "pip",
            "install",
            "vllm",
            "--no-index",
            "--find-links",
            str(tmp_path),
            "--target",
            str(site),
            "--cache-dir",
            str(cache),
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=180,
        env=env,
        evidence=tmp_path / "install-process",
    )
    plan = json.loads(proc.stdout)
    assert plan["ok"] is True
    assert plan["packages"] == ["vllm"]
    assert plan["installs"][0]["name"] == "vllm"
    assert plan["installs"][0]["source_path"] == str(wheel.resolve())
    assert (site / "vllm" / "__init__.py").exists()
    assert (site / "vllm" / "entrypoints.py").exists()


def test_pcc1_pip_install_tilelang_name_from_find_links_command_shape(tmp_path):
    pcc1 = _current_pcc1_or_verdict(
        "no current pcc1 binary with native package install shim"
    )
    if pcc1 is None:
        return

    wheel = _write_tilelang_command_shape_wheel(
        tmp_path / "tilelang-0.0.0-py3-none-any.whl"
    )
    site = tmp_path / "site"
    cache = tmp_path / "cache"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    env["PCC_PYTHON_IR_PASSES"] = "off"
    env["PCC_PLATFORM_TAG"] = current_platform_tag()
    proc = run_process_tree_timeout(
        [
            str(pcc1),
            "-m",
            "pip",
            "install",
            "tilelang",
            "--no-index",
            "--find-links",
            str(tmp_path),
            "--target",
            str(site),
            "--cache-dir",
            str(cache),
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=180,
        env=env,
        evidence=tmp_path / "install-process",
    )
    plan = json.loads(proc.stdout)
    assert plan["ok"] is True
    assert plan["packages"] == ["tilelang"]
    assert plan["installs"][0]["name"] == "tilelang"
    assert plan["installs"][0]["source_path"] == str(wheel.resolve())
    assert (site / "tilelang" / "__init__.py").exists()
    assert (site / "tilelang" / "language.py").exists()


def test_pcc1_pip_install_vllm_metal_name_from_find_links_command_shape(tmp_path):
    pcc1 = _current_pcc1_or_verdict(
        "no current pcc1 binary with native package install shim"
    )
    if pcc1 is None:
        return

    wheel = _write_vllm_metal_command_shape_wheel(
        tmp_path / "vllm_metal-0.0.0-py3-none-any.whl"
    )
    site = tmp_path / "site"
    cache = tmp_path / "cache"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    env["PCC_PYTHON_IR_PASSES"] = "off"
    env["PCC_PLATFORM_TAG"] = current_platform_tag()
    proc = run_process_tree_timeout(
        [
            str(pcc1),
            "-m",
            "pip",
            "install",
            "vllm-metal",
            "--no-index",
            "--find-links",
            str(tmp_path),
            "--target",
            str(site),
            "--cache-dir",
            str(cache),
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=180,
        env=env,
        evidence=tmp_path / "install-process",
    )
    plan = json.loads(proc.stdout)
    assert plan["ok"] is True
    assert plan["packages"] == ["vllm-metal"]
    assert plan["installs"][0]["name"] == "vllm-metal"
    assert plan["installs"][0]["source_path"] == str(wheel.resolve())
    assert (site / "vllm_metal" / "__init__.py").exists()
    assert (site / "vllm_metal" / "engine.py").exists()
    assert (site / "vllm_metal" / "kernels" / "prefill.metallib").exists()


def test_pcc1_pip_install_mlx_namespace_extension_tree_direct(tmp_path):
    pcc1 = _current_pcc1_or_verdict(
        "no current pcc1 binary with native package install shim"
    )
    if pcc1 is None:
        return

    source = _write_mlx_namespace_extension_tree(tmp_path / "mlx_tree")
    site = tmp_path / "site"
    cache = tmp_path / "cache"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    env["PCC_PYTHON_IR_PASSES"] = "off"
    env["PCC_PLATFORM_TAG"] = current_platform_tag()
    proc = run_process_tree_timeout(
        [
            str(pcc1),
            "-m",
            "pip",
            "install",
            str(source),
            "--abi",
            "cpython-compat",
            "--target",
            str(site),
            "--cache-dir",
            str(cache),
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=180,
        env=env,
        evidence=tmp_path / "install-process",
    )
    plan = json.loads(proc.stdout)
    assert plan["ok"] is True
    assert plan["installs"][0]["abi_mode"] == "cpython-compat"
    assert plan["installs"][0]["installed_path"] == str((site / "mlx").resolve())
    assert plan["installs"][0]["uses_cpython_extension_abi"] is True
    assert (site / "mlx" / "__main__.py").exists()
    assert (site / "mlx" / "core.cpython-313-darwin.so").exists()
    assert (site / "mlx" / "pcc-package.json").exists()


def test_pcc1_pip_install_vllm_extension_tree_direct(tmp_path):
    pcc1 = _current_pcc1_or_verdict(
        "no current pcc1 binary with native package install shim"
    )
    if pcc1 is None:
        return

    source = _write_vllm_extension_tree(tmp_path / "vllm_tree")
    site = tmp_path / "site"
    cache = tmp_path / "cache"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    env["PCC_PYTHON_IR_PASSES"] = "off"
    env["PCC_PLATFORM_TAG"] = current_platform_tag()
    proc = run_process_tree_timeout(
        [
            str(pcc1),
            "-m",
            "pip",
            "install",
            str(source),
            "--abi",
            "cpython-compat",
            "--target",
            str(site),
            "--cache-dir",
            str(cache),
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=180,
        env=env,
        evidence=tmp_path / "install-process",
    )
    plan = json.loads(proc.stdout)
    assert plan["ok"] is True
    assert plan["installs"][0]["abi_mode"] == "cpython-compat"
    assert plan["installs"][0]["installed_path"] == str((site / "vllm").resolve())
    assert plan["installs"][0]["uses_cpython_extension_abi"] is True
    assert (site / "vllm" / "__init__.py").exists()
    assert (site / "vllm" / "_C.cpython-313-darwin.so").exists()
    assert (site / "vllm" / "pcc-package.json").exists()


def test_pcc1_pip_install_tilelang_extension_tree_direct(tmp_path):
    pcc1 = _current_pcc1_or_verdict(
        "no current pcc1 binary with native package install shim"
    )
    if pcc1 is None:
        return

    source = _write_tilelang_extension_tree(tmp_path / "tilelang_tree")
    site = tmp_path / "site"
    cache = tmp_path / "cache"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    env["PCC_PYTHON_IR_PASSES"] = "off"
    env["PCC_PLATFORM_TAG"] = current_platform_tag()
    proc = run_process_tree_timeout(
        [
            str(pcc1),
            "-m",
            "pip",
            "install",
            str(source),
            "--abi",
            "cpython-compat",
            "--target",
            str(site),
            "--cache-dir",
            str(cache),
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=180,
        env=env,
        cwd=tmp_path,
        evidence=tmp_path / "install-process",
    )
    plan = json.loads(proc.stdout)
    assert plan["ok"] is True
    assert plan["installs"][0]["abi_mode"] == "cpython-compat"
    assert plan["installs"][0]["installed_path"] == str((site / "tilelang").resolve())
    assert plan["installs"][0]["uses_cpython_extension_abi"] is True
    assert (site / "tilelang" / "__init__.py").exists()
    assert (site / "tilelang" / "_ffi.cpython-313-darwin.so").exists()
    assert (site / "tilelang" / "pcc-package.json").exists()


def test_pcc1_pip_install_vllm_metal_extension_tree_direct(tmp_path):
    pcc1 = _current_pcc1_or_verdict(
        "no current pcc1 binary with native package install shim"
    )
    if pcc1 is None:
        return

    source = _write_vllm_metal_extension_tree(tmp_path / "vllm_metal_tree")
    site = tmp_path / "site"
    cache = tmp_path / "cache"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    env["PCC_PYTHON_IR_PASSES"] = "off"
    env["PCC_PLATFORM_TAG"] = current_platform_tag()
    proc = run_process_tree_timeout(
        [
            str(pcc1),
            "-m",
            "pip",
            "install",
            str(source),
            "--abi",
            "cpython-compat",
            "--target",
            str(site),
            "--cache-dir",
            str(cache),
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=180,
        env=env,
        evidence=tmp_path / "install-process",
    )
    plan = json.loads(proc.stdout)
    assert plan["ok"] is True
    assert plan["installs"][0]["abi_mode"] == "cpython-compat"
    assert plan["installs"][0]["installed_path"] == str((site / "vllm_metal").resolve())
    assert plan["installs"][0]["uses_cpython_extension_abi"] is True
    assert (site / "vllm_metal" / "__init__.py").exists()
    assert (site / "vllm_metal" / "_metal.cpython-313-darwin.so").exists()
    assert (site / "vllm_metal" / "kernels" / "prefill.metallib").exists()
    assert (site / "vllm_metal" / "pcc-package.json").exists()


def test_pcc1_mlx_namespace_extension_import_rejected_by_no_libpython_boundary(
    tmp_path,
):
    pcc1 = _current_pcc1_or_verdict(
        "no current pcc1 binary with native package install shim"
    )
    if pcc1 is None:
        return

    source = _write_mlx_namespace_extension_tree(tmp_path / "mlx_tree")
    site = tmp_path / "site"
    cache = tmp_path / "cache"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    env["PCC_PYTHON_IR_PASSES"] = "off"
    env["PCC_PLATFORM_TAG"] = current_platform_tag()
    run_process_tree_timeout(
        [
            str(pcc1),
            "-m",
            "pip",
            "install",
            str(source),
            "--abi",
            "cpython-compat",
            "--target",
            str(site),
            "--cache-dir",
            str(cache),
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=180,
        env=env,
        evidence=tmp_path / "install-process",
    )

    main = tmp_path / "main.py"
    main.write_text(
        "import mlx.core as mx\n"
        "def main():\n"
        "    print('mlx-imported')\n"
        "main()\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [
            str(pcc1),
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(main),
            "-o",
            str(tmp_path / "main"),
        ],
        check=False,
        text=True,
        capture_output=True,
        timeout=300,
        env={**env, "PCC_PACKAGE_SITE": str(site)},
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert "PCC-PKG-004" in combined
    assert "no-libpython import" in combined
    assert "core.cpython-313-darwin.so" in combined


def test_pcc1_vllm_extension_import_rejected_by_no_libpython_boundary(
    tmp_path,
):
    pcc1 = _current_pcc1_or_verdict(
        "no current pcc1 binary with native package install shim"
    )
    if pcc1 is None:
        return

    source = _write_vllm_extension_tree(tmp_path / "vllm_tree")
    site = tmp_path / "site"
    cache = tmp_path / "cache"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    env["PCC_PYTHON_IR_PASSES"] = "off"
    env["PCC_PLATFORM_TAG"] = current_platform_tag()
    run_process_tree_timeout(
        [
            str(pcc1),
            "-m",
            "pip",
            "install",
            str(source),
            "--abi",
            "cpython-compat",
            "--target",
            str(site),
            "--cache-dir",
            str(cache),
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=180,
        env=env,
        evidence=tmp_path / "install-process",
    )

    main = tmp_path / "main.py"
    main.write_text(
        "import vllm._C as ext\n"
        "def main():\n"
        "    print('vllm-imported')\n"
        "main()\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [
            str(pcc1),
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(main),
            "-o",
            str(tmp_path / "main"),
        ],
        check=False,
        text=True,
        capture_output=True,
        timeout=300,
        env={**env, "PCC_PACKAGE_SITE": str(site)},
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert "PCC-PKG-004" in combined
    assert "no-libpython import" in combined
    assert "_C.cpython-313-darwin.so" in combined


def test_pcc1_tilelang_extension_import_rejected_by_no_libpython_boundary(
    tmp_path,
):
    pcc1 = _current_pcc1_or_verdict(
        "no current pcc1 binary with native package install shim"
    )
    if pcc1 is None:
        return

    source = _write_tilelang_extension_tree(tmp_path / "tilelang_tree")
    site = tmp_path / "site"
    cache = tmp_path / "cache"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    env["PCC_PYTHON_IR_PASSES"] = "off"
    env["PCC_PLATFORM_TAG"] = current_platform_tag()
    run_process_tree_timeout(
        [
            str(pcc1),
            "-m",
            "pip",
            "install",
            str(source),
            "--abi",
            "cpython-compat",
            "--target",
            str(site),
            "--cache-dir",
            str(cache),
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=180,
        env=env,
        cwd=tmp_path,
        evidence=tmp_path / "install-process",
    )

    main = tmp_path / "main.py"
    main.write_text(
        "import tilelang._ffi as ext\n"
        "def main():\n"
        "    print('tilelang-imported')\n"
        "main()\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [
            str(pcc1),
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(main),
            "-o",
            str(tmp_path / "main"),
        ],
        check=False,
        text=True,
        capture_output=True,
        timeout=300,
        env={**env, "PCC_PACKAGE_SITE": str(site)},
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert "PCC-PKG-004" in combined
    assert "no-libpython import" in combined
    assert "_ffi.cpython-313-darwin.so" in combined


def test_pcc1_vllm_metal_extension_import_rejected_by_no_libpython_boundary(
    tmp_path,
):
    pcc1 = _current_pcc1_or_verdict(
        "no current pcc1 binary with native package install shim"
    )
    if pcc1 is None:
        return

    source = _write_vllm_metal_extension_tree(tmp_path / "vllm_metal_tree")
    site = tmp_path / "site"
    cache = tmp_path / "cache"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    env["PCC_PYTHON_IR_PASSES"] = "off"
    env["PCC_PLATFORM_TAG"] = current_platform_tag()
    run_process_tree_timeout(
        [
            str(pcc1),
            "-m",
            "pip",
            "install",
            str(source),
            "--abi",
            "cpython-compat",
            "--target",
            str(site),
            "--cache-dir",
            str(cache),
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=180,
        env=env,
        evidence=tmp_path / "install-process",
    )

    main = tmp_path / "main.py"
    main.write_text(
        "import vllm_metal._metal as ext\n"
        "def main():\n"
        "    print('vllm-metal-imported')\n"
        "main()\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [
            str(pcc1),
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(main),
            "-o",
            str(tmp_path / "main"),
        ],
        check=False,
        text=True,
        capture_output=True,
        timeout=300,
        env={**env, "PCC_PACKAGE_SITE": str(site)},
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert "PCC-PKG-004" in combined
    assert "no-libpython import" in combined
    assert "_metal.cpython-313-darwin.so" in combined


def test_pcc1_pip_install_real_numpy_artifact_opt_in(tmp_path):
    gate = _real_package_gate_or_reason(
        package="NumPy",
        run_env="PCC_RUN_REAL_NUMPY_INSTALL",
        artifact_env="PCC_NUMPY_ARTIFACT",
        default_artifact=DEFAULT_NUMPY_ARTIFACT,
    )
    if isinstance(gate, str):
        _assert_skipped_with_reason(gate)
        return
    pcc1, artifact_path = gate

    find_links = artifact_path.parent if artifact_path.is_file() else artifact_path
    site = tmp_path / "site"
    cache = tmp_path / "cache"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    env["PCC_PYTHON_IR_PASSES"] = "off"
    env["PCC_PLATFORM_TAG"] = current_platform_tag()
    proc = run_process_tree_timeout(
        [
            str(pcc1),
            "-m",
            "pip",
            "install",
            "numpy",
            "--no-index",
            "--find-links",
            str(find_links),
            "--abi",
            "cpython-compat",
            "--target",
            str(site),
            "--cache-dir",
            str(cache),
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=300,
        env=env,
        evidence=tmp_path / "install-process",
    )
    plan = json.loads(proc.stdout)
    assert plan["ok"] is True
    assert plan["installs"][0]["name"] == "numpy"
    assert (site / "numpy").exists()


def test_pcc1_pip_install_real_mlx_artifact_opt_in(tmp_path):
    gate = _real_package_gate_or_reason(
        package="MLX",
        run_env="PCC_RUN_REAL_MLX_INSTALL",
        artifact_env="PCC_MLX_ARTIFACT",
    )
    if isinstance(gate, str):
        _assert_skipped_with_reason(gate)
        return
    pcc1, artifact_path = gate

    site = tmp_path / "site"
    cache = tmp_path / "cache"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    env["PCC_PYTHON_IR_PASSES"] = "off"
    env["PCC_PLATFORM_TAG"] = current_platform_tag()
    proc = subprocess.run(
        _real_mlx_install_command(pcc1, artifact_path, site, cache),
        check=True,
        text=True,
        capture_output=True,
        timeout=300,
        env=env,
    )
    plan = json.loads(proc.stdout)
    assert plan["ok"] is True
    assert (site / "mlx").exists()


def test_pcc1_pip_install_real_vllm_metal_artifact_opt_in(tmp_path):
    gate = _real_package_gate_or_reason(
        package="vLLM-Metal",
        run_env="PCC_RUN_REAL_VLLM_METAL_INSTALL",
        artifact_env="PCC_VLLM_METAL_ARTIFACT",
    )
    if isinstance(gate, str):
        _assert_skipped_with_reason(gate)
        return
    pcc1, artifact_path = gate

    site = tmp_path / "site"
    cache = tmp_path / "cache"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    env["PCC_PYTHON_IR_PASSES"] = "off"
    env["PCC_PLATFORM_TAG"] = current_platform_tag()
    proc = subprocess.run(
        _real_vllm_metal_install_command(pcc1, artifact_path, site, cache),
        check=True,
        text=True,
        capture_output=True,
        timeout=300,
        env=env,
    )
    plan = json.loads(proc.stdout)
    assert plan["ok"] is True
    install = plan["installs"][0]
    assert install["abi_mode"] == "cpython-compat"
    assert install["uses_cpython_extension_abi"] is True
    assert (site / "vllm_metal").exists()
    assert any(
        path.endswith((".so", ".dylib", ".pyd"))
        for path in install["cpython_extension_abi_paths"]
    )


def test_pcc1_real_numpy_first_import_boundary_opt_in(tmp_path):
    gate = _real_package_gate_or_reason(
        package="NumPy",
        run_env="PCC_RUN_REAL_NUMPY_IMPORT_BOUNDARY",
        artifact_env="PCC_NUMPY_ARTIFACT",
        default_artifact=DEFAULT_NUMPY_ARTIFACT,
    )
    if isinstance(gate, str):
        _assert_skipped_with_reason(gate)
        return
    pcc1, artifact_path = gate

    find_links = artifact_path.parent if artifact_path.is_file() else artifact_path
    site = tmp_path / "site"
    cache = tmp_path / "cache"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    env["PCC_PYTHON_IR_PASSES"] = "off"
    env["PCC_PLATFORM_TAG"] = current_platform_tag()
    install_proc = run_process_tree_timeout(
        [
            str(pcc1),
            "-m",
            "pip",
            "install",
            "numpy",
            "--no-index",
            "--find-links",
            str(find_links),
            "--abi",
            "cpython-compat",
            "--target",
            str(site),
            "--cache-dir",
            str(cache),
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=300,
        env=env,
        evidence=tmp_path / "install-process",
    )
    install_plan = json.loads(install_proc.stdout)
    assert install_plan["ok"] is True
    assert (site / "numpy").exists()

    main = tmp_path / "main.py"
    main.write_text(
        "import numpy\n" "def main():\n" "    print('numpy-imported')\n" "main()\n",
        encoding="utf-8",
    )
    exe = tmp_path / "numpy_import"
    compile_env = {**env, "PCC_PACKAGE_SITE": str(site)}
    compile_proc = subprocess.run(
        [
            str(pcc1),
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(main),
            "-o",
            str(exe),
        ],
        check=False,
        text=True,
        capture_output=True,
        timeout=300,
        env=compile_env,
    )
    combined = compile_proc.stdout + compile_proc.stderr
    if compile_proc.returncode != 0:
        assert "PCC-PY-COMPILE-001" in combined
        if "PCC-PKG-004" in combined:
            assert "no-libpython import" in combined
            assert "cpython" in combined
            return
        assert "multi-file compile" in combined
        assert "requires libpython fallback for multi-file compile" in combined
        assert "numpy.f2py.symbolic" in combined
        assert "numpy.f2py.func2subr" in combined
        return

    run_proc = subprocess.run(
        [str(exe)],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=compile_env,
    )
    assert run_proc.stdout.strip() == "numpy-imported"


def test_pcc1_real_mlx_first_import_boundary_opt_in(tmp_path):
    gate = _real_package_gate_or_reason(
        package="MLX",
        run_env="PCC_RUN_REAL_MLX_IMPORT_BOUNDARY",
        artifact_env="PCC_MLX_ARTIFACT",
    )
    if isinstance(gate, str):
        _assert_skipped_with_reason(gate)
        return
    pcc1, artifact_path = gate

    site = tmp_path / "site"
    cache = tmp_path / "cache"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    env["PCC_PYTHON_IR_PASSES"] = "off"
    env["PCC_PLATFORM_TAG"] = current_platform_tag()
    install_proc = subprocess.run(
        _real_mlx_install_command(pcc1, artifact_path, site, cache),
        check=True,
        text=True,
        capture_output=True,
        timeout=300,
        env=env,
    )
    install_plan = json.loads(install_proc.stdout)
    assert install_plan["ok"] is True
    assert (site / "mlx").exists()

    main = tmp_path / "main.py"
    main.write_text(
        "import mlx.core as mx\n"
        "def main():\n"
        "    print('mlx-imported')\n"
        "main()\n",
        encoding="utf-8",
    )
    exe = tmp_path / "mlx_import"
    compile_env = {**env, "PCC_PACKAGE_SITE": str(site)}
    compile_proc = subprocess.run(
        [
            str(pcc1),
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(main),
            "-o",
            str(exe),
        ],
        check=False,
        text=True,
        capture_output=True,
        timeout=300,
        env=compile_env,
    )
    combined = compile_proc.stdout + compile_proc.stderr
    if compile_proc.returncode != 0:
        assert "PCC-PY-COMPILE-001" in combined or "PCC-PKG-004" in combined
        if "PCC-PKG-004" in combined:
            assert "no-libpython import" in combined
            assert "cpython" in combined
        return

    run_proc = subprocess.run(
        [str(exe)],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=compile_env,
    )
    assert run_proc.stdout.strip() == "mlx-imported"


def test_pcc1_real_vllm_metal_first_import_boundary_opt_in(tmp_path):
    gate = _real_package_gate_or_reason(
        package="vLLM-Metal",
        run_env="PCC_RUN_REAL_VLLM_METAL_IMPORT_BOUNDARY",
        artifact_env="PCC_VLLM_METAL_ARTIFACT",
    )
    if isinstance(gate, str):
        _assert_skipped_with_reason(gate)
        return
    pcc1, artifact_path = gate

    site = tmp_path / "site"
    cache = tmp_path / "cache"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    env["PCC_PYTHON_IR_PASSES"] = "off"
    env["PCC_PLATFORM_TAG"] = current_platform_tag()
    install_proc = subprocess.run(
        _real_vllm_metal_install_command(pcc1, artifact_path, site, cache),
        check=True,
        text=True,
        capture_output=True,
        timeout=300,
        env=env,
    )
    install_plan = json.loads(install_proc.stdout)
    assert install_plan["ok"] is True
    assert (site / "vllm_metal").exists()

    main = tmp_path / "main.py"
    main.write_text(
        "import vllm_metal\n"
        "def main():\n"
        "    print(vllm_metal.__version__)\n"
        "main()\n",
        encoding="utf-8",
    )
    exe = tmp_path / "vllm_metal_import"
    compile_env = {**env, "PCC_PACKAGE_SITE": str(site)}
    compile_proc = subprocess.run(
        [
            str(pcc1),
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(main),
            "-o",
            str(exe),
        ],
        check=False,
        text=True,
        capture_output=True,
        timeout=300,
        env=compile_env,
    )
    combined = compile_proc.stdout + compile_proc.stderr
    assert compile_proc.returncode != 0
    assert "PCC-PKG-004" in combined
    assert "no-libpython import" in combined
    assert "cpython" in combined


def test_pcc1_pip_install_chain_no_host_ext_abi_smoke(tmp_path):
    pcc1 = _current_pcc1_or_verdict("no current pcc1 binary with native ext-abi shim")
    if pcc1 is None:
        return

    wheel = _write_importable_wheel(tmp_path / "demo_pkg-0.1-py3-none-any.whl")
    site = tmp_path / "site"
    cache = tmp_path / "cache"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    env["PCC_PYTHON_IR_PASSES"] = "off"
    env["PCC_PLATFORM_TAG"] = current_platform_tag()

    proc = run_process_tree_timeout(
        [
            str(pcc1),
            "-m",
            "pip",
            "install",
            str(wheel),
            "--target",
            str(site),
            "--cache-dir",
            str(cache),
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=180,
        env=env,
        evidence=tmp_path / "install-process",
    )
    install_plan = json.loads(proc.stdout)
    assert install_plan["ok"] is True

    proc = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "ext-abi",
            "--symbol",
            "Py_Initialize",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=180,
        env=env,
    )
    plan = json.loads(proc.stdout)
    assert plan["ok"] is True
    assert plan["missing_symbols"] == []

    main = tmp_path / "main.py"
    _write_import_package_main(main)
    exe = tmp_path / "main_pcc1"
    subprocess.run(
        [
            str(pcc1),
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(main),
            "-o",
            str(exe),
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=300,
        env={**env, "PCC_PACKAGE_SITE": str(site)},
    )
    proc = subprocess.run(
        [str(exe)],
        check=True,
        text=True,
        capture_output=True,
        timeout=180,
        env={**env, "PCC_PACKAGE_SITE": str(site)},
    )
    assert proc.stdout.strip() == "43"


def test_pip_install_offline_bare_name_fails_closed(tmp_path):
    """Explicit offline mode never silently delegates or attempts a network."""
    from pcc.package.pip_shim import pip_install_plan

    plan = pip_install_plan(
        [
            "install",
            "definitely-not-a-pkg-xyz",
            "--acquire=offline",
            "--target",
            str(tmp_path / "site"),
            "--cache-dir",
            str(tmp_path / "cache"),
        ]
    )
    assert plan["ok"] is False
    assert plan["acquire_mode_requested"] == "offline"
    assert plan["acquisitions"][0]["diagnostic"] == "PCC-PKG-ACQUIRE-OFFLINE"
    assert "acquire_hint" not in plan


def test_pip_install_pathlike_spec_failure_gets_no_acquire_hint(tmp_path):
    """A mistyped local path/artifact failure is not an acquisition problem."""
    from pcc.package.pip_shim import pip_install_plan

    plan = pip_install_plan(
        [
            "install",
            "./no-such-dir/x.whl",
            "--target",
            str(tmp_path / "site"),
            "--cache-dir",
            str(tmp_path / "cache"),
        ]
    )
    assert plan["ok"] is False
    assert "acquire_hint" not in plan
