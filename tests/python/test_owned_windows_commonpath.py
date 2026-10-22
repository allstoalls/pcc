"""Windows provider containment with explicit host models and owned IR.

The port model executes the existing lexical algorithm over synthetic memory;
it is not Windows kernel execution or a GC/allocator ownership proof.
"""

import ast
import inspect
import ntpath
from pathlib import Path
import posixpath
import re
from types import SimpleNamespace

import pytest

from tests.python.test_owned_path_basename import (
    function_body, modeled_os,
)


ROOT = Path(__file__).resolve().parents[2]
WINDOWS_ROOT = r"D:\a\pcc\pcc\pcc\runtime"
CASES = (
    (WINDOWS_ROOT + r"\py\py_os_path.py", WINDOWS_ROOT),
    ("C:/Work/./Alpha", "c:\\work\\Beta"),
    ("C:Work/Alpha", "c:work/Beta"),
    ("C:/Work/../Alpha", "c:/work/../Beta"),
    ("C:/One", "c:/Two"), ("one", "two"),
    ("C:/Ä/Alpha", "c:/ä/Beta"),
    ("//HOST/Share/Folder/One", "\\\\host\\share\\folder\\Two"),
    ("C:", "C:"), ("C:", "c:child"), ("C:\\", "c:\\child"),
    ("C:one", "C:other"), ("C:/one", "D:/two"),
    ("C:/one", "C:two"), ("/one", "relative"),
    ("\\root\\one", "\\root\\two"), ("\\root", "root"),
    ("\\\\host\\share", "\\\\host\\share\\child"),
    ("\\\\host\\share\\one", "\\\\host\\other\\two"),
    ("\\\\?\\C:\\Root\\One", "\\\\?\\c:\\root\\Two"),
    ("\\\\?\\UNC\\host\\share\\One", "\\\\?\\UNC\\HOST\\SHARE\\Two"),
    ("C:/root", "C:/rooted/file"), ("a/b", "a/b/é"),
    ("", "leaf"), (".", ".."), ("./a", "a/b"),
    ("a/../b", "a/../c"), ("a//b/", "a/b/c"),
    (), ("",), ("C:/Root",), ("\\\\host\\share\\",),
)


def outcome(call):
    try:
        return ("value", call())
    except Exception as error:
        return ("error", type(error))


def modeled_windows_provider(*, native=True):
    provider = modeled_os(ntpath, native=native)
    provider._platform_name = "win32"
    provider._path_commonpath = ntpath.commonpath
    return provider


@pytest.mark.parametrize("paths", CASES)
@pytest.mark.parametrize("native", (False, True), ids=("host-provider", "modeled-owned-abi"))
def test_commonpath_windows_provider_matches_lexical_oracle(paths, native):
    provider = modeled_windows_provider(native=native)
    assert outcome(lambda: provider.path.commonpath(paths)) == outcome(lambda: ntpath.commonpath(paths))


def runtime_commonpath_model(paths):
    """Execute unchanged port control flow with bounded synthetic byte memory."""
    memory = bytearray(64)

    def allocate(size):
        pointer = len(memory)
        memory.extend(bytes(size))
        return pointer

    def store_i64(pointer, offset, value):
        memory[pointer + offset:pointer + offset + 8] = value.to_bytes(8, "little")

    def text(pointer, size):
        return bytes(memory[pointer:pointer + size]).decode("utf-8")

    def new_string(pointer, size):
        result = allocate(40 + size)
        store_i64(result, 16, size)
        if pointer:
            memory[result + 40:result + 40 + size] = memory[pointer:pointer + size]
        return result

    def coerce(value):
        data = value.encode("utf-8")
        result = new_string(0, len(data))
        memory[result + 40:result + 40 + len(data)] = data
        return result, 0

    def copy(destination, source, size):
        memory[destination:destination + size] = memory[source:source + size]

    def fail(error):
        raise error

    namespace = {
        "_path_windows": lambda: 1,
        "_path_seq_len": len, "_path_seq_borrow": lambda values, index: values[index],
        "_coerce_path_str": coerce, "ptr_is_null": lambda pointer: pointer == 0,
        "ptr_add": lambda pointer, offset: pointer + offset,
        "load_i8": lambda pointer, offset: memory[pointer + offset],
        "store_i8": lambda pointer, offset, value: memory.__setitem__(pointer + offset, value),
        "load_i64": lambda pointer, offset: int.from_bytes(memory[pointer + offset:pointer + offset + 8], "little"),
        "store_i64": store_i64, "malloc": allocate, "free": lambda pointer: None,
        "py_decref": lambda pointer: None, "null": lambda: 0,
        "py_str_new": new_string, "memmove": copy,
        "_path_windows_equal": lambda a, na, b, nb: ntpath.normcase(text(a, na)) == ntpath.normcase(text(b, nb)),
        "py_raise_owned": fail, "py_exc_new": lambda kind, message: ValueError(message),
        "cstr": lambda value: value,
    }
    names = {"_path_sep", "_path_drive_length", "_windows_component_spans",
             "_windows_commonpath", "py_os_path_commonpath"}
    body = [node for node in ast.parse((ROOT / "pcc/runtime/py/py_os_path.py").read_text()).body
            if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in body} == names
    for node in body:
        node.decorator_list = []
    exec(compile(ast.Module(body=body, type_ignores=[]), "<owned-path-runtime-model>", "exec"), namespace)
    result = namespace["py_os_path_commonpath"](paths)
    return text(result + 40, namespace["load_i64"](result, 16))


@pytest.mark.parametrize("paths", CASES)
def test_existing_windows_runtime_algorithm_matches_component_oracle(paths):
    assert outcome(lambda: runtime_commonpath_model(paths)) == outcome(lambda: ntpath.commonpath(paths))


def test_windows_commonpath_adapts_iterables_path_protocol_and_errors():
    provider = modeled_windows_provider()
    events = []

    class Item:
        def __fspath__(self):
            events.append("fspath")
            return "C:\\root\\child"

    def helper(values):
        assert type(values) is list
        events.append(tuple(values))
        return ntpath.commonpath(values)

    provider._path_commonpath = helper
    assert provider.path.commonpath(value for value in (Item(), "C:\\root")) == "C:\\root"
    assert events == ["fspath", ("C:\\root\\child", "C:\\root")]
    with pytest.raises(NotImplementedError, match="bytes paths"):
        provider.path.commonpath([b"root"])
    error = ValueError("original path error")

    def fail(values):
        raise error

    provider._path_commonpath = fail
    with pytest.raises(ValueError) as caught:
        provider.path.commonpath(["C:\\root"])
    assert caught.value is error
    provider._path_commonpath = lambda values: None
    with pytest.raises(MemoryError, match="commonpath result allocation failed"):
        provider.path.commonpath(["C:\\root"])


def test_posix_commonpath_body_is_unchanged():
    provider = modeled_windows_provider()
    provider._platform_name = "linux"

    def forbidden(values):
        raise AssertionError("POSIX entered Windows adapter")

    provider._path_commonpath = forbidden
    for paths in (("a/b/c", "a/b/d"), ("/root/a", "/root/b"),
                  ("a\\b/x", "a\\b/y")):
        assert provider.path.commonpath(paths) == posixpath.commonpath(paths)
    # Preserve the existing explicitly documented narrow POSIX behavior.
    assert provider.path.commonpath(["/root", "relative"]) == ""
    with pytest.raises(ValueError, match="empty sequence"):
        provider.path.commonpath([])


@pytest.mark.parametrize("escape", (False, True))
def test_provenance_keeps_realpath_containment_strict(monkeypatch, escape):
    from pcc.tools import runtime_archive_provenance as provenance

    provider = modeled_windows_provider()
    source = WINDOWS_ROOT + r"\py\py_os_path.py"
    outside = WINDOWS_ROOT + r"-other\py\py_os_path.py"
    calls = []

    def realpath(value):
        calls.append(value)
        if value == WINDOWS_ROOT:
            return WINDOWS_ROOT
        return outside if escape else source

    provider.path = SimpleNamespace(realpath=realpath, join=ntpath.join,
                                    commonpath=provider.path.commonpath)
    provider.sep = "\\"
    monkeypatch.setattr(provenance, "os", provider)
    if escape:
        with pytest.raises(provenance.ProvenanceError, match="escapes runtime root"):
            provenance._source_from_logical_path("pcc/runtime/py/py_os_path.py", Path(WINDOWS_ROOT))
    else:
        result = provenance._source_from_logical_path("pcc/runtime/py/py_os_path.py", Path(WINDOWS_ROOT))
        assert str(result) == source
    assert len(calls) == 2 and calls[0] == WINDOWS_ROOT


def test_provenance_commonpath_uses_compiled_provider(tmp_path):
    from pcc.tools import runtime_archive_provenance as provenance
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.pipeline_context import build_closed_world_context
    from pcc.frontends.python.type_infer import infer_module
    from tests.owned_ir_validation import verify_ir_text

    # Retain the actual production function body and import shape.
    consumer = tmp_path / "provenance_route.py"
    consumer.write_text('import os\nfrom pathlib import Path\n'
                        'class ProvenanceError(ValueError):\n    pass\n'
                        '_LOGICAL_RUNTIME_ROOT = "pcc/runtime"\n'
                        + inspect.getsource(provenance._source_from_logical_path))
    paths = [str(consumer)] + [str(ROOT / "pcc/stdlib" / name)
                              for name in ("os.py", "pathlib.py", "sys.py")]
    names = ["provenance_route", "os", "pathlib", "sys"]
    modules, exports, derived = build_closed_world_context(paths, names)
    emitted = {}
    for index in (0, 1):
        name = names[index]
        external = {key: value for key, value in exports.items() if key != name}
        module = infer_module(modules[index], external_exports=external, derived_class_map=derived)
        generator = L1CodeGen(module, emit_cpy_main_exitcode=False, ir_scaffold_mode="on")
        generator._target_triple = "x86_64-pc-windows-msvc"
        generator._strict_no_libpython = True
        generator._prefer_native_callable_values = True
        generator._skip_program_main = True
        generator._sibling_module_inits = names
        generator._native_module_exports = external
        text = str(generator.generate())
        (tmp_path / (name + ".ll")).write_text(text)
        verify_ir_text(text)
        emitted[name] = text
    caller = function_body(emitted["provenance_route"], "user_provenance_route__source_from_logical_path")
    assert "@py_compiled_module_import_by_name(" in emitted["provenance_route"]
    assert "@.pyattr.commonpath" in caller
    assert "import.method.commonpath" in caller
    assert re.search(r"call[^\n]*@py_obj_call_slots\(", caller)
    assert "@py_os_path_commonpath(" not in caller
    wrapper = function_body(emitted["os"], "user_os__path_commonpath")
    assert re.search(r"call ptr[^\n]*@py_os_path_commonpath\(", wrapper)
    for body in (caller, wrapper):
        assert not re.search(r"call[^\n]*@py_cpy_", body)
        assert "strict.nolib.stub" not in body
