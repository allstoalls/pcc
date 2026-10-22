"""Strict, stage-local receipts for host Stage1 direct ELF objects.

The launcher owns immutable build inputs and the exclusive checkpoint lock.
The coordinator regenerates the entire graph before calling ``prepare_graph``.
Workers retain their original assignments. Only a committed module receipt can
admit an object; filenames and objects without receipts are never cache hits.
Native stages must reject checkpoint mode before calling this helper.
"""

from __future__ import annotations

import hashlib
import json
import os


BUILD_SCHEMA = "pcc.stage1-checkpoint.build.v1"
GRAPH_SCHEMA = "pcc.stage1-checkpoint.graph.v1"
MODULE_SCHEMA = "pcc.stage1-checkpoint.module.v1"
OBJECT_FORMAT = "ELF64-LE-ET_REL"
VALIDATION_SCHEMA = "pcc.stage1-checkpoint.elf-x86-64.v1"
_RECORD_FIELDS = (
    "needs_libpython", "needs_native_extension_exports", "ir_bytes_before_passes",
    "ir_text", "artifact_kind", "artifact_path", "parse_ms", "infer_ms", "codegen_ms",
)
_MODULE_FIELDS = (
    "schema", "stage", "producer_role", "owner", "backend", "build_digest",
    "graph_digest", "index", "module_name", "assigned_indices",
    "producer_attempt_id", "record", "object_format", "object_sha256",
    "object_size", "validation_schema", "receipt_sha256",
)
_GRAPH_FIELDS = (
    "stage", "producer_role", "owner", "backend", "target", "module_names",
    "original_chunks", "sources", "entry_module", "sibling_inits",
    "libpython_mode", "ir_scaffold_mode", "worker_prefix", "jobs",
    "exports_sha256", "exports_size", "runtime_path", "runtime_sha256",
    "worker_schema", "ast_schema", "export_reader_by_chunk",
)
_TEMP_SEQUENCE = 0


class CheckpointError(Exception):
    """The checkpoint cannot safely provide or persist the requested state."""


def _hex4(value: int) -> str:
    digits = "0123456789abcdef"
    return (digits[(value >> 12) & 15] + digits[(value >> 8) & 15]
            + digits[(value >> 4) & 15] + digits[value & 15])


def _quote(value: str) -> str:
    # Match JSON ensure_ascii=True without expanding the native JSON keyword
    # surface. Native checkpoint use is forbidden, but this source is compiled.
    parts = ['"']
    escapes = {8: "\\b", 9: "\\t", 10: "\\n", 12: "\\f", 13: "\\r",
               34: '\\"', 92: "\\\\"}
    for character in value:
        code = ord(character)
        if code in escapes:
            parts.append(escapes[code])
        elif code < 32 or code >= 127:
            if code > 65535:
                code -= 65536
                parts.append("\\u" + _hex4(55296 + (code >> 10)))
                parts.append("\\u" + _hex4(56320 + (code & 1023)))
            else:
                parts.append("\\u" + _hex4(code))
        else:
            parts.append(character)
    parts.append('"')
    return "".join(parts)


def _canonical_json(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _quote(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        text = repr(value)
        if text in ("nan", "inf", "-inf"):
            raise CheckpointError("checkpoint JSON requires finite numbers")
        return text
    if isinstance(value, list):
        return "[" + ",".join([_canonical_json(item) for item in value]) + "]"
    if isinstance(value, dict):
        for key in value:
            if not isinstance(key, str):
                raise CheckpointError("checkpoint JSON object keys must be strings")
        return "{" + ",".join([
            _quote(key) + ":" + _canonical_json(value[key]) for key in sorted(value)
        ]) + "}"
    raise CheckpointError("unsupported checkpoint JSON value")


def canonical_digest(payload) -> str:
    """SHA-256 of sorted, compact, ASCII JSON with preserved array order."""
    return hashlib.sha256(_canonical_json(payload).encode("ascii")).hexdigest()


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            data = stream.read(1024 * 1024)
            if not data:
                break
            digest.update(data)
    return digest.hexdigest()


def _digest(value) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(character in "0123456789abcdef" for character in value))


def _integer(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _name(value) -> bool:
    return (isinstance(value, str) and bool(value)
            and not any(character in value for character in "\0\t\r\n"))


def _fields(value, fields, label: str) -> None:
    if not isinstance(value, dict) or sorted(value) != sorted(fields):
        raise CheckpointError("invalid " + label + " field schema")


def _root(root: str) -> str:
    if not isinstance(root, str) or not root:
        raise CheckpointError("checkpoint root is required")
    resolved = os.path.realpath(os.path.abspath(root))
    if not os.path.isdir(resolved):
        raise CheckpointError("checkpoint root does not exist")
    return resolved


def _path(root: str, relative: str) -> str:
    # Internal paths are fixed or content-addressed, never adopted from an
    # arbitrary persisted filename. Reject symlinks even within the store.
    if (not isinstance(relative, str) or not relative or os.path.isabs(relative)
            or "\\" in relative or "\0" in relative):
        raise CheckpointError("invalid checkpoint relative path")
    current = root
    for part in relative.split("/"):
        if part in ("", ".", ".."):
            raise CheckpointError("checkpoint path escapes its root")
        current = os.path.join(current, part)
        if os.path.islink(current):
            raise CheckpointError("checkpoint paths must not be symlinks")
    if not os.path.realpath(current).startswith(root + os.sep):
        raise CheckpointError("checkpoint path escapes its root")
    return current


def _sync_directory(path: str) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _directory(root: str, relative: str) -> str:
    path = _path(root, relative)
    if not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)
        _sync_directory(root)
    return path


def _atomic_bytes(root: str, relative: str, data: bytes) -> None:
    global _TEMP_SEQUENCE
    destination = _path(root, relative)
    _TEMP_SEQUENCE += 1
    temporary_relative = relative + ".tmp." + str(os.getpid()) + "." + str(_TEMP_SEQUENCE)
    temporary = _path(root, temporary_relative)
    created = False
    try:
        # Exclusive creation prevents following an abandoned temporary link.
        with open(temporary, "xb") as stream:
            created = True
            written = stream.write(data)
            if written != len(data):
                raise CheckpointError("short checkpoint write")
            stream.flush()
            os.fsync(stream.fileno())
        if file_sha256(temporary) != hashlib.sha256(data).hexdigest():
            raise CheckpointError("checkpoint temporary bytes changed")
        _path(root, relative)
        os.replace(temporary, destination)
        _sync_directory(os.path.dirname(destination))
    except OSError as exc:
        raise CheckpointError("checkpoint publication failed: " + str(exc)) from exc
    finally:
        if created and os.path.exists(temporary):
            os.unlink(temporary)


def _write_json(root: str, relative: str, value) -> None:
    _atomic_bytes(root, relative, (_canonical_json(value) + "\n").encode("ascii"))


def _read_json(root: str, relative: str):
    try:
        path = _path(root, relative)
        if not os.path.isfile(path):
            raise CheckpointError("checkpoint receipt is not a regular file: " + relative)
        with open(path, "r", encoding="utf-8") as stream:
            text = stream.read()
        value = json.loads(text)
        if text != _canonical_json(value) + "\n":
            raise CheckpointError("noncanonical or duplicate checkpoint JSON fields")
        return value
    except (OSError, ValueError, TypeError) as exc:
        raise CheckpointError("cannot read checkpoint " + relative + ": " + str(exc)) from exc


def _build(root: str, build_digest: str):
    if not _digest(build_digest):
        raise CheckpointError("invalid checkpoint build digest")
    if str(os.environ.get("PCC_STAGE1_CHECKPOINT_STAGE", "1")) != "1":
        raise CheckpointError("checkpoint is restricted to Stage1")
    envelope = _read_json(root, "build.json")
    _fields(envelope, ("schema", "identity_sha256", "payload"), "build envelope")
    payload = envelope["payload"]
    if (envelope["schema"] != BUILD_SCHEMA or not isinstance(payload, dict)
            or envelope["identity_sha256"] != build_digest
            or canonical_digest(payload) != build_digest):
        raise CheckpointError("checkpoint build identity mismatch")
    if (not _integer(payload.get("stage")) or payload.get("stage") != 1
            or payload.get("producer_role") != "host-pcc0"
            or payload.get("owner") != "cpython" or payload.get("backend") != "self"):
        raise CheckpointError("checkpoint requires host CPython Stage1 and self backend")
    target = payload.get("target")
    if not isinstance(target, str) or not target.startswith("x86_64-") or "-linux-" not in target:
        raise CheckpointError("checkpoint requires an x86_64 Linux target")
    return payload


def _graph_payload(payload) -> None:
    _fields(payload, _GRAPH_FIELDS, "graph payload")
    if (not _integer(payload["stage"]) or payload["stage"] != 1
            or payload["producer_role"] != "host-pcc0" or payload["owner"] != "cpython"
            or payload["backend"] != "self" or payload["libpython_mode"] != "off"
            or not isinstance(payload["target"], str)
            or not payload["target"].startswith("x86_64-")
            or "-linux-" not in payload["target"]):
        raise CheckpointError("graph requires host Stage1 self no-libpython x86_64 Linux")
    names = payload.get("module_names")
    chunks = payload.get("original_chunks")
    if not isinstance(names, list) or not names or not isinstance(chunks, list) or not chunks:
        raise CheckpointError("graph requires full module names and original chunks")
    seen_names = {}
    for name in names:
        if not _name(name) or name in seen_names:
            raise CheckpointError("invalid or duplicate graph module name")
        seen_names[name] = True
    assigned = {}
    for chunk in chunks:
        if not isinstance(chunk, list) or not chunk:
            raise CheckpointError("graph has an empty or invalid original chunk")
        for index in chunk:
            if not _integer(index) or index >= len(names) or index in assigned:
                raise CheckpointError("graph has duplicate or out-of-range module indices")
            assigned[index] = True
    if len(assigned) != len(names):
        raise CheckpointError("graph assignments do not cover every module")
    sources = payload["sources"]
    if not isinstance(sources, list) or len(sources) != len(names):
        raise CheckpointError("graph requires every source and AST identity")
    for index, source in enumerate(sources):
        _fields(source, ("index", "module_name", "source_path", "source_sha256",
                         "ast_sha256", "ast_size", "passes"), "graph source")
        if (not _integer(source["index"]) or source["index"] != index
                or source["module_name"] != names[index]
                or not _name(source["source_path"]) or not os.path.isabs(source["source_path"])
                or not _digest(source["source_sha256"]) or not _digest(source["ast_sha256"])
                or not _integer(source["ast_size"]) or source["ast_size"] == 0
                or not isinstance(source["passes"], list)
                or any(not _name(item) for item in source["passes"])):
            raise CheckpointError("invalid graph source identity")
    readers = ["module_indexed" if len(chunk) == 1 else "full_graph" for chunk in chunks]
    if (payload["export_reader_by_chunk"] != readers
            or payload["worker_schema"] != "pcc.frontends.python.codegen_worker.v5"
            or payload["ast_schema"] != "pcc.frontends.python.py_ast.v1"
            or not _digest(payload["exports_sha256"]) or not _digest(payload["runtime_sha256"])
            or not _integer(payload["exports_size"]) or payload["exports_size"] == 0
            or not _name(payload["runtime_path"]) or not os.path.isabs(payload["runtime_path"])
            or not _integer(payload["jobs"]) or payload["jobs"] == 0
            or not isinstance(payload["worker_prefix"], list) or not payload["worker_prefix"]
            or any(not _name(item) for item in payload["worker_prefix"])
            or not isinstance(payload["sibling_inits"], list)
            or not isinstance(payload["entry_module"], str)
            or not isinstance(payload["ir_scaffold_mode"], str)):
        raise CheckpointError("invalid graph execution context")


def _graph(root: str, build_digest: str, graph_digest: str = ""):
    envelope = _read_json(root, "graph.json")
    _fields(envelope, ("schema", "build_digest", "identity_sha256", "payload"), "graph envelope")
    payload = envelope["payload"]
    _graph_payload(payload)
    actual = canonical_digest(payload)
    if (envelope["schema"] != GRAPH_SCHEMA or envelope["build_digest"] != build_digest
            or envelope["identity_sha256"] != actual
            or (graph_digest and graph_digest != actual)):
        raise CheckpointError("checkpoint graph identity mismatch")
    return envelope


def prepare_graph(root: str, build_digest: str, graph_payload) -> str:
    """Commit or compare the full regenerated graph before admitting modules."""
    root = _root(root)
    build = _build(root, build_digest)
    _graph_payload(graph_payload)
    if graph_payload["target"] != build["target"]:
        raise CheckpointError("checkpoint graph target differs from build")
    digest = canonical_digest(graph_payload)
    path = _path(root, "graph.json")
    if os.path.exists(path):
        _graph(root, build_digest, digest)
    else:
        _write_json(root, "graph.json", {
            "schema": GRAPH_SCHEMA, "build_digest": build_digest,
            "identity_sha256": digest, "payload": graph_payload,
        })
    return digest


def load_graph(root: str, build_digest: str, graph_digest: str):
    """Return the validated full graph for current-input checks by the worker."""
    root = _root(root)
    build = _build(root, build_digest)
    graph = _graph(root, build_digest, graph_digest)
    if graph["payload"]["target"] != build["target"]:
        raise CheckpointError("checkpoint graph target differs from build")
    return graph["payload"]


def _binding(payload, index: int, module_name: str, assigned_indices) -> None:
    if (not _integer(index) or index >= len(payload["module_names"])
            or payload["module_names"][index] != module_name
            or not isinstance(assigned_indices, list)
            or any(not _integer(item) for item in assigned_indices)
            or index not in assigned_indices
            or assigned_indices not in payload["original_chunks"]):
        raise CheckpointError("module does not match its full graph and original assignment")


def _record(record) -> None:
    _fields(record, _RECORD_FIELDS, "module result")
    for key in ("needs_libpython", "needs_native_extension_exports"):
        if not isinstance(record[key], bool):
            raise CheckpointError("module result requires boolean " + key)
    for key in ("ir_bytes_before_passes", "parse_ms", "infer_ms", "codegen_ms"):
        if not _integer(record[key]):
            raise CheckpointError("module result requires nonnegative integer " + key)
    if (record["artifact_kind"] != "PCO" or not _name(record["artifact_path"])
            or not isinstance(record["ir_text"], str)):
        raise CheckpointError("module result requires a direct PCO artifact and IR text")


def _validate_object(data: bytes) -> None:
    from pcc.backend.elf_x86_64 import ElfError, parse_relocatable

    try:
        obj = parse_relocatable(data, compact_relocations=True)
        if obj.machine != 62:
            raise CheckpointError("checkpoint object requires ELF machine 62")
    except ElfError as exc:
        raise CheckpointError("invalid checkpoint ELF object: " + str(exc)) from exc


def _module(root: str, build_digest: str, graph_digest: str, index: int,
            module_name: str, assigned_indices):
    receipt = _read_json(root, "modules/" + str(index) + ".json")
    _fields(receipt, _MODULE_FIELDS, "module receipt")
    check = dict(receipt)
    del check["receipt_sha256"]
    if not _digest(receipt["receipt_sha256"]) or canonical_digest(check) != receipt["receipt_sha256"]:
        raise CheckpointError("module receipt integrity mismatch")
    if (receipt["schema"] != MODULE_SCHEMA
            or not _integer(receipt["stage"]) or receipt["stage"] != 1
            or receipt["producer_role"] != "host-pcc0" or receipt["owner"] != "cpython"
            or receipt["backend"] != "self" or receipt["build_digest"] != build_digest
            or receipt["graph_digest"] != graph_digest or not _integer(receipt["index"])
            or receipt["index"] != index or receipt["module_name"] != module_name
            or receipt["assigned_indices"] != assigned_indices
            or any(not _integer(item) for item in receipt["assigned_indices"])
            or not _name(receipt["producer_attempt_id"])):
        raise CheckpointError("module receipt producer or graph binding mismatch")
    _record(receipt["record"])
    digest = receipt["object_sha256"]
    if (receipt["object_format"] != OBJECT_FORMAT or not _digest(digest)
            or not _integer(receipt["object_size"]) or receipt["object_size"] == 0
            or receipt["validation_schema"] != VALIDATION_SCHEMA
            or receipt["record"]["artifact_path"] != "objects/" + digest + ".pco"):
        raise CheckpointError("invalid module object metadata")
    object_path = _path(root, receipt["record"]["artifact_path"])
    if not os.path.isfile(object_path):
        raise CheckpointError("checkpoint object is not a regular file")
    if os.path.getsize(object_path) != receipt["object_size"]:
        raise CheckpointError("module object size mismatch before read")
    with open(object_path, "rb") as stream:
        data = stream.read()
    if len(data) != receipt["object_size"] or hashlib.sha256(data).hexdigest() != digest:
        raise CheckpointError("module object size or digest mismatch")
    _validate_object(data)
    return receipt, data


def load_module(root: str, build_digest: str, graph_digest: str, index: int,
                module_name: str, assigned_indices, artifact_dir: str):
    """Return complete restored metadata and an independent object copy, or miss."""
    if not _digest(graph_digest):
        raise CheckpointError("invalid checkpoint graph digest")
    root = _root(root)
    _build(root, build_digest)
    graph = _graph(root, build_digest, graph_digest)
    _binding(graph["payload"], index, module_name, assigned_indices)
    try:
        receipt, data = _module(root, build_digest, graph_digest, index, module_name, assigned_indices)
    except (CheckpointError, OSError, ValueError, TypeError):
        return None
    # Materialization failures are build failures, not successful hits or
    # permission to mutate the durable object through a hardlink.
    artifacts = _root(artifact_dir)
    if artifacts == root or artifacts.startswith(root + os.sep):
        raise CheckpointError("restored artifacts require a separate attempt directory")
    relative = "module_" + str(index) + ".direct.pco"
    _atomic_bytes(artifacts, relative, data)
    record = dict(receipt["record"])
    record["artifact_path"] = _path(artifacts, relative)
    if file_sha256(record["artifact_path"]) != receipt["object_sha256"]:
        raise CheckpointError("restored checkpoint object changed during materialization")
    return record


def publish_module(root: str, build_digest: str, graph_digest: str, index: int,
                   module_name: str, assigned_indices, record):
    """Fsync validated bytes, then atomically commit their complete metadata."""
    if not _digest(graph_digest):
        raise CheckpointError("invalid checkpoint graph digest")
    root = _root(root)
    _build(root, build_digest)
    graph = _graph(root, build_digest, graph_digest)
    _binding(graph["payload"], index, module_name, assigned_indices)
    _record(record)
    attempt = os.environ.get("PCC_STAGE1_CHECKPOINT_ATTEMPT", "")
    if not _name(attempt):
        raise CheckpointError("checkpoint publication requires a producer attempt ID")
    try:
        if not os.path.isfile(record["artifact_path"]):
            raise CheckpointError("produced checkpoint object is not a regular file")
        with open(record["artifact_path"], "rb") as stream:
            data = stream.read()
        _validate_object(data)
        digest = hashlib.sha256(data).hexdigest()
        relative = "objects/" + digest + ".pco"
        _directory(root, "objects")
        _directory(root, "modules")
        _atomic_bytes(root, relative, data)
        persisted = dict(record)
        persisted["artifact_path"] = relative
        receipt = {
            "schema": MODULE_SCHEMA, "stage": 1, "producer_role": "host-pcc0",
            "owner": "cpython", "backend": "self", "build_digest": build_digest,
            "graph_digest": graph_digest, "index": index, "module_name": module_name,
            "assigned_indices": list(assigned_indices), "producer_attempt_id": attempt,
            "record": persisted, "object_format": OBJECT_FORMAT,
            "object_sha256": digest, "object_size": len(data),
            "validation_schema": VALIDATION_SCHEMA,
        }
        receipt["receipt_sha256"] = canonical_digest(receipt)
        _write_json(root, "modules/" + str(index) + ".json", receipt)
    except OSError as exc:
        raise CheckpointError("checkpoint publication failed: " + str(exc)) from exc
    return dict(record)


def summarize(root: str, build_digest: str, attempt_id: str = ""):
    """Count only validated durable modules; never infer completion from names.

    Attempt counts partition durable contributions by their producer attempt.
    They do not count failed/repeated compilation work or prove final linking.
    Prior durable modules are not called reused: a failed attempt may never
    reach them. Actual reuse belongs to the coordinator's execution counters.
    """
    root = _root(root)
    _build(root, build_digest)
    result = {
        "graph_digest": "", "module_count": 0, "unique_completed_modules": 0,
        "new_durable_modules_this_attempt": 0, "prior_durable_modules": 0,
        "missing_modules": 0, "rejected_modules": 0, "modules": [],
    }
    if not os.path.exists(_path(root, "graph.json")):
        return result
    graph = _graph(root, build_digest)
    graph_digest = graph["identity_sha256"]
    payload = graph["payload"]
    result["graph_digest"] = graph_digest
    result["module_count"] = len(payload["module_names"])
    assignments = {}
    for chunk in payload["original_chunks"]:
        for index in chunk:
            assignments[index] = chunk
    for index, name in enumerate(payload["module_names"]):
        try:
            path = _path(root, "modules/" + str(index) + ".json")
            if not os.path.exists(path):
                result["missing_modules"] += 1
                continue
            receipt, data = _module(root, build_digest, graph_digest, index, name, assignments[index])
            del data
        except (CheckpointError, OSError, ValueError, TypeError):
            result["rejected_modules"] += 1
            continue
        row = dict(receipt["record"])
        del row["ir_text"]
        del row["artifact_kind"]
        del row["artifact_path"]
        row.update({
            "index": index, "module_name": name,
            "object_path": receipt["record"]["artifact_path"],
            "object_sha256": receipt["object_sha256"], "object_size": receipt["object_size"],
            "producer_attempt_id": receipt["producer_attempt_id"],
        })
        result["modules"].append(row)
        result["unique_completed_modules"] += 1
        if attempt_id and receipt["producer_attempt_id"] == attempt_id:
            result["new_durable_modules_this_attempt"] += 1
        else:
            result["prior_durable_modules"] += 1
    return result
