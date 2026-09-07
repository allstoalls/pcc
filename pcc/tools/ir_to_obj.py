"""Emit a target object file from LLVM IR text.

This helper exists for build-time paths that already have valid LLVM IR
but cannot safely hand that text to the host ``clang``. Some Linux
toolchains still parse typed-pointer IR by default, while pcc's Python
frontend emits opaque ``ptr`` IR. llvmlite owns a matching LLVM build, so
use its target machine directly.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import sys
import tempfile

from llvmlite import binding as llvm


class ObjectEmissionContractError(ValueError):
    """The requested object-emission mode conflicts with the input module."""


_UNKNOWN_TARGET_TRIPLES = {"", "unknown-unknown-unknown"}
_MODULE_ASM_RE = re.compile(r'^\s*module\s+asm\s+"', flags=re.MULTILINE)

# Measured runtime-only policy. Libc/allocator implementations need separate
# libcall-recursion qualification and deliberately retain their existing path.
_OPTIMIZED_RUNTIME_SOURCES = frozenset({
    "py_obj.py", "py_list.py", "py_gen.py", "py_gc_backend.py",
    "freestanding_gc_index_table.py",
})


def runtime_optimization_level(source_path=None, runtime_root=None) -> int:
    if source_path is None or runtime_root is None:
        return 0
    source = Path(source_path).resolve()
    root = Path(runtime_root).resolve()
    if source.parent == root / "py" and source.name in _OPTIMIZED_RUNTIME_SOURCES:
        return 2
    return 0


def runtime_emitter_identity() -> str:
    digest = hashlib.sha256(Path(__file__).read_bytes())
    digest.update(repr(llvm.llvm_version_info).encode("ascii"))
    return digest.hexdigest()


def _declared_module_triple(mod) -> str:
    try:
        triple = str(mod.triple or "").strip()
    except Exception:
        return ""
    if triple in _UNKNOWN_TARGET_TRIPLES:
        return ""
    return triple


def _module_triple(mod) -> str:
    return _declared_module_triple(mod) or llvm.get_default_triple()


def _target_triples_match(left: str, right: str) -> bool:
    """Compare triples after LLVM has normalized aliases and omitted fields."""
    return llvm.get_triple_parts(left) == llvm.get_triple_parts(right)


def _resolve_target_triple(mod, requested: str | None) -> str:
    declared = _declared_module_triple(mod)
    if requested is not None:
        target = str(requested)
        if not target or target != target.strip():
            raise ObjectEmissionContractError(
                "explicit target triple must be non-empty and have no surrounding "
                "whitespace"
            )
        if declared and not _target_triples_match(target, declared):
            raise ObjectEmissionContractError(
                "target triple mismatch: requested "
                + repr(target)
                + " but the module declares "
                + repr(declared)
            )
        return target
    return declared or llvm.get_default_triple()


def _module_contains_inline_asm(mod) -> bool:
    # Module-level assembly is not exposed as a ValueRef by llvmlite.  LLVM's
    # normalized module spelling makes this anchored check unambiguous; inline
    # assembly used as a call target is detected structurally below.
    if _MODULE_ASM_RE.search(str(mod)) is not None:
        return True
    for fn in mod.functions:
        for block in fn.blocks:
            for instruction in block.instructions:
                for operand in instruction.operands:
                    if operand.value_kind == llvm.ValueKind.inline_asm:
                        return True
    return False


def _validate_module_target_contract(mod, triple: str, tm) -> None:
    target_layout = str(tm.target_data)
    module_layout = str(mod.data_layout or "").strip()
    if module_layout and module_layout != target_layout:
        raise ObjectEmissionContractError(
            "target data layout mismatch for "
            + repr(triple)
            + ": the module declares "
            + repr(module_layout)
            + " but the target machine requires "
            + repr(target_layout)
        )
    mod.triple = triple
    if not module_layout:
        mod.data_layout = target_layout


def _validate_inline_asm_parser_contract(mod, triple: str) -> None:
    if not _module_contains_inline_asm(mod):
        return
    native_triple = llvm.get_default_triple()
    target_arch = llvm.get_triple_parts(triple).Arch
    native_arch = llvm.get_triple_parts(native_triple).Arch
    if target_arch != native_arch:
        raise ObjectEmissionContractError(
            "foreign-target inline assembly is unsupported: target "
            + repr(triple)
            + " uses architecture "
            + repr(target_arch)
            + ", but this process initialized only the native "
            + repr(native_arch)
            + " assembly parser for "
            + repr(native_triple)
        )


def _emit_object_with_triple(
    ir_text: str, *, target_triple: str | None = None, optimization_level: int = 0,
) -> tuple[bytes, str]:
    if optimization_level not in (0, 1, 2, 3):
        raise ObjectEmissionContractError("optimization level must be 0..3")
    llvm.initialize_all_targets()
    llvm.initialize_all_asmprinters()
    # Runtime modules use compiler-owned inline assembly for native syscall
    # boundaries.  Target/printer registration alone is insufficient: LLVM's
    # object streamer also needs the native assembly parser before it can lower
    # those inline-asm call sites.
    llvm.initialize_native_asmparser()
    mod = llvm.parse_assembly(ir_text)
    mod.verify()
    triple = _resolve_target_triple(mod, target_triple)
    target = llvm.Target.from_triple(triple)
    tm = target.create_target_machine()
    _validate_module_target_contract(mod, triple, tm)
    _validate_inline_asm_parser_contract(mod, triple)
    if optimization_level:
        tuning = llvm.PipelineTuningOptions(speed_level=optimization_level, size_level=0)
        builder = llvm.create_pass_builder(tm, tuning)
        passes = builder.getModulePassManager()
        passes.run(mod, builder)
        mod.verify()
    return tm.emit_object(mod), triple


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
        )
        if any(value is not None for value in provenance_arguments):
            if any(
                value is None
                for value in (args.provenance, args.source, args.runtime_root)
            ):
                raise ValueError(
                    "--provenance, --source, and --runtime-root must be used together"
                )
        with open(args.input, "r", encoding="utf-8") as f:
            ir_text = f.read()
        obj, resolved_triple = _emit_object_with_triple(
            ir_text, target_triple=args.target,
            optimization_level=runtime_optimization_level(args.source, args.runtime_root),
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
                object_bytes=obj,
                output_path=pending_receipt,
                member=args.member,
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
