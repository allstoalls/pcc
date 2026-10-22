"""Emit a target object from pcc IR using its owned backend and object writer."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import sys
import tempfile

# NOT ``PCC_IR_TO_OBJ``: pcc/runtime/Makefile:24 already uses that name
# for the *command* that runs this tool
# (``PCC_IR_TO_OBJ ?= $(PYTHON) -m pcc.tools.ir_to_obj``), and
# tests/test_runtime_archive_make_concurrency.py passes a path through it.
# Reading it here would compare a path against "pcc"/"llvmlite".
_EMITTER_ENV = "PCC_IR_TO_OBJ_EMITTER"
_EMITTER_PCC = "pcc"
_PCC_OWNED_TARGET_IDENTITIES = frozenset({"self-aarch64-darwin-v0", "self-x86_64-linux-v0", "self-aarch64-linux-v0", "self-x86_64-windows-v0"})






class ObjectEmissionContractError(ValueError):
    """The requested object-emission mode conflicts with the input module."""


_UNKNOWN_TARGET_TRIPLES = {"", "unknown-unknown-unknown"}
_MODULE_ASM_RE = re.compile(r'^\s*module\s+asm\s+"', flags=re.MULTILINE)
















def _host_target_triple() -> str:
    """Use the frontend's host ABI identity without querying LLVM or cc."""
    from pcc.frontends.python.pipeline_targets import host_target_triple

    triple = host_target_triple()
    if triple == "unknown-unknown-unknown":
        raise ObjectEmissionContractError(
            "host ABI is not a pcc-owned object target; pass --target"
        )
    return triple


def _resolve_triple_without_llvm(ir_text: str, target_triple: str | None) -> str:
    from pcc.backend.self_backend_parse import (
        parse_self_backend_target_triple,
    )

    declared = parse_self_backend_target_triple(ir_text) if re.search(r'^\s*target\s+triple\s*=', ir_text, re.MULTILINE) else ""
    if declared.strip().lower() in _UNKNOWN_TARGET_TRIPLES:
        declared = ""
    if target_triple is not None:
        if not target_triple or target_triple != target_triple.strip():
            raise ObjectEmissionContractError("explicit target triple must be non-empty and have no surrounding whitespace")
        if declared and _owned_triple_key(declared) != _owned_triple_key(target_triple):
            raise ObjectEmissionContractError(
                "target triple mismatch: requested " + repr(target_triple)
                + " but the module declares " + repr(declared)
            )
        return target_triple
    return declared or _host_target_triple()


def _owned_triple_key(triple: str) -> tuple[str, ...]:
    from pcc.backend.self_backend_target_match import _target_components

    parts = _target_components(triple)
    if not parts:
        return (triple,)
    if parts[0] == "arm64":
        parts[0] = "aarch64"
    elif parts[0] == "amd64":
        parts[0] = "x86_64"
    if len(parts) == 2 or (len(parts) == 3 and parts[1].startswith("linux")):
        parts.insert(1, "unknown")
    if len(parts) == 3:
        parts.append("unknown")
    parts[2] = re.sub(r"[0-9].*$", "", parts[2])
    return tuple(parts)


def _validate_owned_data_layout(ir_text: str, triple: str) -> None:
    match = re.search(r'^\s*target\s+datalayout\s*=\s*"([^"\n]*)"', ir_text, re.MULTILINE)
    if match is None or not match.group(1):
        return
    layout = match.group(1)
    mangling = "m:o" if "apple" in triple else "m:e"
    valid = True
    for item in layout.split("-"):
        if item in ("e", mangling, "S128", "Fn32", "Fi8", "a:0:64"):
            continue
        if re.fullmatch(r"n(?:8:)?(?:16:)?32:64", item):
            continue
        pointer = re.fullmatch(r"p(\d*):(\d+):(\d+)(?::\d+){0,2}", item)
        if pointer:
            space, size, align = pointer.groups()
            # The owned scalar ABI uses 64-bit AS0 pointers. The conventional
            # x86 non-default spaces are valid layout metadata; their use in
            # IR still requires support from the IR parser/emitter.
            if (space in ("", "0") and size == "64" and align == "64") or (
                "x86_64" in triple and (space, size, align) in (
                    ("270", "32", "32"), ("271", "32", "32"), ("272", "64", "64"),
                )
            ):
                continue
        scalar = re.fullmatch(r"([ifv])(\d+):(\d+)(?::\d+)?", item)
        if scalar:
            kind, width, align = scalar.groups()
            expected = 128 if kind == "f" and width == "80" else int(width)
            if expected in (8, 16, 32, 64, 128) and int(align) == expected:
                continue
        valid = False
        break
    if not valid:
        raise ObjectEmissionContractError(
            "target data layout mismatch for " + repr(triple) + ": " + repr(layout)
            + " is not supported by the owned ABI"
        )


def _pcc_owned_target_identity(triple: str) -> str | None:
    from pcc.backend.self_backend_dispatch import self_backend_target_identity

    try:
        identity = self_backend_target_identity(triple)
    except Exception:
        return None
    return identity if identity in _PCC_OWNED_TARGET_IDENTITIES else None


def _select_emitter(identity: str | None) -> str:
    requested = os.environ.get(_EMITTER_ENV, "").strip().lower()
    if requested and requested not in (_EMITTER_PCC,):
        raise ObjectEmissionContractError(
            "unknown " + _EMITTER_ENV + " value " + repr(requested)
            + "; expected " + repr(_EMITTER_PCC)
        )
    if identity is None:
        raise ObjectEmissionContractError(
            "target pcc does not own; pcc emits objects for "
            + repr(sorted(_PCC_OWNED_TARGET_IDENTITIES))
        )
    return _EMITTER_PCC


def _emit_object_pcc(ir_text: str, triple: str) -> bytes:
    """Assemble and write the object with pcc's own backend."""
    from pcc.backend.owned_object_emit import emit_owned_object

    try:
        return emit_owned_object(ir_text, triple)
    except ValueError as exc:
        raise ObjectEmissionContractError(str(exc)) from exc


def _emit_object_with_triple(
    ir_text: str, *, target_triple: str | None = None, optimization_level: int = 0,
) -> tuple[bytes, str, str]:
    if optimization_level not in (0, 1, 2, 3):
        raise ObjectEmissionContractError("optimization level must be 0..3")
    pcc_triple = _resolve_triple_without_llvm(ir_text, target_triple)
    identity = _pcc_owned_target_identity(pcc_triple)
    if _select_emitter(identity) == _EMITTER_PCC:
        _validate_owned_data_layout(ir_text, pcc_triple)
        if optimization_level:
            raise ObjectEmissionContractError(
                "the pcc object emitter takes already-optimized IR; "
                "optimization_level must be 0"
            )
        return _emit_object_pcc(ir_text, pcc_triple), pcc_triple, _EMITTER_PCC


def emit_object(ir_text: str, *, target_triple: str | None = None) -> bytes:
    return _emit_object_with_triple(ir_text, target_triple=target_triple)[0]


def _unique_temporary_sibling(path: Path, *, suffix: str) -> Path:
    with tempfile.NamedTemporaryFile(
        prefix=f".{path.name}.",
        suffix=suffix,
        dir=path.parent,
        delete=False,
    ) as stream:
        return Path(stream.name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="emit a target object file from LLVM IR text",
    )
    parser.add_argument("input", help="input .ll file")
    parser.add_argument("output", help="output .o file")
    parser.add_argument(
        "--target",
        default=None,
        help="target triple; defaults to the module triple or host triple",
    )
    parser.add_argument(
        "--provenance",
        default=None,
        help="write a pcc-Python object provenance receipt to this path",
    )
    parser.add_argument(
        "--source",
        default=None,
        help="pcc-Python source that produced the input IR",
    )
    parser.add_argument(
        "--runtime-root",
        default=None,
        help="runtime root used to derive a stable logical source path",
    )
    parser.add_argument(
        "--member",
        default=None,
        help="archive member basename; defaults to the output basename",
    )
    parser.add_argument(
        "--runtime-threads", choices=("0", "1"), default=None,
        help="archive thread configuration recorded in the runtime receipt",
    )
    parser.add_argument(
        "--runtime-refcount", default=None,
        help="archive refcount configuration recorded with --runtime-threads",
    )
    args = parser.parse_args(argv)

    output_path = Path(args.output)
    provenance_path = Path(args.provenance) if args.provenance is not None else None
    temporary_object: Path | None = None
    pending_receipt: Path | None = None
    object_published = False
    try:
        provenance_arguments = (
            args.provenance,
            args.source,
            args.runtime_root,
            args.member,
            args.runtime_threads,
            args.runtime_refcount,
        )
        if any(value is not None for value in provenance_arguments):
            if any(
                value is None
                for value in (args.provenance, args.source, args.runtime_root)
            ):
                raise ValueError(
                    "--provenance, --source, and --runtime-root must be used together"
                )
        runtime_build_config = None
        if args.runtime_threads is not None or args.runtime_refcount is not None:
            if args.runtime_threads is None or args.runtime_refcount is None:
                raise ValueError(
                    "--runtime-threads and --runtime-refcount must be used together"
                )
            runtime_build_config = {
                "threads": args.runtime_threads == "1",
                "refcount": args.runtime_refcount.strip().lower() or "atomic",
            }
        with open(args.input, "r", encoding="utf-8") as f:
            ir_text = f.read()
        obj, resolved_triple, resolved_emitter = _emit_object_with_triple(
            ir_text, target_triple=args.target,
        )
        temporary_object = _unique_temporary_sibling(
            output_path,
            suffix=".object.tmp",
        )
        with open(temporary_object, "wb") as f:
            f.write(obj)
        if args.provenance is not None:
            from pcc.tools.runtime_archive_provenance import (
                write_pcc_python_receipt,
            )

            assert provenance_path is not None
            pending_receipt = _unique_temporary_sibling(
                provenance_path,
                suffix=".receipt.pending",
            )
            write_pcc_python_receipt(
                object_path=output_path,
                ir_path=Path(args.input),
                source_path=Path(args.source),
                runtime_root=Path(args.runtime_root),
                target_triple=resolved_triple,
                object_emitter=resolved_emitter,
                object_bytes=obj,
                output_path=pending_receipt,
                member=args.member,
                runtime_build_config=runtime_build_config,
            )
        os.replace(temporary_object, output_path)
        temporary_object = None
        object_published = True
        if pending_receipt is not None:
            assert provenance_path is not None
            os.replace(pending_receipt, provenance_path)
            pending_receipt = None
    except Exception as exc:
        if object_published and provenance_path is not None:
            try:
                output_path.unlink()
            except FileNotFoundError:
                pass
        for temporary in (temporary_object, pending_receipt):
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
        sys.stderr.write("ir_to_obj: " + str(exc) + "\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
