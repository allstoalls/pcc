"""Ordinary dirname owns closed-world pathlib source-root discovery.

Windows cases interpret the provider over a lexical ABI oracle. They are
host checks, not native Windows execution or bootstrap qualification.
"""

import ntpath
from pathlib import Path
import posixpath
import re

import pytest

from tests.python.test_owned_path_basename import (
    PATHS,
    function_body,
    load_provider,
    modeled_os,
)


ROOT = Path(__file__).resolve().parents[2]
WINDOWS_ORIGIN = r"D:\a\pcc\pcc\pcc\tools\runtime_archive_provenance.py"


def dirname_provider(oracle, *, native=True):
    provider = modeled_os(oracle, native=native)
    provider._path_dirname = oracle.dirname
    return provider


@pytest.mark.parametrize("oracle", (posixpath, ntpath), ids=("posix", "windows"))
@pytest.mark.parametrize("native", (False, True), ids=("host-provider", "modeled-owned-abi"))
@pytest.mark.parametrize("path", PATHS + (WINDOWS_ORIGIN,))
def test_dirname_matches_target_lexical_oracle(oracle, native, path):
    provider = dirname_provider(oracle, native=native)
    assert provider.path.dirname(path) == oracle.dirname(path)


def test_dirname_preserves_path_protocol_owner_and_error():
    provider = dirname_provider(ntpath)
    events = []

    class PathValue:
        def __fspath__(self):
            events.append("fspath")
            return WINDOWS_ORIGIN

        def __str__(self):
            raise AssertionError("unexpected stringification")

    def helper(value):
        events.append(value)
        return ntpath.dirname(value)

    provider._path_dirname = helper
    assert provider.path.dirname(PathValue()) == ntpath.dirname(WINDOWS_ORIGIN)
    assert events == ["fspath", WINDOWS_ORIGIN]
    failure = LookupError("original dirname failure")

    def fail(value):
        raise failure

    provider._path_dirname = fail
    with pytest.raises(LookupError) as caught:
        provider.path.dirname(WINDOWS_ORIGIN)
    assert caught.value is failure
    provider._path_dirname = lambda value: None
    with pytest.raises(MemoryError, match="dirname result allocation failed"):
        provider.path.dirname(WINDOWS_ORIGIN)
    with pytest.raises(NotImplementedError, match="bytes paths"):
        provider.path.dirname(b"parent/leaf.py")


def test_host_dirname_does_not_enter_owned_abi():
    provider = dirname_provider(posixpath, native=False)

    def forbidden(value):
        raise AssertionError("host call entered owned ABI")

    provider._path_dirname = forbidden
    assert provider.path.dirname(b"parent/leaf.py") == b"parent"


def test_real_path_parents_preserve_windows_source_root():
    provider = dirname_provider(ntpath)
    paths = load_provider("pcc/stdlib/pathlib.py", "dirname_pathlib_provider")
    paths._op = provider.path
    origin = paths.Path(WINDOWS_ORIGIN)
    assert str(origin.parent) == ntpath.dirname(WINDOWS_ORIGIN)
    assert [str(parent) for parent in origin.parents] == [
        r"D:\a\pcc\pcc\pcc\tools", r"D:\a\pcc\pcc\pcc",
        r"D:\a\pcc\pcc", r"D:\a\pcc", r"D:\a", "D:\\",
    ]
    assert all(isinstance(parent, paths.Path) for parent in origin.parents)


@pytest.mark.parametrize("target", (
    "x86_64-unknown-linux-gnu", "x86_64-pc-windows-msvc",
))
def test_path_parents_use_compiled_provider_and_owned_dirname(tmp_path, target):
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.pipeline_context import build_closed_world_context
    from pcc.frontends.python.type_infer import infer_module
    from tests.owned_ir_validation import verify_ir_text

    paths = [str(ROOT / "pcc/stdlib" / name)
             for name in ("pathlib.py", "os.py", "sys.py")]
    names = ["pathlib", "os", "sys"]
    modules, exports, derived = build_closed_world_context(paths, names)
    emitted = {}
    for index in (0, 1):
        name = names[index]
        external = {key: value for key, value in exports.items() if key != name}
        module = infer_module(modules[index], external_exports=external,
                              derived_class_map=derived)
        generator = L1CodeGen(module, emit_cpy_main_exitcode=False,
                             ir_scaffold_mode="on")
        generator._module_source_path = paths[index]
        generator._target_triple = target
        generator._strict_no_libpython = True
        generator._prefer_native_callable_values = True
        generator._skip_program_main = True
        generator._sibling_module_inits = names
        generator._native_module_exports = external
        text = str(generator.generate())
        (tmp_path / (name + ".ll")).write_text(text)
        verify_ir_text(text)
        emitted[name] = text
    dirname = function_body(emitted["os"], "user_os__path_dirname")
    assert re.search(r"call ptr[^\n]*@py_os_path_dirname\(", dirname)
    assert "@py_compiled_module_import_by_name(" in emitted["pathlib"]
    bodies = [dirname]
    for method in ("parent", "_parent_raw_paths"):
        body = function_body(emitted["pathlib"], "user_pathlib_PurePath_" + method)
        assert re.search(r"call[^\n]*@py_(?:obj_getattr|module_attr_get)\(", body)
        assert re.search(r"call[^\n]*@py_obj_call_slots\(", body)
        assert "@py_os_path_dirname(" not in body
        bodies.append(body)
    for body in bodies:
        assert not re.search(r"call[^\n]*@py_cpy_", body)
        assert "strict.nolib.stub" not in body
