from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sysconfig
import textwrap

import pytest

from pcc1_gate import (
    find_current_pcc1,
    require_current_pcc1_enabled,
)


def _repo_root_from_here() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pcc" / "__main__.py").exists() and (parent / "tests").exists():
            return parent
    return here.parents[2]


REPO = _repo_root_from_here()


def _assert_skipped_with_reason(reason: str) -> None:
    assert reason.startswith("SKIPPED_WITH_REASON:")
    assert reason[len("SKIPPED_WITH_REASON:") :].strip()


def _real_proxy_gate_or_reason(
    *,
    run_env: str,
    artifact_env: str | None = None,
) -> tuple[Path, Path | None] | str:
    reasons: list[str] = []
    if os.environ.get(run_env) != "1":
        reasons.append(f"{run_env}=1 is not set")
    artifact_path: Path | None = None
    if artifact_env is not None:
        artifact = os.environ.get(artifact_env)
        if not artifact:
            reasons.append(f"{artifact_env} is not set")
        else:
            artifact_path = Path(artifact).expanduser().resolve()
            if not artifact_path.exists():
                reasons.append(f"{artifact_env} path does not exist: {artifact_path}")
    pcc1 = find_current_pcc1(REPO)
    if pcc1 is None:
        if require_current_pcc1_enabled():
            pytest.fail(
                "no current pcc1 binary for real CPython proxy run; "
                "PCC_REQUIRE_CURRENT_PCC1=1 makes pcc1 proxy parity a hard gate"
            )
        reasons.append("no current pcc1 binary for real CPython proxy run")
    if reasons:
        return "SKIPPED_WITH_REASON: " + "; ".join(reasons)
    assert pcc1 is not None
    return pcc1, artifact_path


def _pcc1_proxy_ir_gate_or_reason(reason: str) -> Path | str:
    pcc1 = find_current_pcc1(REPO)
    if pcc1 is not None:
        return pcc1
    if require_current_pcc1_enabled():
        pytest.fail(
            reason + "; PCC_REQUIRE_CURRENT_PCC1=1 makes pcc1 proxy IR a hard gate"
        )
    return "SKIPPED_WITH_REASON: " + reason


def _write_mlx_proxy_probe(tmp_path: Path) -> Path:
    src = tmp_path / "mlx_proxy_probe.py"
    src.write_text(
        textwrap.dedent("""
            import mlx.core as mx

            def f():
                a = mx.array([1, 2, 3])
                return (a + a).sum()
            """).lstrip(),
        encoding="utf-8",
    )
    return src


def _write_vllm_proxy_probe(tmp_path: Path) -> Path:
    src = tmp_path / "vllm_proxy_probe.py"
    src.write_text(
        textwrap.dedent("""
            import vllm

            def f():
                return vllm.LLM
            """).lstrip(),
        encoding="utf-8",
    )
    return src


def _write_tilelang_proxy_probe(tmp_path: Path) -> Path:
    src = tmp_path / "tilelang_proxy_probe.py"
    src.write_text(
        textwrap.dedent("""
            import tilelang
            import tilelang.language as T

            def f():
                return tilelang.jit, T.Kernel
            """).lstrip(),
        encoding="utf-8",
    )
    return src


def _write_vllm_metal_proxy_probe(tmp_path: Path) -> Path:
    src = tmp_path / "vllm_metal_proxy_probe.py"
    src.write_text(
        textwrap.dedent("""
            import vllm_metal as vm

            def f():
                return vm.Engine
            """).lstrip(),
        encoding="utf-8",
    )
    return src


def _compile_and_run_with_package_site(
    tmp_path: Path,
    package_site: Path,
    source: str,
    *,
    compiler: list[str] | None = None,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    src = tmp_path / "main.py"
    exe = tmp_path / "main"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = str(package_site)
    env["PCC_RUNTIME_CC"] = "pcc"
    env["PCC_PYTHON_IR_PASSES"] = "off"
    cmd = compiler or ["uv", "run", "pcc"]
    compile_proc = subprocess.run(
        [
            *cmd,
            "--backend",
            "self",
            "--python-libpython=on",
            "--ir-scaffold=auto",
            str(src),
            "-o",
            str(exe),
        ],
        text=True,
        capture_output=True,
        timeout=timeout,
        env=env,
    )
    assert compile_proc.returncode == 0, compile_proc.stdout + compile_proc.stderr
    run_proc = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=120,
        env=env,
    )
    return run_proc


def _write_synthetic_cpython_compat_vllm_metal_site(site: Path) -> None:
    pkg = site / "vllm_metal"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text('__version__ = "0.0.site"\n', encoding="utf-8")
    ext_suffix = sysconfig.get_config_var("EXT_SUFFIX") or ".so"
    fake_ext = pkg / ("_rs" + ext_suffix)
    fake_ext.write_bytes(b"fake extension marker\n")
    (pkg / "pcc-package.json").write_text(
        json.dumps(
            {
                "ok": True,
                "abi_mode": "cpython-compat",
                "uses_cpython_extension_abi": True,
                "cpython_extension_abi_paths": [str(fake_ext)],
            }
        ),
        encoding="utf-8",
    )


def _install_real_mlx_site(site: Path) -> None:
    artifact = os.environ.get("PCC_MLX_ARTIFACT")
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    if artifact:
        artifact_path = Path(artifact).expanduser().resolve()
        if artifact_path.is_dir() and (artifact_path / "mlx").is_dir():
            site.mkdir(parents=True)
            subprocess.run(
                ["cp", "-R", str(artifact_path / "mlx"), str(site / "mlx")],
                check=True,
                text=True,
                capture_output=True,
                timeout=60,
                env=env,
            )
            dist_infos = list(artifact_path.glob("mlx*.dist-info"))
            for dist_info in dist_infos:
                subprocess.run(
                    ["cp", "-R", str(dist_info), str(site / dist_info.name)],
                    check=True,
                    text=True,
                    capture_output=True,
                    timeout=60,
                    env=env,
                )
            return
        cmd = [
            "uv",
            "pip",
            "install",
            "--target",
            str(site),
            "--only-binary=:all:",
            str(artifact_path),
        ]
    else:
        cmd = [
            "uv",
            "pip",
            "install",
            "--target",
            str(site),
            "--only-binary=:all:",
            "mlx",
        ]
    proc = subprocess.run(
        cmd,
        check=False,
        text=True,
        capture_output=True,
        timeout=300,
        env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def _assert_mlx_proxy_ir(ir: str) -> None:
    assert "py_cpy_import" in ir
    assert "@.cpy.attr.array" in ir
    assert "@.cpy.attr.sum" in ir
    assert "py_cpy_binop" in ir
    assert "@.pyattr.sum" not in ir


def _assert_vllm_proxy_ir(ir: str) -> None:
    assert "py_cpy_import" in ir
    assert "py_cpy_getattr" in ir
    assert "@.cpy.attr.LLM" in ir
    assert "@.pyattr.LLM" not in ir


def _assert_tilelang_proxy_ir(ir: str) -> None:
    assert "py_cpy_import" in ir
    assert "py_cpy_getattr" in ir
    assert "@.cpy.attr.jit" in ir
    assert "@.cpy.attr.Kernel" in ir
    assert "@.pyattr.jit" not in ir
    assert "@.pyattr.Kernel" not in ir


def _assert_vllm_metal_proxy_ir(ir: str) -> None:
    assert "py_cpy_import" in ir
    assert "py_cpy_getattr" in ir
    assert "@.cpy.attr.Engine" in ir
    assert "@.pyattr.Engine" not in ir


def test_mlx_core_import_routes_through_cpython_proxy_ir(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = _write_mlx_proxy_probe(tmp_path)
    out = tmp_path / "mlx_proxy_probe.ll"

    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        ir_scaffold_mode="auto",
        libpython_mode="on",
    )

    _assert_mlx_proxy_ir(out.read_text(encoding="utf-8"))


def test_vllm_import_routes_through_cpython_proxy_ir(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = _write_vllm_proxy_probe(tmp_path)
    out = tmp_path / "vllm_proxy_probe.ll"

    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        ir_scaffold_mode="auto",
        libpython_mode="on",
    )

    _assert_vllm_proxy_ir(out.read_text(encoding="utf-8"))


def test_tilelang_import_routes_through_cpython_proxy_ir(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = _write_tilelang_proxy_probe(tmp_path)
    out = tmp_path / "tilelang_proxy_probe.ll"

    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        ir_scaffold_mode="auto",
        libpython_mode="on",
    )

    _assert_tilelang_proxy_ir(out.read_text(encoding="utf-8"))


def test_vllm_metal_import_routes_through_cpython_proxy_ir(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = _write_vllm_metal_proxy_probe(tmp_path)
    out = tmp_path / "vllm_metal_proxy_probe.ll"

    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        ir_scaffold_mode="auto",
        libpython_mode="on",
    )

    _assert_vllm_metal_proxy_ir(out.read_text(encoding="utf-8"))


def test_cpython_proxy_runtime_seeds_pcc_package_site(tmp_path):
    site = tmp_path / "site"
    _write_synthetic_cpython_compat_vllm_metal_site(site)

    run_proc = _compile_and_run_with_package_site(
        tmp_path,
        site,
        """
        import vllm_metal

        def main():
            print(vllm_metal.__version__)

        main()
        """,
    )

    assert run_proc.returncode == 0, run_proc.stdout + run_proc.stderr
    assert run_proc.stdout.strip() == "0.0.site"


def test_compiled_module_registry_is_linked_outside_the_capi_shim():
    """libpython mode drops the shim but still needs pcc module imports."""
    runtime_py = REPO / "pcc" / "runtime" / "py"
    makefile_text = (REPO / "pcc" / "runtime" / "Makefile").read_text(
        encoding="utf-8"
    )
    for symbol in (
        "py_compiled_module_register_init",
        "py_compiled_module_import_by_name",
    ):
        owners = sorted(
            path.name
            for path in runtime_py.glob("*.py")
            if f'c_abi_export("{symbol}")' in path.read_text(encoding="utf-8")
        )
        assert owners == ["py_compiled_module_runtime.py"], (symbol, owners)
    py_modules = makefile_text.split("PY_MODULES =", 1)[1].split("\n", 1)[0]
    assert " py_compiled_module_runtime " in f" {py_modules} "
    # The libpython bundle swaps only the extension loader for the CPython
    # bridge; the registry stays in it.
    libpython_rule = makefile_text.split("$(LIB_PCC_PY_LIBPYTHON): ", 1)[1].split(
        "\n\n", 1
    )[0]
    assert '$(AR) d "$@.tmp" py_extension_loader_runtime.o;' in libpython_rule
    assert "py_compiled_module_runtime" not in libpython_rule


def test_pcc1_real_mlx_core_cpython_proxy_runtime_opt_in(tmp_path):
    gate = _real_proxy_gate_or_reason(run_env="PCC_RUN_REAL_MLX_CPYTHON_PROXY")
    if isinstance(gate, str):
        _assert_skipped_with_reason(gate)
        return
    pcc1, _ = gate

    site = tmp_path / "mlx_site"
    _install_real_mlx_site(site)
    run_proc = _compile_and_run_with_package_site(
        tmp_path,
        site,
        """
        import mlx.core as mx

        def main():
            a = mx.array([1, 2, 3])
            print(int(mx.sum(a).item()))

        main()
        """,
        compiler=[str(pcc1)],
        timeout=360,
    )

    assert run_proc.returncode == 0, run_proc.stdout + run_proc.stderr
    assert run_proc.stdout.strip() == "6"


def test_pcc1_real_vllm_metal_cpython_proxy_runtime_opt_in(tmp_path):
    gate = _real_proxy_gate_or_reason(
        run_env="PCC_RUN_REAL_VLLM_METAL_CPYTHON_PROXY",
        artifact_env="PCC_VLLM_METAL_ARTIFACT",
    )
    if isinstance(gate, str):
        _assert_skipped_with_reason(gate)
        return
    pcc1, artifact_path = gate
    assert artifact_path is not None

    package_site = artifact_path
    assert (package_site / "vllm_metal").exists()
    run_proc = _compile_and_run_with_package_site(
        tmp_path,
        package_site,
        """
        import vllm_metal

        def main():
            print(vllm_metal.__version__)

        main()
        """,
        compiler=[str(pcc1)],
        timeout=360,
    )

    assert run_proc.returncode == 0, run_proc.stdout + run_proc.stderr
    assert run_proc.stdout.strip()


def test_pcc1_mlx_core_import_routes_through_cpython_proxy_ir(tmp_path):
    gate = _pcc1_proxy_ir_gate_or_reason(
        "no current pcc1 binary for MLX proxy IR check"
    )
    if isinstance(gate, str):
        _assert_skipped_with_reason(gate)
        return
    pcc1 = gate

    src = _write_mlx_proxy_probe(tmp_path)
    out = tmp_path / "mlx_proxy_probe_pcc1.ll"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    proc = subprocess.run(
        [
            str(pcc1),
            "--python-libpython=on",
            "--ir-scaffold=auto",
            "--emit-llvm=" + str(out),
            str(src),
        ],
        text=True,
        capture_output=True,
        timeout=120,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr

    _assert_mlx_proxy_ir(out.read_text(encoding="utf-8"))


def test_pcc1_vllm_import_routes_through_cpython_proxy_ir(tmp_path):
    gate = _pcc1_proxy_ir_gate_or_reason(
        "no current pcc1 binary for vLLM proxy IR check"
    )
    if isinstance(gate, str):
        _assert_skipped_with_reason(gate)
        return
    pcc1 = gate

    src = _write_vllm_proxy_probe(tmp_path)
    out = tmp_path / "vllm_proxy_probe_pcc1.ll"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    proc = subprocess.run(
        [
            str(pcc1),
            "--python-libpython=on",
            "--ir-scaffold=auto",
            "--emit-llvm=" + str(out),
            str(src),
        ],
        text=True,
        capture_output=True,
        timeout=120,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr

    _assert_vllm_proxy_ir(out.read_text(encoding="utf-8"))


def test_pcc1_tilelang_import_routes_through_cpython_proxy_ir(tmp_path):
    gate = _pcc1_proxy_ir_gate_or_reason(
        "no current pcc1 binary for TileLang proxy IR check"
    )
    if isinstance(gate, str):
        _assert_skipped_with_reason(gate)
        return
    pcc1 = gate

    src = _write_tilelang_proxy_probe(tmp_path)
    out = tmp_path / "tilelang_proxy_probe_pcc1.ll"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    proc = subprocess.run(
        [
            str(pcc1),
            "--python-libpython=on",
            "--ir-scaffold=auto",
            "--emit-llvm=" + str(out),
            str(src),
        ],
        text=True,
        capture_output=True,
        timeout=120,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr

    _assert_tilelang_proxy_ir(out.read_text(encoding="utf-8"))


def test_pcc1_vllm_metal_import_routes_through_cpython_proxy_ir(tmp_path):
    gate = _pcc1_proxy_ir_gate_or_reason(
        "no current pcc1 binary for vLLM-Metal proxy IR check"
    )
    if isinstance(gate, str):
        _assert_skipped_with_reason(gate)
        return
    pcc1 = gate

    src = _write_vllm_metal_proxy_probe(tmp_path)
    out = tmp_path / "vllm_metal_proxy_probe_pcc1.ll"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    proc = subprocess.run(
        [
            str(pcc1),
            "--python-libpython=on",
            "--ir-scaffold=auto",
            "--emit-llvm=" + str(out),
            str(src),
        ],
        text=True,
        capture_output=True,
        timeout=120,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr

    _assert_vllm_metal_proxy_ir(out.read_text(encoding="utf-8"))
