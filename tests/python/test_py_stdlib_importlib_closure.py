"""Focused importlib closure for STDLIB-P1-BUILD-TOOL-CLOSURE.

Host-side checks exercise the provider as ordinary Python.  The integration
case proves the same source-package resource and linked dynamic-import surface
in strict pcc-native/no-libpython mode; it intentionally does not claim runtime
source execution, CPython bytecode loading, or CPython extension ABI support.
"""
from __future__ import annotations

import importlib as host_importlib
import importlib.machinery as host_machinery
import importlib.resources as host_resources
import importlib.util as host_util
import os
from pathlib import Path
import subprocess
import sys

import pytest

from pcc.stdlib import importlib as port_importlib
from pcc.stdlib.importlib import machinery as port_machinery
from pcc.stdlib.importlib import resources as port_resources
from pcc.stdlib.importlib import util as port_util


@pytest.fixture
def resource_package(tmp_path, monkeypatch):
    package_name = "pcc_importlib_resource_fixture"
    package = tmp_path / package_name
    (package / "nested").mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 17\n", encoding="utf-8")
    (package / "child.py").write_text("VALUE = 23\n", encoding="utf-8")
    (package / "payload.txt").write_text("resource payload\n", encoding="utf-8")
    (package / "newlines.txt").write_bytes(b"first\r\nsecond\rthird\n")
    (package / "nested" / "payload.bin").write_bytes(b"\x00pcc-resource\xff")
    monkeypatch.syspath_prepend(str(tmp_path))
    host_importlib.invalidate_caches()
    yield package_name, package
    for loaded_name in list(sys.modules):
        if loaded_name == package_name or loaded_name.startswith(package_name + "."):
            sys.modules.pop(loaded_name, None)


def test_import_module_returns_leaf_and_resolves_relative_name(resource_package):
    package_name, _package = resource_package
    port_leaf = port_importlib.import_module(package_name + ".child")
    host_leaf = host_importlib.import_module(package_name + ".child")
    assert port_leaf is host_leaf
    assert port_leaf.VALUE == 23
    assert port_importlib.import_module(".child", package_name) is host_leaf
    assert port_importlib._resolve_name("..peer", "top.pkg.child") == (
        host_util.resolve_name("..peer", "top.pkg.child")
    )


def test_importlib_unowned_reexecution_and_bad_relative_names_fail_closed():
    with pytest.raises(NotImplementedError, match="repeat module execution"):
        port_importlib.reload(host_importlib)
    with pytest.raises(TypeError, match="package.*required"):
        port_importlib.import_module(".child")
    with pytest.raises(ValueError, match="Empty module name"):
        port_importlib.import_module("")
    with pytest.raises(ImportError, match="beyond top-level"):
        port_importlib.import_module("...child", "one.two")
    with pytest.raises(ValueError, match="null character"):
        port_importlib.import_module("known\x00truncated")
    assert port_importlib.invalidate_caches() is None


def test_filesystem_resources_match_cpython(resource_package):
    package_name, package = resource_package
    port_root = port_resources.files(package_name)
    host_root = host_resources.files(package_name)
    package_module = host_importlib.import_module(package_name)

    assert port_root.is_dir() == host_root.is_dir()
    assert os.fspath(port_resources.files(package_module)) == os.fspath(port_root)
    assert port_root.name == host_root.name
    assert port_root.joinpath("payload.txt").read_text(encoding="utf-8") == (
        host_root.joinpath("payload.txt").read_text(encoding="utf-8")
    )
    assert port_root.joinpath("nested", "payload.bin").read_bytes() == (
        host_root.joinpath("nested", "payload.bin").read_bytes()
    )
    assert port_root.joinpath("newlines.txt").read_text() == (
        host_root.joinpath("newlines.txt").read_text()
    )
    assert sorted(entry.name for entry in port_root.iterdir()) == sorted(
        entry.name for entry in host_root.iterdir()
    )
    assert os.fspath(port_root) == str(package.resolve())


def test_legacy_resource_helpers_and_as_file(resource_package):
    package_name, _package = resource_package
    assert port_resources.read_text(
        package_name, "payload.txt", encoding="utf-8"
    ) == host_resources.read_text(
        package_name, "payload.txt", encoding="utf-8"
    )
    assert port_resources.read_binary(package_name, "payload.txt") == (
        host_resources.read_binary(package_name, "payload.txt")
    )
    assert port_resources.is_resource(package_name, "payload.txt")
    assert "payload.txt" in port_resources.contents(package_name)

    traversable = port_resources.files(package_name).joinpath("payload.txt")
    with port_resources.as_file(traversable) as materialized:
        assert Path(os.fspath(materialized)).read_text(encoding="utf-8") == (
            "resource payload\n"
        )
    with port_resources.path(package_name, "payload.txt") as materialized:
        assert Path(os.fspath(materialized)).is_file()


def test_resources_reject_escape_and_non_filesystem_boundaries(
    resource_package, tmp_path
):
    package_name, package = resource_package
    with pytest.raises(ValueError, match="parent traversal"):
        port_resources.files(package_name).joinpath("../outside.txt")
    with pytest.raises(ValueError, match="single relative name"):
        port_resources.read_text(package_name, "nested/payload.bin")
    with pytest.raises(NotImplementedError, match="non-filesystem"):
        port_resources.as_file(Path(package / "payload.txt"))
    with pytest.raises(NotImplementedError, match="UTF-8"):
        port_resources.read_text(package_name, "payload.txt", encoding="latin-1")
    with pytest.raises(NotImplementedError, match="strict"):
        port_resources.read_text(package_name, "payload.txt", errors="ignore")
    with pytest.raises(NotImplementedError, match="native file provenance"):
        port_resources.files(package_name).joinpath("payload.txt").open("rb")
    with pytest.raises(NotImplementedError, match="cross-module native stream"):
        port_resources.open_binary(package_name, "payload.txt")
    with pytest.raises(NotImplementedError, match="cross-module native stream"):
        port_resources.open_text(package_name, "payload.txt")

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside", encoding="utf-8")
    os.symlink(outside, package / "redirect")
    with pytest.raises(ValueError, match="outside its package"):
        port_resources.files(package_name).joinpath("redirect/secret.txt")


def test_machinery_metadata_and_source_inspection_are_finite(tmp_path):
    source = tmp_path / "sample.py"
    source.write_bytes(b"VALUE = 5\r\n")
    loader = port_machinery.SourceFileLoader("sample", source)
    host_loader = host_machinery.SourceFileLoader("sample", str(source))

    assert port_machinery.SOURCE_SUFFIXES == [".py"]
    assert port_machinery.BYTECODE_SUFFIXES == []
    assert all("cpython-" not in suffix for suffix in port_machinery.EXTENSION_SUFFIXES)
    assert all("abi3" not in suffix for suffix in port_machinery.EXTENSION_SUFFIXES)
    assert len(port_machinery.EXTENSION_SUFFIXES) == 1
    assert loader.get_filename("sample") == str(source)
    assert loader.get_data(source) == source.read_bytes()
    assert loader.get_source("sample") == host_loader.get_source("sample")
    assert not loader.is_package("sample")
    with pytest.raises(NotImplementedError, match="ahead-of-time"):
        loader.exec_module(object())
    with pytest.raises(NotImplementedError, match="bytecode"):
        port_machinery.SourcelessFileLoader("sample", "sample.pyc").get_code(
            "sample"
        )
    assert port_machinery.SourcelessFileLoader(
        "pkg", "pkg/__init__.pyc"
    ).is_package("pkg")
    extension_suffix = port_machinery.EXTENSION_SUFFIXES[0]
    assert port_machinery.ExtensionFileLoader(
        "pkg", "pkg/__init__" + extension_suffix
    ).is_package("pkg")


def test_module_spec_and_util_metadata_match_owned_cpython_subset(tmp_path):
    source = tmp_path / "pkg" / "__init__.py"
    source.parent.mkdir()
    source.write_text("", encoding="utf-8")
    loader = port_machinery.SourceFileLoader("pkg", source)
    spec = port_util.spec_from_file_location("pkg", source)
    host_spec = host_util.spec_from_file_location("pkg", source)

    assert spec.name == host_spec.name
    assert spec.origin == host_spec.origin
    assert spec.parent == host_spec.parent
    assert spec.has_location == host_spec.has_location
    assert spec.submodule_search_locations == host_spec.submodule_search_locations
    assert loader.is_package("pkg")
    assert port_util.resolve_name(".child", "pkg") == host_util.resolve_name(
        ".child", "pkg"
    )

    cache = port_util.cache_from_source(source, optimization="2")
    assert cache == host_util.cache_from_source(source, optimization="2")
    assert port_util.source_from_cache(cache) == host_util.source_from_cache(cache)
    with pytest.raises(NotImplementedError, match="only .py"):
        port_util.cache_from_source(source.with_suffix(".pyw"))
    encoded_source = b"\xef\xbb\xbfVALUE = 1\r\n"
    assert port_util.decode_source(encoded_source) == host_util.decode_source(
        encoded_source
    )
    with pytest.raises(ValueError, match="optimization tag"):
        port_util.source_from_cache(cache.replace(".opt-2.pyc", ".opt-.pyc"))
    with pytest.raises(NotImplementedError, match="selected and linked"):
        port_util.find_spec("runtime_selected_module")
    with pytest.raises(NotImplementedError, match="module construction"):
        port_util.module_from_spec(spec)


@pytest.mark.parametrize(
    "module_name",
    ["importlib", "importlib.resources", "importlib.machinery", "importlib.util"],
)
def test_importlib_family_is_selected_by_recursive_stdlib_registry(module_name):
    from pcc.frontends.python import pipeline

    source = pipeline._locate_native_stdlib_module_source(module_name)
    assert source is not None
    expected = module_name.replace(".", "/")
    if module_name == "importlib":
        assert source.endswith("/pcc/stdlib/importlib/__init__.py")
    else:
        assert source.endswith("/pcc/stdlib/" + expected + ".py")
    assert pipeline._classify_python_import(module_name) == "native_stdlib"
    assert "importlib" not in pipeline._NATIVE_BUILTIN_IMPORTS


def test_recursive_provider_adds_package_parents_and_relative_siblings(tmp_path):
    from pcc.frontends.python import pipeline

    resource_package = tmp_path / "resource_pkg"
    resource_package.mkdir()
    (resource_package / "__init__.py").write_text("", encoding="utf-8")
    entry = tmp_path / "entry.py"
    entry.write_text(
        "import importlib.util\n"
        "import importlib.resources\n"
        "payload = importlib.resources.files('resource_pkg') / 'payload.txt'\n",
        encoding="utf-8",
    )
    seed_sources, seed_modules = pipeline._collect_relative_module_closure(
        str(entry)
    )
    _sources, modules = pipeline._collect_multi_source_relative_closure(
        seed_sources,
        seed_modules,
        recursive_stdlib=True,
    )
    assert "importlib" in modules
    assert "importlib.util" in modules
    assert "importlib.machinery" in modules
    assert "importlib.resources" in modules
    assert "resource_pkg" in modules


def test_shallow_multi_closure_admits_direct_owned_providers_only(tmp_path):
    """A shallow build links directly named providers, not an unrequested tree.

    Every unconditional import below names a runtime dependency. Optional,
    unrequested standard-library providers stay outside the shallow closure.
    """
    from pcc.frontends.python import pipeline

    entry = tmp_path / "entry.py"
    entry.write_text(
        "import importlib\nimport hashlib\nimport keyword\n",
        encoding="utf-8",
    )
    _sources, modules = pipeline._prepare_multi_source_compile_closure(
        [str(entry)],
        ["entry"],
        recursive_stdlib=False,
        ir_scaffold_mode="on",
    )
    assert "importlib" in modules
    assert "hashlib" in modules
    assert "keyword" in modules
    assert "calendar" not in modules


def test_resource_literal_in_dependency_function_is_a_finite_closure_edge(tmp_path):
    from pcc.frontends.python import pipeline

    resource_package = tmp_path / "resource_pkg"
    resource_package.mkdir()
    (resource_package / "__init__.py").write_text("", encoding="utf-8")
    helper = tmp_path / "helper.py"
    helper.write_text(
        "import importlib.resources\n"
        "def payload():\n"
        "    return importlib.resources.files(\n"
        "        # A build tool commonly keeps the anchor on the next line.\n"
        "        'resource_pkg'\n"
        "    ) / 'payload.txt'\n",
        encoding="utf-8",
    )
    entry = tmp_path / "entry.py"
    entry.write_text("import helper\n", encoding="utf-8")

    seed_sources, seed_modules = pipeline._collect_relative_module_closure(
        str(entry)
    )
    _sources, modules = pipeline._collect_multi_source_relative_closure(
        seed_sources,
        seed_modules,
        recursive_stdlib=True,
    )
    assert "resource_pkg" in modules


def test_unaliased_dotted_native_import_keeps_top_level_package_binding():
    lowering_source = (
        Path(__file__).absolute().parents[2]
        / "pcc" / "frontends" / "python"
        / "codegen"
        / "import_lowering.py"
    ).read_text(encoding="utf-8")
    assert "if top_module in native_table:" in lowering_source
    assert "alias_module = top_module" in lowering_source


def test_compiled_registry_rejects_unknown_names_instead_of_empty_modules():
    repo_root = Path(__file__).absolute().parents[2]
    runtime_mirror = (
        repo_root
        / "pcc" / "runtime"
        / "py"
        / "py_compiled_module_runtime.py"
    ).read_text(encoding="utf-8")
    assert "if not _compiled_module_has_init(name):" in runtime_mirror
    capi_mirror = (
        repo_root
        / "pcc" / "runtime"
        / "py"
        / "py_capi_import_runtime.py"
    ).read_text(encoding="utf-8")
    assert "strlen(cname) != py_str_byte_len(name)" in capi_mirror


@pytest.mark.integration
def test_importlib_resources_and_linked_dynamic_import_match_no_libpython(
    tmp_path,
):
    package = tmp_path / "compiled_resource_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("ROOT_VALUE = 11\n", encoding="utf-8")
    (package / "child.py").write_text(
        "VALUE = 29\nPACKAGE = __package__\nFILE = __file__\n",
        encoding="utf-8",
    )
    (package / "payload.txt").write_text("compiled resource\n", encoding="utf-8")

    source = '''\
import importlib
import importlib.resources
from importlib.machinery import EXTENSION_SUFFIXES, SOURCE_SUFFIXES
from importlib.util import resolve_name
import compiled_resource_pkg
import compiled_resource_pkg.child

module_name = "compiled_resource_pkg." + "child"
child = importlib.import_module(module_name)
resource = importlib.resources.files("compiled_resource_pkg").joinpath("payload.txt")
print("dynamic", child.VALUE, child.__name__)
print("identity", child.PACKAGE, child.__package__, child.FILE == child.__file__)
print("resource", resource.read_text(encoding="utf-8").strip())
print("binary", resource.read_bytes() == b"compiled resource\n")
with importlib.resources.as_file(resource) as materialized:
    print("materialized", str(materialized).endswith("/compiled_resource_pkg/payload.txt"))
with importlib.resources.path("compiled_resource_pkg", "payload.txt") as legacy:
    print("legacy", str(legacy).endswith("/compiled_resource_pkg/payload.txt"))
print("relative", resolve_name(".child", "compiled_resource_pkg"))
print("suffixes", ".py" in SOURCE_SUFFIXES, len(EXTENSION_SUFFIXES) > 0)
try:
    importlib.import_module("pcc_importlib_deliberately_unlinked")
except ModuleNotFoundError:
    print("missing", True)
'''
    src = tmp_path / "importlib_probe.py"
    src.write_text(source, encoding="utf-8")
    executable = tmp_path / "importlib_probe"
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

    run_env = env.copy()
    run_env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    run_env["PATH"] = str(tmp_path / "no-host-python")
    actual = subprocess.run(
        [str(executable)],
        text=True,
        capture_output=True,
        timeout=120,
        env=run_env,
    )
    expected = subprocess.run(
        [sys.executable, str(src)],
        text=True,
        capture_output=True,
        timeout=120,
        env=env,
    )
    assert actual.returncode == 0, actual.stdout + actual.stderr
    assert expected.returncode == 0, expected.stdout + expected.stderr
    assert actual.stdout == expected.stdout
