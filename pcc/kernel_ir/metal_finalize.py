"""PCC Metal device finalize.

Row K-P0-METAL-TVM-FINALIZE. This produces **inspectable device-source
metadata**: the packaging descriptors for a Metal ``.metal -> .air -> .metallib``
pipeline.

When the Xcode Metal command-line tooling is absent (which is the norm on CI and
inside the pcc sandbox), the finalize returns a ``SKIPPED_WITH_REASON`` result
rather than pretending it compiled anything. This keeps the mode boundary
honest (AGENTS.md obligation 1): "descriptor produced" != "Metal source
produced" != "metallib produced" != "kernel launched".

Importable standalone::

    from pcc.kernel_ir.metal_finalize import finalize_metal, MetalFinalizeError
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pcc.kernel_ir.ir import KernelModule, MemoryScope
from pcc.kernel_ir.scalar_semantics import KernelScalarError, coerce_pod_scalar
from pcc.kernel_ir.tirx_adapter import (
    PLAIN_TIR_FREEZE_MARKER,
    PlainTirModule,
    lower_to_plain_tir,
)

# Sentinel status strings. These are part of the claim boundary, not decoration.
STATUS_DESCRIPTOR_ONLY = "descriptor_only"
STATUS_SOURCE_ONLY = "metal_source_only"
STATUS_ARTIFACTS_PRODUCED = "metal_artifacts_produced"
STATUS_SKIPPED_WITH_REASON = "SKIPPED_WITH_REASON"


class MetalFinalizeError(ValueError):
    """The plain-TIR module is not in a shape the Metal finalize can package."""


_METAL_RESERVED_KERNEL_NAMES = {"main"}


def metal_device_entry_name(entry: str) -> str:
    """Return the actual MSL kernel entry name for a logical plain-TIR entry."""
    if not isinstance(entry, str) or not entry:
        raise MetalFinalizeError(f"plain-TIR func has invalid name {entry!r}")
    chars: list[str] = []
    for i, ch in enumerate(entry):
        if ch == "_" or ch.isalpha() or (i > 0 and ch.isdigit()):
            chars.append(ch)
        else:
            chars.append("_")
    name = "".join(chars)
    if not name or name[0].isdigit():
        name = f"pcc_kernel_{name}"
    if name != entry:
        name = f"pcc_kernel_{name}"
    if name in _METAL_RESERVED_KERNEL_NAMES:
        name = f"pcc_{name}_kernel"
    return name


@dataclass(frozen=True)
class MetalPackagingDescriptor:
    """The inspectable packaging plan for one Metal library.

    Describes the ``.metal -> .air -> .metallib`` steps and the tool each step
    would invoke — WITHOUT invoking any of them.
    """

    library_name: str
    entry_points: list[str]
    metal_source_name: str  # e.g. "mod.metal"
    air_name: str  # e.g. "mod.air"
    metallib_name: str  # e.g. "mod.metallib"
    steps: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "library_name": self.library_name,
            "entry_points": list(self.entry_points),
            "artifacts": {
                "metal_source": self.metal_source_name,
                "air": self.air_name,
                "metallib": self.metallib_name,
            },
            "steps": list(self.steps),
        }


@dataclass(frozen=True)
class MetalFinalizeResult:
    """Result of a Metal finalize.

    ``host_launch_claimed`` is deliberately always false here. Host launch
    boundary proof is produced by ``host_device_split.py``; this module owns only
    device artifacts.
    """

    status: str  # STATUS_DESCRIPTOR_ONLY or STATUS_SKIPPED_WITH_REASON
    descriptor: MetalPackagingDescriptor | None
    reason: str | None = None
    metal_source: str | None = None
    artifact_paths: dict[str, str] = field(default_factory=dict)
    metal_source_produced: bool = False
    air_produced: bool = False
    metallib_produced: bool = False

    @property
    def skipped(self) -> bool:
        return self.status == STATUS_SKIPPED_WITH_REASON

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "descriptor": self.descriptor.to_dict() if self.descriptor else None,
            "artifact_paths": dict(self.artifact_paths),
            "metal_source_produced": self.metal_source_produced,
            "air_produced": self.air_produced,
            "metallib_produced": self.metallib_produced,
            # Hard, explicit disclaimers so no caller can misread this result.
            "host_launch_claimed": False,
        }


def _metal_toolchain_available() -> bool:
    """True only if the Xcode Metal CLI is actually present.

    Real pipeline is ``xcrun -sdk macosx metal ... -> .air`` then
    ``xcrun metallib ... -> .metallib``. Probe via ``xcrun --find metal``
    because the compiler is usually not on PATH.
    """
    if shutil.which("xcrun") is None:
        return False
    try:
        from pcc.backend.metal import metal_toolchain_usable

        return metal_toolchain_usable()
    except Exception:
        return False


def _metal_scalar_type(dtype: str) -> str:
    mapping = {
        "bool": "bool",
        "i8": "char",
        "u8": "uchar",
        "i16": "short",
        "u16": "ushort",
        "i32": "int",
        "i64": "long",
        "u32": "uint",
        "u64": "ulong",
        "f16": "half",
        "f32": "float",
        "f64": "double",
    }
    try:
        return mapping[dtype]
    except KeyError as err:
        raise MetalFinalizeError(f"unsupported Metal scalar dtype {dtype!r}") from err


def _func_param_dicts(func: dict[str, Any]) -> list[dict[str, Any]]:
    params = func.get("params")
    if not isinstance(params, list):
        raise MetalFinalizeError(f"plain-TIR func {func!r} has no params list")
    return params


def _written_buffer_names(func: dict[str, Any]) -> set[str]:
    written: set[str] = set()
    for op in func.get("ops", []):
        tir_op = op.get("tir_op")
        args = list(op.get("args", []))
        if tir_op == "tir.copy_loop" and len(args) >= 2:
            written.add(str(args[1]))
        elif tir_op == "tir.fill_loop" and args:
            written.add(str(args[0]))
        elif tir_op == "tir.reduce_loop" and len(args) >= 2:
            written.add(str(args[1]))
        elif tir_op == "tir.gemm_expand" and len(args) >= 3:
            written.add(str(args[2]))
        elif tir_op == "tir.elementwise_add" and len(args) >= 3:
            written.add(str(args[2]))
        elif tir_op == "tir.indexed_store" and args:
            written.add(str(args[0]))
        elif tir_op == "tir.atomic_add" and args:
            written.add(str(args[0]))
    return written


def _kernel_extent(func: dict[str, Any]) -> int | None:
    for op in func.get("ops", []):
        attrs = op.get("attrs") or {}
        extent = attrs.get("extent")
        if isinstance(extent, int) and extent > 0:
            return extent
    fill_targets = [
        str(op.get("args", [""])[0])
        for op in func.get("ops", [])
        if op.get("tir_op") == "tir.fill_loop" and op.get("args")
    ]
    if len(fill_targets) == 1:
        target = fill_targets[0]
        for param in _func_param_dicts(func):
            if param.get("name") != target or param.get("kind") != "buffer":
                continue
            shape = param.get("shape")
            if isinstance(shape, list) and shape and all(
                isinstance(dim, int) and dim > 0 for dim in shape
            ):
                count = 1
                for dim in shape:
                    count *= dim
                return count
    return None


def _literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    raise MetalFinalizeError(f"unsupported Metal literal {value!r}")


def _fill_literal(dtype: str, value: Any) -> str:
    try:
        converted = coerce_pod_scalar(dtype, value)
    except KernelScalarError as exc:
        raise MetalFinalizeError(f"invalid {dtype} fill literal: {exc}") from exc
    ctype = _metal_scalar_type(dtype)
    if dtype == "bool":
        return "true" if converted else "false"
    return f"{ctype}({_literal(converted)})"


def _indexed_expr_to_metal(expr: Any) -> str:
    if not isinstance(expr, dict):
        raise MetalFinalizeError(f"indexed expression must be a record, got {expr!r}")
    kind = expr.get("kind")
    if kind == "name":
        name = expr.get("name")
        if not isinstance(name, str):
            raise MetalFinalizeError(f"indexed name expression is invalid: {expr!r}")
        return name
    if kind == "literal":
        return _literal(expr.get("value"))
    if kind == "thread_id_x":
        return "gid"
    if kind == "load":
        buffer = expr.get("buffer")
        if not isinstance(buffer, str):
            raise MetalFinalizeError(f"indexed load buffer is invalid: {expr!r}")
        return f"{buffer}[{_indexed_expr_to_metal(expr.get('index'))}]"
    if kind == "binary":
        symbols = {"add": "+", "sub": "-", "mul": "*", "div": "/"}
        op = expr.get("op")
        if op not in symbols:
            raise MetalFinalizeError(f"unsupported indexed binary op {op!r}")
        return (
            f"({_indexed_expr_to_metal(expr.get('left'))} {symbols[op]} "
            f"{_indexed_expr_to_metal(expr.get('right'))})"
        )
    if kind == "compare":
        symbols = {"lt": "<", "le": "<=", "gt": ">", "ge": ">=", "eq": "==", "ne": "!="}
        op = expr.get("op")
        if op not in symbols:
            raise MetalFinalizeError(f"unsupported indexed compare op {op!r}")
        return (
            f"({_indexed_expr_to_metal(expr.get('left'))} {symbols[op]} "
            f"{_indexed_expr_to_metal(expr.get('right'))})"
        )
    raise MetalFinalizeError(f"unsupported indexed expression kind {kind!r}")


def _validate_gemm_policy_metadata(*, entry: str, policy: object) -> None:
    if policy is None:
        return
    if isinstance(policy, str) and policy.startswith("GemmWarpPolicy."):
        return
    if isinstance(policy, (list, tuple)) and len(policy) == 2:
        rows, cols = policy
        if (
            not isinstance(rows, bool)
            and not isinstance(cols, bool)
            and isinstance(rows, int)
            and isinstance(cols, int)
            and rows > 0
            and cols > 0
        ):
            return
    raise MetalFinalizeError(
        f"{entry}: unsupported T.gemm policy metadata {policy!r}; "
        "policy is metadata-only for scalar Metal GEMM source"
    )


def _shape_element_count(shape: list[Any], *, name: str) -> int:
    if not shape:
        raise MetalFinalizeError(f"local buffer {name!r} has empty shape")
    count = 1
    for dim in shape:
        if not isinstance(dim, int) or dim <= 0:
            raise MetalFinalizeError(f"local buffer {name!r} has bad shape {shape!r}")
        count *= dim
    return count


def _func_threads(func: dict[str, Any]) -> int:
    threads = func.get("threads")
    if not isinstance(threads, int) or threads <= 0:
        raise MetalFinalizeError(f"plain-TIR func {func!r} has bad thread count {threads!r}")
    return threads


def _local_records(func: dict[str, Any]) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for local in func.get("locals", []):
        if not isinstance(local, dict):
            raise MetalFinalizeError(f"unsupported local record {local!r}")
        name = local.get("name")
        if not isinstance(name, str):
            raise MetalFinalizeError(f"bad local record {local!r}")
        records[name] = local
    return records


def _local_declarations(func: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    declarations: list[str] = []
    scopes: dict[str, str] = {}
    for local in func.get("locals", []):
        if local.get("kind") != "local_buffer":
            raise MetalFinalizeError(f"unsupported local record {local!r}")
        name = local.get("name")
        dtype = local.get("dtype")
        scope = local.get("scope")
        shape = local.get("shape")
        if not isinstance(name, str) or not isinstance(dtype, str) or not isinstance(scope, str):
            raise MetalFinalizeError(f"bad local record {local!r}")
        if not isinstance(shape, list):
            raise MetalFinalizeError(f"local buffer {name!r} has bad shape {shape!r}")
        ctype = _metal_scalar_type(dtype)
        count = _shape_element_count(shape, name=name)
        if scope == MemoryScope.SHARED.value:
            declarations.append(f"    threadgroup {ctype} {name}[{count}];")
        elif scope in {MemoryScope.LOCAL.value, MemoryScope.FRAGMENT.value}:
            declarations.append(f"    thread {ctype} {name}[{count}];")
        else:
            raise MetalFinalizeError(
                f"local buffer {name!r}: unsupported Metal scope {scope!r}"
            )
        scopes[name] = scope
    return declarations, scopes


def _requires_threadgroup_position(func: dict[str, Any]) -> bool:
    reduced_locals: set[str] = set()
    for op in func.get("ops", []):
        tir_op = op.get("tir_op")
        args = list(op.get("args", []))
        if tir_op == "tir.reduce_loop" and len(args) >= 2:
            reduced_locals.add(str(args[1]))
        elif tir_op == "tir.copy_loop" and len(args) >= 2:
            src, dst = str(args[0]), str(args[1])
            if src in reduced_locals and dst not in reduced_locals:
                return True
    return False


def _param_scopes(func: dict[str, Any]) -> dict[str, str]:
    scopes: dict[str, str] = {}
    for param in _func_param_dicts(func):
        if param.get("kind") == "buffer":
            name = param.get("name")
            scope = param.get("scope")
            if isinstance(name, str) and isinstance(scope, str):
                scopes[name] = scope
    return scopes


def _index_for(name: str, scopes: dict[str, str]) -> str:
    scope = scopes.get(name)
    if scope in {MemoryScope.SHARED.value, MemoryScope.LOCAL.value, MemoryScope.FRAGMENT.value}:
        return "tid"
    return "gid"


def _has_threadgroup_sync(func: dict[str, Any]) -> bool:
    return any(
        op.get("tir_op") in {"tir.barrier", "tir.reduce_loop"}
        for op in func.get("ops", [])
    )


def _zero_literal_for_dtype(dtype: str) -> str:
    if dtype in {"f16", "f32", "f64"}:
        return "0.0"
    if dtype == "bool":
        return "false"
    return "0"


def _zero_value_for_storage(dtype: str) -> str:
    ctype = _metal_scalar_type(dtype)
    if ctype == "half":
        return "half(0.0)"
    if ctype == "float":
        return "0.0"
    if ctype == "double":
        return "0.0"
    if ctype == "bool":
        return "false"
    return f"{ctype}(0)"


def _emit_copy_statement(
    *,
    src: str,
    dst: str,
    scopes: dict[str, str],
    extent: int | None,
) -> list[str]:
    src_index = _index_for(src, scopes)
    dst_index = _index_for(dst, scopes)
    stmt = f"{dst}[{dst_index}] = {src}[{src_index}];"
    if extent is not None and scopes.get(dst) == MemoryScope.GLOBAL.value:
        return [f"    if (gid < {extent}u) {{", f"        {stmt}", "    }"]
    return [f"    {stmt}"]


def _emit_fill_statement(
    *,
    dst: str,
    value: str,
    scopes: dict[str, str],
    extent: int | None,
) -> list[str]:
    dst_index = _index_for(dst, scopes)
    stmt = f"{dst}[{dst_index}] = {value};"
    if extent is not None and scopes.get(dst) == MemoryScope.GLOBAL.value:
        return [f"    if (gid < {extent}u) {{", f"        {stmt}", "    }"]
    return [f"    {stmt}"]


def _emit_reduce_sum(
    *,
    entry: str,
    src: str,
    dst: str,
    attrs: dict[str, Any],
    func: dict[str, Any],
    local_records: dict[str, dict[str, Any]],
    scopes: dict[str, str],
    extent: int | None,
) -> list[str]:
    reduction = attrs.get("reduction", "sum")
    if reduction != "sum":
        raise MetalFinalizeError(
            f"{entry}: only sum reduction has real Metal source lowering in this slice"
        )
    if scopes.get(src) != MemoryScope.GLOBAL.value:
        raise MetalFinalizeError(f"{entry}: reduction source {src!r} must be a global buffer")
    if scopes.get(dst) != MemoryScope.SHARED.value:
        raise MetalFinalizeError(
            f"{entry}: reduction accumulator {dst!r} must be a threadgroup local buffer"
        )
    local = local_records.get(dst)
    if local is None:
        raise MetalFinalizeError(f"{entry}: reduction accumulator {dst!r} is not a local buffer")
    shape = local.get("shape")
    dtype = local.get("dtype")
    if not isinstance(shape, list) or not isinstance(dtype, str):
        raise MetalFinalizeError(f"{entry}: bad reduction accumulator local {local!r}")
    local_count = _shape_element_count(shape, name=dst)
    threads = _func_threads(func)
    if local_count < threads:
        raise MetalFinalizeError(
            f"{entry}: reduction accumulator {dst!r} has {local_count} slots for "
            f"{threads} threads"
        )
    import_kind = attrs.get("import_kind")
    if import_kind is not None:
        if import_kind != "tilelang.reduce_sum.static_row.v1":
            raise MetalFinalizeError(
                f"{entry}: unknown imported reduction contract {import_kind!r}"
            )
        source_record = _record_by_name(
            _func_param_dicts(func),
            src,
            entry=entry,
        )
        source_shape = source_record.get("shape")
        source_dtype = source_record.get("dtype")
        if (
            not isinstance(source_shape, list)
            or len(source_shape) != 2
            or not all(isinstance(dim, int) and dim > 0 for dim in source_shape)
        ):
            raise MetalFinalizeError(
                f"{entry}: imported row reduce_sum source must have static rank-2 shape"
            )
        rows, width = source_shape
        if source_dtype not in {"f16", "f32"} or dtype != "f32":
            raise MetalFinalizeError(
                f"{entry}: imported row reduce_sum requires f16/f32 source and f32 scratch"
            )
        expected = {
            "reduction": "sum",
            "dim": 1,
            "clear": True,
            "batch": 1,
            "extent": rows * width,
            "row_count": rows,
            "row_width": width,
            "source_shape": [rows, width],
            "output_shape": [rows, 1],
            "import_kind": "tilelang.reduce_sum.static_row.v1",
        }
        if attrs != expected:
            raise MetalFinalizeError(
                f"{entry}: imported row reduce_sum metadata drifted from {expected!r}; "
                f"got {attrs!r}"
            )
        if func.get("grid") != [rows] or threads != width or local_count != width:
            raise MetalFinalizeError(
                f"{entry}: imported row reduce_sum launch/scratch no longer matches "
                f"rows={rows}, width={width}"
            )
    zero = _zero_literal_for_dtype(dtype)
    load_expr = f"{src}[gid]"
    if extent is not None:
        load_expr = f"(gid < {extent}u) ? {src}[gid] : {zero}"
    return [
        f"    {dst}[tid] = {load_expr};",
        "    threadgroup_barrier(mem_flags::mem_threadgroup);",
        f"    for (uint active = {threads}u; active > 1u; active = (active + 1u) >> 1u) {{",
        "        uint partner = tid + ((active + 1u) >> 1u);",
        "        if (tid < (active >> 1u) && partner < active) {",
        f"            {dst}[tid] += {dst}[partner];",
        "        }",
        "        threadgroup_barrier(mem_flags::mem_threadgroup);",
        "    }",
    ]


def _record_by_name(records: list[dict[str, Any]], name: str, *, entry: str) -> dict[str, Any]:
    for record in records:
        if record.get("name") == name:
            return record
    raise MetalFinalizeError(f"{entry}: symbol {name!r} is not present in this function")


def _shape_from_record(record: dict[str, Any], *, entry: str, name: str) -> list[int]:
    shape = record.get("shape")
    if not isinstance(shape, list) or not shape:
        raise MetalFinalizeError(
            f"{entry}: GEMM source lowering requires static shape metadata for {name!r}"
        )
    dims: list[int] = []
    for dim in shape:
        if not isinstance(dim, int) or dim <= 0:
            raise MetalFinalizeError(f"{entry}: bad static shape for {name!r}: {shape!r}")
        dims.append(dim)
    return dims


def _require_dtype(record: dict[str, Any], *, entry: str, name: str) -> str:
    dtype = record.get("dtype")
    if not isinstance(dtype, str):
        raise MetalFinalizeError(f"{entry}: symbol {name!r} has bad dtype {dtype!r}")
    _metal_scalar_type(dtype)
    return dtype


def _u(value: int) -> str:
    if value < 0:
        raise MetalFinalizeError(f"negative unsigned literal {value}")
    return f"{value}u"


def _ko_increment(step: int) -> str:
    return "++ko" if step == 1 else f"ko += {_u(step)}"


def _ceil_div(a: int, b: int) -> int:
    return (a + b - 1) // b


def _gemm_k_loop_range(*, entry: str, attrs: dict[str, Any], total_k_tiles: int) -> tuple[int, int, int]:
    has_pipeline = "pipeline_extent" in attrs
    has_serial = "serial_extent" in attrs
    if has_pipeline and has_serial:
        raise MetalFinalizeError(f"{entry}: GEMM cannot carry both pipeline_extent and serial_extent")
    if has_pipeline:
        extent = attrs.get("pipeline_extent")
        start = attrs.get("pipeline_start", 0)
        unexpected_start = attrs.get("serial_start")
        unexpected_step = attrs.get("serial_step")
        step = attrs.get("pipeline_step", 1)
        label = "pipeline"
        extent_key = "pipeline_extent"
    elif has_serial:
        extent = attrs.get("serial_extent")
        start = attrs.get("serial_start", 0)
        unexpected_start = attrs.get("pipeline_start")
        unexpected_step = attrs.get("pipeline_step")
        step = attrs.get("serial_step", 1)
        label = "serial"
        extent_key = "serial_extent"
    else:
        extent = total_k_tiles
        start = 0
        unexpected_start = None
        unexpected_step = attrs.get("serial_step") or attrs.get("pipeline_step")
        step = 1
        label = "implicit"
        extent_key = "implicit_extent"
    if unexpected_start is not None:
        raise MetalFinalizeError(f"{entry}: GEMM loop start metadata does not match loop kind")
    if unexpected_step is not None:
        raise MetalFinalizeError(f"{entry}: GEMM loop step metadata does not match loop kind")
    if not isinstance(start, int) or start < 0:
        raise MetalFinalizeError(f"{entry}: GEMM {label}_start must be a non-negative integer")
    if not isinstance(extent, int) or extent <= 0:
        raise MetalFinalizeError(f"{entry}: GEMM {label}_extent must be a positive integer")
    if isinstance(step, bool) or not isinstance(step, int) or step <= 0:
        raise MetalFinalizeError(f"{entry}: GEMM {label}_step must be a positive integer")
    if start + extent > total_k_tiles:
        raise MetalFinalizeError(
            f"{entry}: GEMM {extent_key} range start={start} extent={extent} exceeds "
            f"ceildiv(K, block_K) {total_k_tiles}"
        )
    return start, extent, step


def _split_k_span_from_copy_attrs(
    *,
    entry: str,
    copy_attrs: dict[str, Any],
    k: int,
    split_k: int,
) -> tuple[int, bool]:
    modes: set[str] = set()
    spans: set[int] = set()
    for key in ("a_copy", "b_copy"):
        attrs = copy_attrs.get(key)
        if not isinstance(attrs, dict):
            continue
        mode = attrs.get("split_k_span_mode")
        span = attrs.get("split_k_span")
        if mode is None and span is None:
            continue
        if mode not in {"floor_div", "ceildiv"} or not isinstance(span, int) or span <= 0:
            raise MetalFinalizeError(f"{entry}: split-k copy span metadata is malformed")
        modes.add(mode)
        spans.add(span)
    if len(modes) > 1 or len(spans) > 1:
        raise MetalFinalizeError(f"{entry}: split-k A/B copy span metadata must match")
    if not modes:
        if k % split_k != 0:
            raise MetalFinalizeError(
                f"{entry}: split-k atomic GEMM without explicit ceildiv copy span requires "
                f"K divisible by split_k, got K={k}, split_k={split_k}"
            )
        return k // split_k, False
    mode = next(iter(modes))
    span = next(iter(spans))
    if mode == "ceildiv":
        expected = _ceil_div(k, split_k)
        if span != expected:
            raise MetalFinalizeError(
                f"{entry}: split-k ceildiv span {span} does not match ceildiv(K, split_k) {expected}"
            )
        return span, True
    expected_floor = k // split_k
    if span != expected_floor:
        raise MetalFinalizeError(
            f"{entry}: split-k floor-div span {span} does not match K // split_k {expected_floor}"
        )
    if k % split_k != 0:
        raise MetalFinalizeError(
            f"{entry}: split-k atomic GEMM with floor-div copy span requires "
            f"K divisible by split_k, got K={k}, split_k={split_k}"
        )
    return span, False


def _gemm_source_pattern(
    *,
    entry: str,
    params: list[dict[str, Any]],
    func: dict[str, Any],
    local_records: dict[str, dict[str, Any]],
    scopes: dict[str, str],
) -> dict[str, Any]:
    ops = func.get("ops", [])
    if not isinstance(ops, list):
        raise MetalFinalizeError(f"{entry}: bad ops list")
    gemm_ops = [op for op in ops if op.get("tir_op") == "tir.gemm_expand"]
    if len(gemm_ops) != 1:
        raise MetalFinalizeError(
            f"{entry}: GEMM Metal source lowering requires exactly one gemm op"
        )
    gemm = gemm_ops[0]
    gemm_args = list(gemm.get("args", []))
    if len(gemm_args) < 3:
        raise MetalFinalizeError(f"{entry}: GEMM op must have A, B, and C operands")
    a_shared, b_shared, c_local = (str(gemm_args[0]), str(gemm_args[1]), str(gemm_args[2]))
    gemm_attrs = gemm.get("attrs") or {}
    if gemm_attrs.get("transpose_A") not in (None, False, True):
        raise MetalFinalizeError(f"{entry}: GEMM transpose_A must be boolean")
    if gemm_attrs.get("transpose_B") not in (None, False, True):
        raise MetalFinalizeError(f"{entry}: GEMM transpose_B must be boolean")
    _validate_gemm_policy_metadata(entry=entry, policy=gemm_attrs.get("policy"))

    a_global = b_global = c_global = None
    c_staging = None
    a_copy_attrs: dict[str, Any] | None = None
    b_copy_attrs: dict[str, Any] | None = None
    c_staging_copy_attrs: dict[str, Any] | None = None
    c_staging_output_attrs: dict[str, Any] | None = None
    swizzle_attrs: dict[str, Any] | None = None
    output_mode: str | None = None
    fill_seen = False
    for op in ops:
        tir_op = op.get("tir_op")
        args = list(op.get("args", []))
        attrs = op.get("attrs") or {}
        if tir_op == "tir.fill_loop" and args and str(args[0]) == c_local:
            if "parallel_extents" in attrs or "vectorized_extent" in attrs:
                raise MetalFinalizeError(
                    f"{entry}: Metal GEMM source supports T.Parallel/T.vectorized only on "
                    "A/B/C tile copy staging"
                )
            if attrs.get("value", 0) != 0:
                raise MetalFinalizeError(f"{entry}: GEMM accumulator fill must be zero")
            fill_seen = True
        elif tir_op == "tir.copy_loop" and len(args) >= 2:
            src, dst = str(args[0]), str(args[1])
            if dst == a_shared:
                a_global = src
                a_copy_attrs = attrs
            elif dst == b_shared:
                b_global = src
                b_copy_attrs = attrs
            elif src == c_local:
                if dst in local_records:
                    c_staging = dst
                    c_staging_copy_attrs = attrs
                else:
                    c_global = dst
                output_mode = "copy"
            elif c_staging is not None and src == c_staging:
                c_global = dst
                c_staging_output_attrs = attrs
                output_mode = "copy"
        elif tir_op == "tir.atomic_add" and len(args) >= 2:
            dst, src = str(args[0]), str(args[1])
            if src == c_local:
                c_global = dst
                output_mode = "atomic_add"
        elif tir_op in {"tir.gemm_expand"}:
            if "parallel_extents" in attrs or "vectorized_extent" in attrs:
                raise MetalFinalizeError(
                    f"{entry}: Metal GEMM source supports T.Parallel/T.vectorized only on "
                    "A/B/C tile copy staging"
                )
            continue
        elif tir_op == "tir.use_swizzle":
            validated = _validate_swizzle_metadata(entry=entry, op=op)
            if validated.get("enable", True):
                if swizzle_attrs is not None and swizzle_attrs != validated:
                    raise MetalFinalizeError(f"{entry}: multiple enabled T.use_swizzle annotations conflict")
                swizzle_attrs = validated
            continue
        elif tir_op == "tir.annotate_layout":
            _validate_layout_annotation_noop(entry=entry, op=op)
            continue
        else:
            raise MetalFinalizeError(
                f"{entry}: unsupported op {tir_op!r} in GEMM source pattern"
            )
    if not fill_seen:
        raise MetalFinalizeError(f"{entry}: GEMM source lowering requires a zero fill")
    if a_global is None or b_global is None or c_global is None:
        raise MetalFinalizeError(
            f"{entry}: GEMM source lowering requires global->shared A/B copies "
            "and a fragment->global C copy or atomic add"
        )
    if output_mode is None:
        raise MetalFinalizeError(f"{entry}: GEMM source lowering requires a C output op")

    param_map = {p.get("name"): p for p in params if isinstance(p.get("name"), str)}
    for name in (a_global, b_global, c_global):
        record = param_map.get(name)
        if record is None or scopes.get(name) != MemoryScope.GLOBAL.value:
            raise MetalFinalizeError(f"{entry}: GEMM operand {name!r} must be a global param")
    for name in (a_shared, b_shared, c_local):
        if name not in local_records:
            raise MetalFinalizeError(f"{entry}: GEMM operand {name!r} must be a local buffer")
    if c_staging is not None and c_staging not in local_records:
        raise MetalFinalizeError(f"{entry}: GEMM staged C output {c_staging!r} must be a local buffer")

    a_param = param_map[a_global]
    b_param = param_map[b_global]
    c_param = param_map[c_global]
    a_local = local_records[a_shared]
    b_local = local_records[b_shared]
    c_local_record = local_records[c_local]
    c_staging_record = local_records[c_staging] if c_staging is not None else None

    a_dtype = _require_dtype(a_param, entry=entry, name=a_global)
    b_dtype = _require_dtype(b_param, entry=entry, name=b_global)
    c_dtype = _require_dtype(c_param, entry=entry, name=c_global)
    c_local_dtype = _require_dtype(c_local_record, entry=entry, name=c_local)
    c_staging_dtype = (
        _require_dtype(c_staging_record, entry=entry, name=c_staging)
        if c_staging_record is not None
        else None
    )

    a_shape = _shape_from_record(a_param, entry=entry, name=a_global)
    b_shape = _shape_from_record(b_param, entry=entry, name=b_global)
    c_shape = _shape_from_record(c_param, entry=entry, name=c_global)
    a_tile = _shape_from_record(a_local, entry=entry, name=a_shared)
    b_tile = _shape_from_record(b_local, entry=entry, name=b_shared)
    c_tile = _shape_from_record(c_local_record, entry=entry, name=c_local)
    c_staging_tile = (
        _shape_from_record(c_staging_record, entry=entry, name=c_staging)
        if c_staging_record is not None
        else None
    )
    if len(a_shape) != 2 or len(b_shape) != 2 or len(c_shape) != 2:
        raise MetalFinalizeError(f"{entry}: GEMM source requires rank-2 A/B/C tensors")
    if len(a_tile) != 2 or len(b_tile) != 2 or len(c_tile) != 2:
        raise MetalFinalizeError(f"{entry}: GEMM source requires rank-2 tile locals")
    if c_staging_tile is not None:
        if len(c_staging_tile) != 2:
            raise MetalFinalizeError(f"{entry}: GEMM staged C output requires a rank-2 tile local")
        if c_staging_tile != c_tile:
            raise MetalFinalizeError(f"{entry}: GEMM staged C output tile shape must match C_local")
        if local_records[c_staging].get("scope") != MemoryScope.SHARED.value:
            raise MetalFinalizeError(f"{entry}: {c_staging!r} must be threadgroup/shared")
        if c_staging_dtype != c_dtype or c_local_dtype not in {c_dtype, "f32"}:
            raise MetalFinalizeError(
                f"{entry}: GEMM staged C output currently requires C_shared/C dtype "
                "to match and C_local to be matching or f32 accumulator"
            )

    transpose_a = bool(gemm_attrs.get("transpose_A", False))
    transpose_b = bool(gemm_attrs.get("transpose_B", False))
    c_m, c_n = c_shape
    c_block_m, c_block_n = c_tile
    m, n = c_m, c_n
    if transpose_a:
        a_k, a_m = a_shape
        a_block_k, block_m = a_tile
        if a_m != m:
            raise MetalFinalizeError(f"{entry}: transpose_A expects A(K,M)")
        k = a_k
        block_k = a_block_k
    else:
        a_m, a_k = a_shape
        block_m, block_k = a_tile
        if a_m != m:
            raise MetalFinalizeError(
                f"{entry}: GEMM tensor shapes must be A(M,K), B(K,N), C(M,N)"
            )
        k = a_k
    if transpose_b:
        b_n, b_k = b_shape
        b_block_n, b_block_k = b_tile
        if b_n != n or b_k != k:
            raise MetalFinalizeError(f"{entry}: transpose_B expects B(N,K)")
        block_n = b_block_n
        if b_block_k != block_k:
            raise MetalFinalizeError(
                f"{entry}: GEMM local shapes must be consistent with transpose flags"
            )
    else:
        b_k, b_n = b_shape
        b_block_k, block_n = b_tile
        if b_k != k or b_n != n:
            raise MetalFinalizeError(
                f"{entry}: GEMM tensor shapes must be A(M,K), B(K,N), C(M,N)"
            )
        if b_block_k != block_k:
            raise MetalFinalizeError(
                f"{entry}: GEMM local shapes must be consistent with transpose flags"
            )
    if c_block_m != block_m or c_block_n != block_n:
        raise MetalFinalizeError(
            f"{entry}: GEMM local shapes must be A_shared(M,K), "
            "B_shared(K,N), C_local(M,N), adjusted for transpose flags"
        )
    if local_records[a_shared].get("scope") != MemoryScope.SHARED.value:
        raise MetalFinalizeError(f"{entry}: {a_shared!r} must be threadgroup/shared")
    if local_records[b_shared].get("scope") != MemoryScope.SHARED.value:
        raise MetalFinalizeError(f"{entry}: {b_shared!r} must be threadgroup/shared")
    if local_records[c_local].get("scope") not in {MemoryScope.FRAGMENT.value, MemoryScope.LOCAL.value}:
        raise MetalFinalizeError(f"{entry}: {c_local!r} must be fragment/local")

    grid = func.get("grid")
    expected_grid = [_ceil_div(n, block_n), _ceil_div(m, block_m)]
    if not isinstance(grid, list) or len(grid) < 2 or grid[:2] != expected_grid:
        raise MetalFinalizeError(
            f"{entry}: GEMM grid {grid!r} does not match expected {expected_grid}"
        )
    split_k = 1
    k_span = k
    split_k_tail_safe = False
    if output_mode == "atomic_add":
        if len(grid) < 3 or not isinstance(grid[2], int) or grid[2] <= 0:
            raise MetalFinalizeError(f"{entry}: split-k atomic GEMM requires a positive 3-D grid z")
        split_k = grid[2]
        copy_attrs: dict[str, Any] = {}
        if a_copy_attrs is not None:
            copy_attrs["a_copy"] = dict(a_copy_attrs)
        if b_copy_attrs is not None:
            copy_attrs["b_copy"] = dict(b_copy_attrs)
        k_span, split_k_tail_safe = _split_k_span_from_copy_attrs(
            entry=entry,
            copy_attrs=copy_attrs,
            k=k,
            split_k=split_k,
        )

    total_k_tiles = _ceil_div(k_span, block_k)
    pipeline_start, pipeline_extent, pipeline_step = _gemm_k_loop_range(
        entry=entry,
        attrs=gemm_attrs,
        total_k_tiles=total_k_tiles,
    )
    _validate_scheduled_tile_copy_metadata(
        entry=entry,
        func=func,
        a_global=a_global,
        b_global=b_global,
        c_global=c_global,
        c_staging=c_staging,
        a_shared=a_shared,
        b_shared=b_shared,
        c_local=c_local,
        a_tile=a_tile,
        b_tile=b_tile,
        c_tile=c_tile,
    )

    return {
        "a_shared": a_shared,
        "b_shared": b_shared,
        "c_local": c_local,
        "a_global": a_global,
        "b_global": b_global,
        "c_global": c_global,
        "c_staging": c_staging,
        "a_dtype": a_dtype,
        "b_dtype": b_dtype,
        "c_dtype": c_dtype,
        "a_shape": a_shape,
        "b_shape": b_shape,
        "c_shape": c_shape,
        "a_tile": a_tile,
        "b_tile": b_tile,
        "c_tile": c_tile,
        "m": m,
        "n": n,
        "k": k,
        "block_m": block_m,
        "block_n": block_n,
        "block_k": block_k,
        "threads": _func_threads(func),
        "pipeline_start": pipeline_start,
        "pipeline_extent": pipeline_extent,
        "pipeline_step": pipeline_step,
        "transpose_A": transpose_a,
        "transpose_B": transpose_b,
        "output_mode": output_mode,
        "split_k": split_k,
        "k_span": k_span,
        "split_k_tail_safe": split_k_tail_safe,
        "swizzle_attrs": swizzle_attrs,
    }


def _validate_swizzle_metadata(*, entry: str, op: dict[str, Any]) -> dict[str, Any]:
    attrs = op.get("attrs") or {}
    if not isinstance(attrs, dict):
        raise MetalFinalizeError(f"{entry}: T.use_swizzle attrs must be a dict")
    enable = attrs.get("enable", True)
    if not isinstance(enable, bool):
        raise MetalFinalizeError(f"{entry}: T.use_swizzle enable must be boolean")
    panel_size = attrs.get("panel_size")
    if panel_size is not None and (not isinstance(panel_size, int) or panel_size <= 0):
        raise MetalFinalizeError(f"{entry}: T.use_swizzle panel_size must be a positive integer")
    if enable:
        if panel_size is None:
            raise MetalFinalizeError(f"{entry}: T.use_swizzle enable=True requires panel_size")
        order = attrs.get("order", "row")
        if order not in {"row", "col"}:
            raise MetalFinalizeError(f"{entry}: T.use_swizzle order must be 'row' or 'col'")
    return attrs


def _validate_layout_annotation_noop(*, entry: str, op: dict[str, Any]) -> None:
    attrs = op.get("attrs") or {}
    if not isinstance(attrs, dict):
        raise MetalFinalizeError(f"{entry}: T.annotate_layout attrs must be a dict")
    if attrs.get("entries") != 0 or len(attrs) != 1:
        raise MetalFinalizeError(
            f"{entry}: Metal GEMM source supports T.annotate_layout only for an empty no-op annotation"
        )


def _dtype_bits(dtype: str, *, entry: str, name: str) -> int:
    if dtype == "f16":
        return 16
    if dtype == "f32":
        return 32
    raise MetalFinalizeError(
        f"{entry}: swizzled layout for {name!r} has unsupported dtype {dtype!r}"
    )


def _metal_local_index_expr(
    *,
    entry: str,
    local_records: dict[str, dict[str, Any]],
    name: str,
    row: str,
    col: str,
) -> str:
    record = local_records[name]
    shape = _shape_from_record(record, entry=entry, name=name)
    if len(shape) != 2:
        raise MetalFinalizeError(f"{entry}: {name!r} local layout indexing requires rank-2 shape")
    rows, cols = shape
    if record.get("layout") != "swizzled":
        return f"(({row} * {_u(cols)}) + {col})"
    if record.get("scope") != MemoryScope.SHARED.value:
        raise MetalFinalizeError(f"{entry}: swizzled layout for {name!r} requires shared memory")

    dtype = _require_dtype(record, entry=entry, name=name)
    element_bits = _dtype_bits(dtype, entry=entry, name=name)
    vector_size = 128 // element_bits
    if cols % (vector_size * 8) == 0:
        if rows != 4 and rows % 8 != 0:
            raise MetalFinalizeError(
                f"{entry}: swizzled shared-memory layout for {name!r} requires rows==4 or rows%8==0"
            )
        groups = 8
        swizzle_term = f"(({col} / {_u(vector_size)}) % 8u) ^ ({row} % 8u)"
    elif cols % (vector_size * 4) == 0:
        if rows != 4 and rows % 8 != 0:
            raise MetalFinalizeError(
                f"{entry}: swizzled shared-memory layout for {name!r} requires rows==4 or rows%8==0"
            )
        groups = 4
        swizzle_term = f"(({col} / {_u(vector_size)}) % 4u) ^ (({row} % 8u) / 2u)"
    elif cols % (vector_size * 2) == 0:
        if rows != 4 and rows % 8 != 0:
            raise MetalFinalizeError(
                f"{entry}: swizzled shared-memory layout for {name!r} requires rows==4 or rows%8==0"
            )
        groups = 2
        swizzle_term = f"(({col} / {_u(vector_size)}) % 2u) ^ (({row} % 8u) / 4u)"
    else:
        padded = cols
        if (element_bits * cols) % 256 == 0:
            padded += vector_size
        return f"(({row} * {_u(padded)}) + {col})"

    ts_extent = 1 if rows == 4 else rows // 8
    index_extent = 8 * groups * vector_size
    tc = f"(({col} / {_u(vector_size)}) / {_u(groups)})"
    ts = f"({row} / 8u)"
    vec = f"({col} % {_u(vector_size)})"
    index = f"({vec} + (({swizzle_term}) + (({row} % 8u) * {_u(groups)})) * {_u(vector_size)})"
    return f"((({tc} * {_u(ts_extent)} + {ts}) * {_u(index_extent)}) + {index})"


def _metal_local_physical_elems(
    *,
    entry: str,
    local_records: dict[str, dict[str, Any]],
    name: str,
) -> int:
    record = local_records[name]
    shape = _shape_from_record(record, entry=entry, name=name)
    if len(shape) != 2:
        raise MetalFinalizeError(f"{entry}: {name!r} local layout allocation requires rank-2 shape")
    rows, cols = shape
    if record.get("layout") != "swizzled":
        return rows * cols
    if record.get("scope") != MemoryScope.SHARED.value:
        raise MetalFinalizeError(f"{entry}: swizzled layout for {name!r} requires shared memory")

    dtype = _require_dtype(record, entry=entry, name=name)
    element_bits = _dtype_bits(dtype, entry=entry, name=name)
    vector_size = 128 // element_bits
    if (
        cols % (vector_size * 8) == 0
        or cols % (vector_size * 4) == 0
        or cols % (vector_size * 2) == 0
    ):
        return rows * cols

    padded = cols
    if (element_bits * cols) % 256 == 0:
        padded += vector_size
    return rows * padded


def _metal_swizzle_tile_id_lines(
    *,
    entry: str,
    swizzle_attrs: dict[str, Any] | None,
    grid_x: int,
    grid_y: int,
) -> tuple[list[str], str, str]:
    if swizzle_attrs is None:
        return [], "tgid.x", "tgid.y"
    order = swizzle_attrs.get("order", "row")
    panel_width = swizzle_attrs.get("panel_size")
    if order not in {"row", "col"} or not isinstance(panel_width, int) or panel_width <= 0:
        raise MetalFinalizeError(f"{entry}: bad enabled T.use_swizzle metadata")
    lines = [
        f"    uint swizzle_grid_x = {_u(grid_x)};",
        f"    uint swizzle_grid_y = {_u(grid_y)};",
        "    uint swizzle_block_idx = tgid.x + (tgid.y * swizzle_grid_x);",
        "    uint swizzle_grid_size = swizzle_grid_x * swizzle_grid_y;",
    ]
    if order == "row":
        lines.extend(
            [
                f"    uint swizzle_panel_size = {_u(panel_width)} * swizzle_grid_x;",
                "    uint swizzle_panel_offset = swizzle_block_idx % swizzle_panel_size;",
                "    uint swizzle_panel_idx = swizzle_block_idx / swizzle_panel_size;",
                "    uint swizzle_total_panel = (swizzle_grid_size + swizzle_panel_size - 1u) / swizzle_panel_size;",
                f"    uint swizzle_stride = (swizzle_panel_idx + 1u < swizzle_total_panel) ? {_u(panel_width)} : ((swizzle_grid_size - (swizzle_panel_idx * swizzle_panel_size)) / swizzle_grid_x);",
                "    uint tile_gid_x = (swizzle_panel_idx & 1u) ? (swizzle_grid_x - 1u - (swizzle_panel_offset / swizzle_stride)) : (swizzle_panel_offset / swizzle_stride);",
                f"    uint tile_gid_y = (swizzle_panel_offset % swizzle_stride) + (swizzle_panel_idx * {_u(panel_width)});",
            ]
        )
    else:
        lines.extend(
            [
                f"    uint swizzle_panel_size = {_u(panel_width)} * swizzle_grid_y;",
                "    uint swizzle_panel_offset = swizzle_block_idx % swizzle_panel_size;",
                "    uint swizzle_panel_idx = swizzle_block_idx / swizzle_panel_size;",
                "    uint swizzle_total_panel = (swizzle_grid_size + swizzle_panel_size - 1u) / swizzle_panel_size;",
                f"    uint swizzle_stride = (swizzle_panel_idx + 1u < swizzle_total_panel) ? {_u(panel_width)} : ((swizzle_grid_size - (swizzle_panel_idx * swizzle_panel_size)) / swizzle_grid_y);",
                "    uint tile_gid_y = (swizzle_panel_idx & 1u) ? (swizzle_grid_y - 1u - (swizzle_panel_offset / swizzle_stride)) : (swizzle_panel_offset / swizzle_stride);",
                f"    uint tile_gid_x = (swizzle_panel_offset % swizzle_stride) + (swizzle_panel_idx * {_u(panel_width)});",
            ]
        )
    return lines, "tile_gid_x", "tile_gid_y"


def _validate_scheduled_tile_copy_metadata(
    *,
    entry: str,
    func: dict[str, Any],
    a_global: str,
    b_global: str,
    c_global: str,
    c_staging: str | None,
    a_shared: str,
    b_shared: str,
    c_local: str,
    a_tile: tuple[int, ...],
    b_tile: tuple[int, ...],
    c_tile: tuple[int, ...],
) -> None:
    ops = func.get("ops")
    if not isinstance(ops, list):
        raise MetalFinalizeError(f"{entry}: bad ops list")
    allowed = {
        (a_global, a_shared): list(a_tile),
        (b_global, b_shared): list(b_tile),
        (c_local, c_global): list(c_tile),
        (c_global, c_local): list(c_tile),
    }
    if c_staging is not None:
        allowed[(c_local, c_staging)] = list(c_tile)
        allowed[(c_staging, c_global)] = list(c_tile)
    for op in ops:
        if not isinstance(op, dict):
            raise MetalFinalizeError(f"{entry}: bad op record {op!r}")
        attrs = op.get("attrs") or {}
        if not isinstance(attrs, dict):
            raise MetalFinalizeError(f"{entry}: op attrs must be a dict")
        if "parallel_extents" not in attrs and "vectorized_extent" not in attrs:
            continue
        tir_op = op.get("tir_op")
        if tir_op not in {"tir.copy_loop", "tir.atomic_add"}:
            raise MetalFinalizeError(
                f"{entry}: Metal GEMM source supports T.Parallel/T.vectorized only on "
                "A/B/C tile copy or atomic-output staging"
            )
        args = list(op.get("args", []))
        if len(args) < 2:
            raise MetalFinalizeError(f"{entry}: scheduled op must have source and destination")
        key = (str(args[0]), str(args[1]))
        if key not in allowed:
            raise MetalFinalizeError(
                f"{entry}: Metal GEMM source supports T.Parallel/T.vectorized only on "
                "A/B/C tile copy or atomic-output staging"
            )
        tile_shape = allowed[key]
        shape_label = key[1]
        if "parallel_extents" in attrs:
            extents = attrs["parallel_extents"]
            vars_ = attrs.get("parallel_vars")
            if (
                not isinstance(vars_, list)
                or len(vars_) != len(extents)
                or any(not isinstance(var, str) for var in vars_)
            ):
                raise MetalFinalizeError(f"{entry}: T.Parallel vars must match extents")
            coalesced_width = attrs.get("parallel_coalesced_width")
            if (
                coalesced_width is not None
                and (
                    isinstance(coalesced_width, bool)
                    or not isinstance(coalesced_width, int)
                    or coalesced_width <= 0
                )
            ):
                raise MetalFinalizeError(f"{entry}: T.Parallel coalesced_width must be a positive integer")
            prefer_async = attrs.get("parallel_prefer_async")
            if prefer_async is True:
                raise MetalFinalizeError(f"{entry}: T.Parallel prefer_async=True is not supported for Metal")
            if prefer_async is not None and not isinstance(prefer_async, bool):
                raise MetalFinalizeError(f"{entry}: T.Parallel prefer_async must be a boolean")
            annotations = attrs.get("parallel_annotations")
            if annotations is not None and not isinstance(annotations, dict):
                raise MetalFinalizeError(f"{entry}: T.Parallel annotations must be a dict")
        else:
            extents = []
        if "vectorized_extent" in attrs:
            extent = attrs["vectorized_extent"]
            var = attrs.get("vectorized_var")
            if not isinstance(var, str):
                raise MetalFinalizeError(f"{entry}: T.vectorized var must be a string")
            annotations = attrs.get("vectorized_annotations")
            if annotations is not None and not isinstance(annotations, dict):
                raise MetalFinalizeError(f"{entry}: T.vectorized annotations must be a dict")
            scheduled_extents = [*extents, extent]
        else:
            scheduled_extents = list(extents)
        if scheduled_extents != tile_shape:
            if "parallel_extents" in attrs and "vectorized_extent" not in attrs:
                raise MetalFinalizeError(
                    f"{entry}: T.Parallel extents {extents!r} do not match {shape_label!r} "
                    f"tile shape {tile_shape!r}"
                )
            if "vectorized_extent" in attrs and "parallel_extents" not in attrs:
                raise MetalFinalizeError(
                    f"{entry}: T.vectorized extent {scheduled_extents[-1]!r} "
                    f"does not match {shape_label!r} tile shape {tile_shape!r}"
                )
            raise MetalFinalizeError(
                f"{entry}: scheduled tile-copy extents {scheduled_extents!r} "
                f"do not match {shape_label!r} tile shape {tile_shape!r}"
            )


def _emit_scalar_tiled_gemm_source_func(
    *,
    entry: str,
    params: list[dict[str, Any]],
    func: dict[str, Any],
    local_records: dict[str, dict[str, Any]],
    scopes: dict[str, str],
) -> tuple[list[str], list[str]]:
    """Emit the first honest Metal GEMM source path.

    This is a scalar fallback for the TileLang Metal matmul shape, not a
    simdgroup/tensor-core lowering. It deliberately requires static tensor and
    tile shapes so index math is explicit and reviewable.
    """
    pattern = _gemm_source_pattern(
        entry=entry,
        params=params,
        func=func,
        local_records=local_records,
        scopes=scopes,
    )
    a_shared = pattern["a_shared"]
    b_shared = pattern["b_shared"]
    c_local = pattern["c_local"]
    a_global = pattern["a_global"]
    b_global = pattern["b_global"]
    c_global = pattern["c_global"]
    a_dtype = pattern["a_dtype"]
    b_dtype = pattern["b_dtype"]
    c_dtype = pattern["c_dtype"]
    output_mode = pattern.get("output_mode", "copy")
    if a_dtype not in {"f16", "f32"} or b_dtype not in {"f16", "f32"} or c_dtype not in {"f16", "f32"}:
        raise MetalFinalizeError(
            f"{entry}: scalar tiled GEMM source currently supports f16/f32 inputs "
            "and f16/f32 copy output only"
        )
    if output_mode == "atomic_add" and c_dtype != "f32":
        raise MetalFinalizeError(f"{entry}: split-k atomic GEMM supports f32 output only")

    threads = pattern["threads"]
    pipeline_start = pattern["pipeline_start"]
    pipeline_extent = pattern["pipeline_extent"]
    pipeline_step = pattern["pipeline_step"]
    transpose_a = bool(pattern.get("transpose_A", False))
    transpose_b = bool(pattern.get("transpose_B", False))
    m = pattern["m"]
    n = pattern["n"]
    k = pattern["k"]
    k_span = pattern.get("k_span", k)
    split_k_tail_safe = bool(pattern.get("split_k_tail_safe", False))
    swizzle_attrs = pattern.get("swizzle_attrs")
    block_m = pattern["block_m"]
    block_n = pattern["block_n"]
    block_k = pattern["block_k"]

    a_ctype = _metal_scalar_type(a_dtype)
    b_ctype = _metal_scalar_type(b_dtype)
    c_ctype = _metal_scalar_type(c_dtype)
    a_tile_elems = block_m * block_k
    b_tile_elems = block_k * block_n
    c_tile_elems = block_m * block_n
    swizzle_lines, tile_gid_x, tile_gid_y = _metal_swizzle_tile_id_lines(
        entry=entry,
        swizzle_attrs=swizzle_attrs if isinstance(swizzle_attrs, dict) else None,
        grid_x=_ceil_div(n, block_n),
        grid_y=_ceil_div(m, block_m),
    )
    a_physical_elems = _metal_local_physical_elems(
        entry=entry,
        local_records=local_records,
        name=a_shared,
    )
    b_physical_elems = _metal_local_physical_elems(
        entry=entry,
        local_records=local_records,
        name=b_shared,
    )

    metal_params: list[str] = []
    for index, param in enumerate(params):
        kind = param.get("kind")
        name = param.get("name")
        dtype = param.get("dtype")
        if not isinstance(name, str) or not isinstance(dtype, str):
            raise MetalFinalizeError(f"bad parameter record {param!r}")
        ctype = _metal_scalar_type(dtype)
        if kind != "buffer":
            raise MetalFinalizeError(f"{entry}: GEMM source supports buffer params only")
        qualifier = "device" if name == c_global else "const device"
        if output_mode == "atomic_add" and name == c_global:
            if dtype != "f32":
                raise MetalFinalizeError(f"{entry}: split-k atomic GEMM supports f32 output only")
            ctype = "atomic_float"
        metal_params.append(f"    {qualifier} {ctype}* {name} [[buffer({index})]]")
    metal_params.append("    uint3 tid3 [[thread_position_in_threadgroup]]")
    metal_params.append("    uint3 tgid [[threadgroup_position_in_grid]]")

    def local_index(name: str, row: str, col: str) -> str:
        return _metal_local_index_expr(entry=entry, local_records=local_records, name=name, row=row, col=col)

    split_prefix = "split_k0 + " if output_mode == "atomic_add" else ""
    k_limit = "split_k_end" if output_mode == "atomic_add" else _u(k)

    if transpose_a:
        a_store_idx = local_index(a_shared, "a_local_k", "a_local_m")
        a_load_index_lines = [
            f"                uint a_local_k = load / {_u(block_m)};",
            f"                uint a_local_m = load % {_u(block_m)};",
            f"                uint a_row = {split_prefix}ko * {_u(block_k)} + a_local_k;",
            "                uint a_col = tile_row0 + a_local_m;",
            f"                uint a_shared_idx = {a_store_idx};",
            f"                {a_shared}[a_shared_idx] = (a_row < {k_limit} && a_col < {_u(m)}) "
            f"? {a_global}[(a_row * {_u(m)}) + a_col] : {_zero_value_for_storage(a_dtype)};",
        ]
        a_acc = f"{a_shared}[{local_index(a_shared, 'kk', 'local_m')}]"
    else:
        a_store_idx = local_index(a_shared, "a_local_m", "a_local_k")
        a_load_index_lines = [
            f"                uint a_local_m = load / {_u(block_k)};",
            f"                uint a_local_k = load % {_u(block_k)};",
            "                uint a_row = tile_row0 + a_local_m;",
            f"                uint a_col = {split_prefix}ko * {_u(block_k)} + a_local_k;",
            f"                uint a_shared_idx = {a_store_idx};",
            f"                {a_shared}[a_shared_idx] = (a_row < {_u(m)} && a_col < {k_limit}) "
            f"? {a_global}[(a_row * {_u(k)}) + a_col] : {_zero_value_for_storage(a_dtype)};",
        ]
        a_acc = f"{a_shared}[{local_index(a_shared, 'local_m', 'kk')}]"
    if transpose_b:
        b_store_idx = local_index(b_shared, "b_local_n", "b_local_k")
        b_load_index_lines = [
            f"                uint b_local_n = load / {_u(block_k)};",
            f"                uint b_local_k = load % {_u(block_k)};",
            "                uint b_row = tile_col0 + b_local_n;",
            f"                uint b_col = {split_prefix}ko * {_u(block_k)} + b_local_k;",
            f"                uint b_shared_idx = {b_store_idx};",
            f"                {b_shared}[b_shared_idx] = (b_row < {_u(n)} && b_col < {k_limit}) "
            f"? {b_global}[(b_row * {_u(k)}) + b_col] : {_zero_value_for_storage(b_dtype)};",
        ]
        b_acc = f"{b_shared}[{local_index(b_shared, 'local_n', 'kk')}]"
    else:
        b_store_idx = local_index(b_shared, "b_local_k", "b_local_n")
        b_load_index_lines = [
            f"                uint b_local_k = load / {_u(block_n)};",
            f"                uint b_local_n = load % {_u(block_n)};",
            f"                uint b_row = {split_prefix}ko * {_u(block_k)} + b_local_k;",
            "                uint b_col = tile_col0 + b_local_n;",
            f"                uint b_shared_idx = {b_store_idx};",
            f"                {b_shared}[b_shared_idx] = (b_row < {k_limit} && b_col < {_u(n)}) "
            f"? {b_global}[(b_row * {_u(n)}) + b_col] : {_zero_value_for_storage(b_dtype)};",
        ]
        b_acc = f"{b_shared}[{local_index(b_shared, 'kk', 'local_n')}]"

    split_k_lines: list[str] = []
    if output_mode == "atomic_add":
        split_k_end_expr = (
            f"min(split_k0 + {_u(k_span)}, {_u(k)})"
            if split_k_tail_safe
            else f"split_k0 + {_u(k_span)}"
        )
        split_k_lines = [
            "    uint split_k_index = tgid.z;",
            f"    uint split_k0 = split_k_index * {_u(k_span)};",
            f"    uint split_k_end = {split_k_end_expr};",
        ]

    body = [
        f"    threadgroup {a_ctype} {a_shared}[{a_physical_elems}];",
        f"    threadgroup {b_ctype} {b_shared}[{b_physical_elems}];",
        "    uint tid = tid3.x;",
        *split_k_lines,
        *swizzle_lines,
        f"    uint tile_col0 = {tile_gid_x} * {_u(block_n)};",
        f"    uint tile_row0 = {tile_gid_y} * {_u(block_m)};",
        f"    for (uint tile_linear_base = 0u; tile_linear_base < {_u(c_tile_elems)}; "
        f"tile_linear_base += {_u(threads)}) {{",
        "        uint linear = tile_linear_base + tid;",
        f"        uint local_m = linear / {_u(block_n)};",
        f"        uint local_n = linear % {_u(block_n)};",
        "        uint row = tile_row0 + local_m;",
        "        uint col = tile_col0 + local_n;",
        "        float acc = 0.0;",
        f"        for (uint ko = {_u(pipeline_start)}; "
        f"ko < {_u(pipeline_start + pipeline_extent)}; {_ko_increment(pipeline_step)}) {{",
        f"            for (uint load = tid; load < {_u(a_tile_elems)}; load += {_u(threads)}) {{",
        *a_load_index_lines,
        "            }",
        f"            for (uint load = tid; load < {_u(b_tile_elems)}; load += {_u(threads)}) {{",
        *b_load_index_lines,
        "            }",
        "            threadgroup_barrier(mem_flags::mem_threadgroup);",
        f"            if (linear < {_u(c_tile_elems)} && row < {_u(m)} && col < {_u(n)}) {{",
        f"                for (uint kk = 0u; kk < {_u(block_k)}; ++kk) {{",
        f"                    acc += float({a_acc}) * float({b_acc});",
        "                }",
        "            }",
        "            threadgroup_barrier(mem_flags::mem_threadgroup);",
        "        }",
        f"        if (linear < {_u(c_tile_elems)} && row < {_u(m)} && col < {_u(n)}) {{",
    ]
    if output_mode == "atomic_add":
        body.extend(
            [
                f"            atomic_fetch_add_explicit(&{c_global}[(row * {_u(n)}) + col], acc, memory_order_relaxed);",
                "        }",
                "    }",
            ]
        )
    else:
        body.extend(
            [
                f"            {c_global}[(row * {_u(n)}) + col] = ({c_ctype})acc;",
                "        }",
                "    }",
            ]
        )
    return metal_params, body


def _emit_fixed_sparse_2_to_4_gemm_source_func(
    *,
    entry: str,
    params: list[dict[str, Any]],
    func: dict[str, Any],
    local_records: dict[str, dict[str, Any]],
    scopes: dict[str, str],
) -> tuple[list[str], list[str]]:
    """Emit the first pcc-owned Metal ``gemm_sp`` correctness kernel.

    The accepted record is intentionally one fixed 5x7x16 TileLang shape. It
    decodes the int16 2:4 metadata in ordinary Metal scalar code; this is not a
    sparse-MMA, tensor-core, or performance claim.
    """
    ops = func.get("ops")
    if not isinstance(ops, list):
        raise MetalFinalizeError(f"{entry}: sparse GEMM_SP requires an ops list")
    expected_ops = [
        "tir.fill_loop",
        "tir.use_swizzle",
        "tir.copy_loop",
        "tir.copy_loop",
        "tir.copy_loop",
        "tir.gemm_sp_expand",
        "tir.copy_loop",
        "tir.copy_loop",
    ]
    if [op.get("tir_op") for op in ops] != expected_ops:
        raise MetalFinalizeError(
            f"{entry}: fixed sparse GEMM_SP Metal slice requires the exact "
            "fill/swizzle/A/E/B/gemm_sp/C-staging/C-output op sequence"
        )
    if any(not isinstance(op.get("attrs") or {}, dict) for op in ops):
        raise MetalFinalizeError(f"{entry}: sparse GEMM_SP op attrs must be dicts")

    fill, swizzle, a_copy, e_copy, b_copy, gemm, c_stage, c_output = ops
    gemm_args = list(gemm.get("args", []))
    if len(gemm_args) != 4:
        raise MetalFinalizeError(
            f"{entry}: sparse GEMM_SP requires A_sparse, E, B, and C local operands"
        )
    a_shared, e_shared, b_shared, c_local = map(str, gemm_args)
    a_copy_args = list(a_copy.get("args", []))
    e_copy_args = list(e_copy.get("args", []))
    b_copy_args = list(b_copy.get("args", []))
    c_stage_args = list(c_stage.get("args", []))
    c_output_args = list(c_output.get("args", []))
    if any(len(args) != 2 for args in (a_copy_args, e_copy_args, b_copy_args, c_stage_args, c_output_args)):
        raise MetalFinalizeError(f"{entry}: sparse GEMM_SP copies must have source and destination")
    a_global, a_copy_dst = map(str, a_copy_args)
    e_global, e_copy_dst = map(str, e_copy_args)
    b_global, b_copy_dst = map(str, b_copy_args)
    c_stage_src, c_staging = map(str, c_stage_args)
    c_output_src, c_global = map(str, c_output_args)
    if (
        a_copy_dst != a_shared
        or e_copy_dst != e_shared
        or b_copy_dst != b_shared
        or c_stage_src != c_local
        or c_output_src != c_staging
    ):
        raise MetalFinalizeError(f"{entry}: sparse GEMM_SP copy graph does not match gemm operands")
    if list(fill.get("args", [])) != [c_local] or (fill.get("attrs") or {}) != {"value": 0}:
        raise MetalFinalizeError(f"{entry}: sparse GEMM_SP requires an exact zero accumulator fill")
    swizzle_attrs = swizzle.get("attrs") or {}
    if swizzle_attrs.get("enable") is not False or set(swizzle_attrs) - {"enable", "panel_size"}:
        raise MetalFinalizeError(
            f"{entry}: fixed sparse GEMM_SP Metal slice requires disabled T.use_swizzle"
        )
    loop_attrs = {"num_stages": 0, "pipeline_extent": 1}
    for label, op in (("A", a_copy), ("E", e_copy), ("B", b_copy)):
        if (op.get("attrs") or {}) != loop_attrs:
            raise MetalFinalizeError(
                f"{entry}: fixed sparse GEMM_SP {label} copy requires one non-staged K tile"
            )
    if (c_stage.get("attrs") or {}) or (c_output.get("attrs") or {}):
        raise MetalFinalizeError(f"{entry}: fixed sparse GEMM_SP output copies cannot be scheduled")
    gemm_attrs = gemm.get("attrs") or {}
    allowed_gemm_attrs = {
        "num_stages",
        "pipeline_extent",
        "policy",
        "transpose_A",
        "transpose_E",
        "transpose_B",
    }
    if set(gemm_attrs) - allowed_gemm_attrs:
        raise MetalFinalizeError(f"{entry}: unsupported sparse GEMM_SP attrs {gemm_attrs!r}")
    if gemm_attrs.get("num_stages") != 0 or gemm_attrs.get("pipeline_extent") != 1:
        raise MetalFinalizeError(f"{entry}: fixed sparse GEMM_SP requires exactly one K tile")
    if any(gemm_attrs.get(key) is not False for key in ("transpose_A", "transpose_E", "transpose_B")):
        raise MetalFinalizeError(f"{entry}: fixed sparse GEMM_SP does not support transposes")
    _validate_gemm_policy_metadata(entry=entry, policy=gemm_attrs.get("policy"))

    param_map = {str(param.get("name")): param for param in params}
    if len(params) != 4 or set(param_map) != {a_global, e_global, b_global, c_global}:
        raise MetalFinalizeError(f"{entry}: fixed sparse GEMM_SP requires exactly four buffer params")
    for name in (a_global, e_global, b_global, c_global):
        record = param_map[name]
        if record.get("kind") != "buffer" or scopes.get(name) != MemoryScope.GLOBAL.value:
            raise MetalFinalizeError(f"{entry}: sparse GEMM_SP operand {name!r} must be a global buffer")
    required_locals = {a_shared, e_shared, b_shared, c_local, c_staging}
    if set(local_records) != required_locals:
        raise MetalFinalizeError(
            f"{entry}: fixed sparse GEMM_SP requires exactly A/E/B shared, C local, and C shared buffers"
        )

    expected_params = {
        a_global: ("f16", [5, 8]),
        e_global: ("i16", [5, 1]),
        b_global: ("f16", [16, 7]),
        c_global: ("f32", [5, 7]),
    }
    for name, (dtype, shape) in expected_params.items():
        record = param_map[name]
        if _require_dtype(record, entry=entry, name=name) != dtype or _shape_from_record(
            record, entry=entry, name=name
        ) != shape:
            raise MetalFinalizeError(
                f"{entry}: fixed sparse GEMM_SP expects {name!r} as {dtype}{shape}"
            )
    expected_locals = {
        a_shared: ("f16", [8, 8], MemoryScope.SHARED.value),
        e_shared: ("i16", [8, 1], MemoryScope.SHARED.value),
        b_shared: ("f16", [16, 8], MemoryScope.SHARED.value),
        c_local: ("f32", [8, 8], MemoryScope.FRAGMENT.value),
        c_staging: ("f32", [8, 8], MemoryScope.SHARED.value),
    }
    for name, (dtype, shape, scope) in expected_locals.items():
        record = local_records[name]
        if (
            _require_dtype(record, entry=entry, name=name) != dtype
            or _shape_from_record(record, entry=entry, name=name) != shape
            or record.get("scope") != scope
        ):
            raise MetalFinalizeError(
                f"{entry}: fixed sparse GEMM_SP local {name!r} must be {scope} {dtype}{shape}"
            )
    if func.get("grid") != [1, 1] or _func_threads(func) != 32:
        raise MetalFinalizeError(
            f"{entry}: fixed sparse GEMM_SP requires grid [1, 1] and 32 threads"
        )

    metal_params: list[str] = []
    for index, param in enumerate(params):
        name = str(param["name"])
        ctype = _metal_scalar_type(str(param["dtype"]))
        qualifier = "device" if name == c_global else "const device"
        metal_params.append(f"    {qualifier} {ctype}* {name} [[buffer({index})]]")
    metal_params.append("    uint3 tid3 [[thread_position_in_threadgroup]]")

    body = [
        "    uint tid = tid3.x;",
        "    for (uint tile_linear_base = 0u; tile_linear_base < 64u; tile_linear_base += 32u) {",
        "        uint linear = tile_linear_base + tid;",
        "        uint row = linear / 8u;",
        "        uint col = linear % 8u;",
        "        if (linear < 64u && row < 5u && col < 7u) {",
        "            float acc = 0.0;",
        "            for (uint k_index = 0u; k_index < 16u; ++k_index) {",
        "                uint group = k_index / 4u;",
        "                uint offset = k_index % 4u;",
        f"                ushort metadata_word = ushort({e_global}[row]);",
        "                uint code = (uint(metadata_word) >> (4u * (group % 4u))) & 15u;",
        "                uint idx0 = code & 3u;",
        "                uint idx1 = (code >> 2u) & 3u;",
        "                uint sparse_col = group * 2u;",
        "                float aval = 0.0;",
        f"                if (offset == idx0) aval = float({a_global}[(row * 8u) + sparse_col]);",
        f"                else if (offset == idx1) aval = float({a_global}[(row * 8u) + sparse_col + 1u]);",
        f"                acc += aval * float({b_global}[(k_index * 7u) + col]);",
        "            }",
        f"            {c_global}[(row * 7u) + col] = acc;",
        "        }",
        "    }",
    ]
    return metal_params, body


def _simdgroup_fragment_type(dtype: str, *, entry: str, name: str) -> str:
    if dtype == "f16":
        return "simdgroup_half8x8"
    if dtype == "f32":
        return "simdgroup_float8x8"
    raise MetalFinalizeError(
        f"{entry}: simdgroup GEMM operand {name!r} has unsupported dtype {dtype!r}"
    )


def _emit_simdgroup_gemm_source_func(
    *,
    entry: str,
    params: list[dict[str, Any]],
    func: dict[str, Any],
    local_records: dict[str, dict[str, Any]],
    scopes: dict[str, str],
) -> tuple[list[str], list[str]]:
    """Emit an opt-in 8x8 Metal simdgroup GEMM source path.

    This is deliberately separate from ``emit_metal_source`` so the scalar
    fallback remains the default correctness path. The legality mirrors
    TileLang/TVM's Metal backend constraint: only constant 8x8 simdgroup
    matrices are accepted in this slice.
    """
    pattern = _gemm_source_pattern(
        entry=entry,
        params=params,
        func=func,
        local_records=local_records,
        scopes=scopes,
    )
    a_dtype = pattern["a_dtype"]
    b_dtype = pattern["b_dtype"]
    c_dtype = pattern["c_dtype"]
    output_mode = pattern.get("output_mode", "copy")
    if output_mode not in {"copy", "atomic_add"}:
        raise MetalFinalizeError(
            f"{entry}: simdgroup GEMM source does not support output mode {output_mode!r}"
        )
    if a_dtype != "f16" or b_dtype != "f16" or c_dtype != "f32":
        raise MetalFinalizeError(
            f"{entry}: simdgroup GEMM source currently supports f16 x f16 -> f32 only"
        )

    m = pattern["m"]
    n = pattern["n"]
    k = pattern["k"]
    block_m = pattern["block_m"]
    block_n = pattern["block_n"]
    block_k = pattern["block_k"]
    transpose_a = bool(pattern.get("transpose_A", False))
    transpose_b = bool(pattern.get("transpose_B", False))
    c_block_m, c_block_n = pattern["c_tile"]
    simdgroups_m = block_m // 8 if block_m % 8 == 0 else 0
    simdgroups_n = block_n // 8 if block_n % 8 == 0 else 0
    simdgroups_total = simdgroups_m * simdgroups_n
    if (
        block_k != 8
        or c_block_m != block_m
        or c_block_n != block_n
        or simdgroups_m < 1
        or simdgroups_n < 1
    ):
        raise MetalFinalizeError(
            f"{entry}: simdgroup GEMM source requires MxNx8 tile locals with M/N multiples of 8, got "
            f"A{pattern['a_tile']} B{pattern['b_tile']} C{pattern['c_tile']}"
        )
    needs_copy_edge_staging = output_mode == "copy" and (
        m % 8 != 0 or n % 8 != 0 or k % 8 != 0
    )
    if simdgroups_total > 32:
        raise MetalFinalizeError(
            f"{entry}: simdgroup GEMM source currently supports at most thirty-two "
            f"simdgroups per threadgroup, got {simdgroups_total}"
        )
    expected_threads = 32 * simdgroups_total
    if pattern["threads"] != expected_threads:
        raise MetalFinalizeError(
            f"{entry}: simdgroup GEMM source requires exactly {expected_threads} "
            f"threads per threadgroup, got {pattern['threads']}"
        )

    a_global = pattern["a_global"]
    b_global = pattern["b_global"]
    c_global = pattern["c_global"]
    a_frag = _simdgroup_fragment_type(a_dtype, entry=entry, name=a_global)
    b_frag = _simdgroup_fragment_type(b_dtype, entry=entry, name=b_global)
    c_frag = _simdgroup_fragment_type(c_dtype, entry=entry, name=c_global)
    pipeline_start = pattern["pipeline_start"]
    pipeline_extent = pattern["pipeline_extent"]
    pipeline_step = pattern["pipeline_step"]
    k_span = pattern.get("k_span", k)
    split_k_tail_safe = bool(pattern.get("split_k_tail_safe", False))
    if output_mode == "atomic_add":
        if not isinstance(k_span, int) or k_span <= 0:
            raise MetalFinalizeError(f"{entry}: simdgroup split-k metadata is malformed")
    needs_atomic_staging = output_mode == "atomic_add" and (
        m % 8 != 0 or n % 8 != 0 or split_k_tail_safe or k_span % 8 != 0
    )
    needs_staging = needs_copy_edge_staging or needs_atomic_staging
    needs_multi_simdgroup_atomic = simdgroups_total > 1 and output_mode == "atomic_add"
    if simdgroups_total > 1 and output_mode not in {"copy", "atomic_add"}:
        raise MetalFinalizeError(
            f"{entry}: multi-simdgroup source currently supports direct copy-output "
            "or split-k atomic tiles only"
        )
    swizzle_attrs = pattern.get("swizzle_attrs")
    swizzle_lines, tile_gid_x, tile_gid_y = _metal_swizzle_tile_id_lines(
        entry=entry,
        swizzle_attrs=swizzle_attrs if isinstance(swizzle_attrs, dict) else None,
        grid_x=_ceil_div(n, 8),
        grid_y=_ceil_div(m, 8),
    )

    metal_params: list[str] = []
    for index, param in enumerate(params):
        kind = param.get("kind")
        name = param.get("name")
        dtype = param.get("dtype")
        if not isinstance(name, str) or not isinstance(dtype, str):
            raise MetalFinalizeError(f"bad parameter record {param!r}")
        ctype = _metal_scalar_type(dtype)
        if kind != "buffer":
            raise MetalFinalizeError(f"{entry}: simdgroup GEMM source supports buffer params only")
        qualifier = "device" if name == c_global else "const device"
        if output_mode == "atomic_add" and name == c_global:
            ctype = "atomic_float"
        metal_params.append(f"    {qualifier} {ctype}* {name} [[buffer({index})]]")
    if output_mode == "atomic_add" or needs_staging:
        metal_params.append("    uint3 tid [[thread_position_in_threadgroup]]")
    if output_mode == "atomic_add" or needs_staging:
        metal_params.append("    uint3 tgid [[threadgroup_position_in_grid]]")
    else:
        metal_params.append("    uint2 tgid [[threadgroup_position_in_grid]]")
    if simdgroups_total > 1:
        metal_params.append("    uint simdgroup_idx [[simdgroup_index_in_threadgroup]]")
    if simdgroups_total > 1 and (needs_staging or needs_multi_simdgroup_atomic):
        metal_params.append("    uint simdgroup_lane [[thread_index_in_simdgroup]]")

    split_k_lines: list[str] = []
    if output_mode == "atomic_add":
        split_k_end_expr = (
            f"min(split_k0 + {_u(k_span)}, {_u(k)})"
            if split_k_tail_safe
            else f"split_k0 + {_u(k_span)}"
        )
        split_k_lines = [
            "    uint split_k_index = tgid.z;",
            f"    uint split_k0 = split_k_index * {_u(k_span)};",
        ]
        if needs_atomic_staging:
            split_k_lines.append(f"    uint split_k_end = {split_k_end_expr};")
    if needs_staging:
        staged_elements = 64 * simdgroups_total if simdgroups_total > 1 else 64
        staging_lines = [
            f"    threadgroup half A_tile[{staged_elements}];",
            f"    threadgroup half B_tile[{staged_elements}];",
            f"    threadgroup float C_tile[{staged_elements}];",
        ]
    elif output_mode == "atomic_add":
        c_staged_elements = 64 * simdgroups_total if needs_multi_simdgroup_atomic else 64
        staging_lines = [f"    threadgroup float C_tile[{c_staged_elements}];"]
    else:
        staging_lines = []
    multi_simdgroup_lines: list[str] = []
    if simdgroups_total > 1:
        multi_simdgroup_lines = [
            f"    uint simdgroup_tile_m = simdgroup_idx / {_u(simdgroups_n)};",
            f"    uint simdgroup_tile_n = simdgroup_idx % {_u(simdgroups_n)};",
        ]
        if needs_staging or needs_multi_simdgroup_atomic:
            multi_simdgroup_lines.append("    uint simdgroup_tile_offset = simdgroup_idx * 64u;")
        row_tile_index = (
            f"({tile_gid_y} * {_u(block_m)}) + (simdgroup_tile_m * 8u)"
            if simdgroups_m > 1
            else f"{tile_gid_y} * 8u"
        )
        col_tile_index = (
            f"({tile_gid_x} * {_u(block_n)}) + (simdgroup_tile_n * 8u)"
            if simdgroups_n > 1
            else f"{tile_gid_x} * 8u"
        )
    else:
        row_tile_index = f"{tile_gid_y} * 8u"
        col_tile_index = f"{tile_gid_x} * 8u"
    row_tile_expr = f"({row_tile_index})"
    col_tile_expr = f"({col_tile_index})"
    k_offset_expr = "split_k0 + (ko * 8u)" if output_mode == "atomic_add" else "ko * 8u"
    if transpose_a:
        a_load_ptr = f"{a_global} + (({k_offset_expr}) * {_u(m)}) + {row_tile_expr}"
        a_stride = _u(m)
        a_transpose = "true"
    else:
        a_load_ptr = f"{a_global} + (({row_tile_index}) * {_u(k)}) + ({k_offset_expr})"
        a_stride = _u(k)
        a_transpose = "false"
    if transpose_b:
        b_load_ptr = f"{b_global} + (({col_tile_index}) * {_u(k)}) + ({k_offset_expr})"
        b_stride = _u(k)
        b_transpose = "true"
    else:
        b_load_ptr = f"{b_global} + (({k_offset_expr}) * {_u(n)}) + {col_tile_expr}"
        b_stride = _u(n)
        b_transpose = "false"

    body = [
        f"    {a_frag} A_frag[1];",
        f"    {b_frag} B_frag[1];",
        f"    {c_frag} C_frag[1];",
        *staging_lines,
        *split_k_lines,
        *swizzle_lines,
        *multi_simdgroup_lines,
        "    C_frag[0] = make_filled_simdgroup_matrix<float, 8, 8>(0.0);",
    ]
    if needs_staging:
        tile_iter = "simdgroup_lane" if simdgroups_total > 1 else "tid.x"
        a_tile_ref = (
            "A_tile[simdgroup_tile_offset + tile_linear]"
            if simdgroups_total > 1
            else "A_tile[tile_linear]"
        )
        b_tile_ref = (
            "B_tile[simdgroup_tile_offset + tile_linear]"
            if simdgroups_total > 1
            else "B_tile[tile_linear]"
        )
        c_tile_ref = (
            "C_tile[simdgroup_tile_offset + c_linear]"
            if simdgroups_total > 1
            else "C_tile[c_linear]"
        )
        a_tile_ptr = "A_tile + simdgroup_tile_offset" if simdgroups_total > 1 else "A_tile"
        b_tile_ptr = "B_tile + simdgroup_tile_offset" if simdgroups_total > 1 else "B_tile"
        c_tile_ptr = "C_tile + simdgroup_tile_offset" if simdgroups_total > 1 else "C_tile"
        a_staged_load = (
            f"{a_global}[(global_k * {_u(m)}) + global_m]"
            if transpose_a
            else f"{a_global}[(global_m * {_u(k)}) + global_k]"
        )
        b_staged_load = (
            f"{b_global}[(global_n * {_u(k)}) + global_k]"
            if transpose_b
            else f"{b_global}[(global_k * {_u(n)}) + global_n]"
        )
        k_base_expr = "split_k0 + (ko * 8u)" if output_mode == "atomic_add" else "ko * 8u"
        k_guard = "global_k < split_k_end" if output_mode == "atomic_add" else f"global_k < {_u(k)}"
        body.extend(
            [
                f"    for (uint ko = {_u(pipeline_start)}; "
                f"ko < {_u(pipeline_start + pipeline_extent)}; {_ko_increment(pipeline_step)}) {{",
                f"        uint k_base = {k_base_expr};",
                f"        for (uint tile_linear = {tile_iter}; tile_linear < 64u; tile_linear += 32u) {{",
                "            uint local_m = tile_linear / 8u;",
                "            uint local_k = tile_linear % 8u;",
                f"            uint global_m = {row_tile_expr} + local_m;",
                "            uint global_k = k_base + local_k;",
                f"            {a_tile_ref} = (global_m < {_u(m)} && {k_guard}) ? {a_staged_load} : half(0.0);",
                "        }",
                f"        for (uint tile_linear = {tile_iter}; tile_linear < 64u; tile_linear += 32u) {{",
                "            uint local_k = tile_linear / 8u;",
                "            uint local_n = tile_linear % 8u;",
                "            uint global_k = k_base + local_k;",
                f"            uint global_n = {col_tile_expr} + local_n;",
                f"            {b_tile_ref} = ({k_guard} && global_n < {_u(n)}) ? {b_staged_load} : half(0.0);",
                "        }",
                "        threadgroup_barrier(mem_flags::mem_threadgroup);",
                f"        simdgroup_load(A_frag[0], {a_tile_ptr}, 8u, 0, false);",
                f"        simdgroup_load(B_frag[0], {b_tile_ptr}, 8u, 0, false);",
                "        simdgroup_multiply_accumulate(C_frag[0], A_frag[0], B_frag[0], C_frag[0]);",
                "        threadgroup_barrier(mem_flags::mem_threadgroup);",
                "    }",
                f"    simdgroup_store(C_frag[0], {c_tile_ptr}, 8u, 0, false);",
                "    threadgroup_barrier(mem_flags::mem_threadgroup);",
                f"    for (uint c_linear = {tile_iter}; c_linear < 64u; c_linear += 32u) {{",
                "        uint local_m = c_linear / 8u;",
                "        uint local_n = c_linear % 8u;",
                f"        uint row = {row_tile_expr} + local_m;",
                f"        uint col = {col_tile_expr} + local_n;",
                f"        if (row < {_u(m)} && col < {_u(n)}) {{",
                (
                    f"            atomic_fetch_add_explicit(&{c_global}[(row * {_u(n)}) + col], {c_tile_ref}, memory_order_relaxed);"
                    if output_mode == "atomic_add"
                    else f"            {c_global}[(row * {_u(n)}) + col] = {c_tile_ref};"
                ),
                "        }",
                "    }",
            ]
        )
        return metal_params, body

    body.extend(
        [
        f"    for (uint ko = {_u(pipeline_start)}; "
        f"ko < {_u(pipeline_start + pipeline_extent)}; {_ko_increment(pipeline_step)}) {{",
        f"        simdgroup_load(A_frag[0], {a_load_ptr}, {a_stride}, 0, {a_transpose});",
        f"        simdgroup_load(B_frag[0], {b_load_ptr}, {b_stride}, 0, {b_transpose});",
        "        simdgroup_multiply_accumulate(C_frag[0], A_frag[0], B_frag[0], C_frag[0]);",
        "    }",
        ]
    )
    if output_mode == "atomic_add":
        atomic_tile_iter = "simdgroup_lane" if needs_multi_simdgroup_atomic else "tid.x"
        atomic_c_tile_ptr = (
            "C_tile + simdgroup_tile_offset" if needs_multi_simdgroup_atomic else "C_tile"
        )
        atomic_c_tile_ref = (
            "C_tile[simdgroup_tile_offset + c_linear]"
            if needs_multi_simdgroup_atomic
            else "C_tile[c_linear]"
        )
        body.extend(
            [
                f"    simdgroup_store(C_frag[0], {atomic_c_tile_ptr}, 8u, 0, false);",
                "    threadgroup_barrier(mem_flags::mem_threadgroup);",
                f"    for (uint c_linear = {atomic_tile_iter}; c_linear < 64u; c_linear += 32u) {{",
                "        uint local_m = c_linear / 8u;",
                "        uint local_n = c_linear % 8u;",
                f"        uint row = {row_tile_expr} + local_m;",
                f"        uint col = {col_tile_expr} + local_n;",
                f"        atomic_fetch_add_explicit(&{c_global}[(row * {_u(n)}) + col], {atomic_c_tile_ref}, memory_order_relaxed);",
                "    }",
            ]
        )
    else:
        body.append(
            f"    simdgroup_store(C_frag[0], {c_global} + (({row_tile_index}) * {_u(n)}) + {col_tile_expr}, {_u(n)}, 0, false);"
        )
    return metal_params, body


def emit_metal_simdgroup_gemm_source(module: KernelModule | PlainTirModule) -> str:
    """Emit opt-in Metal simdgroup GEMM source for the 8x8 microkernel slice."""
    plain = module if isinstance(module, PlainTirModule) else lower_to_plain_tir(module, target="metal")
    _build_descriptor(plain)
    lines: list[str] = [
        "#include <metal_stdlib>",
        "using namespace metal;",
        "",
    ]
    for func in plain.funcs:
        entry = func.get("name")
        device_entry = metal_device_entry_name(entry)
        params = _func_param_dicts(func)
        local_records = _local_records(func)
        scopes = _param_scopes(func)
        scopes.update({name: record["scope"] for name, record in local_records.items()})
        if not any(op.get("tir_op") == "tir.gemm_expand" for op in func.get("ops", [])):
            raise MetalFinalizeError(f"{entry}: simdgroup source requires a GEMM op")
        metal_params, body = _emit_simdgroup_gemm_source_func(
            entry=entry,
            params=params,
            func=func,
            local_records=local_records,
            scopes=scopes,
        )
        lines.extend(
            [
                f"kernel void {device_entry}(",
                ",\n".join(metal_params),
                ") {",
                "\n".join(body),
                "}",
                "",
            ]
        )
    return "\n".join(lines)


def emit_metal_source(module: KernelModule | PlainTirModule) -> str:
    """Emit minimal, compilable Metal source for the supported plain-TIR subset.

    This is intentionally narrow. It supports the first host/device proof
    kernels (global-buffer ``copy`` / ``fill`` plus scalar parameters) and a
    bounded threadgroup ``sum`` reduction. More complex TileLang/TIRx operations
    may stay descriptor-only until they have a real lowering.
    """
    plain = module if isinstance(module, PlainTirModule) else lower_to_plain_tir(module, target="metal")
    _build_descriptor(plain)

    lines: list[str] = [
        "#include <metal_stdlib>",
        "using namespace metal;",
        "",
    ]
    for func in plain.funcs:
        entry = func.get("name")
        device_entry = metal_device_entry_name(entry)
        params = _func_param_dicts(func)
        written = _written_buffer_names(func)
        local_decls, local_scopes = _local_declarations(func)
        local_records = _local_records(func)
        scopes = _param_scopes(func)
        scopes.update(local_scopes)
        records_by_name = {
            str(record.get("name")): record
            for record in [*params, *local_records.values()]
            if isinstance(record.get("name"), str)
        }

        if any(op.get("tir_op") == "tir.gemm_sp_expand" for op in func.get("ops", [])):
            metal_params, body = _emit_fixed_sparse_2_to_4_gemm_source_func(
                entry=entry,
                params=params,
                func=func,
                local_records=local_records,
                scopes=scopes,
            )
            lines.extend(
                [
                    f"kernel void {device_entry}(",
                    ",\n".join(metal_params),
                    ") {",
                    "\n".join(body),
                    "}",
                    "",
                ]
            )
            continue

        if any(op.get("tir_op") == "tir.gemm_expand" for op in func.get("ops", [])):
            metal_params, body = _emit_scalar_tiled_gemm_source_func(
                entry=entry,
                params=params,
                func=func,
                local_records=local_records,
                scopes=scopes,
            )
            lines.extend(
                [
                    f"kernel void {device_entry}(",
                    ",\n".join(metal_params),
                    ") {",
                    "\n".join(body),
                    "}",
                    "",
                ]
            )
            continue

        metal_params: list[str] = []
        for index, param in enumerate(params):
            kind = param.get("kind")
            name = param.get("name")
            dtype = param.get("dtype")
            if not isinstance(name, str) or not isinstance(dtype, str):
                raise MetalFinalizeError(f"bad parameter record {param!r}")
            ctype = _metal_scalar_type(dtype)
            if kind == "buffer":
                scope = param.get("scope")
                if scope != MemoryScope.GLOBAL.value:
                    raise MetalFinalizeError(
                        f"kernel {entry!r} param {name!r}: Metal artifact "
                        f"emission only accepts host-visible global buffers; "
                        f"got scope {scope!r}"
                    )
                qualifier = "device" if name in written else "const device"
                metal_params.append(
                    f"    {qualifier} {ctype}* {name} [[buffer({index})]]"
                )
            elif kind == "scalar":
                metal_params.append(
                    f"    constant {ctype}& {name} [[buffer({index})]]"
                )
            else:
                raise MetalFinalizeError(f"unsupported Metal parameter kind {kind!r}")
        metal_params.append("    uint gid [[thread_position_in_grid]]")
        if local_scopes:
            metal_params.append("    uint tid [[thread_position_in_threadgroup]]")
        if _requires_threadgroup_position(func):
            metal_params.append("    uint tgid [[threadgroup_position_in_grid]]")

        body: list[str] = list(local_decls)
        extent = _kernel_extent(func)
        if extent is not None and not _has_threadgroup_sync(func):
            body.append(f"    if (gid >= {extent}u) {{")
            body.append("        return;")
            body.append("    }")
        reduced_locals: set[str] = set()
        imported_reduction_attrs: dict[str, dict[str, Any]] = {}
        structured_depth = 1
        for op in func.get("ops", []):
            tir_op = op.get("tir_op")
            args = list(op.get("args", []))
            attrs = op.get("attrs") or {}
            if tir_op == "tir.copy_loop" and len(args) >= 2:
                src, dst = str(args[0]), str(args[1])
                if src in reduced_locals and scopes.get(dst) == MemoryScope.GLOBAL.value:
                    imported_attrs = imported_reduction_attrs.get(src)
                    if imported_attrs is not None:
                        if attrs != {"reduction_output": True}:
                            raise MetalFinalizeError(
                                f"{entry}: imported row reduce_sum output copy metadata "
                                f"must be {{'reduction_output': True}}, got {attrs!r}"
                            )
                        output_record = records_by_name.get(dst)
                        if (
                            output_record is None
                            or output_record.get("dtype") != "f32"
                            or output_record.get("shape") != imported_attrs.get("output_shape")
                        ):
                            raise MetalFinalizeError(
                                f"{entry}: imported row reduce_sum output {dst!r} no "
                                "longer matches its f32 keep-dimension shape contract"
                            )
                    body.extend(
                        [
                            "    if (tid == 0u) {",
                            f"        {dst}[tgid] = {src}[0];",
                            "    }",
                        ]
                    )
                else:
                    body.extend(
                        _emit_copy_statement(
                            src=src,
                            dst=dst,
                            scopes=scopes,
                            extent=extent,
                        )
                    )
            elif tir_op == "tir.fill_loop" and args:
                dst = str(args[0])
                record = records_by_name.get(dst)
                dtype = record.get("dtype") if record is not None else None
                if not isinstance(dtype, str):
                    raise MetalFinalizeError(
                        f"{entry}: fill target {dst!r} has no scalar dtype"
                    )
                value = _fill_literal(dtype, attrs.get("value", 0))
                body.extend(
                    _emit_fill_statement(
                        dst=dst,
                        value=value,
                        scopes=scopes,
                        extent=extent,
                    )
                )
            elif tir_op == "tir.parallel_for":
                body.append("    // parallel extent is represented by gid.")
            elif tir_op == "tir.barrier":
                body.append("    threadgroup_barrier(mem_flags::mem_threadgroup);")
            elif tir_op == "tir.reduce_loop" and len(args) >= 2:
                src, dst = str(args[0]), str(args[1])
                body.extend(
                    _emit_reduce_sum(
                        entry=entry,
                        src=src,
                        dst=dst,
                        attrs=attrs,
                        func=func,
                        local_records=local_records,
                        scopes=scopes,
                        extent=extent,
                    )
                )
                reduced_locals.add(dst)
                if attrs.get("import_kind") == "tilelang.reduce_sum.static_row.v1":
                    imported_reduction_attrs[dst] = dict(attrs)
            elif tir_op == "tir.elementwise_add" and len(args) >= 3:
                lhs, rhs, dst = str(args[0]), str(args[1]), str(args[2])
                index = str(attrs.get("index") or "gid")
                guard = attrs.get("guard")
                if index != "gid":
                    body.append(f"    uint {index} = gid;")
                if guard is not None:
                    body.append(f"    if ({index} < {guard}) {{")
                    body.append(f"        {dst}[{index}] = ({lhs}[{index}] + {rhs}[{index}]);")
                    body.append("    }")
                else:
                    body.append(f"    {dst}[{index}] = ({lhs}[{index}] + {rhs}[{index}]);")
            elif tir_op == "tir.scalar_assign":
                target = attrs.get("target")
                dtype = attrs.get("dtype")
                declare = attrs.get("declare")
                if not isinstance(target, str) or not isinstance(dtype, str):
                    raise MetalFinalizeError(f"{entry}: bad scalar assignment {attrs!r}")
                prefix = f"{_metal_scalar_type(dtype)} " if declare else ""
                indent = "    " * structured_depth
                body.append(
                    f"{indent}{prefix}{target} = "
                    f"{_indexed_expr_to_metal(attrs.get('expr'))};"
                )
            elif tir_op == "tir.indexed_store" and args:
                indent = "    " * structured_depth
                body.append(
                    f"{indent}{args[0]}["
                    f"{_indexed_expr_to_metal(attrs.get('index'))}] = "
                    f"{_indexed_expr_to_metal(attrs.get('value'))};"
                )
            elif tir_op == "tir.if_begin":
                indent = "    " * structured_depth
                body.append(
                    f"{indent}if ({_indexed_expr_to_metal(attrs.get('condition'))}) {{"
                )
                structured_depth += 1
            elif tir_op == "tir.else":
                structured_depth -= 1
                indent = "    " * structured_depth
                body.append(f"{indent}}} else {{")
                structured_depth += 1
            elif tir_op == "tir.if_end":
                structured_depth -= 1
                indent = "    " * structured_depth
                body.append(f"{indent}}}")
            elif tir_op in {"tir.fence", "tir.gemm_expand", "tir.gemm_sp_expand"}:
                raise MetalFinalizeError(
                    f"{entry}: {tir_op} has no real Metal source lowering in "
                    "this slice; keep it descriptor-only until a focused "
                    "lowering exists"
                )
            else:
                raise MetalFinalizeError(f"{entry}: unsupported Metal op {tir_op!r}")
        if not body:
            body.append("    return;")

        lines.extend(
            [
                f"kernel void {device_entry}(",
                ",\n".join(metal_params),
                ") {",
                "\n".join(body),
                "}",
                "",
            ]
        )
    return "\n".join(lines)


def _build_descriptor(
    plain: PlainTirModule,
    *,
    metal_source_tool: str = "pcc.kernel_ir.metal_finalize.emit_metal_source",
) -> MetalPackagingDescriptor:
    if plain.marker != PLAIN_TIR_FREEZE_MARKER:
        raise MetalFinalizeError(
            f"expected a plain-TIR-frozen module (marker "
            f"{PLAIN_TIR_FREEZE_MARKER!r}), got marker {plain.marker!r}"
        )
    if plain.target != "metal":
        raise MetalFinalizeError(
            f"Metal finalize requires target 'metal', got {plain.target!r}"
        )
    entry_points = [metal_device_entry_name(f.get("name")) for f in plain.funcs]
    if not entry_points:
        raise MetalFinalizeError("plain-TIR module has no kernel entry points")
    if len(set(entry_points)) != len(entry_points):
        raise MetalFinalizeError(
            f"plain-TIR module has duplicate Metal entry points {entry_points!r}"
        )

    base = plain.module
    metal_src = f"{base}.metal"
    air = f"{base}.air"
    metallib = f"{base}.metallib"
    steps = [
        {
            "step": "emit_metal_source",
            "produces": metal_src,
            "tool": metal_source_tool,
        },
        {
            "step": "compile_to_air",
            "produces": air,
            "tool": "xcrun -sdk macosx metal -c",
        },
        {
            "step": "package_metallib",
            "produces": metallib,
            "tool": "xcrun -sdk macosx metallib",
        },
    ]
    return MetalPackagingDescriptor(
        library_name=base,
        entry_points=entry_points,
        metal_source_name=metal_src,
        air_name=air,
        metallib_name=metallib,
        steps=steps,
    )


def finalize_metal(
    module: Any,
    *,
    toolchain_available: bool | None = None,
    artifact_dir: str | Path | None = None,
    compile_toolchain: bool = False,
    metal_source_emitter: Callable[[PlainTirModule], str] | None = None,
    metal_source_tool: str = "pcc.kernel_ir.metal_finalize.emit_metal_source",
    timeout: float = 30.0,
) -> MetalFinalizeResult:
    """Produce a Metal packaging descriptor for a kernel module.

    *module* may be a ``KernelModule`` (it will be frozen to plain TIR first) or
    an already-frozen ``PlainTirModule``.

    By default this preserves the original descriptor-only behavior. Passing
    ``artifact_dir`` emits a real ``.metal`` source artifact. Passing
    ``compile_toolchain=True`` additionally invokes the Metal CLI and produces
    ``.air`` / ``.metallib`` when the toolchain is present.

    ``toolchain_available`` overrides the real probe (used by tests to exercise
    both branches deterministically). When the toolchain is absent, returns a
    ``SKIPPED_WITH_REASON`` result — it never claims to have compiled anything.
    """
    if isinstance(module, PlainTirModule):
        plain = module
    else:
        plain = lower_to_plain_tir(module, target="metal")

    source_emitter = emit_metal_source if metal_source_emitter is None else metal_source_emitter
    descriptor = _build_descriptor(plain, metal_source_tool=metal_source_tool)

    available = _metal_toolchain_available() if toolchain_available is None else toolchain_available
    if artifact_dir is not None:
        out_dir = Path(artifact_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        metal_source = source_emitter(plain)
        metal_path = out_dir / descriptor.metal_source_name
        air_path = out_dir / descriptor.air_name
        metallib_path = out_dir / descriptor.metallib_name
        metal_path.write_text(metal_source, encoding="utf-8")
        artifact_paths = {"metal_source": str(metal_path)}

        if not compile_toolchain:
            return MetalFinalizeResult(
                status=STATUS_SOURCE_ONLY,
                descriptor=descriptor,
                reason=(
                    "Metal source artifact emitted; compile_toolchain=False, "
                    "so no AIR/metallib was produced and no kernel was launched."
                ),
                metal_source=metal_source,
                artifact_paths=artifact_paths,
                metal_source_produced=True,
            )

        if not available:
            return MetalFinalizeResult(
                status=STATUS_SKIPPED_WITH_REASON,
                descriptor=descriptor,
                reason=(
                    "Xcode Metal command-line tooling is not usable; emitted "
                    ".metal source only. No .air/.metallib was produced and "
                    "no kernel was launched."
                ),
                metal_source=metal_source,
                artifact_paths=artifact_paths,
                metal_source_produced=True,
            )

        try:
            from pcc.backend.metal import MetalCompileError, MetalToolchainUnavailable, compile_air_to_metallib, compile_metal_source_to_air

            compile_metal_source_to_air(metal_source, air_path, timeout=timeout)
            compile_air_to_metallib([air_path], metallib_path, timeout=timeout)
        except MetalToolchainUnavailable as exc:
            return MetalFinalizeResult(
                status=STATUS_SKIPPED_WITH_REASON,
                descriptor=descriptor,
                reason=(
                    f"Metal toolchain became unavailable while compiling: {exc}. "
                    "No .metallib was produced and no kernel was launched."
                ),
                metal_source=metal_source,
                artifact_paths=artifact_paths,
                metal_source_produced=True,
            )
        except MetalCompileError as exc:
            raise MetalFinalizeError(str(exc)) from exc

        artifact_paths["air"] = str(air_path)
        artifact_paths["metallib"] = str(metallib_path)
        return MetalFinalizeResult(
            status=STATUS_ARTIFACTS_PRODUCED,
            descriptor=descriptor,
            reason=(
                "Metal source, AIR, and metallib artifacts produced. This proves "
                "device artifact staging only; no host launch was executed."
            ),
            metal_source=metal_source,
            artifact_paths=artifact_paths,
            metal_source_produced=True,
            air_produced=True,
            metallib_produced=True,
        )

    if not available:
        return MetalFinalizeResult(
            status=STATUS_SKIPPED_WITH_REASON,
            descriptor=descriptor,
            reason=(
                "Xcode Metal command-line tooling (xcrun + metal) not found; "
                "emitting packaging descriptor only. No .metallib was produced "
                "and no kernel was launched."
            ),
        )

    # Even when the toolchain IS present, this slice only emits the descriptor —
    # real .metal source emission is not implemented yet. Be explicit.
    return MetalFinalizeResult(
        status=STATUS_DESCRIPTOR_ONLY,
        descriptor=descriptor,
        reason=(
            "Metal toolchain present, but device-source emission is not "
            "implemented in this slice; descriptor only. No .metallib produced."
        ),
    )


def finalize_dump(module: Any, *, toolchain_available: bool | None = None) -> str:
    """Deterministic golden dump of the finalize result."""
    import json

    result = finalize_metal(module, toolchain_available=toolchain_available)
    return json.dumps(result.to_dict(), indent=2, sort_keys=True)


__all__ = [
    "STATUS_DESCRIPTOR_ONLY",
    "STATUS_SOURCE_ONLY",
    "STATUS_ARTIFACTS_PRODUCED",
    "STATUS_SKIPPED_WITH_REASON",
    "MetalFinalizeError",
    "MetalPackagingDescriptor",
    "MetalFinalizeResult",
    "emit_metal_source",
    "emit_metal_simdgroup_gemm_source",
    "metal_device_entry_name",
    "finalize_metal",
    "finalize_dump",
]
