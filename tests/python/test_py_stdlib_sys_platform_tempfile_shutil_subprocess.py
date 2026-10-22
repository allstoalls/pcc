from __future__ import annotations

import subprocess
import sys as host_sys
from pathlib import Path

from pcc.stdlib import platform
from pcc.stdlib import shlex
from pcc.stdlib import shutil
from pcc.stdlib import sys
from pcc.stdlib import tempfile


def test_sys_version_streams_and_implementation():
    assert sys.version_info.major == 3
    assert tuple(sys.version_info)[:3] == (3, 15, 0)
    assert sys.version == "3.15.0 (pcc self-host)"
    assert sys.stdout.write("x") == 1
    assert sys.stderr.write("err") == 3
    assert sys.getdefaultencoding() == "utf-8"
    assert sys.implementation.name == "pcc"
    assert sys.intern("abc") == "abc"
    assert sys.byteorder == "little"


def test_platform_subset():
    assert platform.system() in ("Darwin", "Linux", "Windows", "darwin")
    assert isinstance(platform.machine(), str)
    assert platform.python_implementation() == host_sys.implementation.name
    assert platform.python_version_tuple() == ("3", "15", "0")
    assert platform.python_version() == "3.15.0"
    assert len(platform.uname()) == 6


def test_platform_provider_compiles_as_top_level_module_no_libpython(tmp_path):
    from pcc.frontends.python.pipeline import compile_python_multi

    entry = tmp_path / "entry.py"
    entry.write_text(
        "import platform\n"
        "print(platform.system())\n"
        "print(platform.python_implementation())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "platform_provider.out"
    compile_python_multi(
        [
            str(Path("pcc/stdlib/platform.py")),
            str(Path("pcc/stdlib/sys.py")),
            str(entry),
        ],
        str(exe),
        entry_module="entry",
        module_names=["platform", "sys", "entry"],
        backend="self",
        libpython_mode="off",
    )
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert run.returncode == 0, run.stderr
    lines = run.stdout.splitlines()
    assert lines[0] in ("Darwin", "Linux", "Windows")
    assert lines[1] == "pcc"


def test_tempfile_and_shutil_file_ops(tmp_path):
    td = tempfile.TemporaryDirectory(dir=str(tmp_path))
    try:
        p = Path(td.name) / "a.txt"
        p.write_text("hello", encoding="utf-8")
        dst = Path(td.name) / "b.txt"
        assert shutil.copyfile(str(p), str(dst)) == str(dst)
        assert dst.read_text(encoding="utf-8") == "hello"
        moved = Path(td.name) / "c.txt"
        assert shutil.move(str(dst), str(moved)) == str(moved)
        assert moved.read_text(encoding="utf-8") == "hello"
    finally:
        td.cleanup()

    ntf = tempfile.NamedTemporaryFile(dir=str(tmp_path), mode="w+")
    try:
        ntf.write("x")
        ntf.flush()
        assert Path(ntf.name).read_text(encoding="utf-8") == "x"
    finally:
        ntf.close()


def test_shlex_existing_surface_still_works():
    assert shlex.split("a 'b c'") == ["a", "b c"]
    assert shlex.join(["a", "b c"]) == "a 'b c'"


def test_subprocess_runtime_wrapper_symbols_are_wired():
    src = Path("pcc/stdlib/subprocess.py").read_text(encoding="utf-8")
    assert "py_subprocess_check_output" in src
    assert "py_subprocess_run" in src
    assert "CompletedProcess" in src


def test_subprocess_called_process_error_export_matches_object_int_scaffold_abi():
    from pcc.frontends.python.pipeline import build_closed_world_context
    from pcc.frontends.python.codegen.layer1_support import (
        _default_native_module_exports,
    )

    provider = Path("pcc/stdlib/subprocess.py")
    _modules, exports, _derived = build_closed_world_context(
        [str(provider)],
        ["subprocess"],
    )
    provider_export = exports["subprocess"]["CalledProcessError"]
    methods = provider_export["methods"]
    init_export = next(method for method in methods if method["name"] == "__init__")
    assert init_export["box_int_abi"] is True

    static_exports = _default_native_module_exports("pcc.driver.cli_bootstrap")
    static_export = static_exports["subprocess"]["CalledProcessError"]
    parallel_exports = _default_native_module_exports(
        "pcc.frontends.python.pipeline_frontend_parallel"
    )
    assert parallel_exports is not None
    assert parallel_exports["subprocess"]["CalledProcessError"] == static_export
    for key in ("class_name", "base_names", "field_names", "field_types"):
        assert static_export[key] == provider_export[key]
    static_init = static_export["methods"][0]
    for key in ("symbol", "return_ty", "param_types", "box_int_abi"):
        if key == "param_types":
            assert tuple(static_init[key]) == tuple(init_export[key])
        else:
            assert static_init[key] == init_export[key]
