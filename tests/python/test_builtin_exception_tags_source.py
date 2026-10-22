import ast
from importlib.util import resolve_name
from pathlib import Path
import re

import pytest


def _runtime_exception_names() -> list[str]:
    """The runtime's builtin exception name table, indexed by tag."""
    py_substrate = _read("pcc/runtime/py/py_substrate.py")
    names = dict(
        (int(index), name)
        for index, name in re.findall(
            r'define_global_cstr\("PY_EXC_NAME_(\d+)", "(\w+)"\)', py_substrate
        )
    )
    return [names[index] for index in range(len(names))]

def test_warning_siblings_have_distinct_native_identity_and_handlers(
    tmp_path, pcc_runtime_archive,
):
    import os
    import subprocess
    import sys

    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / "warning_identity.py"
    source.write_text('''
import gc
import warnings
def main():
    def check_class(cls):
        print(isinstance(cls, type))
        print(isinstance(cls, (type, str)))
        print(isinstance(cls, type(UserWarning)))
    check_class(UserWarning)
    check_class(UserWarning("instance"))
    print(UserWarning is DeprecationWarning)
    print(UserWarning.__name__)
    print(DeprecationWarning.__name__)
    print(issubclass(UserWarning, Warning))
    print(issubclass(UserWarning, DeprecationWarning))
    try:
        raise UserWarning("probe")
    except DeprecationWarning:
        print("wrong-handler")
    except UserWarning:
        print("correct-handler")
    ResourceWarning("warm")
    gc.collect()
    print(ResourceWarning.__name__)
    try:
        raise ResourceWarning("last tag")
    except Warning:
        print("base-handler")
    warnings.simplefilter("error", UserWarning)
    try:
        warnings.warn("default category")
    except UserWarning:
        print("default-warning-handler")
main()
''', encoding="utf-8")
    reference = subprocess.run(
        [sys.executable, str(source)], capture_output=True, text=True, timeout=10,
    )
    assert reference.returncode == 0, reference.stderr
    output = tmp_path / "warning_identity"
    compile_python(
        str(source), str(output), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        result = subprocess.run(
            [str(output)], env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
            capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 0, f"GC{backend}: {result.stderr}"
        assert result.stdout == reference.stdout, f"GC{backend}: {result.stdout}"


def _find_repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        if (parent / "AGENTS.md").is_file() and (parent / "pcc").is_dir():
            return parent
    raise RuntimeError(f"cannot locate pcc repo root from {here}")


_REPO_ROOT = _find_repo_root()
_CODEGEN_DIR = _REPO_ROOT / "pcc" / "frontends" / "python" / "codegen"


def _read(rel: str) -> str:
    return (_REPO_ROOT / rel).read_text(encoding="utf-8")


def test_all_builtin_type_cache_slots_have_matching_c_and_python_gc_roots():
    py = _read("pcc/runtime/py/py_obj_ops_dispatch.py")
    declared = re.findall(r'define_global_ptr_null\("(pcc_type_cls_\w+|pcc_slice_cls)"\)', py)
    visitor = py.split('"pcc_builtin_type_root_slots",', 1)[1].split('\n)', 1)[0]
    assert re.findall(r'"(\w+)"', visitor) == declared


def test_builtin_exception_tag_metadata_has_one_authoritative_source():
    codegen_files = sorted(_CODEGEN_DIR.glob("*.py"))
    _assert_tag_table_owner({
        str(path.relative_to(_REPO_ROOT)): path.read_text(encoding="utf-8")
        for path in codegen_files
    })

    for rel in (
        "pcc/frontends/python/codegen/call_expression_lowering.py",
        "pcc/frontends/python/codegen/class_gen.py",
        "pcc/frontends/python/codegen/comprehension_lowering.py",
        "pcc/frontends/python/codegen/exception_lowering.py",
        "pcc/frontends/python/codegen/for_loop_lowering.py",
        "pcc/frontends/python/codegen/isinstance_lowering.py",
    ):
        package = ".".join(Path(rel).parts[:-1])
        _assert_tag_import_owner(_read(rel), package)


def _assert_tag_table_owner(sources: dict[str, str]):
    definitions = []
    for path, source in sources.items():
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    for name in ast.walk(target):
                        if isinstance(name, ast.Name) and name.id in (
                            "BUILTIN_EXC_TAG", "_BUILTIN_EXC_TAG",
                        ):
                            assert isinstance(node.value, ast.Dict), path
                            definitions.append(path)
    assert definitions == ["pcc/frontends/python/codegen/builtin_exceptions.py"]


def _assert_tag_import_owner(source: str, package: str):
    imports = []
    symbols = {"BUILTIN_EXC_TAG", "builtin_exc_tag_or_missing"}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and any(
            alias.name.lstrip("_") in symbols
            or (alias.asname or "").lstrip("_") in symbols
            for alias in node.names
        ):
            imports.append(resolve_name("." * node.level + (node.module or ""), package))
    assert imports, "missing builtin exception metadata import"
    assert set(imports) == {"pcc.frontends.python.codegen.builtin_exceptions"}


def test_metadata_import_owner_resolves_relative_and_absolute_imports():
    package = "pcc.frontends.python.codegen"
    for module in (".builtin_exceptions", "..codegen.builtin_exceptions",
                   "pcc.frontends.python.codegen.builtin_exceptions"):
        _assert_tag_import_owner(
            f"from {module} import BUILTIN_EXC_TAG as _BUILTIN_EXC_TAG", package,
        )
    for module in (".foreign_tags", "foreign.builtin_exceptions"):
        with pytest.raises(AssertionError):
            _assert_tag_import_owner(
                f"from {module} import BUILTIN_EXC_TAG as _BUILTIN_EXC_TAG", package,
            )
    with pytest.raises(AssertionError):
        _assert_tag_import_owner(
            "from .builtin_exceptions import BUILTIN_EXC_TAG\n"
            "from .foreign_tags import builtin_exc_tag_or_missing", package,
        )


def test_metadata_owner_rejects_duplicate_and_foreign_tag_definitions():
    owner = "pcc/frontends/python/codegen/builtin_exceptions.py"
    source = "BUILTIN_EXC_TAG = {'Exception': 1}\n"
    _assert_tag_table_owner({owner: source})
    with pytest.raises(AssertionError):
        _assert_tag_table_owner({owner: source + source})
    with pytest.raises(AssertionError):
        _assert_tag_table_owner({owner: source, "foreign.py": "_BUILTIN_EXC_TAG: dict = {}"})
    with pytest.raises(AssertionError):
        _assert_tag_table_owner({owner: "BUILTIN_EXC_TAG = foreign.BUILTIN_EXC_TAG"})


def test_builtin_exception_tag_lookup_covers_runtime_tags():
    from pcc.frontends.python.codegen.builtin_exceptions import (
        BUILTIN_EXC_TAG,
        builtin_exc_tag_or_missing,
    )

    assert BUILTIN_EXC_TAG["BaseException"] == 0
    assert BUILTIN_EXC_TAG["StopIteration"] == 8
    assert BUILTIN_EXC_TAG["StopAsyncIteration"] == 17
    assert BUILTIN_EXC_TAG["ReferenceError"] == 18
    assert BUILTIN_EXC_TAG["MemoryError"] == 19
    assert BUILTIN_EXC_TAG["ImportError"] == 20
    assert BUILTIN_EXC_TAG["ModuleNotFoundError"] == 21
    # Every builtin exception has its own tag (the runtime parent table makes
    # subclasses match their bases); only the historical aliases share one.
    assert builtin_exc_tag_or_missing("FileNotFoundError") == 34
    assert BUILTIN_EXC_TAG["IOError"] == BUILTIN_EXC_TAG["OSError"]
    assert BUILTIN_EXC_TAG["EnvironmentError"] == BUILTIN_EXC_TAG["OSError"]
    assert builtin_exc_tag_or_missing("NotABuiltinException") == -1
    runtime_names = _runtime_exception_names()
    for name, tag in BUILTIN_EXC_TAG.items():
        if name in ("IOError", "EnvironmentError"):
            continue
        assert runtime_names[tag] == name, (name, tag)


def test_class_base_exception_lookup_uses_shared_tags():
    from pcc.frontends.python.codegen.class_gen import _builtin_exception_tag_for_base_name
    from pcc.frontends.python.codegen.builtin_exceptions import BUILTIN_EXC_TAG

    assert (
        _builtin_exception_tag_for_base_name("Exception")
        == BUILTIN_EXC_TAG["Exception"]
    )
    assert (
        _builtin_exception_tag_for_base_name("FileNotFoundError")
        == BUILTIN_EXC_TAG["FileNotFoundError"]
    )
    assert _builtin_exception_tag_for_base_name("NotABuiltinException") is None


def test_memory_error_runtime_tables_match_header_and_pcc_python():
    c_header = _read("pcc/runtime/include/py_runtime.h")
    py_substrate = _read("pcc/runtime/py/py_substrate.py")
    py_gc = _read("pcc/runtime/py/freestanding_gc_mapped_roots.py")

    assert "PY_EXC_MEMORYERROR       = 19" in c_header
    assert "PY_EXC_IMPORTERROR       = 20" in c_header
    assert "PY_EXC_MODULENOTFOUNDERROR = 21" in c_header
    assert "PY_EXC_WARNING           = 22," in c_header
    count = int(re.search(r"PY_EXC_N_BUILTIN\s*=\s*(\d+)", c_header).group(1))
    names = _runtime_exception_names()
    assert len(names) == count
    assert names[19:22] == ["MemoryError", "ImportError", "ModuleNotFoundError"]
    py_parent_block = py_substrate.split('"PY_EXC_PARENT",', 1)[1].split("\n)", 1)[0]
    py_parents = [int(v) for v in re.findall(r"-?\d+", py_parent_block)]
    # One parent per builtin, each a valid tag (or -1 for the root).
    assert len(py_parents) == count
    assert all(-1 <= parent < count for parent in py_parents)
    assert f'define_global_null_ptr_array("py_exc_classes", {count})' in py_substrate
    assert f"def py_subs_exc_n_builtin() -> int:\n    return {count}" in py_substrate
    assert "def pcc_gc_visit_builtin_exception_cache_slots" in py_gc
    assert f"pcc_gc_visit_mapped_root_slots(\n        {count}," in py_gc
