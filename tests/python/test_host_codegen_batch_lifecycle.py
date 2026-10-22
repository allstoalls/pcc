"""Draft host batch lifecycle contracts; execution is UNRUN.

The real worker loop/release/collection runs with stub inference, L1, passes,
parsing and emission. Separate integration must establish real gc.freeze
behavior, valid PCO/link/run parity, memory bounds, and native-worker isolation.
Backend symbol checks prove call order only; no real object assembly runs.
"""

import gc
import os
from types import SimpleNamespace
import weakref

import pytest


@pytest.fixture
def batch_worker(tmp_path, monkeypatch):
    from pcc.backend import (
        self_backend_ir, self_backend_parse,
        self_backend_x86_64_linux as x86, target_objects,
    )
    from pcc.backend.self_backend_module_symbols import PreparedModuleSymbols
    from pcc.frontends.python import (
        compiled_owned_passes,
        pipeline_frontend_worker_execution as execution,
        type_infer,
    )
    from pcc.frontends.python.codegen import layer1, marshal
    from pcc.ir import direct_indexed_kernel
    from pcc.ir.compat import ir

    state = SimpleNamespace(
        events=[], references={}, errors=[], reads=[], fail_index=None,
        contaminate=False, path=tmp_path, marshal=marshal, ir=ir,
        export_reads=[], preload_builds=[], infer_preloads=[], indexed_exports=False,
    )
    monkeypatch.setattr(ir, "global_context", ir.Context())
    monkeypatch.setattr(marshal, "_BOXED_I64_CONSTANTS", {})
    monkeypatch.setattr(x86, "_MODULE_SYMBOLS", PreparedModuleSymbols(
        "", frozenset(), frozenset(), frozenset(),
    ))
    monkeypatch.setattr(x86, "_TLS_GLOBALS", {})
    monkeypatch.setattr(x86, "_VARARG_FUNCTIONS", frozenset())
    monkeypatch.setattr(x86, "_X86_EMISSION_ACTIVE", False)
    # Real retirement helpers must not clear another test's cache objects.
    for module, names in (
        (self_backend_ir, ("_TEXT_KEY_INDEX_CACHE", "_OPERAND_INTERN")),
        (self_backend_parse, ("_NUMERIC_SSA_NAME_CACHE", "_DOT_NUMERIC_SSA_NAME_CACHE")),
    ):
        for name in names:
            monkeypatch.setattr(module, name, {})
    manifest = {
        "result_path": str(tmp_path / "result.tsv"), "job_kind": "codegen",
        "src_paths": [str(tmp_path / (name + ".py")) for name in ("main", "sibling")],
        "module_names": ["main", "sibling"], "entry_module": "main",
        "sibling_inits": ["sibling"], "libpython_mode": "off",
        "ir_scaffold_mode": "on", "verbose": False,
        "assigned_indices": [0, 1], "ir_dir": str(tmp_path),
        "exports_path": str(tmp_path / "exports.json"), "ast_dir": str(tmp_path / "ast"),
    }
    for name in (
        "PCC_DIRECT_INDEXED_KERNEL_EMIT", "PCC_DIRECT_INDEXED_KERNEL_CAPTURE",
        "PCC_DIRECT_INDEXED_NATIVE_OBJECT", "PCC_DIRECT_INDEXED_KERNEL_RELEASE_FRONTEND",
        "PCC_DIRECT_INDEXED_KERNEL_REQUIRE_ZERO_FALLBACK",
    ):
        monkeypatch.setenv(name, "1")
    for name in (
        "PCC_DIRECT_INDEXED_KERNEL_VALIDATE", "PCC_TEXT_INDEXED_KERNEL_EMIT",
        "PCC_DIRECT_INDEXED_SIDECAR", "PCC_HOST_INDEXED_PROCESS_SPLIT",
        "PCC_DEBUG_WORKER_RERAISE",
    ):
        monkeypatch.setenv(name, "0")
    for name in (
        "PCC_INDEXED_HANDOFF_REQUEST", "PCC_STAGE1_CHECKPOINT_DIR",
        "PCC_STAGE1_CHECKPOINT_BUILD", "PCC_STAGE1_CHECKPOINT_ATTEMPT",
        "PCC_COMPILE_PROGRESS_FILE", "PCC_PY_FRONTEND_IN_PROCESS_CODEGEN",
        "PCC_DEFER_FRONTEND_CODEGEN_PLAN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PCC_STAGE1_CHECKPOINT_STAGE", "1")
    monkeypatch.setenv("PCC_HOST_CODEGEN_BATCH", "2")
    monkeypatch.setenv("PCC_WORKER_TREE_BUDGET_BYTES", str(8 * 1024**3))
    monkeypatch.setenv("PCC_BACKEND", "self")

    class Node:
        def __init__(self, index, kind):
            self.index = index
            state.references[index, kind] = weakref.ref(self)

        def __str__(self):
            return "module:" + str(self.index)

    class Codegen:
        def __init__(self, typed, *_args):
            index = typed.index
            state.references[index, "codegen"] = weakref.ref(self)
            self.typed = typed
            self.cycle = self
            self.module = Node(index, "generated")
            self.module._functions = []
            self.module._globals = []
            self.module.globals = {}
            self.module._direct_indexed_fallback_records = 0
            for name in (
                "functions", "runtime", "env", "_module_globals",
                "_module_global_init_flags", "_funcdef_functions",
                "_native_symbol_funcdefs", "_fn_err_exit_blocks",
            ):
                setattr(self, name, {})
            self._direct_indexed_module = Node(index, "capture")

        def generate(self, _typed):
            # A real cache entry retains this frontend node until reset.
            marshal.note_boxed_i64_constant(self.module, 7)
            state.events.append((self.module.index, "cache seeded"))
            if state.contaminate:
                ir.global_context.get_identified_type("batch_leak")
            return self.module

        def _generate_initial_module(self):
            # The worker's route for a fresh instance: its constructor AST.
            return self.generate(self.typed)

    class EmittedBytes(bytes):
        def __new__(cls, index):
            result = super().__new__(cls, b"object")
            result.index = index
            return result

        def __del__(self):
            state.events.append((self.index, "encoded released"))

    class RetainedIR(str):
        def __new__(cls, index):
            result = super().__new__(cls, "passed:" + str(index))
            result.index = index
            return result

        def __del__(self):
            state.events.append((self.index, "IR released"))

    def assert_frontend_released(index):
        for kind in ("AST", "typed", "codegen", "generated", "capture", "direct"):
            assert state.references[index, kind]() is None, (index, kind)

    def assert_retired(index):
        assert_frontend_released(index)
        assert (index, "encoded released") in state.events
        assert (index, "IR released") in state.events
        assert not marshal._BOXED_I64_CONSTANTS

    def read_ast(path):
        index = int(os.path.basename(path).split("_")[1].split(".")[0])
        if index:
            assert_retired(index - 1)
            assert "module-retired:" + str(index - 1) in state.events
        state.reads.append(index)
        state.events.append("read:" + str(index))
        return Node(index, "AST")

    def read_exports(path, root):
        assert path == manifest["exports_path"]
        state.export_reads.append(root)
        return {"main": {}, "sibling": {}}, {}, {"first-root": True}, state.indexed_exports

    def build_preload(external):
        root = manifest["module_names"][state.reads[-1]]
        state.preload_builds.append((root, sorted(external)))
        return {}

    def infer(ast, **_kwargs):
        state.infer_preloads.append((ast.index, dict(_kwargs["unique_external_class_preload"])))
        typed = Node(ast.index, "typed")
        typed.ast = ast
        return typed

    def parse_direct(text):
        result = Node(int(text.split(":")[1]), "direct")
        result.triple = "x86_64-unknown-linux-gnu"
        return result

    def emit(module, **_kwargs):
        x86._MODULE_SYMBOLS = PreparedModuleSymbols(
            internal_prefix=".test_", defined_symbols=frozenset({"deferred"}),
            internal_symbols=frozenset({"deferred"}),
            thread_local_symbols=frozenset({"deferred_tls"}),
        )
        x86._TLS_GLOBALS = {"deferred_tls": object()}
        x86._VARARG_FUNCTIONS = frozenset({"deferred"})
        return "assembly:" + str(module.index)

    def encode(assembly, _target, **_kwargs):
        index = int(assembly.split(":")[1])
        # No fixture gc.collect may mask failure in the actual release path.
        assert_frontend_released(index)
        # Deferred packed-stackmap consumers still need these target symbols.
        assert x86._asm_symbol("deferred") == ".test_deferred"
        assert "deferred_tls" in x86._TLS_GLOBALS
        assert x86._VARARG_FUNCTIONS == frozenset({"deferred"})
        state.events.append((index, "backend symbols consumed"))
        if index == state.fail_index:
            raise RuntimeError("injected second-module encoder failure")
        return EmittedBytes(index)

    def publish(stage):
        if stage.startswith("module-retired:"):
            index = int(stage.split(":")[1])
            assert_retired(index)
            assert (index, "backend symbols consumed") in state.events
            assert not x86._MODULE_SYMBOLS.defined_symbols
            assert not x86._MODULE_SYMBOLS.internal_symbols
            assert not x86._MODULE_SYMBOLS.thread_local_symbols
            assert x86._MODULE_SYMBOLS.type_context is None
            assert not x86._TLS_GLOBALS
            assert not x86._VARARG_FUNCTIONS
        state.events.append(stage)

    def write_error(path, text):
        state.errors.append(text)
        # Match the worker ERR wire contract without importing process helpers.
        with open(path, "w", encoding="utf-8") as stream:
            stream.write("ERR\t" + text.replace("\t", " ").replace("\n", " ") + "\n")

    def unexpected(*_args, **_kwargs):
        pytest.fail("unexpected checkpoint, full-reader, graph, or other worker route")

    monkeypatch.setattr(execution, "_validate_stage1_checkpoint_worker", unexpected)
    monkeypatch.setattr(execution, "_validate_stage1_checkpoint_module_context", unexpected)
    # Freezing pytest's host graph is process-wide; count the calls instead.
    monkeypatch.setattr(execution, "_freeze_worker_survivors",
                        lambda: state.events.append("freeze"))
    monkeypatch.setattr(execution, "publish_worker_resource", publish)
    monkeypatch.setattr(execution, "_direct_owned_pass_names", lambda _name: ["mem2reg", "simplifycfg"])
    monkeypatch.setattr(type_infer, "infer_module", infer)
    monkeypatch.setattr(type_infer, "build_unique_external_class_preload", build_preload)
    monkeypatch.setattr(layer1, "L1CodeGen", Codegen)
    monkeypatch.setattr(compiled_owned_passes, "run_owned_passes",
                        lambda text, *_args: RetainedIR(int(text.split(":")[1])))
    monkeypatch.setattr(self_backend_parse, "parse_self_backend_module", parse_direct)
    monkeypatch.setattr(direct_indexed_kernel, "direct_indexed_module_first_libpython_edge",
                        lambda _module: "")
    monkeypatch.setattr(target_objects, "emit_indexed_assembly", emit)
    monkeypatch.setattr(target_objects, "encode_assembly_object", encode)
    monkeypatch.setattr(target_objects, "emit_x86_64_streamed_module",
                        lambda module, **kwargs: _StreamedTextStub(emit(module, **kwargs), encode))

    def run():
        return execution.run_codegen_worker(
            "tracked.manifest", read_manifest=lambda _path: manifest,
            run_export_worker_callback=unexpected, run_summary_worker_callback=unexpected,
            worker_timing_enabled=lambda: False, native_worker_executable=lambda: False,
            read_native_exports_wire=unexpected,
            read_native_exports_wire_for_module=read_exports, read_ast_wire=read_ast,
            build_closed_world_context=unexpected,
            module_imports_native_extension=lambda *_args, **_kwargs: True,
            contextual_host_params_for_module=lambda *_args: {},
            module_uses_default_native_exports=lambda _name: False,
            copy_native_module_exports=lambda value: value,
            closed_world_function_object_exports=lambda *_args: {},
            log=lambda *_args: None, ir_needs_libpython=lambda _text: False,
            safe_exception_text=str, write_worker_error=write_error, pipeline_error=RuntimeError,
        )

    state.run = run
    state.assert_retired = assert_retired
    return state


def test_batch_retires_each_module_before_next_ast(batch_worker):
    worker = batch_worker
    stale = b"STALE prior attempt bytes\n"
    result_path = worker.path / "result.tsv"
    partial_path = worker.path / "result.tsv.batch.partial"
    result_path.write_bytes(stale)
    partial_path.write_bytes(stale)
    for index in (0, 1):
        for suffix in (".ll", ".direct.pco"):
            (worker.path / ("module_" + str(index) + suffix)).write_bytes(stale)
    assert worker.run() == 0, worker.errors
    assert worker.errors == []
    assert worker.reads == [0, 1]
    assert worker.export_reads == ["main"]
    assert worker.preload_builds == [("sibling", ["main"])]
    assert worker.infer_preloads == [(0, {"first-root": True}), (1, {})]
    assert worker.events.count("freeze") == 1
    assert worker.events.index("freeze") < worker.events.index("read:0")
    assert [event for event in worker.events if isinstance(event, str)
            and event.startswith("module-retired:")] == ["module-retired:0", "module-retired:1"]
    for index in (0, 1):
        worker.assert_retired(index)
        assert (worker.path / ("module_" + str(index) + ".direct.pco")).read_bytes() == b"object"
        assert (worker.path / ("module_" + str(index) + ".ll")).read_text() == "passed:" + str(index)
    assert not partial_path.exists()
    assert stale not in result_path.read_bytes()
    rows = [line.split("\t") for line in result_path.read_text().splitlines()]
    assert len(rows) == 2
    assert [row[:3] for row in rows] == [["OK", "0", "main"], ["OK", "1", "sibling"]]
    assert all(row[7] == "PCO" for row in rows)
    assert worker.events[-1] == "complete"


def test_second_module_failure_never_publishes_partial_success(batch_worker):
    worker = batch_worker
    worker.fail_index = 1
    assert worker.run() == 1
    assert worker.reads == [0, 1]
    assert len(worker.errors) == 1
    assert "codegen[sibling]" in worker.errors[0]
    assert "injected second-module encoder failure" in worker.errors[0]
    rows = (worker.path / "result.tsv").read_text().splitlines()
    assert len(rows) == 1 and rows[0].startswith("ERR\t")
    assert not any(row.startswith("OK\t") for row in rows)
    assert (worker.path / "module_0.direct.pco").exists()  # Orphan output is not acceptance.
    assert not (worker.path / "module_1.direct.pco").exists()
    assert "module-retired:1" not in worker.events
    assert "complete" not in worker.events


def test_batch_rejects_indexed_exports_before_ast(batch_worker):
    worker = batch_worker
    worker.indexed_exports = True
    assert worker.run() == 1
    assert worker.export_reads == ["main"]
    assert worker.reads == []
    assert worker.preload_builds == []
    assert len(worker.errors) == 1 and "full-graph export input" in worker.errors[0]
    rows = (worker.path / "result.tsv").read_text().splitlines()
    assert len(rows) == 1 and rows[0].startswith("ERR\t")
    assert "complete" not in worker.events


@pytest.mark.parametrize("when", ["before-first", "during-first"])
def test_batch_rejects_named_context_before_next_ast(batch_worker, when):
    worker = batch_worker
    if when == "before-first":
        worker.ir.global_context.get_identified_type("preexisting")
    else:
        worker.contaminate = True
    assert worker.run() == 1
    assert worker.reads == ([] if when == "before-first" else [0])
    assert len(worker.errors) == 1 and "identified" in worker.errors[0].lower()
    rows = (worker.path / "result.tsv").read_text().splitlines()
    assert len(rows) == 1 and rows[0].startswith("ERR\t")
    assert "complete" not in worker.events


def test_marshal_reset_precedes_frontend_collection(batch_worker, monkeypatch):
    worker = batch_worker
    real_reset = worker.marshal.reset_boxed_i64_constants
    real_collect = gc.collect
    events = []

    def reset():
        if worker.marshal._BOXED_I64_CONSTANTS:
            events.append("reset")
        real_reset()

    def collect(*args, **kwargs):
        assert not worker.marshal._BOXED_I64_CONSTANTS, "frontend collected before marshal reset"
        events.append("collect")
        return real_collect(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(worker.marshal, "reset_boxed_i64_constants", reset)
        patch.setattr(gc, "collect", collect)
        assert worker.run() == 0, worker.errors
    assert events.count("reset") == 2
    assert events[0] == "reset"
    assert "collect" in events
    assert not worker.marshal._BOXED_I64_CONSTANTS


class _StreamedTextStub:
    """The worker's x86 object route emits into sections, then encodes later.

    Replay that boundary through this test's text emit/encode stubs so the
    release and symbol-lifetime checks still run at the encode step.
    """

    def __init__(self, assembly, encode):
        self.assembly = assembly
        self.encode = encode

    def encode_object(self, _phase_timing=None):
        return self.encode(self.assembly, "x86_64-unknown-linux-gnu")
