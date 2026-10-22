"""Native cost gate for the unchanged preload serialization loop boundary.

The production function body and real py_ast/export_meta modules are compiled.
Only reconstruction is replaced by an already-built class map, isolating the
profiled serialization cost. This is not a full frontend/bootstrap proof.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest


_PREFIX = '''
import sys
from pcc.frontends.python.py_ast import ClassType, IntType, Module
from pcc.frontends.python.export_meta import encode_type, encode_type_memo

class _InferCtx:
    def __init__(self, module: Module, external_exports):
        self.class_types: dict[str, ClassType] = external_exports
        self._record_preload_dependencies: bool = False
        self._preload_dependency_modules: set[str] = set()

def _preload_unique_external_classes(ctx: _InferCtx) -> None:
    pass

'''.lstrip()

_SUFFIX = '''

def run(count: int) -> None:
    integer = IntType(name="int", width=64, signed=True)
    leaf = ClassType(name="Leaf", module="provider", fields=(
        ("a", integer), ("b", integer), ("c", integer), ("d", integer),
    ), bases=())
    classes: dict[str, ClassType] = {}
    index = 0
    while index < count:
        name = "Record" + str(index)
        ty = ClassType(name=name, module="provider", fields=(
            ("left", leaf), ("right", leaf), ("other", leaf), ("last", leaf),
        ), bases=())
        classes[name] = ty
        classes["provider." + name] = ty
        index += 1
    result = build_unique_external_class_preload(classes)
    assert len(result["types"]) == count
    assert len(result["keys"]) == count * 2
    assert result["dependencies"] == ()
    index = 0
    while index < count:
        assert result["keys"][index * 2][1] == index
        assert result["keys"][index * 2 + 1][1] == index
        descriptor = result["types"][index]
        assert descriptor[0] == "class"
        assert descriptor[1] == "Record" + str(index)
        assert descriptor[3][0][1][1] == "Leaf"
        assert descriptor[3][0][1][3][0][1] == ("int", 64, True)
        index += 1
    print(count)
    print("native-preload-encoding-ok")

run(int(sys.argv[1]))
'''


# requires explicit immutable PCC_RUNTIME_ARCHIVE
@pytest.mark.pcc_gate(env="PCC_RUNTIME_ARCHIVE")
def test_native_preload_encoding_preserves_alias_ids_and_nested_types(tmp_path):
    from pcc.frontends.python.pipeline import compile_python_multi

    archive_name = os.environ.get("PCC_RUNTIME_ARCHIVE")
    repo = Path(__file__).resolve().parents[2]
    source_root = Path(os.environ.get("PCC_PRELOAD_FUNCTION_SOURCE_ROOT", str(repo))).resolve()
    source_path = source_root / "pcc/frontends/python/type_infer.py"
    source = source_path.read_text(encoding="utf-8")
    functions = [node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)
                 and node.name == "build_unique_external_class_preload"]
    assert len(functions) == 1
    body = ast.get_source_segment(source, functions[0])
    assert body
    consumer = tmp_path / "preload_encoding.py"
    consumer.write_text(_PREFIX + body + "\n" + _SUFFIX, encoding="utf-8")
    modules = ("pcc.frontends.python.py_ast", "pcc.frontends.python.export_meta")
    paths = [repo / (name.replace(".", "/") + ".py") for name in modules]
    archive = Path(archive_name).resolve(strict=True)
    hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in [source_path, *paths, archive]}
    output = tmp_path / "preload_encoding"
    compile_python_multi(
        [str(consumer), *(str(p) for p in paths)], str(output),
        entry_module="pcc.frontends.python.preload_encoding",
        module_names=["pcc.frontends.python.preload_encoding", *modules],
        libpython_mode="off", ir_scaffold_mode="on", backend="self",
        recursive_stdlib=False, target_triple="arm64-apple-darwin23.6.0",
        runtime_archive=str(archive),
    )
    result = subprocess.run([str(output), "32"], capture_output=True, text=True, timeout=10)
    (tmp_path / "preload_encoding.stdout").write_text(result.stdout)
    (tmp_path / "preload_encoding.stderr").write_text(result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "32\nnative-preload-encoding-ok\n"
    assert hashes == {path: hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in hashes}
    receipt = {
        "status": "COMPLETE", "claim": "native serialization phase only; reconstruction prebuilt",
        "sources": hashes, "function_source_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "binary": str(output), "binary_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "stdout": result.stdout,
    }
    path = tmp_path / "preload_encoding.receipt.json"
    path.write_text(json.dumps(receipt, indent=2) + "\n")
    print(f"preload encoding receipt: {path}")
