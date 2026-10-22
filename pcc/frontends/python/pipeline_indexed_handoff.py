"""Host-only sealed PIDX handoff between retired frontend/backend workers.

This module imports only the standard library. It neither decodes PIDX nor
projects instructions. The existing codec owns the wire format; its fixed
arena order is mirrored here solely for a componentwise admission envelope.
Only the pool retires successful inputs, after the backend process exits.
"""

import hashlib
import json
import os


ENV_REQUEST = "PCC_INDEXED_HANDOFF_REQUEST"
ENV_SEAL = "PCC_INDEXED_HANDOFF_SEAL_SHA256"
RESOURCE_TOKEN_ENV = "PCC_WORKER_RESOURCE_TOKEN"
REQUEST_SCHEMA = "pcc.indexed-handoff.request.v1"
SEAL_SCHEMA = "pcc.indexed-handoff.seal.v1"
RECEIPT_SCHEMA = "pcc.indexed-handoff.receipt.v1"
CODEC_SCHEMA = "pcc.self-backend.indexed-module.v1"
_MAGIC = b"PCCIDXMOD1\n"
_MAX_JSON_BYTES = 65536
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_MAX_HEADER_BYTES = 512 * 1024 * 1024
_ARENA_FIELDS = (
    "call_scalars", "call_arg_scalars", "block_facts", "instruction_facts",
    "instruction_kind_ids", "instruction_metadata", "instruction_record_dest_ids",
    "instruction_record_scalars", "gep_index_scalars", "gep_scalars",
    "instruction_overflow_use_ids", "terminator_case_scalars", "terminator_scalars",
    "block_phi_facts", "phi_incoming_scalars", "phi_scalars", "error_edge_scalars",
    "error_edge_spans", "error_landing_scalars", "value_scalars",
    "definition_positions", "used_value_ids",
)
_REQUEST_KEYS = frozenset((
    "schema", "manifest_path", "manifest_sha256", "source_identity", "index",
    "module", "target", "passes", "sidecar_path", "output_path", "seal_path",
    "receipt_path",
))
_SEAL_KEYS = frozenset((
    "schema", "codec_schema", "request_sha256", "source_identity", "frontend_token",
    "index", "module", "target", "passes", "needs_libpython", "needs_native_exports",
    "shape", "pidx_sha256", "pidx_size",
))
_RECEIPT_KEYS = frozenset((
    "schema", "request_sha256", "seal_sha256", "pidx_sha256", "pidx_size",
    "backend_token", "output_sha256", "output_size",
))


class HandoffError(ValueError):
    """A private handoff does not match its coordinator-owned request."""


def _require(condition, message):
    if not condition:
        raise HandoffError("indexed handoff: " + message)


def _text(value, label, limit=4096):
    _require(type(value) is str and 0 < len(value) <= limit
             and not any(char in value for char in "\x00\r\n\t"), "invalid " + label)
    return value


def _digest(value, label):
    _require(type(value) is str and len(value) == 64
             and all(char in "0123456789abcdef" for char in value), "invalid " + label)
    return value


def _integer(value, label):
    _require(type(value) is int and 0 <= value <= (1 << 63) - 1, "invalid " + label)
    return value


def _path(value, label):
    _text(value, label)
    _require(os.path.isabs(value) and os.path.normpath(value) == value,
             "noncanonical " + label)
    # macOS commonly exposes private temporary roots through /var aliases.
    # Parent aliases are valid; artifact files themselves must not be links.
    _require(not os.path.islink(value), "symlink " + label)
    return value


def _exists(path):
    return os.path.exists(path) or os.path.islink(path)


def _regular(path):
    _require(not os.path.islink(path) and os.path.isfile(path),
             "not a regular file: " + path)


def _read_bytes(path, limit):
    _regular(path)
    with open(path, "rb") as stream:
        raw = stream.read(limit + 1)
    _require(len(raw) <= limit, "file exceeds size limit: " + path)
    return raw


def _hash_file(path):
    _regular(path)
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def _load_json(path, keys):
    raw = _read_bytes(path, _MAX_JSON_BYTES)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs)
    except (UnicodeError, ValueError) as exc:
        raise HandoffError("indexed handoff: invalid JSON") from exc
    _require(type(value) is dict and set(value) == keys, "JSON fields mismatch")
    return value, hashlib.sha256(raw).hexdigest()


def _atomic_json(path, value, exclusive=False):
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":"),
                      allow_nan=False) + "\n").encode("utf-8")
    _require(len(raw) <= _MAX_JSON_BYTES, "JSON exceeds size limit")
    temporary = path + ".tmp"
    _path(temporary, "JSON temporary path")
    if exclusive and _exists(path):
        raise FileExistsError(path)
    if not exclusive and _exists(temporary):
        # A retried phase is the sole writer after its predecessor has retired.
        _regular(temporary)
        os.unlink(temporary)
    created = False
    published = False
    try:
        # The fixed exclusive temporary serializes request publishers. A
        # stale temporary fails closed; a new request needs a fresh IR root.
        with open(temporary, "xb") as stream:
            created = True
            written = stream.write(raw)
            _require(written == len(raw), "short JSON write")
            stream.flush()
        if exclusive and _exists(path):
            raise FileExistsError(path)
        os.replace(temporary, path)
        published = True
    finally:
        # After replacement another caller may own the temporary name. Never
        # remove it once this caller has completed its atomic publication.
        if created and not published and _exists(temporary):
            os.unlink(temporary)


def _passes(value):
    _require(type(value) is list and len(value) <= 128, "invalid passes")
    for name in value:
        _text(name, "pass name", 128)
    return value


def _manifest(request):
    path = request["manifest_path"]
    raw = _read_bytes(path, _MAX_MANIFEST_BYTES)
    _require(hashlib.sha256(raw).hexdigest() == request["manifest_sha256"],
             "manifest digest mismatch")
    try:
        lines = raw.decode("utf-8").splitlines()
        _require(len(lines) >= 13
                 and lines[0] == "pcc.frontends.python.codegen_worker.v4",
                 "expected singleton v4 manifest")
        result_path = _path(lines[1], "result path")
        ir_dir = _path(lines[2], "IR directory")
        _require(lines[4] == "codegen" and lines[7] == "off",
                 "expected codegen/off manifest")
        siblings = int(lines[10])
        _require(siblings >= 0, "negative sibling count")
        position = 11 + siblings
        count = int(lines[position])
        position += 1
        _require(0 <= request["index"] < count and count <= len(lines),
                 "manifest index mismatch")
        for ordinal in range(count):
            parts = lines[position + ordinal].split("\t")
            _require(len(parts) == 3 and parts[0] == str(ordinal),
                     "manifest module row mismatch")
            if ordinal == request["index"]:
                _require(parts[1] == request["module"], "manifest module mismatch")
        position += count
        _require(lines[position:] == ["1", str(request["index"])],
                 "manifest assignment mismatch")
    except (UnicodeError, IndexError, ValueError) as exc:
        raise HandoffError("indexed handoff: invalid manifest") from exc
    _require(os.path.isdir(ir_dir), "IR directory missing")
    # Results are in the manifest's private coordinator directory. No paths
    # supplied by a seal or receipt are ever used for filesystem mutations.
    _require(os.path.dirname(result_path) == os.path.dirname(path),
             "result outside manifest directory")
    return ir_dir, result_path


def _request(request_path, value=None):
    _path(request_path, "request path")
    if value is None:
        value, digest = _load_json(request_path, _REQUEST_KEYS)
    else:
        _require(type(value) is dict and set(value) == _REQUEST_KEYS,
                 "request fields mismatch")
        digest = ""
    _require(value["schema"] == REQUEST_SCHEMA, "request schema mismatch")
    _digest(value["manifest_sha256"], "manifest digest")
    _integer(value["index"], "module index")
    _text(value["source_identity"], "source identity", 16384)
    _text(value["module"], "module name")
    _text(value["target"], "target", 256)
    _require(value["target"] != "unknown-unknown-unknown", "unresolved target")
    _passes(value["passes"])
    for name in ("manifest_path", "sidecar_path", "output_path", "seal_path", "receipt_path"):
        _path(value[name], name)
    ir_dir, result_path = _manifest(value)
    stem = os.path.join(ir_dir, "module_" + str(value["index"]) + ".direct")
    _require(value["sidecar_path"] == stem + ".pidx"
             and value["output_path"] == stem + ".pco"
             and value["seal_path"] == stem + ".pidx.seal.json"
             and value["receipt_path"] == stem + ".pco.receipt.json"
             and request_path == stem + ".pidx.request.json", "artifact path mismatch")
    for path in (request_path, value["sidecar_path"], value["output_path"],
                 value["seal_path"], value["receipt_path"]):
        _require(os.path.dirname(os.path.realpath(path)) == os.path.realpath(ir_dir),
                 "artifact outside owned IR directory")
    return value, digest, result_path


def write_request(request_path, fields):
    """Publish one immutable coordinator request in a fresh private IR root."""
    request, _digest_value, _result = _request(request_path, fields)
    _atomic_json(request_path, request, exclusive=True)


def _header_size(path):
    _regular(path)
    with open(path, "rb") as stream:
        _require(stream.read(len(_MAGIC)) == _MAGIC, "PIDX magic mismatch")
        line = stream.readline(32)
    _require(line.endswith(b"\n") and line[:-1].isdigit(), "PIDX header length invalid")
    size = int(line[:-1])
    _require(str(size).encode("ascii") + b"\n" == line
             and 0 < size <= _MAX_HEADER_BYTES, "PIDX header length invalid")
    return size, len(_MAGIC) + len(line)


def _shape(module, header_bytes):
    functions = module.functions
    shape = [header_bytes, len(functions), len(module.globals_)]
    for field in _ARENA_FIELDS:
        total, largest = 0, 0
        for function in functions:
            kernel = function.indexed_kernel
            _require(kernel is not None, "function has no published indexed kernel")
            count = len(getattr(kernel, field))
            total += count
            largest = max(largest, count)
        shape.extend((total, largest))
    _validate_shape(shape)
    return shape


def _validate_shape(shape):
    _require(type(shape) is list and len(shape) == 3 + 2 * len(_ARENA_FIELDS),
             "shape dimension mismatch")
    for value in shape:
        _integer(value, "shape component")
    _require(0 < shape[0] <= _MAX_HEADER_BYTES, "shape header invalid")
    for position in range(3, len(shape), 2):
        total, largest = shape[position:position + 2]
        _require(largest <= total <= largest * shape[1], "shape arena bounds mismatch")


def _verify_input(request, seal):
    digest, size = _hash_file(request["sidecar_path"])
    _require(digest == seal["pidx_sha256"] and size == seal["pidx_size"],
             "PIDX digest/size mismatch")
    header, prefix = _header_size(request["sidecar_path"])
    _require(header == seal["shape"][0]
             and prefix + header + 8 * sum(seal["shape"][3::2]) == size,
             "PIDX shape/size mismatch")


def _bound(request_path, verify_input=True):
    request, request_digest, result_path = _request(request_path)
    seal, seal_digest = _load_json(request["seal_path"], _SEAL_KEYS)
    _require(seal["schema"] == SEAL_SCHEMA and seal["codec_schema"] == CODEC_SCHEMA,
             "seal schema mismatch")
    _require(seal["request_sha256"] == request_digest
             and seal["source_identity"] == request["source_identity"],
             "seal request/source mismatch")
    for name in ("index", "module", "target", "passes"):
        _require(type(seal[name]) is type(request[name])
                 and seal[name] == request[name], "seal " + name + " mismatch")
    _digest(seal["frontend_token"], "frontend token")
    _digest(seal["pidx_sha256"], "PIDX digest")
    _integer(seal["pidx_size"], "PIDX size")
    _require(type(seal["needs_libpython"]) is bool and not seal["needs_libpython"]
             and type(seal["needs_native_exports"]) is bool, "seal flags mismatch")
    _validate_shape(seal["shape"])
    if verify_input:
        _verify_input(request, seal)
    return request, seal, request_digest, seal_digest, result_path


def publish_handoff(request_path, manifest_path, index, module, direct_module,
                    passes, needs_libpython, needs_native_exports, encode_callback):
    """Encode the retained direct module only after frontend owners are released."""
    request, request_digest, _result = _request(request_path)
    _require(manifest_path == request["manifest_path"]
             and type(index) is int and index == request["index"]
             and module == request["module"], "producer ownership mismatch")
    _require(direct_module.triple == request["target"]
             and type(passes) is list and passes == request["passes"],
             "producer target/passes mismatch")
    _require(type(needs_libpython) is bool and not needs_libpython
             and type(needs_native_exports) is bool, "producer flags mismatch")
    token = _digest(os.environ.get(RESOURCE_TOKEN_ENV, ""), "frontend token")
    sidecar = request["sidecar_path"]
    # A killed frontend may leave a partial payload. Reuse this one owned
    # filename on retry so pressure cancellation cannot accumulate PIDX files.
    temporary = sidecar + ".tmp"
    _path(temporary, "producer temporary path")
    if _exists(temporary):
        _regular(temporary)
        os.unlink(temporary)
    try:
        encode_callback(temporary, direct_module)
        header_bytes, prefix_bytes = _header_size(temporary)
        shape = _shape(direct_module, header_bytes)
        pidx_digest, pidx_size = _hash_file(temporary)
        _require(prefix_bytes + header_bytes + 8 * sum(shape[3::2]) == pidx_size,
                 "encoded PIDX size does not match kernel shape")
        os.replace(temporary, sidecar)
        seal = {
            "schema": SEAL_SCHEMA, "codec_schema": CODEC_SCHEMA,
            "request_sha256": request_digest, "source_identity": request["source_identity"],
            "frontend_token": token, "index": index, "module": module,
            "target": direct_module.triple, "passes": list(passes),
            "needs_libpython": needs_libpython, "needs_native_exports": needs_native_exports,
            "shape": shape, "pidx_sha256": pidx_digest, "pidx_size": pidx_size,
        }
        _atomic_json(request["seal_path"], seal)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return sidecar


def _validate_result(request, seal, path):
    try:
        rows = _read_bytes(path, _MAX_JSON_BYTES).decode("utf-8").splitlines()
        _require(len(rows) == 1, "expected one frontend result row")
        parts = rows[0].split("\t")
        _require(len(parts) in (9, 12), "frontend result fields mismatch")
        marker = 7 if len(parts) == 9 else 10
        expected_ir = os.path.join(os.path.dirname(request["sidecar_path"]),
                                   "module_" + str(request["index"]) + ".ll")
        _require(parts[:5] == ["OK", str(request["index"]), request["module"], "0",
                               "1" if seal["needs_native_exports"] else "0"]
                 and parts[6] == expected_ir
                 and parts[marker:] == ["PIDX", request["sidecar_path"]],
                 "frontend result binding mismatch")
        for value in [parts[5]] + parts[7:marker]:
            _require(value.isascii() and value.isdigit() and int(value) >= 0,
                     "frontend result count/timing mismatch")
    except UnicodeError as exc:
        raise HandoffError("indexed handoff: invalid frontend result") from exc


def _task_bound(task):
    _require(type(task.get("depends_on")) is int and task["depends_on"] >= 0,
             "backend task has no frontend dependency")
    request, seal, request_digest, seal_digest, result_path = _bound(task["handoff_request"])
    _require(task.get("handoff_source_identity") == request["source_identity"],
             "task source identity mismatch")
    if task.get("input_ready", False):
        _require(task.get("source_identity") == seal["pidx_sha256"]
                 and task.get("handoff_seal_sha256") == seal_digest
                 and task.get("inputs") == seal["shape"]
                 and (not str(task.get("startup_prior_model", "")).startswith("host-indexed-backend-")
                      or task.get("prior_input_bytes") == seal["pidx_size"]),
                 "prepared task binding mismatch")
    return request, seal, request_digest, seal_digest, result_path


def prepare_handoff_task(task, expected_frontend_token):
    """Install the actual kernel envelope after the frontend has fully retired."""
    _digest(expected_frontend_token, "expected frontend token")
    request, seal, _request_digest, seal_digest, result_path = _task_bound(task)
    _require(seal["frontend_token"] == expected_frontend_token, "stale frontend token")
    _validate_result(request, seal, result_path)
    base = task.get("handoff_base_class", task["class"])
    _text(base, "backend base class", 32768)
    execution_class = base + "|indexed-handoff|" + json.dumps(
        [SEAL_SCHEMA, CODEC_SCHEMA, request["target"], request["passes"]],
        separators=(",", ":"),
    )
    _require(task["class"] in (base, execution_class), "backend class mismatch")
    task["handoff_base_class"] = base
    task["class"] = execution_class
    task["inputs"] = list(seal["shape"])
    task["source_identity"] = seal["pidx_sha256"]
    task["handoff_seal_sha256"] = seal_digest
    if str(task.get("startup_prior_model", "")).startswith("host-indexed-backend-"):
        task["prior_input_bytes"] = seal["pidx_size"]
    task["input_ready"] = True


def _environment_seal(seal_digest):
    _require(_digest(os.environ.get(ENV_SEAL, ""), "environment seal digest") == seal_digest,
             "backend seal digest mismatch")


def validate_handoff_input(request_path, sidecar_path, output_path, artifact_kind):
    """Validate the pool-pinned capsule before the existing decoder runs."""
    request, seal, _request_digest, seal_digest, _result = _bound(request_path)
    _environment_seal(seal_digest)
    _require(artifact_kind == "PCO" and sidecar_path == request["sidecar_path"]
             and output_path == request["output_path"], "backend artifact binding mismatch")
    return request, seal


def validate_handoff_module(request_path, module):
    """Compare the already decoded module; never decode or project it again."""
    request, seal, _request_digest, seal_digest, _result = _bound(request_path, verify_input=False)
    _environment_seal(seal_digest)
    _require(module.triple == request["target"]
             and _shape(module, seal["shape"][0]) == seal["shape"],
             "decoded module target/shape mismatch")


def publish_handoff_result(request_path):
    """Write a receipt after the emitter's existing atomic PCO publication."""
    request, seal, request_digest, seal_digest, _result = _bound(request_path)
    _environment_seal(seal_digest)
    token = _digest(os.environ.get(RESOURCE_TOKEN_ENV, ""), "backend token")
    output_digest, output_size = _hash_file(request["output_path"])
    _require(output_size > 0, "empty backend output")
    receipt = {
        "schema": RECEIPT_SCHEMA, "request_sha256": request_digest,
        "seal_sha256": seal_digest, "pidx_sha256": seal["pidx_sha256"],
        "pidx_size": seal["pidx_size"], "backend_token": token,
        "output_sha256": output_digest, "output_size": output_size,
    }
    _atomic_json(request["receipt_path"], receipt)


def reset_handoff_output(task):
    """Reset only this attempt's exact outputs; preserve cancelled PIDX input."""
    _require(task.get("input_ready") is True, "backend task input is not prepared")
    request, _seal, _request_digest, _seal_digest, _result = _task_bound(task)
    for path in (request["output_path"], request["output_path"] + ".tmp", request["receipt_path"]):
        _path(path, "output reset path")
        if _exists(path):
            _regular(path)
            os.unlink(path)


def retire_handoff_task(task, expected_backend_token):
    """Pool-only: validate a successful exited attempt, then retire its input."""
    _digest(expected_backend_token, "expected backend token")
    _require(task.get("input_ready") is True, "backend task input is not prepared")
    request, seal, request_digest, seal_digest, _result = _task_bound(task)
    receipt, _receipt_digest = _load_json(request["receipt_path"], _RECEIPT_KEYS)
    _require(receipt["schema"] == RECEIPT_SCHEMA
             and receipt["request_sha256"] == request_digest
             and receipt["seal_sha256"] == seal_digest
             and receipt["pidx_sha256"] == seal["pidx_sha256"]
             and type(receipt["pidx_size"]) is int
             and receipt["pidx_size"] == seal["pidx_size"]
             and receipt["backend_token"] == expected_backend_token,
             "backend receipt binding/token mismatch")
    _digest(receipt["output_sha256"], "output digest")
    _integer(receipt["output_size"], "output size")
    output_digest, output_size = _hash_file(request["output_path"])
    _require(output_size > 0 and output_digest == receipt["output_sha256"]
             and output_size == receipt["output_size"], "backend output digest/size mismatch")
    # Requests, receipts, and outputs remain. Only coordinator-validated fixed
    # input names are retired, and only after every check above has passed.
    os.unlink(request["sidecar_path"])
    os.unlink(request["seal_path"])
