"""Host-only freshness contracts for relocated compiled module origins."""

import hashlib
import io
import ntpath
from pathlib import Path

import pytest

from pcc.backend import self_backend_cache_identity as backend_identity
from pcc.driver import bootstrap_cache_identity as bootstrap_identity
from pcc.tools import runtime_archive_provenance as provenance


def _source(root, *, frontend="frontend = 1\n"):
    paths = set(backend_identity.MACHO_LINK_SOURCE_PATHS) | {
        "pcc/__init__.py", "pcc/stdlib/__init__.py", "pcc/frontends/python/pipeline.py",
        "pcc/backend/self_backend_dispatch.py",
        "pcc/driver/bootstrap_cache_identity.py",
        "pcc/backend/self_backend_cache_identity.py",
        "pcc/tools/runtime_archive_provenance.py", "pcc/tools/ir_to_obj.py",
        "pcc/backend/owned_object_emit.py", "scripts/bootstrap.py",
    }
    for relative in paths:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# synthetic identity input: " + relative + "\n")
    (root / "pcc/frontends/python/pipeline.py").write_text(frontend)
    return root


def _expected(root):
    emitter = hashlib.sha256()
    emitter.update((root / "pcc/tools/ir_to_obj.py").read_bytes())
    emitter.update((root / "pcc/backend/owned_object_emit.py").read_bytes())
    emitter.update(backend_identity.macho_linker_source_identity(root).encode("ascii"))
    value = bootstrap_identity.bootstrap_source_sha256(root) + ":" + emitter.hexdigest()
    return hashlib.sha256(value.encode("ascii")).hexdigest()


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.delenv("PCC_SOURCE_ROOT", raising=False)
    monkeypatch.delenv("PCC_REPO_ROOT", raising=False)
    monkeypatch.setattr(provenance, "_CODEGEN_CHECKSUM_CACHE", {})
    return _source(tmp_path / "source")


def _origins(monkeypatch, root):
    for module, relative in (
        (provenance, "pcc/tools/runtime_archive_provenance.py"),
        (bootstrap_identity, "pcc/driver/bootstrap_cache_identity.py"),
        (backend_identity, "pcc/backend/self_backend_cache_identity.py"),
    ):
        monkeypatch.setattr(module, "__file__", str(root / relative))


@pytest.mark.parametrize("selection", ["source-repo", "source-package", "repo", "both"])
def test_explicit_root_relocates_all_codegen_hash_inputs(source, tmp_path, monkeypatch, selection):
    expected = _expected(source)
    _origins(monkeypatch, tmp_path / "missing-compiled-origin")
    if selection.startswith("source") or selection == "both":
        monkeypatch.setenv("PCC_SOURCE_ROOT", str(source / "pcc" if selection == "source-package" else source))
    if selection in ("repo", "both"):
        monkeypatch.setenv("PCC_REPO_ROOT", str(source))
    assert provenance.codegen_checksum() == expected
    assert not provenance.manifest_is_stale_for_current_codegen({"members": [{"codegen_checksum": expected}]})
    for invalid in ("unknown", "f" * 64, ""):
        assert provenance.manifest_is_stale_for_current_codegen({"members": [{"codegen_checksum": invalid}]})


def test_no_override_preserves_module_source_identity(source, monkeypatch):
    expected = _expected(source)
    _origins(monkeypatch, source)
    assert provenance.codegen_checksum() == expected


@pytest.mark.parametrize("selection", ["source", "repo", "conflict", "incomplete", "file"])
def test_invalid_explicit_root_cannot_reuse_warm_cache(source, tmp_path, monkeypatch, selection):
    _origins(monkeypatch, source)
    expected = provenance.codegen_checksum()
    assert expected == _expected(source)
    other = tmp_path / "other"
    if selection == "conflict":
        _source(other)
        monkeypatch.setenv("PCC_SOURCE_ROOT", str(source))
        monkeypatch.setenv("PCC_REPO_ROOT", str(other))
    else:
        if selection == "incomplete":
            (other / "pcc").mkdir(parents=True)
            (other / "pcc/__init__.py").write_text("")
        elif selection == "file":
            other.write_text("not a source directory\n")
        monkeypatch.setenv("PCC_REPO_ROOT" if selection == "repo" else "PCC_SOURCE_ROOT", str(other))
    assert provenance.codegen_checksum() == "unknown"
    assert provenance.manifest_is_stale_for_current_codegen({"members": [{"codegen_checksum": expected}]})


def test_cache_is_bound_to_selected_source_root(source, tmp_path, monkeypatch):
    other = _source(tmp_path / "other", frontend="frontend = 2\n")
    _origins(monkeypatch, tmp_path / "missing-compiled-origin")
    monkeypatch.setenv("PCC_SOURCE_ROOT", str(source))
    first = provenance.codegen_checksum()
    monkeypatch.setenv("PCC_SOURCE_ROOT", str(other))
    second = provenance.codegen_checksum()
    assert first == _expected(source)
    assert second == _expected(other)
    assert first != second
    assert provenance.manifest_is_stale_for_current_codegen({"members": [{"codegen_checksum": first}]})


@pytest.mark.parametrize("relative", [
    "pcc/tools/ir_to_obj.py", "pcc/backend/owned_object_emit.py",
    backend_identity.MACHO_LINK_SOURCE_PATHS[0],
])
def test_missing_emitter_input_remains_unknown(source, tmp_path, monkeypatch, relative):
    expected = _expected(source)
    (source / relative).unlink()
    _origins(monkeypatch, tmp_path / "missing-compiled-origin")
    monkeypatch.setenv("PCC_SOURCE_ROOT", str(source))
    assert provenance.codegen_checksum() == "unknown"
    assert provenance.manifest_is_stale_for_current_codegen({"members": [{"codegen_checksum": expected}]})


def test_package_and_repository_overrides_may_name_same_source(source, tmp_path, monkeypatch):
    _origins(monkeypatch, tmp_path / "missing-compiled-origin")
    monkeypatch.setenv("PCC_SOURCE_ROOT", str(source / "pcc"))
    monkeypatch.setenv("PCC_REPO_ROOT", str(source))
    assert provenance.codegen_checksum() == _expected(source)


def test_missing_origin_without_override_remains_unknown(source, tmp_path, monkeypatch):
    _origins(monkeypatch, tmp_path / "missing-compiled-origin")
    assert provenance.codegen_checksum() == "unknown"
    assert provenance.manifest_is_stale_for_current_codegen({"members": [{"codegen_checksum": _expected(source)}]})


def test_identical_source_at_another_root_preserves_digest(source, tmp_path, monkeypatch):
    other = _source(tmp_path / "other")
    monkeypatch.setenv("PCC_SOURCE_ROOT", str(source))
    first = provenance.codegen_checksum()
    monkeypatch.setenv("PCC_SOURCE_ROOT", str(other))
    assert provenance.codegen_checksum() == first == _expected(other)


def test_unknown_identity_does_not_poison_repaired_source(source, monkeypatch):
    monkeypatch.setenv("PCC_SOURCE_ROOT", str(source))
    path = source / "pcc/tools/ir_to_obj.py"
    original = path.read_bytes()
    path.unlink()
    assert provenance.codegen_checksum() == "unknown"
    path.write_bytes(original)
    assert provenance.codegen_checksum() == _expected(source)


@pytest.mark.parametrize("selection", ["source", "source-package", "repo", "repo-package", "both"])
def test_selected_native_path_provider_hashes_relocated_source(source, tmp_path, monkeypatch, selection):
    from pcc.stdlib.pathlib import Path as NativePath

    expected = _expected(source)
    _origins(monkeypatch, tmp_path / "missing-compiled-origin")
    for module in (provenance, bootstrap_identity, backend_identity):
        monkeypatch.setattr(module, "Path", NativePath)
    if selection.startswith("source") or selection == "both":
        monkeypatch.setenv("PCC_SOURCE_ROOT", str(source / "pcc" if selection == "source-package" else source))
    if selection.startswith("repo") or selection == "both":
        monkeypatch.setenv("PCC_REPO_ROOT", str(source / "pcc" if selection == "repo-package" else source))
    assert provenance.codegen_checksum() == expected


@pytest.mark.parametrize("provider", ["host", "pcc"])
@pytest.mark.parametrize("selection", ["source-alias", "package-alias", "repo-alias"])
def test_symlink_aliases_select_one_codegen_root(source, tmp_path, monkeypatch, provider, selection):
    from pcc.stdlib.pathlib import Path as NativePath

    expected = _expected(source)
    alias = tmp_path / "source-alias"
    alias.symlink_to(source, target_is_directory=True)
    _origins(monkeypatch, tmp_path / "missing-compiled-origin")
    if provider == "pcc":
        # Interpret the actual selected Path provider; this is not native
        # execution. Its POSIX resolve() only performs lexical normalization.
        for module in (provenance, bootstrap_identity, backend_identity):
            monkeypatch.setattr(module, "Path", NativePath)
    if selection == "repo-alias":
        monkeypatch.setenv("PCC_SOURCE_ROOT", str(source))
        monkeypatch.setenv("PCC_REPO_ROOT", str(alias))
    else:
        monkeypatch.setenv("PCC_SOURCE_ROOT", str(alias / "pcc" if selection == "package-alias" else alias))
        monkeypatch.setenv("PCC_REPO_ROOT", str(source))
    assert provenance.codegen_checksum() == expected
    assert provenance._CODEGEN_CHECKSUM_CACHE["root"] == str(source)
    assert not provenance.manifest_is_stale_for_current_codegen({"members": [{"codegen_checksum": expected}]})


def test_literal_tilde_matches_runtime_source_path_interpretation(source, tmp_path, monkeypatch):
    literal = _source(tmp_path / "~" / "source")
    _origins(monkeypatch, tmp_path / "missing-compiled-origin")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "different-home"))
    monkeypatch.setenv("PCC_SOURCE_ROOT", "~/source")
    assert provenance.codegen_checksum() == _expected(literal)


def test_lexical_parent_is_normalized_before_path_resolution(source, tmp_path, monkeypatch):
    expected = _expected(source)
    _origins(monkeypatch, tmp_path / "missing-compiled-origin")
    raw = str(tmp_path / "link" / ".." / "source")
    original_path = provenance.Path
    seen = []

    def normalized_path(value):
        seen.append(str(value))
        assert str(value) != raw, "discovery normalizes lexical parents before resolving"
        return original_path(value)

    monkeypatch.setattr(provenance, "Path", normalized_path)
    monkeypatch.setenv("PCC_SOURCE_ROOT", raw)
    assert provenance.codegen_checksum() == expected
    assert str(source) in seen


@pytest.mark.parametrize("selection", ("origin", "source", "source-package", "repo-package"))
@pytest.mark.parametrize("crlf", (False, True), ids=("lf", "crlf"))
def test_windows_owned_providers_compute_current_checksum(source, monkeypatch, selection, crlf):
    """Keep real os/pathlib provider bodies; model only Windows ABI and I/O.

    This host check covers discovery, inventory and bytes. It is not native
    execution. The old dirname body failed before hashing with parents=['.'].
    """
    from tests.python.test_owned_path_basename import load_provider, modeled_os

    windows_root = r"D:\a\pcc\pcc"
    windows_os = modeled_os(ntpath)
    windows_os.sep = "\\"
    windows_os.environ = {}

    def mapped(value):
        relative = ntpath.relpath(str(value), windows_root)
        if relative == ".." or relative.startswith("..\\"):
            raise OSError("outside modeled checkout")
        return source / relative.replace("\\", "/")

    def source_bytes(path):
        data = path.read_bytes()
        return data.replace(b"\n", b"\r\n") if crlf else data

    # Independently construct the existing on-disk identity, preserving
    # platform-relative names and line endings rather than normalizing them.
    frontend = hashlib.sha256()
    files = bootstrap_identity.bootstrap_source_files(source)
    for path in sorted(files, key=lambda value: str(value.relative_to(source)).replace("/", "\\")):
        relative = str(path.relative_to(source)).replace("/", "\\").encode("utf-8")
        frontend.update(len(relative).to_bytes(8, "big"))
        frontend.update(relative)
        frontend.update(hashlib.sha256(source_bytes(path)).digest())
    linker = hashlib.sha256(backend_identity._MACHO_LINK_IDENTITY_SCHEMA)
    for relative in backend_identity.MACHO_LINK_SOURCE_PATHS:
        name = relative.encode("utf-8")
        linker.update(len(name).to_bytes(8, "big"))
        linker.update(name)
        linker.update(source_bytes(source / relative))
    emitter = hashlib.sha256()
    for relative in ("pcc/tools/ir_to_obj.py", "pcc/backend/owned_object_emit.py"):
        emitter.update(source_bytes(source / relative))
    emitter.update(linker.hexdigest().encode("ascii"))
    expected = hashlib.sha256(
        (frontend.hexdigest() + ":" + emitter.hexdigest()).encode("ascii")
    ).hexdigest()

    windows_os._path_abspath = ntpath.normpath
    windows_os._path_realpath = ntpath.normpath
    windows_os._path_dirname = ntpath.dirname
    windows_os._path_splitdrive = ntpath.splitdrive
    windows_os._path_normcase = ntpath.normcase
    windows_os._path_isfile = lambda value: mapped(value).is_file()
    windows_os._path_isdir = lambda value: mapped(value).is_dir()
    windows_os._os_listdir = lambda value: [path.name for path in mapped(value).iterdir()]
    paths = load_provider("pcc/stdlib/pathlib.py", "checksum_windows_pathlib")
    paths.os = windows_os
    paths._op = windows_os.path

    def read_windows(value, mode):
        assert mode == "rb"
        return io.BytesIO(source_bytes(mapped(value)))

    paths.open = read_windows
    for module, relative in (
        (provenance, r"pcc\tools\runtime_archive_provenance.py"),
        (bootstrap_identity, r"pcc\driver\bootstrap_cache_identity.py"),
        (backend_identity, r"pcc\backend\self_backend_cache_identity.py"),
    ):
        monkeypatch.setattr(module, "Path", paths.Path)
        monkeypatch.setattr(module, "__file__", windows_root + "\\" + relative)
    monkeypatch.setattr(provenance, "os", windows_os)
    monkeypatch.setattr(bootstrap_identity, "os", windows_os)
    if selection.startswith("source"):
        windows_os.environ["PCC_SOURCE_ROOT"] = windows_root + (r"\pcc" if selection.endswith("package") else "")
    elif selection == "repo-package":
        windows_os.environ["PCC_REPO_ROOT"] = windows_root + r"\pcc"

    assert str(provenance._codegen_source_root()) == windows_root
    assert len(bootstrap_identity.bootstrap_source_files(windows_root)) == len(files)
    assert provenance.codegen_checksum() == expected
    manifest = {"members": [{"codegen_checksum": expected}]}
    assert not provenance.manifest_is_stale_for_current_codegen(manifest)
    assert provenance.manifest_is_stale_for_current_codegen(
        {"members": [{"codegen_checksum": "f" * 64}]}
    )
    # Root validation must precede the warm cache, including on Windows.
    marker = source / "pcc/frontends/python/pipeline.py"
    marker.unlink()
    assert provenance.codegen_checksum() == "unknown"
    assert provenance.manifest_is_stale_for_current_codegen(manifest)
