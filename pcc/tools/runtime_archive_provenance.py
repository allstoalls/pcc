"""Build-authoritative provenance for the production pcc-Python runtime.

The runtime archive stores object basenames, which is not enough to prove how
those objects were produced.  This module binds every member to a receipt
written by the object emitter and binds the exact ordered C-API anchor
inventory used by native-extension links.  It validates the resulting bundle
without persisting checkout-, cache-, or temporary-directory paths.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Iterable, Sequence

from pcc.backend.ar import ArchiveFormatError, read_members

RECEIPT_SCHEMA = "pcc.runtime-object-provenance.v1"
MANIFEST_SCHEMA = "pcc.runtime-archive-provenance.v2"
PRODUCTION_POLICY = "pcc-production-no-handwritten-c.v1"
RECEIPT_SUFFIX = ".provenance.json"
MANIFEST_SUFFIX = ".provenance.json"
CAPI_INVENTORY_SUFFIX = ".capi_syms"

_PCC_PYTHON_SOURCE_KIND = "pcc-python"
_PCC_PYTHON_PRODUCER = "pcc-python-library-ir-to-obj"
_LLVM_OBJECT_EMITTER = "llvmlite-target-machine"
# pcc's own assembler + Mach-O object writer.  The runtime archive's members
# are what pcc1 links, so emitting them through llvmlite made an
# llvmlite-free pcc1 depend on llvmlite to exist at all.
_PCC_OBJECT_EMITTER = "pcc-self-backend-object-writer"
# Receipts name which one ran; both are legitimate, and `PCC_IR_TO_OBJ_EMITTER`
# selects the llvmlite oracle for differential runs.
_OBJECT_EMITTERS = frozenset({_LLVM_OBJECT_EMITTER, _PCC_OBJECT_EMITTER})
_EMITTER_BY_IR_TO_OBJ_NAME = {
    "pcc": _PCC_OBJECT_EMITTER,
    "llvmlite": _LLVM_OBJECT_EMITTER,
}
_LOGICAL_RUNTIME_ROOT = "pcc/py_runtime"
_REGULAR_AR_MAGIC = b"!<arch>\n"
_RECEIPT_REQUIRED_FIELDS = frozenset(
    {
        "schema",
        "member",
        "object_sha256",
        "ir_sha256",
        "source",
        "source_sha256",
        "source_kind",
        "producer_kind",
        "object_emitter",
        "uses_host_cc",
        "target_triple",
    }
)
_RECEIPT_OPTIONAL_FIELDS = frozenset(
    {
        # Identity of the compiler that produced the object.  Optional for
        # integrity compatibility with receipts written before this field was
        # introduced; automatic local archive selection separately treats a
        # missing or unprovable identity as stale.
        "codegen_checksum",
    }
)
_RECEIPT_FIELDS = _RECEIPT_REQUIRED_FIELDS | _RECEIPT_OPTIONAL_FIELDS
_MANIFEST_FIELDS = frozenset(
    {
        "schema",
        "archive",
        "policy",
        "target_triple",
        "member_count",
        "members_sha256",
        "members",
        "capi_symbol_count",
        "capi_inventory_sha256",
        "capi_symbols",
    }
)


class ProvenanceError(ValueError):
    """A runtime object or archive cannot satisfy its provenance contract."""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _canonical_json(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def _write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(_canonical_json(value))
        os.replace(temporary, path)
    except Exception:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        raise


def _logical_source_path(source_path: Path, runtime_root: Path) -> str:
    source = os.path.realpath(str(source_path))
    root = os.path.realpath(str(runtime_root))
    try:
        if os.path.commonpath([source, root]) != root:
            raise ValueError("source is outside root")
    except ValueError as exc:
        raise ProvenanceError(
            f"runtime source is outside runtime root: {source_path}"
        ) from exc
    relative = os.path.relpath(source, root).replace(os.sep, "/")
    if not relative.startswith("py/") or not relative.endswith(".py") or relative.rsplit("/", 1)[-1] == ".py":
        raise ProvenanceError(
            "pcc-Python runtime source must be a .py file under py/: "
            + relative
        )
    return _LOGICAL_RUNTIME_ROOT + "/" + relative


def _validate_archive_member_name(member: object) -> str:
    if not isinstance(member, str):
        raise ProvenanceError(f"unsafe archive member name: {member!r}")
    if (
        not member
        or member != member.strip()
        or member in {".", ".."}
        or "/" in member
        or "\\" in member
        # With both separators forbidden, only a drive-relative Windows
        # basename remains to reject (for example C:member.o).
        or (len(member) >= 2 and member[1] == ":")
        or any(ord(character) < 32 for character in member)
    ):
        raise ProvenanceError(f"unsafe archive member name: {member!r}")
    return member


def _source_from_logical_path(source: object, runtime_root: Path) -> Path:
    if not isinstance(source, str) or not source:
        raise ProvenanceError("member source must be a non-empty logical path")
    parts = source.split("/")
    if (
        source != source.strip()
        or "\\" in source
        or any(part in ("", ".", "..") for part in parts)
        or any(ord(character) < 32 for character in source)
    ):
        raise ProvenanceError(
            f"member source is not a normalized relative path: {source}"
        )
    prefix = _LOGICAL_RUNTIME_ROOT + "/"
    if source != _LOGICAL_RUNTIME_ROOT and not source.startswith(prefix):
        raise ProvenanceError(
            f"member source is outside {_LOGICAL_RUNTIME_ROOT}: {source}"
        )
    relative = source[len(prefix):]
    if not relative.startswith("py/") or not relative.endswith(".py") or relative.rsplit("/", 1)[-1] == ".py":
        raise ProvenanceError(
            "pcc-Python member source must be a .py file under py/: " + source
        )
    root = os.path.realpath(str(runtime_root))
    resolved = os.path.realpath(os.path.join(root, relative.replace("/", os.sep)))
    try:
        if os.path.commonpath([resolved, root]) != root:
            raise ValueError("source is outside root")
    except ValueError as exc:
        raise ProvenanceError(f"member source escapes runtime root: {source}") from exc
    return Path(resolved)


def receipt_path_for_object(object_path: Path) -> Path:
    return Path(str(object_path) + RECEIPT_SUFFIX)


def manifest_path_for_archive(archive_path: Path) -> Path:
    return Path(str(archive_path) + MANIFEST_SUFFIX)


def capi_inventory_path_for_archive(archive_path: Path) -> Path:
    return Path(str(archive_path) + CAPI_INVENTORY_SUFFIX)


def _validate_capi_symbol(symbol: object) -> str:
    if not isinstance(symbol, str) or not symbol:
        raise ProvenanceError("C-API anchor inventory contains an empty symbol")
    if any(
        not (
            character.isascii()
            and (character.isalnum() or character == "_")
        )
        for character in symbol
    ):
        raise ProvenanceError(
            f"C-API anchor inventory contains an unsafe symbol: {symbol!r}"
        )
    bare = symbol[1:] if symbol.startswith("_") else symbol
    if not (bare.startswith("Py") or bare.startswith("_Py")):
        raise ProvenanceError(
            f"C-API anchor inventory contains a non-C-API symbol: {symbol!r}"
        )
    return symbol


def _canonical_capi_inventory_bytes(symbols: Sequence[object]) -> bytes:
    if not symbols:
        raise ProvenanceError("production C-API anchor inventory must not be empty")
    validated = [_validate_capi_symbol(symbol) for symbol in symbols]
    if len(validated) != len(set(validated)):
        raise ProvenanceError("C-API anchor inventory contains duplicate symbols")
    if validated != sorted(validated):
        raise ProvenanceError(
            "C-API anchor inventory is not in canonical bytewise symbol order"
        )
    return ("\n".join(validated) + "\n").encode("ascii")


def _load_capi_inventory(path: Path) -> tuple[bytes, list[str]]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ProvenanceError(
            f"cannot read C-API anchor inventory {path}: {exc}"
        ) from exc
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ProvenanceError(
            f"C-API anchor inventory is not ASCII: {path}"
        ) from exc
    symbols = text.splitlines()
    canonical = _canonical_capi_inventory_bytes(symbols)
    if data != canonical:
        raise ProvenanceError(
            "C-API anchor inventory is not canonical newline-delimited content: "
            + str(path)
        )
    return data, symbols


def write_pcc_python_receipt(
    *,
    object_path: Path,
    ir_path: Path,
    source_path: Path,
    runtime_root: Path,
    target_triple: str,
    object_emitter: str = _PCC_OBJECT_EMITTER,
    object_bytes: bytes | None = None,
    output_path: Path | None = None,
    member: str | None = None,
) -> dict[str, object]:
    """Write the receipt for one object emitted from a pcc-Python module."""

    object_path = Path(object_path)
    ir_path = Path(ir_path)
    source_path = Path(source_path)
    runtime_root = Path(runtime_root)
    member_name = _validate_archive_member_name(
        object_path.name if member is None else member
    )
    if (
        not isinstance(target_triple, str)
        or not target_triple
        or target_triple != target_triple.strip()
        or any(ord(character) < 32 for character in target_triple)
    ):
        raise ProvenanceError("target triple must be non-empty")
    data = object_bytes if object_bytes is not None else object_path.read_bytes()
    receipt: dict[str, object] = {
        "schema": RECEIPT_SCHEMA,
        "member": member_name,
        "object_sha256": _sha256_bytes(data),
        "ir_sha256": _sha256_bytes(ir_path.read_bytes()),
        "source": _logical_source_path(source_path, runtime_root),
        "source_sha256": _sha256_bytes(source_path.read_bytes()),
        "source_kind": _PCC_PYTHON_SOURCE_KIND,
        "producer_kind": _PCC_PYTHON_PRODUCER,
        "object_emitter": _EMITTER_BY_IR_TO_OBJ_NAME.get(
            object_emitter, object_emitter
        ),
        "uses_host_cc": False,
        "target_triple": target_triple,
        "codegen_checksum": codegen_checksum(),
    }
    _write_json_atomic(output_path or receipt_path_for_object(object_path), receipt)
    return receipt


_CODEGEN_CHECKSUM_CACHE: dict = {}


def _runtime_emitter_source_identity() -> str:
    # A freshness check is not an object-emission request. In particular,
    # checking a prebuilt runtime must not import llvmlite or load LLVM.
    #
    # pcc now writes these objects with its own assembler and Mach-O object
    # writer, so that surface is part of "which emitter produced this object"
    # exactly as ir_to_obj.py is.  `macho_linker_source_identity` already
    # enumerates it for the link action; reuse it rather than restate it.
    from pcc.backend.self_backend_cache_identity import (
        macho_linker_source_identity,
    )

    digest = hashlib.sha256()
    digest.update(Path(__file__).with_name("ir_to_obj.py").read_bytes())
    digest.update(macho_linker_source_identity().encode("ascii"))
    return digest.hexdigest()


def codegen_checksum() -> str:
    """Identity of the COMPILER that produced an object, not just its source.

    `build_py/*.o` receipts recorded only `source_sha256`, so a codegen change
    left every object valid and the archive silently stale.  Measured cost of
    that during one session: a codegen edit was benchmarked against objects
    built by the previous compiler, and three separate conclusions had to be
    retracted after the fact.  Mojo does the same thing with its
    DialectChecksum, for the same stated reason -- false cache hits are very
    hard to debug.

    Reuses `bootstrap_source_sha256`, which already hashes the frontend- and
    backend-relevant source set, so there is one definition of "the compiler
    changed" rather than two that can drift.
    """
    cached = _CODEGEN_CHECKSUM_CACHE.get("value")
    if cached is not None:
        return cached
    try:
        from pcc.bootstrap_cache_identity import bootstrap_source_sha256

        # Frontend identity excludes pcc/tools; bind emitter source without
        # importing its LLVM implementation into the self compilation path.
        value = _sha256_bytes(
            (bootstrap_source_sha256() + ":" + _runtime_emitter_source_identity()).encode("ascii")
        )
    except Exception:
        # Never fail an object build over provenance metadata; an unknown
        # identity is recorded verbatim so a reader can tell it apart from a
        # real mismatch.
        value = "unknown"
    _CODEGEN_CHECKSUM_CACHE["value"] = value
    return value


def receipt_is_stale_for_current_codegen(
    record: object,
    *,
    current_checksum: str | None = None,
) -> bool:
    """True when *record* was produced by a different compiler than this one.

    Separate from receipt *validation* on purpose: an object whose source is
    unchanged but whose compiler changed is stale, not corrupt.  Conflating the
    two makes every pre-existing object fail integrity the moment the field is
    introduced.

    This check is fail-closed for automatically managed local archives: an old
    receipt without the field, a malformed value, or an unavailable current
    identity must rebuild rather than silently reusing an unprovable object.
    Receipt integrity remains a separate question so explicit archives and
    verified wheel bundles are not coupled to the current checkout.
    """
    if not isinstance(record, dict):
        return True
    recorded = record.get("codegen_checksum")
    current = codegen_checksum() if current_checksum is None else current_checksum
    if not _is_sha256(recorded) or not _is_sha256(current):
        return True
    return recorded != current


def manifest_is_stale_for_current_codegen(manifest: object) -> bool:
    """Return whether any member lacks the current compiler identity.

    The caller is responsible for archive/manifest integrity validation.  This
    aggregate deliberately computes the current identity once so a manifest is
    judged against one frozen value.
    """

    if not isinstance(manifest, dict):
        return True
    members = manifest.get("members")
    if not isinstance(members, list) or not members:
        return True
    current = codegen_checksum()
    if not _is_sha256(current):
        return True
    for record in members:
        if receipt_is_stale_for_current_codegen(
            record,
            current_checksum=current,
        ):
            return True
    return False


def _require_regular_archive(archive_path: Path) -> None:
    with open(str(archive_path), "rb") as stream:
        magic = stream.read(len(_REGULAR_AR_MAGIC))
    if magic != _REGULAR_AR_MAGIC:
        raise ProvenanceError(
            f"runtime archive is not a regular ar archive: {archive_path}"
        )


def _archive_members(archive_path: Path, *, ar: str) -> list[str]:
    return list(_extract_archive_members(archive_path, ar=ar))


def _extract_archive_members(archive_path: Path, *, ar: str) -> dict[str, bytes]:
    # Keep the former tool-selection argument source-compatible while callers
    # migrate. Archive verification always uses the owned parser in process.
    del ar
    _require_regular_archive(archive_path)
    try:
        members = read_members(archive_path.read_bytes())
    except ArchiveFormatError as exc:
        raise ProvenanceError(str(exc)) from exc
    extracted: dict[str, bytes] = {}
    for member, payload in members:
        _validate_archive_member_name(member)
        if member in extracted:
            raise ProvenanceError("runtime archive contains duplicate member names")
        extracted[member] = payload
    return extracted


def _load_json_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProvenanceError(f"cannot read provenance JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProvenanceError(f"provenance JSON root must be an object: {path}")
    return value


def _validate_member_record(
    record: dict[str, object],
    *,
    member: str,
    member_bytes: bytes,
    runtime_root: Path,
) -> str:
    if (
        not _RECEIPT_REQUIRED_FIELDS.issubset(record)
        or not set(record).issubset(_RECEIPT_FIELDS)
    ):
        missing = sorted(_RECEIPT_REQUIRED_FIELDS - set(record))
        extra = sorted(set(record) - _RECEIPT_FIELDS)
        raise ProvenanceError(
            f"{member}: invalid receipt fields: missing={missing!r} extra={extra!r}"
        )
    if record.get("schema") != RECEIPT_SCHEMA:
        raise ProvenanceError(f"{member}: invalid object receipt schema")
    if record.get("member") != member:
        raise ProvenanceError(f"{member}: receipt names {record.get('member')!r}")
    if record.get("source_kind") != _PCC_PYTHON_SOURCE_KIND:
        raise ProvenanceError(f"{member}: source is not pcc-Python")
    if record.get("producer_kind") != _PCC_PYTHON_PRODUCER:
        raise ProvenanceError(f"{member}: object was not produced by pcc ir_to_obj")
    if record.get("object_emitter") not in _OBJECT_EMITTERS:
        raise ProvenanceError(
            f"{member}: unexpected object emitter "
            f"{record.get('object_emitter')!r}"
        )
    if record.get("uses_host_cc") is not False:
        raise ProvenanceError(f"{member}: host-CC objects are forbidden")
    target_triple = record.get("target_triple")
    if (
        not isinstance(target_triple, str)
        or not target_triple
        or target_triple != target_triple.strip()
        or any(ord(character) < 32 for character in target_triple)
    ):
        raise ProvenanceError(f"{member}: target triple is missing")
    if record.get("object_sha256") != _sha256_bytes(member_bytes):
        raise ProvenanceError(f"{member}: archived object does not match its receipt")
    source = _source_from_logical_path(record.get("source"), runtime_root)
    if not os.path.isfile(str(source)):
        raise ProvenanceError(
            f"{member}: source file is missing: {record.get('source')}"
        )
    if record.get("source_sha256") != _sha256_bytes(source.read_bytes()):
        raise ProvenanceError(f"{member}: source does not match its receipt")
    for field in ("object_sha256", "ir_sha256", "source_sha256"):
        value = record.get(field)
        if not _is_sha256(value):
            raise ProvenanceError(f"{member}: {field} must be SHA-256")
    # NOTE: `codegen_checksum` is deliberately NOT validated here.  This
    # function checks archive *integrity* -- "is this object what its receipt
    # says it is".  A compiler-identity mismatch means the object is *stale*,
    # which is a rebuild trigger, not corruption; raising here stopped the
    # build outright the moment the field was introduced, because every
    # existing object predates it.  Staleness is exposed by
    # `receipt_is_stale_for_current_codegen` for build logic to consult.
    return target_triple


def _members_sha256(records: Iterable[dict[str, object]]) -> str:
    digest = hashlib.sha256()
    for record in records:
        digest.update(str(record["member"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(record["object_sha256"]).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def assemble_runtime_archive_manifest(
    archive_path: Path,
    object_paths: Sequence[Path],
    *,
    runtime_root: Path,
    output_path: Path | None = None,
    capi_inventory_path: Path | None = None,
    ar: str = "ar",
) -> dict[str, object]:
    """Validate object receipts and write a manifest for an existing archive."""

    archive_path = Path(archive_path)
    runtime_root = Path(runtime_root)
    if not object_paths:
        raise ProvenanceError("production runtime archive requires at least one member")
    expected = [Path(path).name for path in object_paths]
    extracted = _extract_archive_members(archive_path, ar=ar)
    actual = list(extracted)
    if actual != expected:
        raise ProvenanceError(
            f"runtime archive inventory mismatch: expected={expected!r} actual={actual!r}"
        )
    records: list[dict[str, object]] = []
    target_triples: set[str] = set()
    for object_path, member in zip(object_paths, expected, strict=True):
        object_path = Path(object_path)
        receipt = _load_json_object(receipt_path_for_object(object_path))
        object_bytes = object_path.read_bytes()
        if _sha256_bytes(object_bytes) != _sha256_bytes(extracted[member]):
            raise ProvenanceError(f"{member}: archive member differs from build object")
        target_triples.add(
            _validate_member_record(
                receipt,
                member=member,
                member_bytes=object_bytes,
                runtime_root=runtime_root,
            )
        )
        records.append(receipt)
    if len(target_triples) != 1:
        raise ProvenanceError(
            f"runtime archive contains mixed target triples: {sorted(target_triples)!r}"
        )
    target_triple = next(iter(target_triples))
    capi_path = capi_inventory_path or capi_inventory_path_for_archive(archive_path)
    capi_bytes, capi_symbols = _load_capi_inventory(Path(capi_path))
    manifest: dict[str, object] = {
        "schema": MANIFEST_SCHEMA,
        "archive": archive_path.name.removesuffix(".tmp"),
        "policy": PRODUCTION_POLICY,
        "target_triple": target_triple,
        "member_count": len(records),
        "members_sha256": _members_sha256(records),
        "members": records,
        "capi_symbol_count": len(capi_symbols),
        "capi_inventory_sha256": _sha256_bytes(capi_bytes),
        "capi_symbols": capi_symbols,
    }
    _write_json_atomic(output_path or manifest_path_for_archive(archive_path), manifest)
    return manifest


def verify_runtime_archive_manifest(
    archive_path: Path,
    *,
    runtime_root: Path,
    manifest_path: Path | None = None,
    capi_inventory_path: Path | None = None,
    ar: str = "ar",
) -> dict[str, object]:
    """Verify a production archive against its adjacent provenance manifest."""

    archive_path = Path(archive_path)
    runtime_root = Path(runtime_root)
    path = manifest_path or manifest_path_for_archive(archive_path)
    manifest = _load_json_object(path)
    if set(manifest) != _MANIFEST_FIELDS:
        missing = sorted(_MANIFEST_FIELDS - set(manifest))
        extra = sorted(set(manifest) - _MANIFEST_FIELDS)
        raise ProvenanceError(
            f"invalid runtime archive manifest fields: "
            f"missing={missing!r} extra={extra!r}"
        )
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise ProvenanceError("invalid runtime archive manifest schema")
    if manifest.get("archive") != archive_path.name:
        raise ProvenanceError("runtime archive manifest names a different archive")
    if manifest.get("policy") != PRODUCTION_POLICY:
        raise ProvenanceError("runtime archive manifest has an unexpected policy")
    manifest_target = manifest.get("target_triple")
    if (
        not isinstance(manifest_target, str)
        or not manifest_target
        or manifest_target != manifest_target.strip()
        or any(ord(character) < 32 for character in manifest_target)
    ):
        raise ProvenanceError("runtime archive manifest target triple is missing")
    member_count = manifest.get("member_count")
    if type(member_count) is not int or member_count < 1:
        raise ProvenanceError(
            "runtime archive manifest member_count must be a positive integer"
        )
    records = manifest.get("members")
    if not isinstance(records, list) or any(
        not isinstance(row, dict) for row in records
    ):
        raise ProvenanceError("runtime archive manifest members must be objects")
    typed_records: list[dict[str, object]] = list(records)
    if not typed_records:
        raise ProvenanceError("production runtime archive requires at least one member")
    extracted = _extract_archive_members(archive_path, ar=ar)
    members = list(extracted)
    expected = [str(record.get("member", "")) for record in typed_records]
    if members != expected:
        raise ProvenanceError(
            f"runtime archive inventory mismatch: expected={expected!r} actual={members!r}"
        )
    target_triples: set[str] = set()
    for record, member in zip(typed_records, members, strict=True):
        target_triples.add(
            _validate_member_record(
                record,
                member=member,
                member_bytes=extracted[member],
                runtime_root=runtime_root,
            )
        )
    if len(target_triples) != 1:
        raise ProvenanceError(
            f"runtime archive contains mixed target triples: {sorted(target_triples)!r}"
        )
    if manifest_target not in target_triples:
        raise ProvenanceError(
            "runtime archive manifest target triple differs from its members"
        )
    if manifest.get("member_count") != len(typed_records):
        raise ProvenanceError("runtime archive manifest member_count is stale")
    if manifest.get("members_sha256") != _members_sha256(typed_records):
        raise ProvenanceError("runtime archive manifest members_sha256 is stale")
    capi_symbol_count = manifest.get("capi_symbol_count")
    if type(capi_symbol_count) is not int or capi_symbol_count < 1:
        raise ProvenanceError(
            "runtime archive manifest capi_symbol_count must be a positive integer"
        )
    manifest_capi_symbols = manifest.get("capi_symbols")
    if not isinstance(manifest_capi_symbols, list):
        raise ProvenanceError("runtime archive manifest capi_symbols must be a list")
    manifest_capi_bytes = _canonical_capi_inventory_bytes(manifest_capi_symbols)
    if capi_symbol_count != len(manifest_capi_symbols):
        raise ProvenanceError("runtime archive manifest capi_symbol_count is stale")
    manifest_capi_digest = manifest.get("capi_inventory_sha256")
    if not _is_sha256(manifest_capi_digest):
        raise ProvenanceError(
            "runtime archive manifest capi_inventory_sha256 must be SHA-256"
        )
    if manifest_capi_digest != _sha256_bytes(manifest_capi_bytes):
        raise ProvenanceError(
            "runtime archive manifest capi_inventory_sha256 is stale"
        )
    capi_path = capi_inventory_path or capi_inventory_path_for_archive(archive_path)
    capi_bytes, capi_symbols = _load_capi_inventory(Path(capi_path))
    if capi_symbols != manifest_capi_symbols:
        raise ProvenanceError(
            "runtime archive C-API anchor inventory differs from its manifest"
        )
    if _sha256_bytes(capi_bytes) != manifest_capi_digest:
        raise ProvenanceError(
            "runtime archive C-API anchor inventory digest differs from its manifest"
        )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    assemble = subparsers.add_parser("assemble")
    assemble.add_argument("--archive", required=True)
    assemble.add_argument("--runtime-root", required=True)
    assemble.add_argument("--output")
    assemble.add_argument("--capi-inventory")
    assemble.add_argument("--ar", default="ar")
    assemble.add_argument("objects", nargs="+")
    verify = subparsers.add_parser("verify")
    verify.add_argument("--archive", required=True)
    verify.add_argument("--runtime-root", required=True)
    verify.add_argument("--manifest")
    verify.add_argument("--capi-inventory")
    verify.add_argument("--ar", default="ar")
    args = parser.parse_args(argv)
    try:
        if args.command == "assemble":
            assemble_runtime_archive_manifest(
                Path(args.archive),
                [Path(path) for path in args.objects],
                runtime_root=Path(args.runtime_root),
                output_path=Path(args.output) if args.output else None,
                capi_inventory_path=(
                    Path(args.capi_inventory) if args.capi_inventory else None
                ),
                ar=args.ar,
            )
        else:
            verify_runtime_archive_manifest(
                Path(args.archive),
                runtime_root=Path(args.runtime_root),
                manifest_path=Path(args.manifest) if args.manifest else None,
                capi_inventory_path=(
                    Path(args.capi_inventory) if args.capi_inventory else None
                ),
                ar=args.ar,
            )
    except (OSError, ProvenanceError) as exc:
        parser.exit(1, f"runtime archive provenance: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
