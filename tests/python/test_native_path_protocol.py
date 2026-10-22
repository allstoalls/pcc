"""Owned os/pathlib providers coexist with native filesystem intrinsics."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess

import pytest


PATH_PROTOCOL_SOURCE = '''import os
from os import fspath
from pathlib import Path, PurePath

class ExplicitPath(os.PathLike):
    def __fspath__(self):
        return "explicit.py"

class BytesPath:
    def __fspath__(self):
        return b"bytes.py"

class BrokenPath:
    def __fspath__(self):
        raise AttributeError("fspath-body-error")

class InvalidPath:
    def __fspath__(self):
        return 42

def check():
    print(os.fspath("text.py"))
    print(fspath(b"raw.py"))
    path = ExplicitPath()
    print(isinstance(path, (str, os.PathLike)), os.fspath(path))
    print(os.fspath(BytesPath()))
    print(isinstance(Path("native.py"), os.PathLike))
    print(fspath(Path("native.py")))
    print(fspath(PurePath("pure.py")))
    try:
        os.fspath(BrokenPath())
    except AttributeError as exc:
        print(str(exc))
    try:
        os.fspath(InvalidPath())
    except TypeError:
        print("invalid-result")
    try:
        os.fspath(42)
    except TypeError:
        print("missing-protocol")
    print(os.getenv("PCC_PATH_PROTOCOL_CONTROL"))
    print(os.path.join("directory", "leaf.py"))
    print(len(os.getcwd()) > 0)
check()
'''
PATH_PROTOCOL_STDOUT = (
    "text.py\nb'raw.py'\nTrue explicit.py\nb'bytes.py'\nTrue\n"
    "native.py\npure.py\nfspath-body-error\ninvalid-result\n"
    "missing-protocol\nintrinsic-ok\ndirectory/leaf.py\nTrue\n"
)


def test_owned_fspath_preserves_special_lookup_and_method_errors():
    from pcc.stdlib import os as owned_os

    error = AttributeError("fspath-body-error")

    class BrokenPath:
        def __fspath__(self):
            raise error

    class InstanceOnly:
        pass

    value = InstanceOnly()
    value.__fspath__ = lambda: "instance-only"
    for fspath in (os.fspath, owned_os.fspath):
        with pytest.raises(AttributeError) as caught:
            fspath(BrokenPath())
        assert caught.value is error
        with pytest.raises(TypeError):
            fspath(value)


@pytest.mark.parametrize("recursive", [False, True])
def test_os_semantic_provider_is_admitted(tmp_path, recursive):
    from pcc.frontends.python import pipeline_dependency_closure as closure

    source = tmp_path / "path_consumer.py"
    source.write_text("import os\ndef check(value):\n    return os.fspath(value)\n")
    sources, modules = closure._prepare_multi_source_compile_closure(
        [str(source)], ["path_consumer"], recursive_stdlib=recursive,
        ir_scaffold_mode="on",
    )
    assert modules.count("os") == 1
    assert Path(sources[modules.index("os")]).name == "os.py"


def _body(ir_text, symbol):
    found = re.search(r"define[^\n]*@" + re.escape(symbol) + r"\([^\n]*\)[^{]*\{(.*?)\n\}", ir_text, re.S)
    assert found is not None, symbol
    return found.group(1)


def test_path_protocol_ir_uses_provider_and_intrinsics(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / "path_protocol.py"
    output = tmp_path / "path_protocol.ll"
    source.write_text(PATH_PROTOCOL_SOURCE)
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", recursive_stdlib=True, emit_llvm_only=True)
    ir_text = output.read_text()
    check = _body(ir_text, "user_path_protocol_check")
    assert "@user_os_fspath" in check
    for intrinsic in ("py_os_getenv", "py_os_getcwd_str", "py_os_path_join"):
        assert "@" + intrinsic in check
    for body in (check, _body(ir_text, "user_os_fspath")):
        assert "@py_cpy_" not in body
        assert "strict.nolib.stub" not in body
    assert "@pcc_platform_getpid" in _body(ir_text, "user_os_getpid")
    assert "@pcc_platform_access" in _body(ir_text, "user_os_exists")


def test_path_protocol_executes_natively(tmp_path, pcc_runtime_archive):
    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / "path_protocol.py"
    output = tmp_path / "path_protocol"
    source.write_text(PATH_PROTOCOL_SOURCE)
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", recursive_stdlib=True,
                   runtime_archive=str(pcc_runtime_archive))
    for gc in range(5):
        result = subprocess.run(
            [str(output)], capture_output=True, text=True, timeout=20,
            env=dict(os.environ, PCC_PATH_PROTOCOL_CONTROL="intrinsic-ok",
                     PCC_GC_BACKEND=str(gc), PATH="",
                     PCC_HOST_PYTHON="/nonexistent/host-python"),
        )
        assert result.returncode == 0, f"GC{gc}: " + result.stdout + result.stderr
        assert result.stderr == "", f"GC{gc}: " + result.stderr
        assert result.stdout == PATH_PROTOCOL_STDOUT, f"GC{gc}: " + result.stdout


DIAGNOSTIC_SOURCE = '''from pcc.diagnostics.compile_observability import _diagnostic_span_from_compile_args
from pathlib import Path

def check():
    print(_diagnostic_span_from_compile_args(("source.py",)).file)
    print(_diagnostic_span_from_compile_args((Path("path.py"),)).file)
    print(_diagnostic_span_from_compile_args(()) is None)
    print(_diagnostic_span_from_compile_args((42,)) is None)
check()
'''


def _compile_diagnostic_entry(tmp_path, *, emit_llvm_only, runtime_archive=None):
    import pcc
    from pcc.frontends.python.pipeline import compile_python_multi

    source = tmp_path / "diagnostic_entry.py"
    output = tmp_path / ("diagnostic_entry.ll" if emit_llvm_only else "diagnostic_entry")
    source.write_text(DIAGNOSTIC_SOURCE)
    root = Path(pcc.__file__).parent
    sources = [str(source)] + [str(root / name) for name in (
        "diagnostics/__init__.py", "diagnostics/profile_events.py",
        "diagnostics/compile_observability.py",
    )]
    modules = ["diagnostic_entry", "pcc.diagnostics", "pcc.diagnostics.profile_events",
               "pcc.diagnostics.compile_observability"]
    compile_python_multi(
        sources, str(output), module_names=modules, entry_module="diagnostic_entry",
        backend="self", libpython_mode="off", ir_scaffold_mode="on",
        recursive_stdlib=True, emit_llvm_only=emit_llvm_only,
        runtime_archive=runtime_archive,
    )
    return output


def test_diagnostic_span_original_shape_is_not_stubbed(tmp_path):
    ir_text = _compile_diagnostic_entry(tmp_path, emit_llvm_only=True).read_text()
    body = _body(ir_text, "user_pcc_diagnostics_compile_observability__diagnostic_span_from_compile_args")
    assert "@user_os_fspath" in body
    assert "@.class.os.PathLike" in body
    assert "@py_cpy_" not in body
    assert "strict.nolib.stub" not in body


def test_diagnostic_span_original_shape_executes_natively(tmp_path, pcc_runtime_archive):
    output = _compile_diagnostic_entry(tmp_path, emit_llvm_only=False,
                                       runtime_archive=str(pcc_runtime_archive))
    for gc in range(5):
        result = subprocess.run(
            [str(output)], capture_output=True, text=True, timeout=20,
            env=dict(os.environ, PCC_GC_BACKEND=str(gc), PATH="",
                     PCC_HOST_PYTHON="/nonexistent/host-python"),
        )
        assert result.returncode == 0, f"GC{gc}: " + result.stdout + result.stderr
        assert result.stderr == "", f"GC{gc}: " + result.stderr
        assert result.stdout == "source.py\npath.py\nTrue\nTrue\n", f"GC{gc}: " + result.stdout


def test_os_provider_platform_wrappers_execute_natively(tmp_path, pcc_runtime_archive):
    from pcc.frontends.python.pipeline import compile_python

    regular = tmp_path / "regular"
    regular.write_text("owned")
    linked = tmp_path / "linked"
    linked.symlink_to(regular)
    missing = tmp_path / "missing"
    dangling = tmp_path / "dangling"
    dangling.symlink_to(missing)
    source = tmp_path / "provider_platform.py"
    output = tmp_path / "provider_platform"
    source.write_text('''import os
from os import getpid as provider_getpid, exists as provider_exists
def check():
    print(provider_getpid() == os.getpid())
    print(provider_getpid() > 0)
''' + "".join(
        "    print(provider_exists(" + repr(str(path)) + "))\n"
        for path in (regular, tmp_path, linked, missing, dangling)
    ) + "check()\n")
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", recursive_stdlib=True,
                   runtime_archive=str(pcc_runtime_archive))
    for gc in range(5):
        result = subprocess.run(
            [str(output)], capture_output=True, text=True, timeout=20,
            env=dict(os.environ, PCC_GC_BACKEND=str(gc), PATH="",
                     PCC_HOST_PYTHON="/nonexistent/host-python"),
        )
        assert result.returncode == 0, f"GC{gc}: " + result.stdout + result.stderr
        assert result.stderr == "", f"GC{gc}: " + result.stderr
        assert result.stdout == "True\nTrue\nTrue\nTrue\nTrue\nFalse\nFalse\n", f"GC{gc}: " + result.stdout
