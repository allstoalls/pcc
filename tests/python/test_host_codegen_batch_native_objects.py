"""Real host worker order/export/PCO isolation; draft execution is UNRUN.

Each worker has a fresh process so gc.freeze remains real without freezing
pytest. These checks emit owned objects but do not link or execute them. The
full native platform and Stage1 gates still have to establish those boundaries.
"""

import json
import os
import subprocess
import sys

import pytest


_WORKER = r'''
import json
import sys
import weakref
from pcc.frontends.python import pipeline
from pcc.frontends.python import pipeline_exports
from pcc.frontends.python import pipeline_frontend_worker_execution as execution
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.codegen import marshal
from pcc.backend import self_backend_parse

manifest, target, receipt_path, batch = sys.argv[1:]
batch = batch == "2"
original_generate = L1CodeGen.generate
original_read = pipeline._read_native_exports_wire_for_module
original_ast = pipeline._read_py_ast_wire
original_publish = execution.publish_worker_resource
original_parse = self_backend_parse.parse_self_backend_module
references = []
export_views = []
retired = []


def snapshot(exports, derived):
    return json.dumps(pipeline_exports._native_export_to_wire((exports, derived)),
                      sort_keys=True, separators=(",", ":"))


def assert_retired():
    assert all(reference() is None for reference in references), "previous module survives"
    assert not marshal._BOXED_I64_CONSTANTS, "previous boxed integer survives"


def generate(self, module=None):
    assert module is self.ast_module
    self._target_triple = target
    output = original_generate(self, module)
    references.extend((weakref.ref(self), weakref.ref(self.ast_module), weakref.ref(self.module)))
    return output


def read_exports(path, root):
    values = original_read(path, root)
    assert not values[3], "expected unchanged full-graph legacy exports"
    export_views.append((values[0], values[1], snapshot(values[0], values[1])))
    return values


def read_ast(path):
    if batch and references:
        assert_retired()
    ast = original_ast(path)
    references.append(weakref.ref(ast))
    return ast


def parse(text):
    parsed = original_parse(text)
    references.append(weakref.ref(parsed))
    return parsed


def publish(phase):
    original_publish(phase)
    if phase.startswith("module-retired:"):
        assert_retired()
        retired.append(phase)


L1CodeGen.generate = generate
pipeline._read_native_exports_wire_for_module = read_exports
pipeline._read_py_ast_wire = read_ast
execution.publish_worker_resource = publish
self_backend_parse.parse_self_backend_module = parse
status = pipeline.run_python_multi_codegen_worker(manifest)
assert status == 0, "worker failed; inspect result TSV"
assert len(export_views) == 1, "worker decoded exports more than once"
for exports, derived, before in export_views:
    assert snapshot(exports, derived) == before, "shared export metadata mutated"
if batch:
    assert_retired()
    assert len(retired) == 2
with open(receipt_path, "w", encoding="utf-8") as stream:
    json.dump({"decode_calls": len(export_views), "exports_unchanged": True,
               "retired": retired}, stream, sort_keys=True)
'''


def _environment(batch_size):
    env = dict(os.environ)
    for name in (
        "PCC_STAGE1_CHECKPOINT_DIR", "PCC_STAGE1_CHECKPOINT_BUILD",
        "PCC_STAGE1_CHECKPOINT_ATTEMPT", "PCC_INDEXED_HANDOFF_REQUEST",
        "PCC_DEFER_FRONTEND_CODEGEN_PLAN", "PCC_COMPILE_PROGRESS_FILE",
        "PCC_WORKER_RESOURCE_REPORT", "PCC_WORKER_RESOURCE_TOKEN",
        "PCC_PYTHON_IR_PASS_SKIP_MODULE_PREFIXES", "PCC_PY_FRONTEND_WORKER",
    ):
        env.pop(name, None)
    env.update({
        "PCC_HOST_CODEGEN_BATCH": str(batch_size),
        "PCC_WORKER_TREE_BUDGET_BYTES": str(8 * 1024**3),
        "PCC_HOST_INDEXED_PROCESS_SPLIT": "0",
        "PCC_DIRECT_INDEXED_KERNEL_CAPTURE": "1",
        "PCC_DIRECT_INDEXED_KERNEL_EMIT": "1",
        "PCC_DIRECT_INDEXED_KERNEL_FUSE_USES": "1",
        "PCC_DIRECT_INDEXED_KERNEL_RELEASE_FRONTEND": "1",
        "PCC_DIRECT_INDEXED_KERNEL_REQUIRE_ZERO_FALLBACK": "1",
        "PCC_DIRECT_INDEXED_NATIVE_OBJECT": "1",
        "PCC_DIRECT_INDEXED_KERNEL_VALIDATE": "0",
        "PCC_TEXT_INDEXED_KERNEL_EMIT": "0",
        "PCC_DIRECT_INDEXED_SIDECAR": "0",
        "PCC_PY_FRONTEND_IN_PROCESS_CODEGEN": "0",
        "PCC_PYTHON_IR_PASSES": "mem2reg,sroa",
        "PCC_BACKEND": "self",
    })
    return env


@pytest.mark.integration
@pytest.mark.parametrize("target", [
    "x86_64-unknown-linux-gnu", "x86_64-pc-windows-msvc",
    "aarch64-unknown-linux-gnu", "arm64-apple-darwin",
])
def test_real_batch_orders_match_singletons_and_keep_exports_unchanged(tmp_path, target):
    from pcc.frontends.python import pipeline

    sources = {
        "alpha": (
            "from support import BIAS\n"
            "class Shared:\n"
            "    def value(self) -> int:\n"
            "        return BIAS\n"
            "def compute(value: int) -> int:\n"
            "    return value + BIAS\n"
        ),
        "beta": (
            "from alpha import compute\n"
            "from support import Base\n"
            "class Shared(Base):\n"
            "    def value(self) -> int:\n"
            "        return compute(35)\n"
            "def answer(value: int) -> int:\n"
            "    return compute(value)\n"
        ),
        "support": (
            "BIAS = 7\n"
            "class Base:\n"
            "    def value(self) -> int:\n"
            "        return BIAS\n"
        ),
    }
    paths = []
    names = list(sources)
    for name, source in sources.items():
        path = tmp_path / (name + ".py")
        path.write_text(source, encoding="utf-8")
        paths.append(str(path))
    parsed, exports, derived = pipeline.build_closed_world_context(paths, names)
    exports_path = tmp_path / "exports.json"
    pipeline._write_native_exports_wire(str(exports_path), exports, derived)
    frozen_exports = exports_path.read_bytes()
    ast_dir = tmp_path / "ast"
    ast_dir.mkdir()
    for index, ast in enumerate(parsed):
        pipeline._write_py_ast_wire(str(ast_dir / ("module_" + str(index) + ".json")), ast)

    artifacts = {}
    for label, assigned, batch_size in (
        ("single-alpha", [0], 1), ("single-beta", [1], 1),
        ("alpha-beta", [0, 1], 2), ("beta-alpha", [1, 0], 2),
    ):
        output_dir = tmp_path / label
        output_dir.mkdir()
        manifest = output_dir / "worker.manifest"
        result_path = output_dir / "result.tsv"
        receipt_path = output_dir / "isolation.json"
        pipeline._write_python_frontend_worker_manifest(
            str(manifest), str(result_path), str(output_dir), str(exports_path),
            str(ast_dir), paths, names, assigned, entry_module="support",
            sibling_inits=(), libpython_mode="off", ir_scaffold_mode="on", verbose=False,
        )
        # No shell, native runtime build, FFI, external compiler or linker.
        completed = subprocess.run(
            [sys.executable, "-B", "-c", _WORKER, str(manifest), target,
             str(receipt_path), str(batch_size)],
            env=_environment(batch_size), capture_output=True, text=True, timeout=120,
        )
        detail = result_path.read_text() if result_path.exists() else "no result TSV"
        assert completed.returncode == 0, completed.stderr + "\n" + detail
        receipt = json.loads(receipt_path.read_text())
        assert receipt["decode_calls"] == 1 and receipt["exports_unchanged"] is True
        assert receipt["retired"] == (["module-retired:" + str(index) for index in assigned]
                                      if batch_size == 2 else [])
        artifacts[label] = {}
        for index in assigned:
            ir = (output_dir / ("module_" + str(index) + ".ll")).read_bytes()
            pco = (output_dir / ("module_" + str(index) + ".direct.pco")).read_bytes()
            assert b"define " in ir and pco
            artifacts[label][index] = (ir, pco)
        assert exports_path.read_bytes() == frozen_exports
    for index, singleton in ((0, "single-alpha"), (1, "single-beta")):
        assert artifacts["alpha-beta"][index] == artifacts[singleton][index]
        assert artifacts["beta-alpha"][index] == artifacts[singleton][index]
