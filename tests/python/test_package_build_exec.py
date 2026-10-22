"""Explicit external-tool compatibility regressions.

Owned default execution is tested in test_package_owned_build_exec.py; these
fixtures intentionally exercise host cc/Cython/Fortran/ninja compatibility.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

from pcc1_gate import repo_root

import pytest

from pcc1_gate import find_current_pcc1, skip_or_fail_no_current_pcc1

from pcc.package.build_exec import (
    discover_eager_meson_extension_targets,
    execute_build_actions,
)
from pcc.package.metadata import current_platform_tag
from pcc.package.schema import pcc_native_extension_suffix

REPO = repo_root()


def _find_current_pcc1() -> Path | None:
    return find_current_pcc1(REPO)


def _write_exec_project(root: Path) -> Path:
    root.mkdir()
    (root / "demo_pkg").mkdir()
    (root / "demo_pkg" / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "demo_pkg.pyx").write_text("cdef int value\n", encoding="utf-8")
    (root / "native.c").write_text(
        "int native_value(void) { return 1; }\n", encoding="utf-8"
    )
    (root / "solver.f90").write_text("subroutine solver()\nend\n", encoding="utf-8")
    return root


def _write_single_c_extension_project(root: Path) -> Path:
    package = root / "generic_ext"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (package / "_native.c").write_text(
        "#include <Python.h>\n"
        "static int generic_exec(PyObject *module) {\n"
        '    return PyModule_AddIntConstant(module, "ready", 1);\n'
        "}\n"
        "static PyModuleDef_Slot slots[] = {\n"
        "    {Py_mod_exec, generic_exec}, {0, NULL}\n"
        "};\n"
        "static PyModuleDef module = {\n"
        '    PyModuleDef_HEAD_INIT, "_native", NULL, 0, NULL, slots\n'
        "};\n"
        "PyMODINIT_FUNC PyInit__native(void) {\n"
        "    return PyModuleDef_Init(&module);\n"
        "}\n",
        encoding="utf-8",
    )
    return root


def _write_fake_toolchain(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    log = tmp_path / "build.log"
    bin_dir.mkdir()
    script = (
        "#!/bin/sh\n"
        'echo "$(basename "$0") $*" >> "$PCC_BUILD_LOG"\n'
        'out=""\n'
        'prev=""\n'
        'for arg in "$@"; do\n'
        '  if [ "$prev" = "-o" ]; then out="$arg"; fi\n'
        '  prev="$arg"\n'
        "done\n"
        'if [ -n "$out" ]; then\n'
        '  mkdir -p "$(dirname "$out")"\n'
        '  if [ -n "$PCC_FAKE_OUTPUT_TEXT" ]; then\n'
        '    printf \'%s\\n\' "$PCC_FAKE_OUTPUT_TEXT" > "$out"\n'
        "  else\n"
        '    : > "$out"\n'
        "  fi\n"
        "fi\n"
        "exit 0\n"
    )
    for name in ("cc", "c++", "gfortran", "cython", "f2py"):
        tool = bin_dir / name
        tool.write_text(script, encoding="utf-8")
        tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    return bin_dir, log


def _write_fake_meson_tool(bin_dir: Path) -> None:
    tool = bin_dir / "meson"
    tool.write_text(
        "#!/bin/sh\n"
        'echo "meson $*" >> "$PCC_BUILD_LOG"\n'
        'if [ "$1" = "setup" ]; then\n'
        '  builddir="$2"\n'
        '  mkdir -p "$builddir/meson-info"\n'
        '  cat > "$builddir/meson-info/intro-targets.json" <<EOF\n'
        '[{"name":"demo_native","target_sources":[{"language":"c","compiler":["cc"],"parameters":[],"sources":["$PCC_FAKE_MESON_SOURCE"]}]}]\n'
        "EOF\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)


def _write_fake_ninja_tool(bin_dir: Path) -> None:
    tool = bin_dir / "ninja"
    tool.write_text(
        "#!/bin/sh\n"
        'echo "ninja $*" >> "$PCC_BUILD_LOG"\n'
        'builddir="."\n'
        'if [ "$1" = "-C" ]; then\n'
        '  builddir="$2"\n'
        "  shift 2\n"
        "fi\n"
        'if [ "$1" = "-t" ] && [ "$2" = "targets" ]; then\n'
        '  if [ -n "$PCC_FAKE_NINJA_TARGETS" ]; then\n'
        "    printf '%s\\n' \"$PCC_FAKE_NINJA_TARGETS\"\n"
        "  fi\n"
        "  exit 0\n"
        "fi\n"
        'for target in "$@"; do\n'
        '  case "$target" in\n'
        '    /*) out="$target" ;;\n'
        '    *) out="$builddir/$target" ;;\n'
        "  esac\n"
        '  mkdir -p "$(dirname "$out")"\n'
        "  printf 'generated\\n' > \"$out\"\n"
        "done\n"
        "exit 0\n",
        encoding="utf-8",
    )
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)


def _write_fake_libraries(tmp_path: Path) -> Path:
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    (lib_dir / "libopenblas.a").write_text("", encoding="utf-8")
    (lib_dir / "liblapack.a").write_text("", encoding="utf-8")
    return lib_dir


def _write_compile_commands(project: Path) -> None:
    build_dir = project / "build" / "ccdb"
    build_dir.mkdir(parents=True)
    entries = [
        {
            "directory": str(project),
            "file": str(project / "native.c"),
            "command": (
                f"cc -c {project / 'native.c'} " f"-o {build_dir / 'native_ccdb.o'}"
            ),
        },
        {
            "directory": str(project),
            "file": str(project / "solver.f90"),
            "command": (
                f"gfortran -c {project / 'solver.f90'} "
                f"-o {build_dir / 'solver_ccdb.o'}"
            ),
        },
    ]
    (project / "compile_commands.json").write_text(
        json.dumps(entries), encoding="utf-8"
    )


def _write_meson_target_replay_graph(project: Path) -> str:
    (project / "main.c").write_text(
        "int main_value(void) { return 1; }\n", encoding="utf-8"
    )
    (project / "dep.cpp").write_text(
        "int dep_value() { return 2; }\n", encoding="utf-8"
    )
    (project / "unrelated.c").write_text(
        "int unrelated(void) { return 3; }\n", encoding="utf-8"
    )
    build_dir = project / "build" / "pcc-package" / "meson-build"
    scaffold_dir = project / "build" / "scaffold"
    build_dir.mkdir(parents=True)
    scaffold_dir.mkdir(parents=True)
    python_include = "/opt/python3.14/include/python3.14"
    entries = [
        {
            "directory": str(project),
            "file": str(project / "main.c"),
            "command": (
                f"cc -I{python_include} -c {project / 'main.c'} "
                f"-o {scaffold_dir / 'main.o'}"
            ),
        },
        {
            "directory": str(project),
            "file": str(project / "dep.cpp"),
            "command": (
                f"c++ -I{python_include} -D_LIBCPP_ENABLE_ASSERTIONS=1 "
                f"-MD -MF old.d -c {project / 'dep.cpp'} "
                f"-o {scaffold_dir / 'dep.o'}"
            ),
        },
        {
            "directory": str(project),
            "file": str(project / "unrelated.c"),
            "command": (
                f"cc -I{python_include} -c {project / 'unrelated.c'} "
                f"-o {scaffold_dir / 'unrelated.o'}"
            ),
        },
    ]
    (project / "compile_commands.json").write_text(
        json.dumps(entries), encoding="utf-8"
    )
    target = "demo/_native.cpython-314-darwin.so"
    (build_dir / "build.ninja").write_text(
        "\n".join(
            [
                "build demo/_native.cpython-314-darwin.so.p/main.c.o: "
                "c_COMPILER ../../../main.c",
                "build demo/libdep.a.p/dep.cpp.o: cpp_COMPILER ../../../dep.cpp",
                "build demo/libdep.a: STATIC_LINKER demo/libdep.a.p/dep.cpp.o",
                "build demo/unrelated.c.o: c_COMPILER ../../../unrelated.c",
                f"build {target}: cpp_LINKER "
                "demo/_native.cpython-314-darwin.so.p/main.c.o | demo/libdep.a",
                " LINK_ARGS = -bundle -undefined dynamic_lookup demo/libdep.a "
                "-Wl,-dead_strip",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return target


def _write_meson_introspection(project: Path) -> None:
    meson_info = project / "meson-info"
    meson_info.mkdir()
    (project / "meson.build").write_text(
        "project('demo_pkg', 'c', 'fortran')\n", encoding="utf-8"
    )
    entries = [
        {
            "name": "demo_native",
            "type": "shared module",
            "target_sources": [
                {
                    "language": "c",
                    "compiler": ["cc"],
                    "parameters": ["-DMESON_VALUE=1"],
                    "sources": ["native.c"],
                    "generated_sources": [],
                },
                {
                    "language": "fortran",
                    "compiler": ["gfortran"],
                    "parameters": [],
                    "sources": ["solver.f90"],
                    "generated_sources": [],
                },
            ],
        }
    ]
    (meson_info / "intro-targets.json").write_text(
        json.dumps(entries), encoding="utf-8"
    )


def _write_meson_generated_introspection(project: Path) -> tuple[Path, Path]:
    build_dir = project / "build" / "pcc-package" / "meson-build"
    meson_info = build_dir / "meson-info"
    meson_info.mkdir(parents=True)
    (project / "meson.build").write_text("project('demo_pkg', 'c')\n", encoding="utf-8")
    generated_header = build_dir / "generated" / "native_config.h"
    generated_source = build_dir / "generated" / "generated_native.c"
    entries = [
        {
            "name": "native_config",
            "type": "custom",
            "filename": [str(generated_header), str(generated_source)],
            "target_sources": [
                {
                    "language": "c",
                    "compiler": ["cc"],
                    "parameters": [],
                    "sources": [str(project / "native.c")],
                    "generated_sources": [],
                }
            ],
        },
        {
            "name": "demo_native",
            "type": "shared module",
            "target_sources": [
                {
                    "language": "c",
                    "compiler": ["cc"],
                    "parameters": ["-I" + str(generated_header.parent)],
                    "sources": [str(project / "native.c")],
                    "generated_sources": [str(generated_source)],
                },
            ],
        },
    ]
    (meson_info / "intro-targets.json").write_text(
        json.dumps(entries), encoding="utf-8"
    )
    return generated_header, generated_source


def test_execute_build_actions_invokes_fake_generic_toolchain(tmp_path, monkeypatch):
    project = _write_exec_project(tmp_path / "demo_pkg-0.1")
    bin_dir, log = _write_fake_toolchain(tmp_path)
    monkeypatch.setenv("PCC_BUILD_LOG", str(log))

    report = execute_build_actions(
        "demo-pkg",
        project,
        search_paths=[str(bin_dir)],
        execute=True,
        regenerate_cython=True,
        enforce_generated_c=True,
        run_f2py=True, build_mode="host",
    )
    assert report["ok"] is True
    actions = report["actions"]
    assert {action["kind"] for action in actions} >= {
        "cython_regenerate",
        "f2py_build",
        "c_compile",
        "fortran_compile",
    }
    assert {action["status"] for action in actions} == {"passed"}
    log_text = log.read_text(encoding="utf-8")
    assert "cython " in log_text
    assert "f2py " in log_text
    assert "cc " in log_text
    assert "gfortran " in log_text
    assert (project / "demo_pkg.c").exists()
    assert report["generated_c_provenance"][0]["status"] == "up_to_date"


def test_pcc_native_redirects_cpython_includes_to_pcc_capi(tmp_path):
    """pcc-native build drops CPython header include dirs and injects pcc's
    C-API + runtime includes, so a C extension compiles against pcc's object
    model (PyObjectHeader/type_tag) instead of CPython's ABI. Generic — no
    package-specific rules. Package-own includes and macros are preserved."""
    project = tmp_path / "demo_pkg"
    project.mkdir()
    (project / "native.c").write_text("int demo(void){return 0;}\n", encoding="utf-8")
    meson_info = project / "meson-info"
    meson_info.mkdir()
    (project / "meson.build").write_text("project('demo_pkg', 'c')\n", encoding="utf-8")
    cpython_inc = "/opt/Frameworks/Python.framework/Versions/3.14/include/python3.14"
    own_inc = str(project / "src")
    entries = [
        {
            "name": "demo_native",
            "type": "shared module",
            "target_sources": [
                {
                    "language": "c",
                    "compiler": ["cc"],
                    "parameters": ["-I" + cpython_inc, "-I" + own_inc, "-DDEMO=1"],
                    "sources": ["native.c"],
                    "generated_sources": [],
                },
            ],
        }
    ]
    (meson_info / "intro-targets.json").write_text(
        json.dumps(entries), encoding="utf-8"
    )

    report = execute_build_actions(
        "demo-pkg",
        project,
        abi_mode="pcc-native",
        from_meson_introspection=True, build_mode="host",
    )
    c_actions = [
        action
        for action in report["actions"]
        if "native.c" in (action.get("source") or "")
    ]
    assert c_actions, report["actions"]
    cmd = c_actions[0]["command"]
    # CPython header dir dropped entirely.
    assert cpython_inc not in cmd
    assert all("python3.14" not in tok for tok in cmd), cmd
    assert all("Python.framework" not in tok for tok in cmd), cmd
    # pcc's curated C-API include injected (NOT the whole fake_libc_include).
    assert any(
        tok.startswith("-I") and tok.endswith("pcc-capi-include") for tok in cmd
    ), cmd
    # Package's own include dir and macro preserved.
    assert ("-I" + own_inc) in cmd, cmd
    assert "-DDEMO=1" in cmd, cmd


def test_pcc_native_redirect_absent_in_cpython_compat_mode(tmp_path):
    """The redirect is pcc-native only: cpython-compat builds keep the build's
    own (CPython) include dirs untouched."""
    project = tmp_path / "demo_pkg"
    project.mkdir()
    (project / "native.c").write_text("int demo(void){return 0;}\n", encoding="utf-8")
    meson_info = project / "meson-info"
    meson_info.mkdir()
    (project / "meson.build").write_text("project('demo_pkg', 'c')\n", encoding="utf-8")
    cpython_inc = "/opt/Frameworks/Python.framework/Versions/3.14/include/python3.14"
    entries = [
        {
            "name": "demo_native",
            "type": "shared module",
            "target_sources": [
                {
                    "language": "c",
                    "compiler": ["cc"],
                    "parameters": ["-I" + cpython_inc],
                    "sources": ["native.c"],
                    "generated_sources": [],
                },
            ],
        }
    ]
    (meson_info / "intro-targets.json").write_text(
        json.dumps(entries), encoding="utf-8"
    )

    report = execute_build_actions(
        "demo-pkg",
        project,
        abi_mode="cpython-compat",
        from_meson_introspection=True, build_mode="host",
    )
    c_actions = [
        action
        for action in report["actions"]
        if "native.c" in (action.get("source") or "")
    ]
    assert c_actions, report["actions"]
    cmd = c_actions[0]["command"]
    assert ("-I" + cpython_inc) in cmd, cmd
    assert not any(tok.endswith("pcc-capi-include") for tok in cmd), cmd


def test_single_c_source_build_gets_pcc_headers_suffix_and_no_libpython(tmp_path):
    project = _write_single_c_extension_project(tmp_path / "generic-ext-0.1")
    suffix = pcc_native_extension_suffix(current_platform_tag())
    output = "generic_ext/_native" + suffix

    report = execute_build_actions(
        "generic-ext",
        project,
        execute=True,
        link_output=output,
        abi_mode="pcc-native", build_mode="host",
    )

    assert report["ok"] is True, report
    assert (project / output).is_file()
    compile_action = next(
        action for action in report["actions"] if action["kind"] == "c_compile"
    )
    assert "-fPIC" in compile_action["command"]
    assert any(
        token.startswith("-I") and token.endswith("pcc-capi-include")
        for token in compile_action["command"]
    )
    assert report["linkage"]["execution_mode"] == "pcc-native"
    assert report["linkage"]["links_libpython"] is False
    assert report["linkage"]["uses_cpython_extension_abi"] is False


def test_generated_c_policy_blocks_missing_artifact(tmp_path):
    project = _write_exec_project(tmp_path / "demo_pkg-0.1")

    report = execute_build_actions(
        "demo-pkg",
        project,
        execute=False,
        enforce_generated_c=True, build_mode="host",
    )
    assert report["ok"] is False
    assert report["generated_c_provenance"][0]["status"] == "missing"
    assert any(
        diag["code"] == "PCC-PKG-GENERATED-C-MISSING" for diag in report["diagnostics"]
    )


def test_build_exec_excludes_non_build_tree_surfaces(tmp_path):
    project = tmp_path / "demo_pkg-0.1"
    project.mkdir()
    (project / "demo_pkg").mkdir()
    (project / "demo_pkg" / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (project / "demo_pkg" / "_native.pyx").write_text(
        "cdef int value\n", encoding="utf-8"
    )
    (project / "doc" / "examples").mkdir(parents=True)
    (project / "doc" / "examples" / "solver.f90").write_text(
        "subroutine demo()\nend\n", encoding="utf-8"
    )
    (project / "vendored-meson" / "meson" / "test cases" / "cython").mkdir(parents=True)
    (
        project / "vendored-meson" / "meson" / "test cases" / "cython" / "storer.pyx"
    ).write_text(
        "cdef int ignored\n",
        encoding="utf-8",
    )
    (
        project / "vendored-meson" / "meson" / "test cases" / "cython" / "storer.c"
    ).write_text(
        "/* Generated by Cython */\n",
        encoding="utf-8",
    )

    report = execute_build_actions(
        "demo-pkg",
        project,
        execute=False,
        enforce_generated_c=True, build_mode="host",
    )
    assert report["ok"] is False
    assert len(report["generated_c_provenance"]) == 1
    assert report["generated_c_provenance"][0]["pyx"].endswith("demo_pkg/_native.pyx")
    assert all(action["kind"] != "fortran_compile" for action in report["actions"])
    assert not any(
        diag.get("code") == "PCC-PKG-MISSING-FORTRAN"
        for diag in report["diagnostics"]
        if isinstance(diag, dict)
    )


def test_build_exec_does_not_require_vendor_libs_when_meson_allows_fallback(tmp_path):
    project = tmp_path / "demo_pkg-0.1"
    project.mkdir()
    (project / "demo_pkg").mkdir()
    (project / "demo_pkg" / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (project / "demo_pkg" / "blas_probe.c").write_text(
        "int blas_probe(void) { return 0; }\n", encoding="utf-8"
    )
    (project / "demo_pkg" / "lapack_probe.c").write_text(
        "int lapack_probe(void) { return 0; }\n", encoding="utf-8"
    )
    (project / "meson.build").write_text("project('demo_pkg', 'c')\n", encoding="utf-8")
    (project / "meson.options").write_text(
        "option('allow-noblas', type: 'boolean', value: true,\n"
        "        description: 'allow internal fallback routines')\n",
        encoding="utf-8",
    )

    report = execute_build_actions("demo-pkg", project, execute=False, build_mode="host")
    assert report["toolchain"]["requirements"]["blas"] is False
    assert report["toolchain"]["requirements"]["lapack"] is False
    assert not any(
        diag.get("code") in {"PCC-PKG-MISSING-BLAS", "PCC-PKG-MISSING-LAPACK"}
        for diag in report["diagnostics"]
        if isinstance(diag, dict)
    )


def test_build_exec_applies_cython_version_from_pyproject(tmp_path):
    project = tmp_path / "demo_pkg-0.1"
    project.mkdir()
    (project / "demo_pkg").mkdir()
    (project / "demo_pkg" / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (project / "demo_pkg" / "_native.pyx").write_text(
        "cdef int value\n", encoding="utf-8"
    )
    (project / "pyproject.toml").write_text(
        "[build-system]\n"
        "requires = ['Cython>=3.0.6']\n"
        "build-backend = 'mesonpy'\n",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cython = bin_dir / "cython"
    cython.write_text(
        "#!/bin/sh\nprintf '%s\\n' 'Cython version 0.29.36'\nexit 0\n", encoding="utf-8"
    )
    cython.chmod(cython.stat().st_mode | stat.S_IXUSR)

    report = execute_build_actions(
        "demo-pkg",
        project,
        search_paths=[str(bin_dir)],
        execute=False,
        from_meson_introspection=True,
        configure_meson=True, build_mode="host",
    )
    assert any(
        diag.get("code") == "PCC-PKG-CYTHON-VERSION-TOO-OLD"
        for diag in report["diagnostics"]
        if isinstance(diag, dict)
    )


def test_execute_build_actions_links_with_vendor_binding(tmp_path, monkeypatch):
    project = _write_exec_project(tmp_path / "demo_pkg-0.1")
    bin_dir, log = _write_fake_toolchain(tmp_path)
    lib_dir = _write_fake_libraries(tmp_path)
    monkeypatch.setenv("PCC_BUILD_LOG", str(log))

    report = execute_build_actions(
        "demo-pkg",
        project,
        search_paths=[str(bin_dir)],
        library_dirs=[str(lib_dir)],
        execute=True,
        regenerate_cython=True,
        run_f2py=True,
        link_output="demo_pkg/native.so",
        libraries=["blas", "lapack"], build_mode="host",
    )
    assert report["ok"] is True
    assert {binding["link_name"] for binding in report["vendor_bindings"]} == {
        "openblas",
        "lapack",
    }
    assert report["linkage"]["ok"] is True
    assert report["linkage"]["no_libpython_runtime"] is True
    assert any(action["kind"] == "native_link" for action in report["actions"])
    log_text = log.read_text(encoding="utf-8")
    assert "-lopenblas" in log_text
    assert "-llapack" in log_text
    assert (project / "demo_pkg" / "native.so").exists()


def test_execute_build_actions_builds_reusable_numpy_capi_provider_with_include_dirs(
    tmp_path, pcc_runtime_archive,
):
    if shutil.which("cc") is None:
        pytest.fail("C compiler is required for native provider build smoke")
    provider_dir = Path("utils/pcc_numpy_capi_provider").resolve()
    provider_source = provider_dir / "pccnpapi.c"
    fake_include = Path("utils/fake_libc_include").resolve()
    runtime_include = Path("pcc/runtime/include").resolve()
    runtime_lib = Path(pcc_runtime_archive).parent
    if not provider_source.exists():
        pytest.fail("reusable NumPy C-API provider source is not present")
    project = tmp_path / "pccnpapi-src"
    shutil.copytree(provider_dir, project)

    report = execute_build_actions(
        "pccnpapi",
        project,
        include_dirs=[str(fake_include), str(runtime_include)],
        library_dirs=[str(runtime_lib)],
        libraries=["py_runtime_pcc_py"],
        execute=True,
        link_output="pccnpapi.so", build_mode="host",
    )

    assert report["ok"] is True
    compile_actions = [
        action for action in report["actions"] if action["kind"] == "c_compile"
    ]
    assert len(compile_actions) == 1
    command = compile_actions[0]["command"]
    assert "-I" + str(fake_include) in command
    assert "-I" + str(runtime_include) in command
    assert any(action["kind"] == "native_link" for action in report["actions"])
    assert report["linkage"]["no_libpython_runtime"] is True
    assert (project / "pccnpapi.so").exists()


def test_execute_build_actions_uses_compile_commands_as_action_graph(
    tmp_path, monkeypatch
):
    project = _write_exec_project(tmp_path / "demo_pkg-0.1")
    _write_compile_commands(project)
    bin_dir, log = _write_fake_toolchain(tmp_path)
    lib_dir = _write_fake_libraries(tmp_path)
    monkeypatch.setenv("PCC_BUILD_LOG", str(log))

    report = execute_build_actions(
        "demo-pkg",
        project,
        search_paths=[str(bin_dir)],
        library_dirs=[str(lib_dir)],
        execute=True,
        from_compile_commands=True,
        link_output="demo_pkg/from_ccdb.so",
        libraries=["blas"], build_mode="host",
    )
    assert report["ok"] is True
    assert report["from_compile_commands"] is True
    assert [
        action["kind"]
        for action in report["actions"]
        if action["kind"].startswith("compile_command")
    ] == [
        "compile_command_c",
        "compile_command_fortran",
    ]
    assert any(action["kind"] == "native_link" for action in report["actions"])
    log_text = log.read_text(encoding="utf-8")
    assert "native_ccdb.o" in log_text
    assert "solver_ccdb.o" in log_text
    assert "-lopenblas" in log_text


def test_eager_meson_extension_selection_excludes_lazy_function_imports(tmp_path):
    project = tmp_path / "demo_pkg-0.1"
    package = project / "demo_pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("from . import core\n", encoding="utf-8")
    (package / "core.py").write_text(
        "from . import _native\n"
        "def load_optional():\n"
        "    from . import _optional\n",
        encoding="utf-8",
    )
    build = project / "build" / "pcc-package" / "meson-build"
    meson_info = build / "meson-info"
    meson_info.mkdir(parents=True)
    entries = [
        {
            "name": name,
            "type": "shared module",
            "filename": [str(build / "demo_pkg" / filename)],
        }
        for name, filename in (
            ("_native.cpython-313-darwin", "_native.cpython-313-darwin.so"),
            ("_optional.cpython-313-darwin", "_optional.cpython-313-darwin.so"),
        )
    ]
    (meson_info / "intro-targets.json").write_text(
        json.dumps(entries), encoding="utf-8"
    )

    selected = discover_eager_meson_extension_targets("demo-pkg", project)

    assert [row["module"] for row in selected] == ["demo_pkg._native"]
    assert selected[0]["target"] == "demo_pkg/_native.cpython-313-darwin.so"
    assert selected[0]["output"].endswith(
        "demo_pkg/_native" + pcc_native_extension_suffix(current_platform_tag())
    )


def test_execute_build_actions_replays_one_meson_target_into_fresh_objects(
    tmp_path, monkeypatch
):
    project = _write_exec_project(tmp_path / "demo_pkg-0.1")
    target = _write_meson_target_replay_graph(project)
    bin_dir, log = _write_fake_toolchain(tmp_path)
    monkeypatch.setenv("PCC_BUILD_LOG", str(log))

    report = execute_build_actions(
        "demo-pkg",
        project,
        search_paths=[str(bin_dir)],
        execute=True,
        from_compile_commands=True,
        meson_target=target,
        link_output="demo_pkg/_native.pcc3-pcc_native-macosx_14_0_arm64.so",
        jobs=2, build_mode="host",
    )

    assert report["ok"] is True
    assert report["meson_target"] == target
    assert report["jobs"] == 2
    assert report["meson_target_replay"]["objects"] == [
        "demo/_native.cpython-314-darwin.so.p/main.c.o",
        "demo/libdep.a.p/dep.cpp.o",
    ]
    compile_actions = [
        action
        for action in report["actions"]
        if action["kind"].startswith("compile_command_")
    ]
    assert [action["kind"] for action in compile_actions] == [
        "compile_command_c",
        "compile_command_cxx",
    ]
    assert all(
        "build/pcc-package/pcc-native-target/objects" in action["output"]
        for action in compile_actions
    )
    assert not any(
        "python3.14" in token
        for action in compile_actions
        for token in action["command"]
    )
    cxx_command = compile_actions[1]["command"]
    assert "-D_LIBCPP_ENABLE_ASSERTIONS=1" not in cxx_command
    assert "-D_LIBCPP_HARDENING_MODE=_LIBCPP_HARDENING_MODE_FAST" in cxx_command
    assert "-MD" not in cxx_command
    assert "-MF" not in cxx_command
    link_action = next(
        action for action in report["actions"] if action["kind"] == "native_link"
    )
    assert Path(link_action["command"][0]).name == "c++"
    assert sum(token.endswith(".o") for token in link_action["command"]) == 2
    assert "demo/libdep.a" not in link_action["command"]
    assert not any("unrelated" in token for token in link_action["command"])
    assert "-Wl,-dead_strip" in link_action["command"]
    assert Path(link_action["output"]).name.endswith("pcc_native-macosx_14_0_arm64.so")


def test_execute_build_actions_uses_meson_introspection_as_action_graph(
    tmp_path, monkeypatch
):
    project = _write_exec_project(tmp_path / "demo_pkg-0.1")
    _write_meson_introspection(project)
    bin_dir, log = _write_fake_toolchain(tmp_path)
    lib_dir = _write_fake_libraries(tmp_path)
    monkeypatch.setenv("PCC_BUILD_LOG", str(log))

    report = execute_build_actions(
        "demo-pkg",
        project,
        search_paths=[str(bin_dir)],
        library_dirs=[str(lib_dir)],
        execute=True,
        from_meson_introspection=True,
        link_output="demo_pkg/from_meson.so",
        libraries=["blas"], build_mode="host",
    )
    assert report["ok"] is True
    assert report["from_meson_introspection"] is True
    assert [
        action["kind"]
        for action in report["actions"]
        if action["kind"].startswith("meson_compile")
    ] == [
        "meson_compile_c",
        "meson_compile_fortran",
    ]
    assert any(action["kind"] == "native_link" for action in report["actions"])
    log_text = log.read_text(encoding="utf-8")
    assert "native.c" in log_text
    assert "solver.f90" in log_text
    assert "-lopenblas" in log_text


def test_execute_build_actions_can_configure_meson_before_introspection(
    tmp_path, monkeypatch
):
    project = _write_exec_project(tmp_path / "demo_pkg-0.1")
    (project / "meson.build").write_text("project('demo_pkg', 'c')\n", encoding="utf-8")
    bin_dir, log = _write_fake_toolchain(tmp_path)
    _write_fake_meson_tool(bin_dir)
    monkeypatch.setenv("PCC_BUILD_LOG", str(log))
    monkeypatch.setenv("PCC_FAKE_MESON_SOURCE", str(project / "native.c"))

    report = execute_build_actions(
        "demo-pkg",
        project,
        search_paths=[str(bin_dir)],
        execute=True,
        from_meson_introspection=True,
        configure_meson=True,
        link_output="demo_pkg/from_configured_meson.so", build_mode="host",
    )
    assert report["ok"] is True
    assert report["configure_meson"] is True
    assert [action["kind"] for action in report["actions"]] == [
        "meson_setup",
        "meson_compile_c",
        "native_link",
    ]
    log_text = log.read_text(encoding="utf-8")
    assert "meson setup" in log_text
    assert "native.c" in log_text
    assert (project / "demo_pkg" / "from_configured_meson.so").exists()


def test_execute_build_actions_materializes_meson_generated_targets(
    tmp_path, monkeypatch
):
    project = _write_exec_project(tmp_path / "demo_pkg-0.1")
    generated_header, generated_source = _write_meson_generated_introspection(project)
    bin_dir, log = _write_fake_toolchain(tmp_path)
    _write_fake_ninja_tool(bin_dir)
    monkeypatch.setenv("PCC_BUILD_LOG", str(log))
    monkeypatch.setenv(
        "PCC_FAKE_NINJA_TARGETS",
        "\n".join(
            [
                "generated/ninja_only.h: CUSTOM_COMMAND",
                "meson-internal__test: CUSTOM_COMMAND",
                "meson-internal__install: CUSTOM_COMMAND",
                "all: CUSTOM_COMMAND",
                "demo_native: CUSTOM_COMMAND",
            ]
        ),
    )

    report = execute_build_actions(
        "demo-pkg",
        project,
        search_paths=[str(bin_dir)],
        execute=True,
        from_meson_introspection=True,
        link_output="demo_pkg/from_generated_meson.so", build_mode="host",
    )
    assert report["ok"] is True
    assert [action["kind"] for action in report["actions"][:3]] == [
        "meson_generated_targets",
        "meson_compile_c",
        "meson_compile_c",
    ]
    assert generated_header.exists()
    assert generated_source.exists()
    assert (
        project / "build" / "pcc-package" / "meson-build" / "generated" / "ninja_only.h"
    ).exists()
    log_text = log.read_text(encoding="utf-8")
    assert "ninja -C" in log_text
    assert "generated/native_config.h" in log_text
    assert "generated/generated_native.c" in log_text
    assert "generated/ninja_only.h" in log_text
    assert "meson-internal__test" not in log_text
    assert "meson-internal__install" not in log_text
    materialize_commands = [
        line
        for line in log_text.splitlines()
        if line.startswith("ninja -C") and " -t targets all" not in line
    ]
    assert materialize_commands
    assert " all" not in materialize_commands[0]


def test_execute_build_actions_blocks_libpython_linkage(tmp_path, monkeypatch):
    project = _write_exec_project(tmp_path / "demo_pkg-0.1")
    bin_dir, log = _write_fake_toolchain(tmp_path)
    monkeypatch.setenv("PCC_BUILD_LOG", str(log))
    monkeypatch.setenv("PCC_FAKE_OUTPUT_TEXT", "libpython3.13.dylib")

    report = execute_build_actions(
        "demo-pkg",
        project,
        search_paths=[str(bin_dir)],
        execute=True,
        link_output="demo_pkg/bad.so", build_mode="host",
    )
    assert report["ok"] is False
    assert report["linkage"]["ok"] is False
    assert report["linkage"]["links_libpython"] is True
    assert report["linkage"]["diagnostics"][0]["code"] == "PCC-PKG-003"


def test_pcc_package_build_exec_cli(tmp_path):
    project = _write_exec_project(tmp_path / "demo_pkg-0.1")
    bin_dir, log = _write_fake_toolchain(tmp_path)
    lib_dir = _write_fake_libraries(tmp_path)
    include_dir = tmp_path / "include"
    include_dir.mkdir()
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_BUILD_LOG"] = str(log)
    proc = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "build-exec",
            "--build-mode=host",
            "demo-pkg",
            "--path",
            str(project),
            "--search-path",
            str(bin_dir),
            "--include-dir",
            str(include_dir),
            "--library-dir",
            str(lib_dir),
            "--execute",
            "--regenerate-cython",
            "--enforce-generated-c",
            "--run-f2py",
            "--link-output",
            "demo_pkg/native.so",
            "--library",
            "blas",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    report = json.loads(proc.stdout)
    assert report["ok"] is True
    assert str(include_dir) in report["include_dirs"]
    assert any(action["kind"] == "cython_regenerate" for action in report["actions"])
    assert any(action["kind"] == "native_link" for action in report["actions"])
    assert "-I" + str(include_dir) in log.read_text(encoding="utf-8")


def test_pcc1_build_exec_does_not_need_host_python(tmp_path):
    pcc1 = _find_current_pcc1()
    if pcc1 is None:
        skip_or_fail_no_current_pcc1(
            "no current pcc1 binary with native build-exec shim"
        )
    project = _write_exec_project(tmp_path / "demo_pkg-0.1")
    bin_dir, log = _write_fake_toolchain(tmp_path)
    lib_dir = _write_fake_libraries(tmp_path)
    include_dir = tmp_path / "include"
    include_dir.mkdir()
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    env["PCC_BUILD_LOG"] = str(log)
    proc = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "build-exec",
            "--build-mode=host",
            "demo-pkg",
            "--path",
            str(project),
            "--search-path",
            str(bin_dir),
            "--include-dir",
            str(include_dir),
            "--library-dir",
            str(lib_dir),
            "--execute",
            "--regenerate-cython",
            "--enforce-generated-c",
            "--run-f2py",
            "--link-output",
            "demo_pkg/native.so",
            "--library",
            "blas",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    report = json.loads(proc.stdout)
    assert report["ok"] is True
    assert str(include_dir) in report["include_dirs"]
    assert any(action["kind"] == "fortran_compile" for action in report["actions"])
    assert any(action["kind"] == "native_link" for action in report["actions"])
    assert report["vendor_bindings"][0]["found"] is True
    assert report["generated_c_provenance"][0]["status"] == "up_to_date"
    assert "-I" + str(include_dir) in log.read_text(encoding="utf-8")
    assert "PCC_HOST_PYTHON" in env


def test_pcc1_build_exec_builds_reusable_numpy_capi_provider_without_host_python(
    tmp_path, pcc_runtime_archive,
):
    pcc1 = _find_current_pcc1()
    if pcc1 is None:
        skip_or_fail_no_current_pcc1(
            "no current pcc1 binary with native build-exec shim"
        )
    cc_path = shutil.which("cc")
    if cc_path is None:
        pytest.fail("C compiler is required for native provider build smoke")
    provider_dir = Path("utils/pcc_numpy_capi_provider").resolve()
    provider_source = provider_dir / "pccnpapi.c"
    fake_include = Path("utils/fake_libc_include").resolve()
    runtime_include = Path("pcc/runtime/include").resolve()
    runtime_lib = Path(pcc_runtime_archive).parent
    if not provider_source.exists():
        pytest.fail("reusable NumPy C-API provider source is not present")
    project = tmp_path / "pccnpapi-src"
    shutil.copytree(provider_dir, project)
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"

    proc = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "build-exec",
            "--build-mode=host",
            "pccnpapi",
            "--path",
            str(project),
            "--search-path",
            str(Path(cc_path).parent),
            "--include-dir",
            str(fake_include),
            "--include-dir",
            str(runtime_include),
            "--library-dir",
            str(runtime_lib),
            "--library",
            "py_runtime_pcc_py",
            "--execute",
            "--link-output",
            "pccnpapi.so",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    report = json.loads(proc.stdout)
    assert report["ok"] is True
    assert report["include_dirs"] == [str(fake_include), str(runtime_include)]
    assert [action["kind"] for action in report["actions"]] == [
        "c_compile",
        "native_link",
    ]
    assert {action["status"] for action in report["actions"]} == {"passed"}
    assert report["linkage"]["no_libpython_runtime"] is True
    assert (project / "pccnpapi.so").exists()
    assert "PCC_HOST_PYTHON" in env


def test_pcc1_generated_c_policy_blocks_missing_artifact(tmp_path):
    pcc1 = _find_current_pcc1()
    if pcc1 is None:
        skip_or_fail_no_current_pcc1(
            "no current pcc1 binary with native build-exec shim"
        )
    project = _write_exec_project(tmp_path / "demo_pkg-0.1")
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    proc = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "build-exec",
            "--build-mode=host",
            "demo-pkg",
            "--path",
            str(project),
            "--enforce-generated-c",
            "--json",
        ],
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    assert proc.returncode == 2
    report = json.loads(proc.stdout)
    assert report["ok"] is False
    assert any(
        diag["code"] == "PCC-PKG-GENERATED-C-MISSING" for diag in report["diagnostics"]
    )


def test_pcc1_build_exec_compile_commands_does_not_need_host_python(tmp_path):
    pcc1 = _find_current_pcc1()
    if pcc1 is None:
        skip_or_fail_no_current_pcc1(
            "no current pcc1 binary with native build-exec shim"
        )
    project = _write_exec_project(tmp_path / "demo_pkg-0.1")
    _write_compile_commands(project)
    bin_dir, log = _write_fake_toolchain(tmp_path)
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    env["PCC_BUILD_LOG"] = str(log)
    proc = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "build-exec",
            "--build-mode=host",
            "demo-pkg",
            "--path",
            str(project),
            "--search-path",
            str(bin_dir),
            "--execute",
            "--from-compile-commands",
            "--link-output",
            "demo_pkg/from_ccdb.so",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    report = json.loads(proc.stdout)
    assert report["ok"] is True
    assert report["from_compile_commands"] is True
    assert [
        action["kind"]
        for action in report["actions"]
        if action["kind"].startswith("compile_command")
    ] == ["compile_command_c", "compile_command_fortran"]
    assert any(action["kind"] == "native_link" for action in report["actions"])


def test_pcc1_build_exec_meson_introspection_does_not_need_host_python(tmp_path):
    pcc1 = _find_current_pcc1()
    if pcc1 is None:
        skip_or_fail_no_current_pcc1(
            "no current pcc1 binary with native build-exec shim"
        )
    project = _write_exec_project(tmp_path / "demo_pkg-0.1")
    _write_meson_introspection(project)
    bin_dir, log = _write_fake_toolchain(tmp_path)
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    env["PCC_BUILD_LOG"] = str(log)
    proc = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "build-exec",
            "--build-mode=host",
            "demo-pkg",
            "--path",
            str(project),
            "--search-path",
            str(bin_dir),
            "--execute",
            "--from-meson-introspection",
            "--link-output",
            "demo_pkg/from_meson.so",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    report = json.loads(proc.stdout)
    assert report["ok"] is True
    assert report["from_meson_introspection"] is True
    assert any(action["kind"] == "meson_compile_c" for action in report["actions"])
    assert any(
        action["kind"] == "meson_compile_fortran" for action in report["actions"]
    )
    assert any(action["kind"] == "native_link" for action in report["actions"])


def test_pcc1_build_exec_can_configure_meson_without_host_python(tmp_path):
    pcc1 = _find_current_pcc1()
    if pcc1 is None:
        skip_or_fail_no_current_pcc1(
            "no current pcc1 binary with native build-exec shim"
        )
    project = _write_exec_project(tmp_path / "demo_pkg-0.1")
    (project / "meson.build").write_text("project('demo_pkg', 'c')\n", encoding="utf-8")
    bin_dir, log = _write_fake_toolchain(tmp_path)
    _write_fake_meson_tool(bin_dir)
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    env["PCC_BUILD_LOG"] = str(log)
    env["PCC_FAKE_MESON_SOURCE"] = str(project / "native.c")
    proc = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "build-exec",
            "--build-mode=host",
            "demo-pkg",
            "--path",
            str(project),
            "--search-path",
            str(bin_dir),
            "--execute",
            "--from-meson-introspection",
            "--configure-meson",
            "--link-output",
            "demo_pkg/from_configured_meson.so",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    report = json.loads(proc.stdout)
    assert report["ok"] is True
    assert report["configure_meson"] is True
    assert [action["kind"] for action in report["actions"]] == [
        "meson_setup",
        "meson_compile_c",
        "native_link",
    ]
    assert "PCC_HOST_PYTHON" in env


def test_pcc1_build_exec_materializes_meson_generated_targets_without_host_python(
    tmp_path,
):
    pcc1 = _find_current_pcc1()
    if pcc1 is None:
        skip_or_fail_no_current_pcc1(
            "no current pcc1 binary with native build-exec shim"
        )
    project = _write_exec_project(tmp_path / "demo_pkg-0.1")
    _write_meson_generated_introspection(project)
    bin_dir, log = _write_fake_toolchain(tmp_path)
    _write_fake_ninja_tool(bin_dir)
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    env["PCC_BUILD_LOG"] = str(log)
    env["PCC_FAKE_NINJA_TARGETS"] = "\n".join(
        [
            "generated/ninja_only.h: CUSTOM_COMMAND",
            "meson-internal__test: CUSTOM_COMMAND",
        ]
    )
    proc = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "build-exec",
            "--build-mode=host",
            "demo-pkg",
            "--path",
            str(project),
            "--search-path",
            str(bin_dir),
            "--execute",
            "--from-meson-introspection",
            "--link-output",
            "demo_pkg/from_generated_meson.so",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    report = json.loads(proc.stdout)
    assert report["ok"] is True
    assert any(
        action["kind"] == "meson_generated_targets" for action in report["actions"]
    )
    log_text = log.read_text(encoding="utf-8")
    assert "generated/ninja_only.h" in log_text
    assert "meson-internal__test" not in log_text
