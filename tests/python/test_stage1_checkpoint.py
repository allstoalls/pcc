"""Host-only checkpoint identity, object validation and crash-boundary contracts."""

import hashlib
import json
import os
import stat
import struct

import pytest

from pcc.backend import elf_x86_64 as elf
from pcc.backend.precise_stackmap import (
    ARCH_X86_64,
    FunctionStackMap,
    PreciseStackMap,
    encode_stack_map,
    function_address_offsets,
    function_id,
)
from pcc.frontends.python import pipeline_stage1_checkpoint as checkpoint


def _json(path, value):
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=True, allow_nan=False) + "\n")


def _object(*, relocations=False, stackmap=False):
    sections = [elf.ElfSection(
        ".text", elf.SHT_PROGBITS, elf.SHF_ALLOC | elf.SHF_EXECINSTR, 16,
        b"\x90" * 7 + b"\xc3",
        relocations=(elf.ElfRelocation(0, 1, elf.R_X86_64_64),) if relocations else (),
    )]
    if stackmap:
        payload = encode_stack_map(PreciseStackMap(ARCH_X86_64, (
            FunctionStackMap(function_id("answer"), 0, 8, 0, ()),
        )))
        sections.append(elf.ElfSection(
            ".pcc_stackmaps", elf.SHT_PROGBITS, elf.SHF_ALLOC, 8, payload,
            relocations=(elf.ElfRelocation(function_address_offsets(payload)[0],
                                           1, elf.R_X86_64_64),),
        ))
    return elf.emit_relocatable(elf.ElfObject(
        tuple(sections), (elf.ElfSymbol.null(),
                         elf.ElfSymbol("answer", 1, 0, 8, elf.STB_GLOBAL, elf.STT_FUNC)),
    ))


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "checkpoint"
    root.mkdir()
    artifacts = tmp_path / "attempt-two"
    artifacts.mkdir()
    monkeypatch.setenv("PCC_STAGE1_CHECKPOINT_STAGE", "1")
    monkeypatch.setenv("PCC_STAGE1_CHECKPOINT_ATTEMPT", "attempt-one")
    payload = {"stage": 1, "producer_role": "host-pcc0", "owner": "cpython",
               "backend": "self", "target": "x86_64-unknown-linux-gnu",
               "compiler_sources": [{"path": "compiler.py", "sha256": "a" * 64}]}
    build = checkpoint.canonical_digest(payload)
    _json(root / "build.json", {"schema": checkpoint.BUILD_SCHEMA,
                               "identity_sha256": build, "payload": payload})
    graph = {
        "stage": 1, "producer_role": "host-pcc0", "owner": "cpython", "backend": "self",
        "target": payload["target"], "module_names": ["main", "sibling"],
        "original_chunks": [[0, 1]], "sources": [{
            "index": index, "module_name": name, "source_path": str(tmp_path / (name + ".py")),
            "source_sha256": str(index) * 64, "ast_sha256": "a" * 64,
            "ast_size": 5, "passes": ["mem2reg", "simplify"],
        } for index, name in enumerate(["main", "sibling"])],
        "entry_module": "main", "sibling_inits": ["sibling"], "libpython_mode": "off",
        "ir_scaffold_mode": "native", "worker_prefix": ["python", "-m", "pcc"], "jobs": 1,
        "exports_sha256": "e" * 64, "exports_size": 42,
        "runtime_path": str(tmp_path / "runtime.a"), "runtime_sha256": "f" * 64,
        "worker_schema": "pcc.frontends.python.codegen_worker.v5",
        "ast_schema": "pcc.frontends.python.py_ast.v1", "export_reader_by_chunk": ["full_graph"],
    }
    graph_digest = checkpoint.prepare_graph(str(root), build, graph)
    original = tmp_path / "new.direct.pco"
    original.write_bytes(_object(stackmap=True))
    record = {
        "needs_libpython": False, "needs_native_extension_exports": True,
        "ir_bytes_before_passes": 314, "ir_text": "", "artifact_kind": "PCO",
        "artifact_path": str(original), "parse_ms": 12, "infer_ms": 34, "codegen_ms": 56,
    }
    return root, build, graph_digest, graph, record, artifacts


def _publish(store, index=0):
    root, build, graph_digest, graph, record, _artifacts = store
    return checkpoint.publish_module(str(root), build, graph_digest, index,
                                     graph["module_names"][index], [0, 1], record)


def _load(store, index=0):
    root, build, graph_digest, graph, _record, artifacts = store
    return checkpoint.load_module(str(root), build, graph_digest, index,
                                  graph["module_names"][index], [0, 1], str(artifacts))


def _receipt(store, index=0):
    path = store[0] / "modules" / (str(index) + ".json")
    return path, json.loads(path.read_text())


def _rewrite_receipt(path, receipt):
    receipt.pop("receipt_sha256", None)
    receipt["receipt_sha256"] = checkpoint.canonical_digest(receipt)
    _json(path, receipt)


@pytest.mark.parametrize("payload", [
    {"z": [True, False, None, 0, -19], "a": "é \U0001f680 \u007f \ud800"},
    {"control": "".join(chr(index) for index in range(128))},
    {"floats": [0.0, -0.0, 1e-7, 1e30, 1.1234567890123457]},
])
def test_canonical_digest_matches_launcher_exactly(payload):
    expected = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("ascii")
    assert checkpoint.canonical_digest(payload) == hashlib.sha256(expected).hexdigest()


@pytest.mark.parametrize("payload", [float("nan"), float("inf"), float("-inf"), {1: "key"}, (1, 2)])
def test_canonical_digest_rejects_unsupported_json(payload):
    with pytest.raises(checkpoint.CheckpointError):
        checkpoint.canonical_digest(payload)


def test_full_record_restores_with_independent_mutable_copy(store):
    root, build, graph_digest, graph, record, _artifacts = store
    assert _load(store) is None
    assert _publish(store) == record
    restored = _load(store)
    assert restored is not None
    assert {key: value for key, value in restored.items() if key != "artifact_path"} == {
        key: value for key, value in record.items() if key != "artifact_path"
    }
    receipt_path, receipt = _receipt(store)
    durable = root / receipt["record"]["artifact_path"]
    with open(restored["artifact_path"], "rb") as stream:
        assert durable.read_bytes() == stream.read()
    assert os.stat(durable).st_ino != os.stat(restored["artifact_path"]).st_ino
    with open(restored["artifact_path"], "wb") as stream:
        stream.write(b"caller may overwrite its independent copy")
    assert _load(store) is not None
    assert checkpoint.load_graph(str(root), build, graph_digest) == graph
    assert receipt_path.exists()


@pytest.mark.parametrize("key,value", [
    ("stage", 2), ("stage", True), ("owner", "pcc1"), ("producer_role", "native-pcc1"),
    ("backend", "llvm"), ("target", "aarch64-unknown-linux-gnu"),
    ("target", "x86_64-apple-darwin"),
])
def test_build_owner_and_target_fail_closed(store, key, value):
    root, _build, _graph_digest, graph, _record, _artifacts = store
    envelope = json.loads((root / "build.json").read_text())
    envelope["payload"][key] = value
    envelope["identity_sha256"] = checkpoint.canonical_digest(envelope["payload"])
    _json(root / "build.json", envelope)
    with pytest.raises(checkpoint.CheckpointError):
        checkpoint.prepare_graph(str(root), envelope["identity_sha256"], graph)


def test_build_same_path_changed_payload_and_stage_env_rejected(store, monkeypatch):
    root, build, graph_digest, _graph, _record, artifacts = store
    _publish(store)
    envelope = json.loads((root / "build.json").read_text())
    envelope["payload"]["compiler_sources"][0]["sha256"] = "b" * 64
    _json(root / "build.json", envelope)
    with pytest.raises(checkpoint.CheckpointError, match="build identity"):
        _load(store)
    monkeypatch.setenv("PCC_STAGE1_CHECKPOINT_STAGE", "2")
    with pytest.raises(checkpoint.CheckpointError, match="Stage1"):
        checkpoint.load_module(str(root), build, graph_digest, 0, "main", [0, 1], str(artifacts))


@pytest.mark.parametrize("change", ["source", "ast", "passes", "exports", "runtime", "entry", "init", "chunks"])
def test_changed_full_graph_cannot_reuse_modules(store, change):
    root, build, _graph_digest, original, _record, _artifacts = store
    graph = json.loads(json.dumps(original))
    if change in ("source", "ast"):
        graph["sources"][0][change + "_sha256"] = "b" * 64
    elif change == "passes":
        graph["sources"][0]["passes"].reverse()
    elif change in ("exports", "runtime"):
        graph[change + "_sha256"] = "b" * 64
    elif change == "entry":
        graph["entry_module"] = "sibling"
    elif change == "init":
        graph["sibling_inits"] = ["main", "sibling"]
    else:
        graph["original_chunks"] = [[0], [1]]
        graph["export_reader_by_chunk"] = ["module_indexed", "module_indexed"]
    with pytest.raises(checkpoint.CheckpointError, match="graph identity"):
        checkpoint.prepare_graph(str(root), build, graph)


@pytest.mark.parametrize("chunks", [[[0, 0]], [[0]], [[0, 2]], [[False, 1]], [[]]])
def test_graph_assignments_require_every_original_index_once(store, chunks):
    root, build, _graph_digest, graph, _record, _artifacts = store
    graph["original_chunks"] = chunks
    with pytest.raises(checkpoint.CheckpointError):
        checkpoint.prepare_graph(str(root), build, graph)


@pytest.mark.parametrize("indices,name,index", [([0], "main", 0), ([1, 0], "main", 0),
                                              ([0, 1], "wrong", 0), ([0, 1], "main", True)])
def test_module_cannot_change_name_index_or_shrink_assignment(store, indices, name, index):
    root, build, graph_digest, _graph, _record, artifacts = store
    _publish(store)
    with pytest.raises(checkpoint.CheckpointError, match="original assignment"):
        checkpoint.load_module(str(root), build, graph_digest, index, name, indices, str(artifacts))


@pytest.mark.parametrize("key,value", [
    ("schema", "legacy"), ("stage", 2), ("owner", "pcc1"), ("graph_digest", "0" * 64),
    ("build_digest", "0" * 64), ("module_name", "sibling"), ("index", 1),
    ("assigned_indices", [0]), ("object_format", "PCCNOBJ"), ("object_size", True),
    ("validation_schema", "unchecked"), ("producer_attempt_id", ""),
])
def test_valid_checksum_cannot_override_receipt_binding(store, key, value):
    _publish(store)
    path, receipt = _receipt(store)
    receipt[key] = value
    _rewrite_receipt(path, receipt)
    assert _load(store) is None


@pytest.mark.parametrize("key,value", [
    ("needs_libpython", 0), ("needs_native_extension_exports", "yes"), ("parse_ms", True),
    ("infer_ms", -1), ("codegen_ms", 0.1), ("ir_bytes_before_passes", -1),
    ("ir_text", None), ("artifact_kind", "ASM"), ("artifact_path", "../outside.pco"),
])
def test_strict_metadata_types_and_paths_are_required(store, key, value):
    _publish(store)
    path, receipt = _receipt(store)
    receipt["record"][key] = value
    _rewrite_receipt(path, receipt)
    assert _load(store) is None


@pytest.mark.parametrize("mutation", ["missing", "empty", "truncate", "append", "wrong_hash", "legacy"])
def test_missing_or_corrupt_objects_never_become_hits(store, mutation):
    _publish(store)
    _path, receipt = _receipt(store)
    object_path = store[0] / receipt["record"]["artifact_path"]
    if mutation == "missing":
        object_path.unlink()
    else:
        payload = object_path.read_bytes()
        changed = {"empty": b"", "truncate": payload[:64], "append": payload + b"x",
                   "wrong_hash": b"x" * len(payload), "legacy": b"PCCNOBJ\0"}[mutation]
        object_path.write_bytes(changed)
    assert _load(store) is None


@pytest.mark.parametrize("mutation", ["larger", "smaller"])
def test_wrong_sized_object_is_rejected_before_opening_payload(store, monkeypatch, mutation):
    _publish(store)
    _path, receipt = _receipt(store)
    object_path = store[0] / receipt["record"]["artifact_path"]
    payload = object_path.read_bytes()
    object_path.write_bytes(payload + b"unexpected" if mutation == "larger" else payload[:-1])
    real_open = open

    def checked_open(path, mode="r", **kwargs):
        if os.fspath(path) == str(object_path):
            pytest.fail("size-mismatched object payload was opened")
        return real_open(path, mode, **kwargs)

    monkeypatch.setattr(checkpoint, "open", checked_open, raising=False)
    assert _load(store) is None
    summary = checkpoint.summarize(str(store[0]), store[1])
    assert summary["unique_completed_modules"] == 0
    assert summary["rejected_modules"] == 1


def _corrupt_object(kind):
    payload = bytearray(_object(relocations=(kind == "relocation"), stackmap=(kind == "stackmap")))
    if kind == "machine":
        struct.pack_into("<H", payload, 18, 183)
    elif kind == "relocation":
        header = struct.unpack_from("<16sHHIQQQIHHHHHH", payload)
        for index in range(header[12]):
            section = struct.unpack_from("<IIQQQQIIQQ", payload, header[6] + index * 64)
            if section[1] == 4:
                struct.pack_into("<Q", payload, section[4], 1000000)
                break
    else:
        offset = payload.index(b"PCCSMAP1")
        payload[offset] = 0
    return bytes(payload)


@pytest.mark.parametrize("kind", ["machine", "relocation", "stackmap"])
def test_owned_elf_validation_checks_even_hash_consistent_bytes(store, kind):
    _publish(store)
    path, receipt = _receipt(store)
    payload = _corrupt_object(kind)
    digest = hashlib.sha256(payload).hexdigest()
    relative = "objects/" + digest + ".pco"
    (store[0] / relative).write_bytes(payload)
    receipt["record"]["artifact_path"] = relative
    receipt["object_sha256"] = digest
    receipt["object_size"] = len(payload)
    _rewrite_receipt(path, receipt)
    assert _load(store) is None
    with open(store[4]["artifact_path"], "wb") as stream:
        stream.write(payload)
    with pytest.raises(checkpoint.CheckpointError):
        _publish(store)


@pytest.mark.parametrize("kind", ["malformed", "duplicate", "unknown", "unsigned", "swapped"])
def test_receipts_require_complete_canonical_records(store, kind):
    _publish(store)
    path, receipt = _receipt(store)
    if kind == "malformed":
        path.write_text('{"schema":')
    elif kind == "duplicate":
        path.write_text(path.read_text().replace('"stage":1', '"stage":1,"stage":1'))
    elif kind == "unknown":
        receipt["unknown"] = True
        _rewrite_receipt(path, receipt)
    elif kind == "unsigned":
        receipt["record"]["codegen_ms"] += 1
        _json(path, receipt)
    else:
        _publish(store, index=1)
        path.write_bytes((store[0] / "modules" / "1.json").read_bytes())
    assert _load(store) is None


def test_object_filename_without_receipt_is_never_adopted(store):
    _publish(store)
    path, _receipt_data = _receipt(store)
    path.unlink()
    assert _load(store) is None
    summary = checkpoint.summarize(str(store[0]), store[1])
    assert summary["unique_completed_modules"] == 0
    assert summary["missing_modules"] == 2


@pytest.mark.parametrize("target", ["objects", "modules", "object", "receipt"])
def test_store_symlinks_cannot_escape_or_alias_mutable_files(store, tmp_path, target):
    _publish(store)
    root = store[0]
    path, receipt = _receipt(store)
    if target in ("objects", "modules"):
        original = root / target
        outside = tmp_path / (target + "-outside")
        original.rename(outside)
        original.symlink_to(outside, target_is_directory=True)
    else:
        original = root / receipt["record"]["artifact_path"] if target == "object" else path
        outside = tmp_path / (target + "-outside")
        original.rename(outside)
        original.symlink_to(outside)
    assert _load(store) is None


def test_materialized_path_cannot_alias_checkpoint_or_follow_symlink(store, tmp_path):
    root, build, graph_digest, _graph, _record, artifacts = store
    _publish(store)
    with pytest.raises(checkpoint.CheckpointError, match="separate attempt"):
        checkpoint.load_module(str(root), build, graph_digest, 0, "main", [0, 1], str(root))
    outside = tmp_path / "victim"
    outside.write_text("untouched")
    (artifacts / "module_0.direct.pco").symlink_to(outside)
    with pytest.raises(checkpoint.CheckpointError, match="symlinks"):
        _load(store)
    assert outside.read_text() == "untouched"


@pytest.mark.parametrize("boundary", ["object_fsync", "object_rename", "after_object_rename", "receipt_fsync",
                                     "receipt_rename", "after_receipt_rename"])
def test_atomic_failures_never_admit_partial_module(store, monkeypatch, boundary):
    root = store[0]
    # Isolate per-file barriers from the first-use directory barriers.
    (root / "objects").mkdir()
    (root / "modules").mkdir()
    real_sync, real_replace = os.fsync, os.replace
    files_synced = 0

    def sync(descriptor):
        nonlocal files_synced
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            files_synced += 1
            if boundary == "object_fsync" and files_synced == 1:
                raise OSError("injected object sync failure")
            if boundary == "receipt_fsync" and files_synced == 2:
                raise OSError("injected receipt sync failure")
        return real_sync(descriptor)

    def replace(source, destination):
        is_receipt = destination.endswith("/modules/0.json")
        if (boundary == "object_rename" and destination.endswith(".pco")) or (
                boundary == "receipt_rename" and is_receipt):
            raise OSError("injected rename failure")
        result = real_replace(source, destination)
        if boundary == "after_object_rename" and destination.endswith(".pco"):
            raise OSError("injected failure after object publication")
        if boundary == "after_receipt_rename" and is_receipt:
            raise OSError("injected failure after commit")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", sync)
        patch.setattr(os, "replace", replace)
        with pytest.raises(checkpoint.CheckpointError):
            _publish(store)
    restored = _load(store)
    assert (restored is not None) == (boundary == "after_receipt_rename")
    _publish(store)
    assert _load(store) is not None


@pytest.mark.parametrize("target", ["object", "receipt"])
@pytest.mark.parametrize("failure", ["short", "exception"])
def test_partial_writes_never_commit_metadata(store, monkeypatch, target, failure):
    real_open = open

    class PartialWriter:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def write(self, data):
            written = self.stream.write(data[:len(data) // 2])
            if failure == "exception":
                raise OSError("injected write interruption")
            return written

    def partial_open(path, mode="r", **kwargs):
        stream = real_open(path, mode, **kwargs)
        wanted = "/objects/" if target == "object" else "/modules/"
        if mode == "xb" and wanted in path:
            return PartialWriter(stream)
        return stream

    with monkeypatch.context() as patch:
        patch.setattr(checkpoint, "open", partial_open, raising=False)
        with pytest.raises(checkpoint.CheckpointError):
            _publish(store)
    assert _load(store) is None
    _publish(store)
    assert _load(store) is not None


@pytest.mark.parametrize("kind", ["build", "graph"])
def test_top_level_malformed_receipts_fail_even_before_a_module_lookup(store, kind):
    path = store[0] / (kind + ".json")
    path.write_text('{"schema":')
    with pytest.raises(checkpoint.CheckpointError):
        _load(store)


def test_duplicate_build_fields_and_object_fifos_are_rejected(store):
    _publish(store)
    path, receipt = _receipt(store)
    object_path = store[0] / receipt["record"]["artifact_path"]
    object_path.unlink()
    os.mkfifo(object_path)
    assert _load(store) is None
    build = store[0] / "build.json"
    build.write_text(build.read_text().replace('"stage":1', '"stage":2,"stage":1'))
    with pytest.raises(checkpoint.CheckpointError, match="duplicate"):
        _load(store)


def test_graph_publication_failure_is_retriable_without_partial_identity(store, monkeypatch):
    root, build, graph_digest, graph, _record, _artifacts = store
    (root / "graph.json").unlink()
    real_replace = os.replace

    def fail_graph(source, destination):
        if destination.endswith("/graph.json"):
            raise OSError("graph commit interrupted")
        return real_replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", fail_graph)
        with pytest.raises(checkpoint.CheckpointError):
            checkpoint.prepare_graph(str(root), build, graph)
    assert not (root / "graph.json").exists()
    assert checkpoint.prepare_graph(str(root), build, graph) == graph_digest


def test_summary_validates_bytes_and_partitions_durable_attempt_contributions(store, monkeypatch):
    _publish(store)
    monkeypatch.setenv("PCC_STAGE1_CHECKPOINT_ATTEMPT", "attempt-two")
    _publish(store, index=1)
    summary = checkpoint.summarize(str(store[0]), store[1], "attempt-two")
    assert summary["module_count"] == summary["unique_completed_modules"] == 2
    assert summary["new_durable_modules_this_attempt"] == summary["prior_durable_modules"] == 1
    assert [row["index"] for row in summary["modules"]] == [0, 1]
    assert all("ir_text" not in row for row in summary["modules"])
    path, receipt = _receipt(store, 1)
    receipt["record"]["codegen_ms"] = -1
    _rewrite_receipt(path, receipt)
    summary = checkpoint.summarize(str(store[0]), store[1], "attempt-two")
    assert summary["unique_completed_modules"] == 1
    assert summary["rejected_modules"] == 1
    assert summary["new_durable_modules_this_attempt"] == 0


def test_publication_requires_nonempty_attempt(store, monkeypatch):
    monkeypatch.delenv("PCC_STAGE1_CHECKPOINT_ATTEMPT")
    with pytest.raises(checkpoint.CheckpointError, match="attempt ID"):
        _publish(store)


def test_worker_releases_frontend_object_and_ir_buffers_before_checkpoint_read(
    store, monkeypatch,
):
    """Run the real two-module worker loop with small tracked emitter buffers."""
    import weakref

    from pcc.backend import self_backend_parse, target_objects
    from pcc.frontends.python import (
        compiled_owned_passes,
        pipeline_frontend_worker_execution as execution,
        type_infer,
    )
    from pcc.frontends.python.codegen import layer1
    from pcc.ir import direct_indexed_kernel

    root, build, graph_digest, graph, _record, artifacts = store
    references = {}
    events = []
    errors = []
    real_publish = checkpoint.publish_module
    encoded_fixture = _object(stackmap=True)

    class Node:
        def __init__(self, index, kind):
            self.index = index
            references[index, kind] = weakref.ref(self)

        def __str__(self):
            return "module:" + str(self.index)

    class Codegen:
        def __init__(self, typed, *_args):
            index = typed.index
            references[index, "codegen"] = weakref.ref(self)
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
            return self.module

    class EmittedBytes(bytes):
        def __new__(cls, data, index):
            result = super().__new__(cls, data)
            result.index = index
            events.append((index, "encoded created"))
            return result

        def __del__(self):
            events.append((self.index, "encoded released"))

    class RetainedIR(str):
        def __new__(cls, index):
            result = super().__new__(cls, "passed:" + str(index))
            result.index = index
            events.append((index, "IR created"))
            return result

        def __del__(self):
            events.append((self.index, "IR released"))

    def read_ast(path):
        index = int(os.path.basename(path).split("_")[1].split(".")[0])
        return Node(index, "AST")

    def infer(ast, **_kwargs):
        typed = Node(ast.index, "typed")
        typed.ast = ast
        return typed

    def parse_direct(text):
        result = Node(int(text.split(":")[1]), "direct")
        result.triple = graph["target"]
        return result

    def assert_frontend_released(index):
        for kind in ("AST", "typed", "codegen", "generated", "capture", "direct"):
            assert references[index, kind]() is None, (index, kind)

    def encode(assembly, _target, **_kwargs):
        index = int(assembly.split(":")[1])
        # The worker's actual release path, including cycle collection, ran
        # before the encoder was invoked. No fixture collection masks a leak.
        assert_frontend_released(index)
        return EmittedBytes(encoded_fixture, index)

    def publish(*args):
        index, name, assigned, record = args[3:]
        assert name == graph["module_names"][index]
        assert assigned == [0, 1]
        assert_frontend_released(index)
        assert (index, "encoded released") in events
        assert (index, "IR released") in events
        assert record["ir_text"] == ""
        assert record["ir_bytes_before_passes"] == len("passed:" + str(index))
        events.append((index, "checkpoint reread"))
        # Exercise the real reread, owned ELF validation and durable receipt
        # publication only after the producer buffer lifetimes are checked.
        return real_publish(*args)

    manifest = {
        "result_path": str(artifacts / "result.tsv"), "job_kind": "codegen",
        "src_paths": [source["source_path"] for source in graph["sources"]],
        "module_names": graph["module_names"], "entry_module": graph["entry_module"],
        "sibling_inits": graph["sibling_inits"], "libpython_mode": "off",
        "ir_scaffold_mode": graph["ir_scaffold_mode"], "verbose": False,
        "assigned_indices": [0, 1], "ir_dir": str(artifacts),
        "exports_path": str(artifacts / "exports.json"), "ast_dir": str(artifacts / "ast"),
        "checkpoint_root": str(root), "checkpoint_build_digest": build,
        "checkpoint_graph_digest": graph_digest, "checkpoint_skip_indices": [],
    }
    for name in ("PCC_DIRECT_INDEXED_KERNEL_EMIT", "PCC_DIRECT_INDEXED_KERNEL_CAPTURE",
                 "PCC_DIRECT_INDEXED_NATIVE_OBJECT", "PCC_DIRECT_INDEXED_KERNEL_RELEASE_FRONTEND",
                 "PCC_DIRECT_INDEXED_KERNEL_REQUIRE_ZERO_FALLBACK"):
        monkeypatch.setenv(name, "1")
    for name in ("PCC_DIRECT_INDEXED_KERNEL_VALIDATE", "PCC_TEXT_INDEXED_KERNEL_EMIT",
                 "PCC_DIRECT_INDEXED_SIDECAR"):
        monkeypatch.setenv(name, "0")
    monkeypatch.setattr(execution, "_validate_stage1_checkpoint_worker", lambda *_args: graph)
    monkeypatch.setattr(execution, "_validate_stage1_checkpoint_module_context", lambda *_args: None)
    # Freezing all host pytest objects is process-wide and is unrelated to the
    # per-module ownership under test. Keep actual release and gc.collect calls.
    monkeypatch.setattr(execution, "_freeze_worker_survivors", lambda: None)
    monkeypatch.setattr(execution, "_direct_owned_pass_names", lambda _name: ["mem2reg", "simplify"])
    monkeypatch.setattr(type_infer, "infer_module", infer)
    monkeypatch.setattr(layer1, "L1CodeGen", Codegen)
    monkeypatch.setattr(compiled_owned_passes, "run_owned_passes",
                        lambda text, *_args: RetainedIR(int(text.split(":")[1])))
    monkeypatch.setattr(self_backend_parse, "parse_self_backend_module", parse_direct)
    monkeypatch.setattr(direct_indexed_kernel, "direct_indexed_module_first_libpython_edge", lambda _module: "")
    monkeypatch.setattr(target_objects, "emit_indexed_assembly",
                        lambda module, **_kwargs: "assembly:" + str(module.index))
    monkeypatch.setattr(target_objects, "encode_assembly_object", encode)
    monkeypatch.setattr(target_objects, "emit_x86_64_streamed_module",
                        lambda module, **_kwargs: _StreamedTextStub("assembly:" + str(module.index), encode))
    monkeypatch.setattr(checkpoint, "publish_module", publish)

    status = execution.run_codegen_worker(
        "tracked.manifest", read_manifest=lambda _path: manifest,
        run_export_worker_callback=lambda *_args: pytest.fail("unexpected export worker"),
        run_summary_worker_callback=lambda *_args: pytest.fail("unexpected summary worker"),
        worker_timing_enabled=lambda: False, native_worker_executable=lambda: False,
        read_native_exports_wire=lambda _path: ({"main": {}, "sibling": {}}, {}),
        read_native_exports_wire_for_module=lambda *_args: pytest.fail("original chunk became singleton"),
        read_ast_wire=read_ast,
        build_closed_world_context=lambda *_args, **_kwargs: pytest.fail("unexpected graph rebuild"),
        module_imports_native_extension=lambda *_args, **_kwargs: True,
        contextual_host_params_for_module=lambda *_args: {},
        module_uses_default_native_exports=lambda _name: False,
        copy_native_module_exports=lambda value: value,
        closed_world_function_object_exports=lambda *_args: {},
        log=lambda *_args: None, ir_needs_libpython=lambda _text: False,
        safe_exception_text=str, write_worker_error=lambda _path, text: errors.append(text),
        pipeline_error=RuntimeError,
    )
    assert status == 0, errors
    assert errors == []
    for index in (0, 1):
        assert_frontend_released(index)
        assert events.index((index, "encoded released")) < events.index((index, "checkpoint reread"))
        assert events.index((index, "IR released")) < events.index((index, "checkpoint reread"))
        assert (artifacts / ("module_" + str(index) + ".direct.pco")).read_bytes() == encoded_fixture
    rows = (artifacts / "result.tsv").read_text().splitlines()
    assert [row.split("\t")[1:3] for row in rows] == [["0", "main"], ["1", "sibling"]]
    summary = checkpoint.summarize(str(root), build, "attempt-one")
    assert summary["unique_completed_modules"] == 2
    assert summary["new_durable_modules_this_attempt"] == 2
    print("Tracked lifetimes:", events)


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
