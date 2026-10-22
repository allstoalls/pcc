"""Ordinary field locals and cross-module integer ABI proof boundaries."""
import os
import re
import subprocess

import pytest

from pcc.backend.owned_object_emit import emit_owned_object
from pcc.frontends.python.pipeline import compile_python
from pcc.frontends.python.pipeline_context import build_closed_world_context


def test_unproved_ordinary_int_field_local_stays_exact(tmp_path, monkeypatch):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "field_local.py"
    source.write_text('''from pcc.unsafe import null
from dataclasses import dataclass
@dataclass
class Row:
    value: int
def probe(row: Row):
    value = row.value
    return value + 1
def main():
    assert probe(Row(18446744073709551615)) == 18446744073709551616
main()
''')
    output = source.with_suffix(".ll")
    compile_python(str(source), str(output), emit_llvm_only=True, backend="self",
                   libpython_mode="off", ir_scaffold_mode="on")
    body = re.search(r"(?ms)^define [^\n]*@user_field_local_probe\(.*?\n\}", output.read_text()).group(0)
    assert re.search(r"%value\.addr[^ ]* = alloca ptr", body)
    assert "@py_int_to_i64_lane" not in body


def _cross_module_sources(tmp_path):
    provider = tmp_path / "provider.py"
    provider.write_text('''from pcc.unsafe import null
def sum_ints(values: list[int]) -> int:
    result: int = 0
    for value in values:
        result = result + value
    return result
assert sum_ints([1, 2, 3]) == 6
''')
    consumer = tmp_path / "consumer.py"
    consumer.write_text('''from provider import sum_ints
def main():
    assert sum_ints([1208925819614629174706176]) == 1208925819614629174706176
    print("CROSS_MODULE_INT_ABI_OK")
main()
''')
    return provider, consumer


def test_external_callers_invalidate_module_local_integer_abi_proof(tmp_path):
    provider, consumer = _cross_module_sources(tmp_path)
    _parsed, exports, _derived = build_closed_world_context(
        [str(provider), str(consumer)], ["provider", "consumer"],
    )
    # Provider-local literal calls do not constrain this actual caller.
    assert exports["provider"]["sum_ints"]["box_int_abi"] is True


def test_eight_module_export_shards_preserve_provider_and_caller_abi(tmp_path, monkeypatch):
    from pcc.frontends.python import pipeline
    from pcc.frontends.python.type_infer import build_unique_external_class_preload_index

    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    for name in ("PCC_DIRECT_INDEXED_KERNEL_EMIT", "PCC_DIRECT_INDEXED_KERNEL_CAPTURE",
                 "PCC_DIRECT_INDEXED_SIDECAR", "PCC_TEXT_INDEXED_KERNEL_EMIT"):
        monkeypatch.setenv(name, "0")
    provider, consumer = _cross_module_sources(tmp_path)
    names = ["consumer"] + ["filler" + str(index) for index in range(7)] + ["provider"]
    paths = [str(consumer)]
    for name in names[1:-1]:
        source = tmp_path / (name + ".py")
        source.write_text("TOKEN = 1\n")
        paths.append(str(source))
    paths.append(str(provider))
    ast_dir = tmp_path / "ast"
    ir_dir = tmp_path / "ir"
    ast_dir.mkdir()
    ir_dir.mkdir()
    exports = {}
    uses = []
    edges = []
    dependencies = {}
    # The provider is in the one-module tail after the actual eight-module
    # export shard. The global manifest, not shard length, owns closedness.
    for shard, indices in enumerate((list(range(8)), [8])):
        result = tmp_path / ("export" + str(shard) + ".tsv")
        manifest = dict(result_path=str(result), ir_dir=str(ir_dir), ast_dir=str(ast_dir),
                        src_paths=paths, module_names=names, assigned_indices=indices)
        assert pipeline._run_python_multi_export_worker(manifest) == 0
        fields = result.read_text().strip().split("\t")
        shard_exports, _derived, shard_uses = pipeline._read_native_exports_wire(
            fields[1], include_function_object_uses=True,
        )
        exports.update(shard_exports)
        uses.extend(shard_uses)
        shard_edges, shard_dependencies = pipeline._read_reexport_edges_wire(
            fields[2], include_module_dependencies=True,
        )
        edges.extend(shard_edges)
        dependencies.update(dict(shard_dependencies))
    pipeline._merge_closed_world_reexport_edges(names, exports, edges)
    pipeline._apply_closed_world_function_object_uses(exports, uses)
    assert exports["provider"]["sum_ints"]["box_int_abi"] is True
    wire = tmp_path / "exports.indexed"
    pipeline._write_native_exports_wire(
        str(wire), exports, {}, module_dependencies=dependencies,
        unique_class_preload_index=build_unique_external_class_preload_index(exports),
    )
    for index in (8, 0):
        selected, _derived, _preload, indexed = pipeline._read_native_exports_wire_for_module(
            str(wire), names[index],
        )
        assert indexed is True
        assert selected["provider"]["sum_ints"]["box_int_abi"] is True
        manifest = tmp_path / ("codegen" + str(index) + ".manifest")
        result = tmp_path / ("codegen" + str(index) + ".tsv")
        pipeline._write_python_frontend_worker_manifest(
            str(manifest), str(result), str(ir_dir), str(wire), str(ast_dir),
            paths, names, [index], entry_module="consumer", sibling_inits=(),
            libpython_mode="off", ir_scaffold_mode="on", verbose=False,
        )
        assert pipeline.run_python_multi_codegen_worker(str(manifest)) == 0, result.read_text()
    provider_ir = (ir_dir / "module_8.ll").read_text()
    consumer_ir = (ir_dir / "module_0.ll").read_text()
    assert re.search(r"define (?:external )?ptr @user_provider_sum_ints\(ptr", provider_ir)
    assert re.search(r"declare (?:external )?ptr @user_provider_sum_ints\(ptr", consumer_ir)
    assert re.search(r"call ptr[^\n]*@user_provider_sum_ints\(ptr", consumer_ir)
    assert emit_owned_object(provider_ir, "x86_64-unknown-linux-gnu")[:4] == b"\x7fELF"
    assert emit_owned_object(consumer_ir, "x86_64-unknown-linux-gnu")[:4] == b"\x7fELF"


@pytest.mark.integration
def test_cross_module_large_integer_native(tmp_path, monkeypatch, pcc_runtime_archive):
    from pcc.frontends.python.pipeline import compile_python_multi

    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    provider, consumer = _cross_module_sources(tmp_path)
    binary = tmp_path / "cross_module_integer"
    compile_python_multi([str(provider), str(consumer)], str(binary),
                         module_names=["provider", "consumer"], entry_module="consumer",
                         backend="self", libpython_mode="off", ir_scaffold_mode="on",
                         runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20,
                             env=dict(os.environ, PATH="/nonexistent", PCC_GC_BACKEND=str(backend)))
        assert ran.returncode == 0, (backend, ran.stdout, ran.stderr)
        assert ran.stdout == "CROSS_MODULE_INT_ABI_OK\n"
