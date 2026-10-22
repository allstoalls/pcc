"""Focused source for the import-safe developer/build-tool stdlib batch."""
from __future__ import annotations

import cProfile as host_cprofile
import compileall as host_compileall
import io
import os
import subprocess
import sys
import zipapp as host_zipapp
import zipfile
from profiling import tracing as host_tracing

import pytest

from pcc.stdlib import cProfile as port_cprofile
from pcc.stdlib import compileall as port_compileall
from pcc.stdlib import zipapp as port_zipapp
from pcc.stdlib.profiling import tracing as port_tracing


def test_devtool_public_exports_match_cpython():
    assert port_cprofile.__all__ == host_cprofile.__all__
    assert port_tracing.__all__ == host_tracing.__all__
    assert port_compileall.__all__ == host_compileall.__all__
    assert port_zipapp.__all__ == host_zipapp.__all__


def _write_zipapp_source(root):
    root.mkdir()
    (root / "__main__.py").write_text(
        "from demo import value\nprint(value())\n", encoding="utf-8"
    )
    (root / "demo.py").write_text(
        "def value():\n    return 'zipapp-ok'\n", encoding="utf-8"
    )
    package = root / "package"
    package.mkdir()
    (package / "data.txt").write_bytes(b"developer tool payload\n")


def _archive_surface(path):
    with zipfile.ZipFile(path) as archive:
        result = []
        for name in sorted(archive.namelist()):
            info = archive.getinfo(name)
            result.append(
                (name, info.flag_bits, info.compress_type, archive.read(name))
            )
        return result


def test_zipapp_directory_archive_matches_cpython_surface(tmp_path):
    source = tmp_path / "app"
    _write_zipapp_source(source)
    port_target = tmp_path / "port.pyz"
    host_target = tmp_path / "host.pyz"
    interpreter = "/usr/bin/env python3"

    port_zipapp.create_archive(
        source,
        port_target,
        interpreter=interpreter,
        compressed=True,
    )
    host_zipapp.create_archive(
        source,
        host_target,
        interpreter=interpreter,
        compressed=True,
    )

    assert port_zipapp.get_interpreter(port_target) == interpreter
    assert host_zipapp.get_interpreter(host_target) == interpreter
    assert _archive_surface(port_target) == _archive_surface(host_target)


def test_zipapp_generated_entry_point_and_fail_closed_edges(tmp_path):
    source = tmp_path / "generated"
    source.mkdir()
    (source / "worker.py").write_text(
        "def main():\n    print('generated')\n", encoding="utf-8"
    )
    target = tmp_path / "generated.pyz"
    port_zipapp.create_archive(source, target, main="worker:main")
    with zipfile.ZipFile(target) as archive:
        assert archive.read("worker.py").startswith(b"def main")
        assert archive.read("__main__.py").endswith(b"worker.main()\n")

    with pytest.raises(NotImplementedError, match="filter callbacks"):
        port_zipapp.create_archive(source, tmp_path / "filtered.pyz", filter=bool)
    with pytest.raises(port_zipapp.ZipAppError, match="outside"):
        port_zipapp.create_archive(source, source / "nested.pyz", main="worker:main")

    outside = tmp_path / "outside.py"
    outside.write_text("VALUE = 1\n", encoding="utf-8")
    link_source = tmp_path / "linked"
    link_source.mkdir()
    os.symlink(outside, link_source / "outside.py")
    with pytest.raises(port_zipapp.ZipAppError, match="escapes"):
        port_zipapp.create_archive(
            link_source,
            tmp_path / "linked.pyz",
            main="outside:VALUE",
        )


def test_zipapp_default_target_and_interpreter_stream_contract(tmp_path):
    source = tmp_path / "bundle.v1"
    _write_zipapp_source(source)
    port_zipapp.create_archive(source, interpreter="")
    target = tmp_path / "bundle.pyz"
    assert target.is_file()
    assert port_zipapp.get_interpreter(target) is None

    stream = io.BytesIO(b"PKremaining archive bytes")
    assert port_zipapp.get_interpreter(stream) is None
    assert stream.tell() == 2

    target_stream = io.BytesIO()
    port_zipapp.create_archive(source, target_stream)
    target_stream.seek(0)
    with zipfile.ZipFile(target_stream) as archive:
        assert archive.read("demo.py").startswith(b"def value")
    nonzero_target = io.BytesIO(b"prefix")
    nonzero_target.seek(1)
    with pytest.raises(port_zipapp.ZipAppError, match="offset zero"):
        port_zipapp.create_archive(source, nonzero_target)

    with pytest.raises(port_zipapp.ZipAppError, match="does not exist"):
        port_zipapp.create_archive(tmp_path / "missing", tmp_path / "none.pyz")
    with pytest.raises(NotImplementedError, match="source-archive copying"):
        port_zipapp.create_archive(target, tmp_path / "copy.pyz")


def test_compileall_reports_no_cpython_bytecode_honestly(tmp_path):
    missing = tmp_path / "missing.py"
    assert port_compileall.compile_file(missing, quiet=2) is True
    assert host_compileall.compile_file(missing, quiet=2) is True

    non_python = tmp_path / "README"
    non_python.write_text("source data", encoding="utf-8")
    assert port_compileall.compile_file(non_python, quiet=2) is True
    assert host_compileall.compile_file(non_python, quiet=2) is True

    source = tmp_path / "module.py"
    source.write_text("VALUE = 42\n", encoding="utf-8")
    assert port_compileall.compile_file(source, quiet=2) is False
    assert not (tmp_path / "__pycache__").exists()

    empty = tmp_path / "empty"
    empty.mkdir()
    assert port_compileall.compile_dir(empty, quiet=2) is True
    assert host_compileall.compile_dir(empty, quiet=2) is True
    missing_dir = tmp_path / "missing-dir"
    assert port_compileall.compile_dir(missing_dir, quiet=2) is True
    assert host_compileall.compile_dir(missing_dir, quiet=2) is True

    nested = tmp_path / "nested"
    nested.mkdir()
    child = nested / "child"
    child.mkdir()
    (child / "module.py").write_text("VALUE = 7\n", encoding="utf-8")
    assert port_compileall.compile_dir(nested, maxlevels=0, quiet=2) is True
    assert host_compileall.compile_dir(nested, maxlevels=0, quiet=2) is True
    assert port_compileall.compile_dir(nested, maxlevels=-1, quiet=2) is True
    with pytest.raises(NotImplementedError, match="symlink destination"):
        port_compileall.compile_file(
            child / "module.py", quiet=2, limit_sl_dest=nested
        )


def test_cprofile_import_state_and_sampling_boundary():
    # CPython 3.15's cProfile re-exports profiling.tracing's run, runctx and
    # Profile; label stays in profiling.tracing.
    assert port_cprofile.Profile is port_tracing.Profile
    assert port_cprofile.Profile().getstats() == host_cprofile.Profile().getstats()
    assert port_cprofile.Profile().clear() is None
    assert hasattr(port_cprofile, "label") == hasattr(host_cprofile, "label")
    assert port_tracing.label("native") == host_tracing.label("native")
    with pytest.raises(NotImplementedError, match="profiling events"):
        port_cprofile.Profile().enable()
    with pytest.raises(NotImplementedError, match="profiling events"):
        port_cprofile.runctx("pass", {}, {})


@pytest.mark.parametrize("module_name", ["cProfile", "compileall", "zipapp"])
def test_devtool_ports_are_selected_by_recursive_stdlib_registry(module_name):
    from pcc.frontends.python import pipeline

    source = pipeline._locate_native_stdlib_module_source(module_name)
    assert source is not None
    assert source.endswith("/pcc/stdlib/" + module_name + ".py")
    assert module_name not in pipeline._NATIVE_BUILTIN_IMPORTS


@pytest.mark.integration
def test_devtool_imports_and_zipapp_match_cpython_no_libpython(tmp_path):
    source_tree = tmp_path / "compiled-app"
    _write_zipapp_source(source_tree)
    archive_path = tmp_path / "compiled-app.pyz"
    generated_tree = tmp_path / "compiled-generated"
    generated_tree.mkdir()
    (generated_tree / "worker.py").write_text(
        "def main():\n    print('compiled-generated')\n", encoding="utf-8"
    )
    generated_archive = tmp_path / "compiled-generated.pyz"
    missing_path = tmp_path / "never-created.py"
    compile_source = tmp_path / "native-compile.py"
    compile_source.write_text("VALUE = 99\n", encoding="utf-8")
    empty_compile_dir = tmp_path / "empty-compile-dir"
    empty_compile_dir.mkdir()
    source = '''\
import cProfile
import compileall
import profiling.tracing
import zipapp
import zipfile

print("cprofile-empty", cProfile.Profile().getstats())
print("cprofile-label", profiling.tracing.label("native"))
print("compileall-missing", compileall.compile_file(%(missing)r, quiet=2))
print("compileall-empty", compileall.compile_dir(%(empty_dir)r, quiet=2))
print("compileall-source", compileall.compile_file(%(compile_source)r, quiet=2))
zipapp.create_archive(
    %(source_tree)r,
    %(archive_path)r,
    interpreter="/usr/bin/env python3",
    compressed=True,
)
print("interpreter", zipapp.get_interpreter(%(archive_path)r))
with zipfile.ZipFile(%(archive_path)r) as archive:
    for name in sorted(archive.namelist()):
        info = archive.getinfo(name)
        print("member", name, info.compress_type, archive.read(name))
zipapp.create_archive(
    %(generated_tree)r,
    %(generated_archive)r,
    main="worker:main",
)
with zipfile.ZipFile(%(generated_archive)r) as archive:
    print("generated-main", archive.read("__main__.py"))
''' % {
        "missing": str(missing_path),
        "empty_dir": str(empty_compile_dir),
        "compile_source": str(compile_source),
        "source_tree": str(source_tree),
        "archive_path": str(archive_path),
        "generated_tree": str(generated_tree),
        "generated_archive": str(generated_archive),
    }
    src = tmp_path / "devtool_probe.py"
    src.write_text(source, encoding="utf-8")
    executable = tmp_path / "devtool_probe"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env.pop("PCC_RUNTIME_CC", None)

    build = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(src),
            "-o",
            str(executable),
        ],
        text=True,
        capture_output=True,
        timeout=900,
        env=env,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    assert "PCC-PY-COMPILE-001" not in build.stdout + build.stderr

    no_host_env = env.copy()
    no_host_env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    no_host_env["PATH"] = str(tmp_path / "no-host-python")
    actual = subprocess.run(
        [str(executable)],
        text=True,
        capture_output=True,
        timeout=120,
        env=no_host_env,
    )
    assert actual.returncode == 0, actual.stdout + actual.stderr
    assert not (tmp_path / "__pycache__").exists()
    expected = subprocess.run(
        [sys.executable, str(src)],
        text=True,
        capture_output=True,
        timeout=120,
        env=env,
    )
    assert expected.returncode == 0, expected.stdout + expected.stderr
    assert "compileall-source False\n" in actual.stdout
    assert "compileall-source True\n" in expected.stdout
    assert actual.stdout.replace(
        "compileall-source False\n", "compileall-source True\n"
    ) == expected.stdout
