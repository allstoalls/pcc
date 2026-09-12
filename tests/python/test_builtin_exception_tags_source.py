from pathlib import Path
import re


def test_warning_siblings_have_distinct_native_identity_and_handlers(
    tmp_path, pcc_py_runtime_archive,
):
    import os
    import subprocess
    import sys

    from pcc.py_frontend.pipeline import compile_python

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
        runtime_archive=str(pcc_py_runtime_archive),
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
_CODEGEN_DIR = _REPO_ROOT / "pcc" / "py_frontend" / "codegen"


def _read(rel: str) -> str:
    return (_REPO_ROOT / rel).read_text(encoding="utf-8")


def test_all_builtin_type_cache_slots_have_matching_c_and_python_gc_roots():
    py = _read("pcc/py_runtime/py/py_obj_ops_dispatch.py")
    c = _read("pcc/py_runtime/src/py_obj_ops_dispatch.c")
    declared = re.findall(r'define_global_ptr_null\("(pcc_type_cls_\w+|pcc_slice_cls)"\)', py)
    visitor = py.split('"pcc_builtin_type_root_slots",', 1)[1].split('\n)', 1)[0]
    assert re.findall(r'"(\w+)"', visitor) == declared
    c_visitor = c.split('void **pcc_builtin_type_root_slots[] = {', 1)[1].split('\n}', 1)[0]
    assert re.findall(r'&(pcc_type_cls_\w+|pcc_slice_cls)', c_visitor) == declared


def test_builtin_exception_tag_metadata_has_one_authoritative_source():
    codegen_files = sorted(_CODEGEN_DIR.glob("*.py"))
    tag_defs = []
    for path in codegen_files:
        source = path.read_text(encoding="utf-8")
        if re.search(r"(?m)^_?BUILTIN_EXC_TAG\s*=\s*\{", source):
            tag_defs.append(str(path.relative_to(_REPO_ROOT)))

    assert tag_defs == ["pcc/py_frontend/codegen/builtin_exceptions.py"]

    for rel in (
        "pcc/py_frontend/codegen/call_expression_lowering.py",
        "pcc/py_frontend/codegen/class_gen.py",
        "pcc/py_frontend/codegen/comprehension_lowering.py",
        "pcc/py_frontend/codegen/exception_lowering.py",
        "pcc/py_frontend/codegen/for_loop_lowering.py",
        "pcc/py_frontend/codegen/isinstance_lowering.py",
    ):
        source = _read(rel)
        assert "from .builtin_exceptions import" in source


def test_builtin_exception_tag_lookup_covers_runtime_tags():
    from pcc.py_frontend.codegen.builtin_exceptions import (
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
    assert builtin_exc_tag_or_missing("FileNotFoundError") == BUILTIN_EXC_TAG["OSError"]
    assert builtin_exc_tag_or_missing("NotABuiltinException") == -1


def test_class_base_exception_lookup_uses_shared_tags():
    from pcc.py_frontend.codegen.class_gen import _builtin_exception_tag_for_base_name
    from pcc.py_frontend.codegen.builtin_exceptions import BUILTIN_EXC_TAG

    assert (
        _builtin_exception_tag_for_base_name("Exception")
        == BUILTIN_EXC_TAG["Exception"]
    )
    assert (
        _builtin_exception_tag_for_base_name("FileNotFoundError")
        == BUILTIN_EXC_TAG["OSError"]
    )
    assert _builtin_exception_tag_for_base_name("NotABuiltinException") is None


def test_memory_error_runtime_tables_match_c_and_pcc_python():
    c_header = _read("pcc/py_runtime/include/py_runtime.h")
    c_substrate = _read("pcc/py_runtime/src/py_substrate.c")
    py_substrate = _read("pcc/py_runtime/py/py_substrate.py")
    py_gc = _read("pcc/py_runtime/py/freestanding_gc_mapped_roots.py")

    assert "PY_EXC_MEMORYERROR       = 19" in c_header
    assert "PY_EXC_IMPORTERROR       = 20" in c_header
    assert "PY_EXC_MODULENOTFOUNDERROR = 21" in c_header
    assert "PY_EXC_WARNING           = 22," in c_header
    assert "PY_EXC_N_BUILTIN         = 34" in c_header
    assert '"MemoryError",' in c_substrate
    assert "[PY_EXC_MEMORYERROR]       = PY_EXC_EXCEPTION" in c_substrate
    assert "[PY_EXC_WARNING]           = PY_EXC_EXCEPTION" in c_substrate
    assert '"Warning",' in c_substrate
    assert 'define_global_cstr("PY_EXC_NAME_19", "MemoryError")' in py_substrate
    assert 'define_global_cstr("PY_EXC_NAME_22", "Warning")' in py_substrate
    assert 'define_global_null_ptr_array("py_exc_classes", 34)' in py_substrate
    assert "def py_subs_exc_n_builtin() -> int:\n    return 34" in py_substrate
    assert "def pcc_gc_visit_builtin_exception_cache_slots" in py_gc
    assert "pcc_gc_visit_mapped_root_slots(\n        34," in py_gc
