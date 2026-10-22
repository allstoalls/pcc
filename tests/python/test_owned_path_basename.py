"""Owned basename adaptation and the real pathlib/archive identity route.

Windows cases use a lexical ABI oracle on the host. They do not claim execution
on Windows; closed-world IR separately checks the compiled provider route.
"""

import importlib.util
import ntpath
from pathlib import Path
import posixpath
import re
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = "libpy_runtime_pcc_py.a"
WINDOWS_ARCHIVE = r"D:\a\pcc\pcc\build\worker-prior-gate\runtime\libpy_runtime_pcc_py.a"
PATHS = (
    "", ".", "..", "leaf.a", "leaf.a.tmp", "/", "//", "///",
    "/root/leaf.a", "/root/leaf.a/", "root//leaf.a", "root\\leaf.a",
    "C:", "C:leaf.a", "C:\\", "C:/", "C:\\root\\leaf.a",
    "C:/root\\leaf.a", "C:\\root/leaf.a", "C:\\root\\leaf.a\\",
    "\\root\\leaf.a", "\\\\server\\share", "\\\\server\\share\\",
    "\\\\server\\share\\leaf.a", "//server/share/leaf.a",
    "\\\\?\\C:\\root\\leaf.a", "\\\\?\\UNC\\server\\share\\leaf.a",
    WINDOWS_ARCHIVE,
)


def load_provider(relative, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def modeled_os(oracle, *, native=True):
    provider = load_provider("pcc/stdlib/os.py", "basename_os_provider")
    provider._native_sys = SimpleNamespace(
        implementation=SimpleNamespace(name="pcc" if native else "cpython"),
    )
    provider._native_os = SimpleNamespace(path=oracle)
    provider._error_pending = lambda: False
    provider.ptr_is_null = lambda value: value is None
    provider._path_basename = oracle.basename
    return provider


@pytest.mark.parametrize("oracle", (posixpath, ntpath), ids=("posix", "windows"))
@pytest.mark.parametrize("native", (False, True), ids=("host-provider", "modeled-owned-abi"))
@pytest.mark.parametrize("path", PATHS)
def test_basename_matches_target_lexical_oracle(oracle, native, path):
    provider = modeled_os(oracle, native=native)
    assert provider.path.basename(path) == oracle.basename(path)


def test_basename_preserves_path_protocol_owner_and_error():
    provider = modeled_os(ntpath)
    events = []

    class PathValue:
        def __fspath__(self):
            events.append("fspath")
            return WINDOWS_ARCHIVE

        def __str__(self):
            raise AssertionError("unexpected stringification")

    def helper(value):
        events.append(value)
        return ntpath.basename(value)

    provider._path_basename = helper
    assert provider.path.basename(PathValue()) == ARCHIVE
    assert events == ["fspath", WINDOWS_ARCHIVE]
    failure = LookupError("original basename failure")

    def fail(value):
        raise failure

    provider._path_basename = fail
    with pytest.raises(LookupError) as caught:
        provider.path.basename(WINDOWS_ARCHIVE)
    assert caught.value is failure
    provider._path_basename = lambda value: None
    with pytest.raises(MemoryError, match="basename result allocation failed"):
        provider.path.basename(WINDOWS_ARCHIVE)
    with pytest.raises(NotImplementedError, match="bytes paths"):
        provider.path.basename(b"leaf.a")


def test_host_basename_does_not_enter_owned_abi():
    provider = modeled_os(posixpath, native=False)

    def forbidden(value):
        raise AssertionError("host call entered owned ABI")

    provider._path_basename = forbidden
    assert provider.path.basename(b"parent/leaf.a") == b"leaf.a"


@pytest.mark.parametrize("spelling", (
    ARCHIVE, "other.a", "directory/" + ARCHIVE, "directory\\" + ARCHIVE,
    ARCHIVE.upper(), ARCHIVE + ".tmp", WINDOWS_ARCHIVE, None,
))
def test_real_path_name_keeps_archive_identity_strict(monkeypatch, spelling):
    from pcc.tools import runtime_archive_provenance as provenance

    provider = modeled_os(ntpath)
    paths = load_provider("pcc/stdlib/pathlib.py", "basename_pathlib_provider")
    paths._op = provider.path
    monkeypatch.setattr(provenance, "Path", paths.Path)
    manifest = dict.fromkeys(provenance._MANIFEST_FIELDS)
    manifest.update(schema=provenance.MANIFEST_SCHEMA, archive=spelling,
                    policy=provenance.PRODUCTION_POLICY,
                    target_triple="x86_64-pc-windows-msvc", member_count=1,
                    members=[{}])
    monkeypatch.setattr(provenance, "_load_json_object", lambda path: manifest)

    class ReachedMemberValidation(Exception):
        pass

    def extract(archive, *, ar):
        assert str(archive) == WINDOWS_ARCHIVE
        raise ReachedMemberValidation()

    monkeypatch.setattr(provenance, "_extract_archive_members", extract)
    assert paths.Path(WINDOWS_ARCHIVE).name == ARCHIVE
    if spelling == ARCHIVE:
        with pytest.raises(ReachedMemberValidation):
            provenance.verify_runtime_archive_manifest(
                WINDOWS_ARCHIVE, runtime_root=r"D:\a\pcc\pcc\pcc\runtime",
            )
    else:
        with pytest.raises(provenance.ProvenanceError, match="names a different archive"):
            provenance.verify_runtime_archive_manifest(
                WINDOWS_ARCHIVE, runtime_root=r"D:\a\pcc\pcc\pcc\runtime",
            )


def function_body(text, symbol):
    match = re.search(r"^define [^\n]*@" + re.escape(symbol)
                      + r"\([^\n]*\n.*?^}", text, re.M | re.S)
    assert match is not None, symbol
    return match.group()


@pytest.mark.parametrize("target", (
    "x86_64-unknown-linux-gnu", "x86_64-pc-windows-msvc",
))
def test_path_name_uses_compiled_os_provider_and_owned_basename(tmp_path, target):
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.pipeline_context import build_closed_world_context
    from pcc.frontends.python.type_infer import infer_module
    from tests.owned_ir_validation import verify_ir_text

    paths = [str(ROOT / "pcc/stdlib" / name) for name in ("pathlib.py", "os.py", "sys.py")]
    names = ["pathlib", "os", "sys"]
    modules, exports, derived = build_closed_world_context(paths, names)
    emitted = {}
    for index in (0, 1):
        name = names[index]
        external = {key: value for key, value in exports.items() if key != name}
        module = infer_module(modules[index], external_exports=external, derived_class_map=derived)
        generator = L1CodeGen(module, emit_cpy_main_exitcode=False, ir_scaffold_mode="on")
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
    name_body = function_body(emitted["pathlib"], "user_pathlib_PurePath_name")
    assert "@py_compiled_module_import_by_name(" in emitted["pathlib"]
    assert re.search(r"call[^\n]*@py_(?:obj_getattr|module_attr_get)\(", name_body)
    assert re.search(r"call[^\n]*@py_obj_call_slots\(", name_body)
    assert "@py_os_path_basename(" not in name_body
    basename = function_body(emitted["os"], "user_os__path_basename")
    assert re.search(r"call ptr[^\n]*@py_os_path_basename\(", basename)
    for body in (name_body, basename):
        assert not re.search(r"call[^\n]*@py_cpy_", body)
        assert "strict.nolib.stub" not in body
