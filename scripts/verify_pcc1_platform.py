"""Read-only admission of the existing Linux x86_64 platform Stage1 product.

Consumes pcc.platform-bootstrap.v1 directly. It neither converts another
receipt protocol nor builds or executes a compiler. A completed Stage1/canary
is not a five-GC result, fixed point or application benchmark. Process sampling
and ELF dependency headers are not an exhaustive execution-ownership proof.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import struct
import subprocess
import sys

from scripts import bootstrap_platform as producer

CORE_ROOT = Path(__file__).resolve().parents[1]
TARGET = "x86_64-unknown-linux-gnu"
SOURCE_INPUTS = ("pcc", "scripts", "src", "include", "utils", "hatch_build.py",
                 "pyproject.toml", "uv.lock", ".python-version")
EXECUTION_FIELDS = {"seconds", "peak_tree_rss", "log", "command", "launched_command"}
STAGE_FIELDS = EXECUTION_FIELDS | {"stage", "sha256", "input_compiler_sha256", "dependencies", "canary"}
RECEIPT_FIELDS = {
    "schema", "target", "gc", "source", "runtime", "configuration", "compiler_path",
    "worker_jobs", "tree_rss_limit", "stages", "fixed_point", "qualification_scope",
    "execution_owner_evidence", "qualified",
}
PRODUCER_SCOPE = "source-stable self-host fixed point and native GC/object/integer/exception canary"
OWNER_EVIDENCE = {
    "method": "process-tree executable sampling and artifact dependency headers",
    "sampling_interval_seconds": 0.2,
    "exhaustive_subprocess_trace": False,
    "limitation": "processes that start and exit between samples may not be observed",
}
# Deliberately pinned to bootstrap_platform.run_chain's barrier. A producer
# change must update this contract and its producer-driven tests together.
CANARY_SOURCE = (
    "from pcc.unsafe import gc_backend_current\n"
    "class Box:\n"
    "    def __init__(self, value: int):\n        self.value = value\n"
    "def run() -> int:\n"
    "    box = Box(2 ** 80)\n"
    "    items = [box]\n"
    "    try:\n        raise ValueError('probe')\n"
    "    except ValueError:\n        return items[0].value + 42\n"
    "print(gc_backend_current())\nprint(run())\n"
).encode("utf-8")


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AdmissionError("duplicate JSON field: " + key)
        result[key] = value
    return result


def _nonfinite_json(value):
    raise AdmissionError("non-finite JSON number: " + value)


class AdmissionError(ValueError):
    """The supplied product cannot establish the requested native identity."""


def _require(condition, message):
    if not condition:
        raise AdmissionError(message)


def _sha256(value):
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _relative(value):
    _require(isinstance(value, str) and bool(value), "missing relative path")
    path = PurePosixPath(value)
    _require(value == path.as_posix() and not path.is_absolute()
             and ".." not in path.parts and "\\" not in value
             and "\0" not in value and value != ".", "unsafe relative path")
    return path


class _Files:
    """Capture endpoint stability for every admitted input; never alter it."""

    def __init__(self):
        self.files = {}

    def add(self, path):
        path = Path(path).absolute()
        _require(not any(part.is_symlink() for part in (path, *path.parents)),
                 "admission inputs must not contain symlinks: " + str(path))
        _require(path.is_file(), "missing regular admission input: " + str(path))
        before = path.stat()
        data = path.read_bytes()
        after = path.stat()
        identity = lambda stat: (stat.st_dev, stat.st_ino, stat.st_mode,
                                  stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        _require(identity(before) == identity(after), "input changed while reading")
        record = {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(),
                  "size_bytes": len(data), "stat": list(identity(after))}
        previous = self.files.get(str(path))
        _require(previous is None or previous == record, "admission input changed")
        self.files[str(path)] = record
        return record

    def json(self, path):
        self.add(path)
        value = json.loads(Path(path).read_text(), object_pairs_hook=_json_pairs,
                           parse_constant=_nonfinite_json)
        _require(isinstance(value, dict), "receipt must contain an object")
        return value

    def verify(self):
        for path in tuple(self.files):
            self.add(path)


def _static_elf64(data):
    """Admit only the owned little-endian x86_64 Linux ET_EXEC ABI."""
    _require(len(data) >= 64 and data[:7] == b"\x7fELF\x02\x01\x01"
             and data[7] in (0, 3) and data[8] == 0, "requires Linux ELF64-LE ABI")
    kind, machine, version = struct.unpack_from("<HHI", data, 16)
    entry, phoff = struct.unpack_from("<QQ", data, 24)
    flags, ehsize, phentsize, phnum = struct.unpack_from("<IHHH", data, 48)
    _require((kind, machine, version, flags, ehsize, phentsize) == (2, 62, 1, 0, 64, 56),
             "requires static x86_64 ET_EXEC header")
    _require(0 < phnum < 65535 and 64 <= phoff
             and phoff + phnum * phentsize <= len(data), "invalid ELF program-header bounds")
    executable_entry = False
    for index in range(phnum):
        segment, flags, offset, address, _, filesz, memsz, alignment = struct.unpack_from(
            "<IIQQQQQQ", data, phoff + index * phentsize,
        )
        _require(segment not in (2, 3), "dynamic ELF dependencies are not admitted")
        _require(offset + filesz <= len(data), "ELF segment exceeds file bounds")
        if segment == 1:
            _require(filesz <= memsz and address + memsz <= 2**64 and flags & ~7 == 0,
                     "invalid ELF load segment")
            _require(alignment in (0, 1) or (
                alignment & (alignment - 1) == 0 and address % alignment == offset % alignment
            ), "invalid ELF load alignment")
            executable_entry |= bool(flags & 1 and address <= entry < address + filesz)
    _require(executable_entry, "ELF entry is not in a file-backed executable load segment")
    return {"format": "ELF64-LE-ET_EXEC", "machine": 62, "static": True, "entry": entry}


def _host_source(source_root):
    root = Path(source_root).absolute()
    _require(sys.platform == "linux" and platform.machine().lower() in ("x86_64", "amd64")
             and struct.calcsize("P") == 8 and sys.byteorder == "little",
             "platform compiler admission requires an x86_64 Linux host ABI")
    _require(root == CORE_ROOT == producer.ROOT, "use the verifier's exact core source root")
    # Do not combine one checkout's receipts with another checkout's cached
    # Python modules. Runtime freshness must use the selected implementation.
    for name, module in tuple(sys.modules.items()):
        if name.split(".", 1)[0] in ("pcc", "scripts"):
            filename = getattr(module, "__file__", None)
            if filename:
                _require(Path(filename).resolve().is_relative_to(root),
                         "mixed source implementation: " + name)
    return root


def _source_files(root, files):
    identity = producer.source_identity()
    _require(identity.get("clean_commit") is True, "source checkout is not clean")
    names = subprocess.check_output([
        "git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", *SOURCE_INPUTS,
    ], cwd=root)
    names = sorted({os.fsdecode(name) for name in names.split(b"\0") if name})
    _require(bool(names), "source closure is empty")
    digest = hashlib.sha256()
    result = {}
    for name in names:
        _relative(name)
        record = files.add(root / name)
        encoded = name.encode("utf-8", errors="surrogateescape")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(b"file\0")
        digest.update(bytes.fromhex(record["sha256"]))
        result[name] = record["sha256"]
    _require(identity.get("source_sha256") == digest.hexdigest()
             and type(identity.get("source_file_count")) is int
             and identity["source_file_count"] == len(names)
             and identity.get("source_scope") == "compiler-runtime-headers-build-harness-v1",
             "source digest/closure differs from producer")
    return identity, result


def _runtime(archive, root, config, files):
    from pcc.backend.elf_x86_64 import ElfError, parse_relocatable, read_archive_payloads
    from pcc.driver.bootstrap_cache_identity import bootstrap_source_sha256
    from pcc.frontends.python import owned_runtime_build
    from pcc.tools import runtime_archive_provenance as provenance

    _host_source(root)
    archive = Path(archive).absolute()
    record = files.add(archive)
    sidecars = {suffix: files.add(Path(str(archive) + suffix))
                for suffix in (".provenance.json", ".capi_syms")}
    # The existing owner enforces the provenance schema. Also reject ambiguous
    # JSON encodings rather than silently accepting the last duplicate field.
    files.json(Path(str(archive) + ".provenance.json"))
    manifest = provenance.verify_runtime_archive_manifest(archive, runtime_root=root / "pcc/runtime")
    _require(owned_runtime_build._manifest_matches_config(
        manifest, str(root / "pcc/runtime"), TARGET, config,
    ), "runtime target, member inventory or thread/refcount profile mismatch")
    # codegen_checksum caches by process lifetime. Re-derive its documented
    # source formula so an earlier checkout state cannot bless a stale archive.
    current_codegen = hashlib.sha256((
        bootstrap_source_sha256() + ":" + provenance._runtime_emitter_source_identity()
    ).encode("ascii")).hexdigest()
    _require(provenance.codegen_checksum() == current_codegen,
             "cached codegen identity does not match current source")
    _require(not provenance.manifest_is_stale_for_current_codegen(manifest),
             "runtime is stale for current codegen")
    try:
        for name, data in read_archive_payloads(archive.read_bytes()):
            _require(len(data) >= 64 and data[:7] == b"\x7fELF\x02\x01\x01"
                     and data[7] in (0, 3) and data[8] == 0
                     and parse_relocatable(data, compact_relocations=True).machine == 62,
                     "runtime member has an incompatible ELF machine/ABI: " + name)
    except ElfError as exc:
        raise AdmissionError("runtime member has malformed ELF: " + str(exc)) from exc
    target_stamp = Path(str(archive) + ".target")
    if target_stamp.exists():
        sidecars[".target"] = files.add(target_stamp)
        _require(target_stamp.read_text().strip() == "linux:x86_64:" + TARGET,
                 "runtime target stamp mismatch")
    # A wheel stamp is optional for owned archives. Bind any supplied bytes;
    # explicit archive admission never uses it to bypass the full verifier.
    wheel = Path(str(archive) + ".wheel")
    if wheel.exists():
        sidecars[".wheel"] = files.add(wheel)
    for member in manifest["members"]:
        files.add(root / _relative(member["source"]))
    return {"archive": record, "sidecars": sidecars, "manifest": manifest,
            "target": TARGET, "runtime_build_config": config}


def verify_runtime_archive(archive, *, source_root, threads, refcount="atomic"):
    """Validate an existing application archive without selecting or building one."""
    _require(type(threads) is bool and refcount in ("atomic", "local"),
             "invalid requested runtime profile")
    root = _host_source(source_root)
    files = _Files()
    try:
        source, source_files = _source_files(root, files)
        result = _runtime(archive, root, {"threads": threads, "refcount": refcount}, files)
        _require(producer.source_identity() == source, "source changed during runtime admission")
        files.verify()
        return {**result, "source_identity": source, "source_files": source_files, "files": files.files}
    except (OSError, ValueError, TypeError, KeyError, struct.error, RuntimeError, OverflowError,
            subprocess.SubprocessError) as exc:
        if isinstance(exc, AdmissionError):
            raise
        raise AdmissionError("invalid runtime evidence: " + str(exc)) from exc


def _execution(record, expected_command, expected_log, rss_limit):
    _require(isinstance(record, dict)
             and set(record) == EXECUTION_FIELDS
             and type(record.get("seconds")) in (int, float)
             and math.isfinite(record["seconds"]) and record["seconds"] >= 0
             and type(record.get("peak_tree_rss")) is int and 0 <= record["peak_tree_rss"] <= rss_limit
             and record.get("command") == record.get("launched_command") == expected_command
             and record.get("log") == str(expected_log), "invalid completed execution record")


def verify_platform_compiler(receipt_path, *, compiler, runtime_archive,
                             source_root, gc_backend, threads, refcount="atomic"):
    """Admit an un-reused Stage1 plus its native canary from the real producer.

    Preserve the receipt directory's canary.py, canary1, logs and resource JSONL
    beside receipt.json, plus the explicit compiler/runtime archive. Artifacts
    may be copied together; original command paths remain bound in the receipt.
    Use the producer's clean source checkout and codegen environment. Callers
    must keep/recheck returned file identities around subsequent execution.
    """
    _require(type(gc_backend) is int and gc_backend in range(5) and type(threads) is bool
             and refcount in ("atomic", "local"), "invalid requested GC/thread/refcount profile")
    root = _host_source(source_root)
    files = _Files()
    receipt_path = Path(receipt_path).absolute()
    compiler, runtime_archive = Path(compiler).absolute(), Path(runtime_archive).absolute()
    try:
        receipt = files.json(receipt_path)
        _require(set(receipt) == RECEIPT_FIELDS
                 and receipt.get("schema") == "pcc.platform-bootstrap.v1"
                 and receipt.get("target") == TARGET and receipt.get("gc") == str(gc_backend),
                 "wrong platform producer, target or GC backend")
        _require(type(receipt.get("worker_jobs")) is int and receipt["worker_jobs"] > 0
                 and type(receipt.get("tree_rss_limit")) is int and receipt["tree_rss_limit"] > 0
                 and receipt.get("qualification_scope") == PRODUCER_SCOPE
                 and receipt.get("execution_owner_evidence") == OWNER_EVIDENCE
                 and receipt["execution_owner_evidence"]["exhaustive_subprocess_trace"] is False,
                 "incomplete or unsupported producer qualification evidence")
        rss_limit = receipt["tree_rss_limit"]
        source, source_files = _source_files(root, files)
        _require(receipt.get("source") == source, "compiler does not match the current clean source")
        expected_config = producer.build_configuration(TARGET)
        _require(expected_config.get("runtime") == {"threads": threads, "refcount": refcount}
                 and receipt.get("configuration") == expected_config,
                 "compiler configuration differs from the current requested profile")
        stages = receipt.get("stages")
        _require(isinstance(stages, list) and len(stages) == 1
                 and receipt.get("fixed_point") is False and receipt.get("qualified") is False,
                 "require a Stage1-only product, not an inferred fixed point")
        stage = stages[0]
        _require(isinstance(stage, dict) and set(stage) == STAGE_FIELDS
                 and type(stage.get("stage")) is int and stage["stage"] == 1
                 and _sha256(stage.get("input_compiler_sha256")),
                 "require the original un-reused Stage1 producer receipt")
        original_compiler = receipt.get("compiler_path")
        _require(isinstance(original_compiler, str) and Path(original_compiler).is_absolute()
                 and Path(original_compiler).name == "pcc1", "invalid producer compiler path")
        original_dir = Path(original_compiler).parent
        command = stage.get("command")
        _require(isinstance(command, list) and len(command) == 11
                 and isinstance(command[0], str) and Path(command[0]).is_absolute()
                 and command[1:8] == ["-m", "pcc", "--backend", "self", "--python-libpython", "off", "--ir-scaffold=on"]
                 and command[-2:] == ["-o", original_compiler]
                 and isinstance(command[8], str) and Path(command[8]).is_absolute()
                 and command[8].endswith("/pcc/__main__.py"), "invalid Stage1 build command")
        stage_execution = {key: stage[key] for key in EXECUTION_FIELDS}
        _execution(stage_execution, command, original_dir / "stage1.log", rss_limit)
        compiler_record = files.add(compiler)
        _require(compiler_record["sha256"] == stage.get("sha256") and os.access(compiler, os.X_OK),
                 "compiler bytes/executable permission differ from producer")
        executable = _static_elf64(compiler.read_bytes())
        _require(stage.get("dependencies") == producer.dependency_receipt(compiler),
                 "compiler dependency evidence changed")
        runtime = _runtime(runtime_archive, root, expected_config["runtime"], files)
        recorded_runtime = receipt.get("runtime")
        _require(isinstance(recorded_runtime, dict)
                 and set(recorded_runtime) == {"path", "sha256", "provenance_sha256", "capi_inventory_sha256"}
                 and isinstance(recorded_runtime.get("path"), str)
                 and Path(recorded_runtime["path"]).is_absolute()
                 and Path(recorded_runtime["path"]).name == runtime_archive.name
                 and recorded_runtime.get("sha256") == runtime["archive"]["sha256"]
                 and recorded_runtime.get("provenance_sha256") == runtime["sidecars"][".provenance.json"]["sha256"]
                 and recorded_runtime.get("capi_inventory_sha256") == runtime["sidecars"][".capi_syms"]["sha256"],
                 "runtime bundle is not the producer's explicit archive")
        canary = stage.get("canary")
        _require(isinstance(canary, dict) and set(canary) == {
            "source_sha256", "executable_sha256", "dependencies", "compile", "run", "output_sha256",
        }, "missing complete native canary evidence")
        original_canary = original_dir / "canary1"
        _execution(canary["compile"], [original_compiler, str(original_dir / "canary.py"), "-o",
                                      str(original_canary), "--backend", "self", "--python-libpython", "off"],
                   original_dir / "canary1-compile.log", rss_limit)
        _execution(canary["run"], [str(original_canary)], original_dir / "canary1-run.log", rss_limit)
        canary_binary = receipt_path.parent / "canary1"
        _static_elf64(canary_binary.read_bytes())
        _require(canary.get("dependencies") == producer.dependency_receipt(canary_binary),
                 "canary dependency evidence changed")
        for name, digest_field in (("canary.py", "source_sha256"), ("canary1", "executable_sha256"),
                                   ("canary1-run.log", "output_sha256")):
            _require(files.add(receipt_path.parent / name)["sha256"] == canary[digest_field],
                     "native canary artifact changed: " + name)
        _require((receipt_path.parent / "canary.py").read_bytes() == CANARY_SOURCE,
                 "native canary source differs from the producer's canonical barrier")
        expected_output = f"{gc_backend}\n{2**80 + 42}\n"
        _require((receipt_path.parent / "canary1-run.log").read_text() == expected_output,
                 "native GC/object/integer/exception canary output mismatch")
        for name in ("stage1", "canary1-compile", "canary1-run"):
            files.add(receipt_path.parent / (name + ".log"))
            files.add(receipt_path.parent / (name + ".resources.jsonl"))
        _require(producer.source_identity() == source, "source changed during compiler admission")
        files.verify()
        return {"schema": "pcc.platform-compiler-admission.v1", "target": TARGET,
                "compiler": compiler_record, "source_identity": source, "source_files": source_files,
                "runtime": runtime, "configuration": expected_config, "gc_backend": gc_backend,
                "executable": executable, "files": files.files,
                "qualification": "Stage1/native-canary artifact only; five-GC, fixed point and application benchmark unproven",
                "execution_owner_limit": "producer process sampling is not an exhaustive subprocess trace"}
    except (OSError, ValueError, TypeError, KeyError, struct.error, RuntimeError, OverflowError,
            subprocess.SubprocessError) as exc:
        if isinstance(exc, AdmissionError):
            raise
        raise AdmissionError("invalid platform compiler evidence: " + str(exc)) from exc
