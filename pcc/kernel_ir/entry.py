from __future__ import annotations

"""Frontend extraction for the first pcc GPU kernel subset."""

import ast
import math
from dataclasses import dataclass
from pathlib import Path

from pcc.backend.metal import compile_air_to_metallib, compile_metal_source_to_air
from pcc.kernel_ir.ir import (
    BufferParam,
    KernelFunc,
    KernelModule,
    KernelOp,
    ScalarParam,
    ScalarType,
    validate_kernel,
)
from pcc.kernel_ir.metal_finalize import emit_metal_source


class GpuKernelError(ValueError):
    """Raised when a ``@gpu.kernel`` function uses unsupported syntax."""


@dataclass(frozen=True)
class GpuKernelArtifact:
    name: str
    metal_path: Path
    air_path: Path
    metallib_path: Path


_TYPE_MAP = {
    "ptr_f32": "ptr_f32",
    "i32": "i32",
    "u32": "u32",
    "f32": "f32",
}


def prepare_gpu_kernels_for_source(
    source: str,
    src_path: str,
    *,
    backend: str,
    artifact_dir: str | Path,
    metallib_path: str | Path,
) -> tuple[str, list[GpuKernelArtifact]]:
    if backend != "metal":
        return source, []
    module = ast.parse(source, filename=src_path)
    kernels = [
        node for node in module.body
        if isinstance(node, ast.FunctionDef) and _is_gpu_kernel(node)
    ]
    if not kernels:
        return source, []

    out_dir = Path(artifact_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    staged = []
    for kernel in kernels:
        metal_source = lower_function_to_metal(kernel)
        metal_path = out_dir / (kernel.name + ".metal")
        air_path = out_dir / (kernel.name + ".air")
        metal_path.write_text(metal_source, encoding="utf-8")
        compile_metal_source_to_air(metal_source, air_path)
        staged.append((kernel.name, metal_path, air_path))

    library_path = compile_air_to_metallib(
        [air_path for _name, _metal_path, air_path in staged],
        metallib_path,
    )
    artifacts = [
        GpuKernelArtifact(
            name=name,
            metal_path=metal_path,
            air_path=air_path,
            metallib_path=library_path,
        )
        for name, metal_path, air_path in staged
    ]
    return strip_gpu_kernel_host_source(source, module), artifacts


def source_contains_gpu_kernel(source: str, src_path: str = "<string>") -> bool:
    module = ast.parse(source, filename=src_path)
    return any(
        isinstance(node, ast.FunctionDef) and _is_gpu_kernel(node)
        for node in module.body
    )


def strip_gpu_kernel_host_source(source: str, module: ast.Module) -> str:
    remove_ranges = []
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and _is_gpu_kernel(node):
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            remove_ranges.append((start, node.end_lineno or node.lineno))
        elif _is_gpu_import(node):
            remove_ranges.append((node.lineno, node.end_lineno or node.lineno))

    if not remove_ranges:
        return source

    remove = set()
    for start, end in remove_ranges:
        for line_no in range(start, end + 1):
            remove.add(line_no)
    kept = []
    for line_no, line in enumerate(source.splitlines(), 1):
        if line_no not in remove:
            kept.append(line)
    stripped = "\n".join(kept) + "\n"
    return rewrite_gpu_host_launch_calls(stripped)


def rewrite_gpu_host_launch_calls(source: str) -> str:
    marker = "gpu.run_add_f32_demo()"
    if marker not in source:
        return source
    rewritten = source.replace(marker, "__pcc_gpu_run_add_f32_demo()")
    prelude = (
        "from pcc.extern import extern, c_int64\n"
        "__pcc_gpu_run_add_f32_demo = extern("
        "\"pcc_metal_demo_add_f32\", (), c_int64)\n"
    )
    return prelude + rewritten


def lower_function_to_metal(fn: ast.FunctionDef) -> str:
    module = lower_function_to_kernel_ir(fn)
    return "// pcc route: Kernel IR -> TIRx -> Metal\n" + emit_metal_source(module)


def lower_function_to_kernel_ir(fn: ast.FunctionDef) -> KernelModule:
    """Import the finite ``@gpu.kernel`` scalar/indexed subset into Kernel IR.

    The common vector-add shape retains its compact primitive. Broader scalar
    assignment, indexed load/store, arithmetic, comparison, and nested if/else
    syntax is represented by validated structured Kernel IR ops. Unsupported
    syntax fails closed; there is no direct AST-to-Metal route.
    """
    if (
        fn.args.posonlyargs
        or fn.args.vararg is not None
        or fn.args.kwonlyargs
        or fn.args.kwarg is not None
        or fn.args.defaults
        or fn.args.kw_defaults
    ):
        raise GpuKernelError(
            f"{fn.name}: GPU kernels require plain positional parameters "
            "without defaults, *args, keyword-only args, or **kwargs"
        )

    params = []
    ptr_params: set[str] = set()
    scalar_params: set[str] = set()
    symbol_types: dict[str, ScalarType] = {}
    buffer_types: dict[str, ScalarType] = {}
    for arg in fn.args.args:
        kind = _gpu_annotation_name(arg.annotation)
        name = _sanitize_name(arg.arg)
        if kind == "ptr_f32":
            params.append(BufferParam(name, ScalarType.F32, rank=1))
            ptr_params.add(name)
            buffer_types[name] = ScalarType.F32
        elif kind == "i32":
            params.append(ScalarParam(name, ScalarType.I32))
            scalar_params.add(name)
            symbol_types[name] = ScalarType.I32
        elif kind == "u32":
            params.append(ScalarParam(name, ScalarType.U32))
            scalar_params.add(name)
            symbol_types[name] = ScalarType.U32
        elif kind == "f32":
            params.append(ScalarParam(name, ScalarType.F32))
            scalar_params.add(name)
            symbol_types[name] = ScalarType.F32
        else:
            raise GpuKernelError(f"{fn.name}: unsupported gpu annotation {kind!r}")

    if len(fn.body) == 2:
        try:
            index_name = _kernel_ir_thread_index_name(fn.body[0])
            guard_name, lhs_name, rhs_name, dst_name = _kernel_ir_vector_add_shape(
                fn.body[1], index_name=index_name
            )
            if not {lhs_name, rhs_name, dst_name} <= ptr_params:
                raise GpuKernelError(
                    f"{fn.name}: vector-add operands must be ptr_f32 params"
                )
            if guard_name not in scalar_params:
                raise GpuKernelError(
                    f"{fn.name}: vector-add guard must be a scalar param"
                )
            body = (
                KernelOp("parallel", (guard_name,), {"extent_param": guard_name}),
                KernelOp(
                    "elementwise_add",
                    (lhs_name, rhs_name, dst_name),
                    {"index": index_name, "guard": guard_name},
                ),
            )
        except GpuKernelError:
            body = tuple(
                _lower_structured_statements(
                    fn.body,
                    symbol_types=dict(symbol_types),
                    buffer_types=buffer_types,
                    parameter_names=set(symbol_types) | set(buffer_types),
                    function_name=fn.name,
                )
            )
    else:
        body = tuple(
            _lower_structured_statements(
                fn.body,
                symbol_types=dict(symbol_types),
                buffer_types=buffer_types,
                parameter_names=set(symbol_types) | set(buffer_types),
                function_name=fn.name,
            )
        )

    func = KernelFunc(
        name=_sanitize_name(fn.name),
        params=tuple(params),
        body=body,
        grid=(1,),
        threads=256,
    )
    return validate_kernel(
        KernelModule(_sanitize_name(fn.name) + "_gpu_kernel_ir", funcs=(func,))
    )


_NUMERIC_TYPES = frozenset({ScalarType.I32, ScalarType.U32, ScalarType.F32})


def _type_compatible(destination: ScalarType, source: ScalarType) -> bool:
    if destination == source:
        return True
    return destination in _NUMERIC_TYPES and source in _NUMERIC_TYPES


def _binary_result_type(left: ScalarType, right: ScalarType) -> ScalarType:
    if left not in _NUMERIC_TYPES or right not in _NUMERIC_TYPES:
        raise GpuKernelError("GPU arithmetic accepts only i32/u32/f32 values")
    if ScalarType.F32 in {left, right}:
        return ScalarType.F32
    if ScalarType.U32 in {left, right}:
        return ScalarType.U32
    return ScalarType.I32


def _expr_record(
    expr: ast.AST,
    *,
    symbol_types: dict[str, ScalarType],
    buffer_types: dict[str, ScalarType],
    function_name: str,
) -> tuple[dict, ScalarType, set[str]]:
    if isinstance(expr, ast.Name):
        name = _sanitize_name(expr.id)
        if name in buffer_types:
            raise GpuKernelError(
                f"{function_name}: buffer {name!r} requires an index"
            )
        dtype = symbol_types.get(name)
        if dtype is None:
            raise GpuKernelError(
                f"{function_name}: unknown GPU scalar name {name!r}"
            )
        return {"kind": "name", "name": name}, dtype, {name}
    if isinstance(expr, ast.Constant):
        value = expr.value
        if isinstance(value, bool):
            return {"kind": "literal", "value": value}, ScalarType.BOOL, set()
        if isinstance(value, int):
            if not (-(1 << 31) <= value < (1 << 32)):
                raise GpuKernelError(
                    f"{function_name}: integer literal {value} does not fit i32/u32"
                )
            dtype = ScalarType.I32 if value < (1 << 31) else ScalarType.U32
            return {"kind": "literal", "value": value}, dtype, set()
        if isinstance(value, float) and math.isfinite(value):
            return {"kind": "literal", "value": value}, ScalarType.F32, set()
        raise GpuKernelError(
            f"{function_name}: GPU literal must be bool/int/finite float"
        )
    if isinstance(expr, ast.Call) and _is_gpu_attr(expr.func, "thread_id_x"):
        if expr.args or expr.keywords:
            raise GpuKernelError("gpu.thread_id_x() takes no arguments")
        return {"kind": "thread_id_x"}, ScalarType.U32, set()
    if isinstance(expr, ast.BinOp):
        left, left_type, left_refs = _expr_record(
            expr.left,
            symbol_types=symbol_types,
            buffer_types=buffer_types,
            function_name=function_name,
        )
        right, right_type, right_refs = _expr_record(
            expr.right,
            symbol_types=symbol_types,
            buffer_types=buffer_types,
            function_name=function_name,
        )
        return (
            {
                "kind": "binary",
                "op": _binary_op_name(expr.op),
                "left": left,
                "right": right,
            },
            _binary_result_type(left_type, right_type),
            left_refs | right_refs,
        )
    if (
        isinstance(expr, ast.Compare)
        and len(expr.ops) == 1
        and len(expr.comparators) == 1
    ):
        left, left_type, left_refs = _expr_record(
            expr.left,
            symbol_types=symbol_types,
            buffer_types=buffer_types,
            function_name=function_name,
        )
        right, right_type, right_refs = _expr_record(
            expr.comparators[0],
            symbol_types=symbol_types,
            buffer_types=buffer_types,
            function_name=function_name,
        )
        op_name = _compare_op_name(expr.ops[0])
        if op_name in {"lt", "le", "gt", "ge"}:
            _binary_result_type(left_type, right_type)
        elif not (
            left_type == right_type
            or (left_type in _NUMERIC_TYPES and right_type in _NUMERIC_TYPES)
        ):
            raise GpuKernelError(
                f"{function_name}: incompatible GPU comparison types "
                f"{left_type.value}/{right_type.value}"
            )
        return (
            {"kind": "compare", "op": op_name, "left": left, "right": right},
            ScalarType.BOOL,
            left_refs | right_refs,
        )
    if isinstance(expr, ast.Subscript):
        if not isinstance(expr.value, ast.Name):
            raise GpuKernelError(
                f"{function_name}: GPU buffer load requires a named buffer"
            )
        buffer = _sanitize_name(expr.value.id)
        dtype = buffer_types.get(buffer)
        if dtype is None:
            raise GpuKernelError(
                f"{function_name}: indexed value {buffer!r} is not a buffer param"
            )
        index, index_type, index_refs = _expr_record(
            expr.slice,
            symbol_types=symbol_types,
            buffer_types=buffer_types,
            function_name=function_name,
        )
        if index_type not in {ScalarType.I32, ScalarType.U32}:
            raise GpuKernelError(
                f"{function_name}: GPU buffer index must be i32/u32"
            )
        return (
            {"kind": "load", "buffer": buffer, "index": index},
            dtype,
            {buffer} | index_refs,
        )
    raise GpuKernelError(
        f"{function_name}: unsupported GPU expression {expr.__class__.__name__}; "
        "no direct Metal fallback exists"
    )


def _store_target_record(
    target: ast.Subscript,
    *,
    symbol_types: dict[str, ScalarType],
    buffer_types: dict[str, ScalarType],
    function_name: str,
) -> tuple[str, dict, set[str]]:
    if not isinstance(target.value, ast.Name):
        raise GpuKernelError(
            f"{function_name}: GPU store requires a named buffer target"
        )
    buffer = _sanitize_name(target.value.id)
    if buffer not in buffer_types:
        raise GpuKernelError(
            f"{function_name}: GPU store target {buffer!r} is not a buffer param"
        )
    index, index_type, refs = _expr_record(
        target.slice,
        symbol_types=symbol_types,
        buffer_types=buffer_types,
        function_name=function_name,
    )
    if index_type not in {ScalarType.I32, ScalarType.U32}:
        raise GpuKernelError(f"{function_name}: GPU buffer index must be i32/u32")
    return buffer, index, refs


def _lower_structured_statements(
    statements: list[ast.stmt],
    *,
    symbol_types: dict[str, ScalarType],
    buffer_types: dict[str, ScalarType],
    parameter_names: set[str],
    function_name: str,
) -> list[KernelOp]:
    ops: list[KernelOp] = []
    for stmt in statements:
        if isinstance(stmt, ast.Assign):
            if len(stmt.targets) != 1:
                raise GpuKernelError(
                    f"{function_name}: GPU assignment must have one target"
                )
            value, value_type, value_refs = _expr_record(
                stmt.value,
                symbol_types=symbol_types,
                buffer_types=buffer_types,
                function_name=function_name,
            )
            target = stmt.targets[0]
            if isinstance(target, ast.Name):
                name = _sanitize_name(target.id)
                if name in parameter_names:
                    raise GpuKernelError(
                        f"{function_name}: assignment to GPU parameter {name!r} "
                        "is not supported"
                    )
                declare = name not in symbol_types
                if declare:
                    symbol_types[name] = value_type
                elif not _type_compatible(symbol_types[name], value_type):
                    raise GpuKernelError(
                        f"{function_name}: assignment changes scalar {name!r} "
                        f"from {symbol_types[name].value} to {value_type.value}"
                    )
                ops.append(
                    KernelOp(
                        "scalar_assign",
                        tuple(sorted(value_refs)),
                        {
                            "target": name,
                            "dtype": symbol_types[name].value,
                            "declare": declare,
                            "expr": value,
                        },
                    )
                )
                continue
            if isinstance(target, ast.Subscript):
                buffer, index, index_refs = _store_target_record(
                    target,
                    symbol_types=symbol_types,
                    buffer_types=buffer_types,
                    function_name=function_name,
                )
                if not _type_compatible(buffer_types[buffer], value_type):
                    raise GpuKernelError(
                        f"{function_name}: cannot store {value_type.value} into "
                        f"{buffer_types[buffer].value} buffer {buffer!r}"
                    )
                refs = {buffer} | index_refs | value_refs
                ops.append(
                    KernelOp(
                        "indexed_store",
                        (buffer, *tuple(sorted(refs - {buffer}))),
                        {"index": index, "value": value},
                    )
                )
                continue
            raise GpuKernelError(
                f"{function_name}: unsupported GPU assignment target "
                f"{target.__class__.__name__}"
            )
        if isinstance(stmt, ast.If):
            condition, condition_type, condition_refs = _expr_record(
                stmt.test,
                symbol_types=symbol_types,
                buffer_types=buffer_types,
                function_name=function_name,
            )
            if condition_type != ScalarType.BOOL:
                raise GpuKernelError(
                    f"{function_name}: GPU if condition must be bool"
                )
            ops.append(
                KernelOp(
                    "if_begin",
                    tuple(sorted(condition_refs)),
                    {"condition": condition},
                )
            )
            ops.extend(
                _lower_structured_statements(
                    stmt.body,
                    symbol_types=dict(symbol_types),
                    buffer_types=buffer_types,
                    parameter_names=parameter_names,
                    function_name=function_name,
                )
            )
            if stmt.orelse:
                ops.append(KernelOp("else"))
                ops.extend(
                    _lower_structured_statements(
                        stmt.orelse,
                        symbol_types=dict(symbol_types),
                        buffer_types=buffer_types,
                        parameter_names=parameter_names,
                        function_name=function_name,
                    )
                )
            ops.append(KernelOp("if_end"))
            continue
        if isinstance(stmt, ast.Pass):
            continue
        raise GpuKernelError(
            f"{function_name}: unsupported GPU statement {stmt.__class__.__name__}; "
            "no direct Metal fallback exists"
        )
    return ops


def _kernel_ir_thread_index_name(stmt: ast.stmt) -> str:
    if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
        raise GpuKernelError("Kernel IR GPU import requires a thread id assignment")
    target = stmt.targets[0]
    if not isinstance(target, ast.Name):
        raise GpuKernelError("Kernel IR GPU import requires a named thread id")
    if not (isinstance(stmt.value, ast.Call) and _is_gpu_attr(stmt.value.func, "thread_id_x")):
        raise GpuKernelError("Kernel IR GPU import requires gpu.thread_id_x()")
    if stmt.value.args or stmt.value.keywords:
        raise GpuKernelError("gpu.thread_id_x() takes no arguments")
    return _sanitize_name(target.id)


def _kernel_ir_vector_add_shape(
    stmt: ast.stmt,
    *,
    index_name: str,
) -> tuple[str, str, str, str]:
    if not isinstance(stmt, ast.If):
        raise GpuKernelError("Kernel IR GPU import requires a guarded vector-add if")
    if stmt.orelse or len(stmt.body) != 1:
        raise GpuKernelError("Kernel IR GPU import accepts one guarded vector-add body")
    guard_name = _kernel_ir_guard_name(stmt.test, index_name=index_name)
    assign = stmt.body[0]
    if not isinstance(assign, ast.Assign) or len(assign.targets) != 1:
        raise GpuKernelError("Kernel IR GPU import requires one vector-add assignment")
    dst_name, dst_index = _kernel_ir_subscript(assign.targets[0])
    if dst_index != index_name:
        raise GpuKernelError("Kernel IR GPU import requires output indexed by thread id")
    if not isinstance(assign.value, ast.BinOp) or not isinstance(assign.value.op, ast.Add):
        raise GpuKernelError("Kernel IR GPU import currently accepts only vector add")
    lhs_name, lhs_index = _kernel_ir_subscript(assign.value.left)
    rhs_name, rhs_index = _kernel_ir_subscript(assign.value.right)
    if lhs_index != index_name or rhs_index != index_name:
        raise GpuKernelError("Kernel IR GPU import requires inputs indexed by thread id")
    return guard_name, lhs_name, rhs_name, dst_name


def _kernel_ir_guard_name(expr: ast.AST, *, index_name: str) -> str:
    if not (
        isinstance(expr, ast.Compare)
        and len(expr.ops) == 1
        and isinstance(expr.ops[0], ast.Lt)
        and len(expr.comparators) == 1
        and isinstance(expr.left, ast.Name)
        and _sanitize_name(expr.left.id) == index_name
        and isinstance(expr.comparators[0], ast.Name)
    ):
        raise GpuKernelError("Kernel IR GPU import requires `thread_index < scalar` guard")
    return _sanitize_name(expr.comparators[0].id)


def _kernel_ir_subscript(expr: ast.AST) -> tuple[str, str]:
    if not (
        isinstance(expr, ast.Subscript)
        and isinstance(expr.value, ast.Name)
        and isinstance(expr.slice, ast.Name)
    ):
        raise GpuKernelError("Kernel IR GPU import requires simple name[index] operands")
    return _sanitize_name(expr.value.id), _sanitize_name(expr.slice.id)


def _is_gpu_kernel(fn: ast.FunctionDef) -> bool:
    for decorator in fn.decorator_list:
        if _is_gpu_attr(decorator, "kernel"):
            return True
        if isinstance(decorator, ast.Name) and decorator.id == "kernel":
            return True
    return False


def _is_gpu_import(node: ast.stmt) -> bool:
    if isinstance(node, ast.Import):
        return any(alias.name == "pcc.gpu" for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        if node.module == "pcc" and any(alias.name == "gpu" for alias in node.names):
            return True
        if node.module == "pcc.gpu":
            return True
    return False


def _is_gpu_attr(expr: ast.AST, attr: str) -> bool:
    return (
        isinstance(expr, ast.Attribute)
        and expr.attr == attr
        and isinstance(expr.value, ast.Name)
        and expr.value.id == "gpu"
    )


def _gpu_annotation_name(annotation: ast.AST | None) -> str:
    if isinstance(annotation, ast.Attribute) and isinstance(annotation.value, ast.Name):
        if annotation.value.id == "gpu" and annotation.attr in _TYPE_MAP:
            return _TYPE_MAP[annotation.attr]
    if isinstance(annotation, ast.Name) and annotation.id in _TYPE_MAP:
        return _TYPE_MAP[annotation.id]
    raise GpuKernelError("GPU kernel parameters require pcc.gpu annotations")


def _binary_op_name(op: ast.operator) -> str:
    if isinstance(op, ast.Add):
        return "add"
    if isinstance(op, ast.Sub):
        return "sub"
    if isinstance(op, ast.Mult):
        return "mul"
    if isinstance(op, ast.Div):
        return "div"
    raise GpuKernelError("unsupported GPU binary operator: " + op.__class__.__name__)


def _compare_op_name(op: ast.cmpop) -> str:
    if isinstance(op, ast.Lt):
        return "lt"
    if isinstance(op, ast.LtE):
        return "le"
    if isinstance(op, ast.Gt):
        return "gt"
    if isinstance(op, ast.GtE):
        return "ge"
    if isinstance(op, ast.Eq):
        return "eq"
    if isinstance(op, ast.NotEq):
        return "ne"
    raise GpuKernelError("unsupported GPU comparison: " + op.__class__.__name__)


def _sanitize_name(name: str) -> str:
    out = []
    for ch in name:
        if ch == "_" or ch.isalnum():
            out.append(ch)
        else:
            out.append("_")
    clean = "".join(out) or "_"
    if clean[0].isdigit():
        clean = "_" + clean
    return clean
