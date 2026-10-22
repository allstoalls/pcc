"""C semantic type names and AST-to-LLVM type projection."""

from __future__ import annotations

from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.c_abi_layout import builtin_scalar_layout, long_double_width
from pcc.ir.compat import ir_c as ir

from pcc.frontends.c.codegen.c_layout import ir_type_align, ir_type_size, is_floating_ir_type, is_struct_ir_type

bool_t = ir.IntType(1)
int8_t = ir.IntType(8)
int16_t = ir.IntType(16)
int32_t = ir.IntType(32)
int64_t = ir.IntType(64)
int128_t = ir.IntType(128)
void_t = ir.VoidType()
float_t = ir.FloatType()
half_t = ir.HalfType()
double_t = ir.DoubleType()
voidptr_t = int8_t.as_pointer()
int64ptr_t = int64_t.as_pointer()
true_bit = bool_t(1)
false_bit = bool_t(0)
true_byte = int8_t(1)
false_byte = int8_t(0)
cstring = voidptr_t


def names_to_key(names):
    """Convert C type-name tokens to the canonical environment key."""

    return names[0] if len(names) == 1 else " ".join(sorted(names))


def long_double_type(target_triple):
    """The long-double format belongs to the target ABI, never the host."""
    width = long_double_width(target_triple)
    if width == 80:
        return ir.X86FP80Type()
    if width == 128:
        return ir.FP128Type()
    return double_t


def get_ir_type(type_str, target_triple=""):
    """Get an IR type from a single C type name or name sequence."""

    names = type_str.split() if isinstance(type_str, str) else type_str
    return get_ir_type_from_names(names, target_triple)


def get_ir_type_from_names(names, target_triple=""):
    """Project a list of C type specifiers to its physical IR type."""

    names = [
        name
        for name in names
        if name
        not in (
            "const",
            "volatile",
            "register",
            "restrict",
            "inline",
            "_Noreturn",
            "_Thread_local",
            "thread_local",
            "signed",
            "extern",
            "static",
        )
    ]
    canonical = " ".join(sorted(names))
    type_map = {
        "_Bool": bool_t,
        "int": int32_t,
        "char": int8_t,
        "void": void_t,
        "double": double_t,
        "float": float_t,
        "_Float16": half_t,
        "short": int16_t,
        "long": int64_t,
        "int short": int16_t,
        "int long": int64_t,
        "long long": int64_t,
        "int long long": int64_t,
        "__int128": int128_t,
        "char unsigned": int8_t,
        "int unsigned": int32_t,
        "unsigned": int32_t,
        "int short unsigned": int16_t,
        "short unsigned": int16_t,
        "int long unsigned": int64_t,
        "long unsigned": int64_t,
        "long long unsigned": int64_t,
        "__int128 unsigned": int128_t,
        "size_t": int64_t,
        "ssize_t": int64_t,
        "ptrdiff_t": int64_t,
        "int8_t": int8_t,
        "int16_t": int16_t,
        "int32_t": int32_t,
        "int64_t": int64_t,
        "uint8_t": int8_t,
        "uint16_t": int16_t,
        "uint32_t": int32_t,
        "uint64_t": int64_t,
        "wchar_t": int32_t,
    }
    long_type = ir.IntType(builtin_scalar_layout(["long"], target_triple).size * 8)
    for spelling in ("long", "int long", "int long unsigned", "long unsigned"):
        type_map[spelling] = long_type
    type_map["wchar_t"] = ir.IntType(builtin_scalar_layout(["wchar_t"], target_triple).size * 8)
    if canonical in type_map:
        return type_map[canonical]
    if "double" in names:
        if "long" in names:
            return long_double_type(target_triple)
        return double_t
    if "_Float16" in names:
        return half_t
    if "float" in names:
        return float_t
    if "char" in names:
        return int8_t
    if "short" in names:
        return int16_t
    return int64_t


def get_ir_type_from_node(node, target_triple=""):
    if isinstance(node, c_ast.EllipsisParam):
        return voidptr_t
    return resolve_node_type(node.type, target_triple)


def resolve_node_type(node_type, target_triple=""):
    """Resolve a C AST type node to a physical IR type."""

    if isinstance(node_type, c_ast.PtrDecl):
        inner = node_type.type
        if isinstance(inner, c_ast.FuncDecl):
            return_type = resolve_node_type(inner.type, target_triple)
            parameter_types = []
            is_var_arg = inner.args is None
            if inner.args:
                for parameter in inner.args.params:
                    if isinstance(parameter, c_ast.EllipsisParam):
                        is_var_arg = True
                        continue
                    parameter_type = get_ir_type_from_node(parameter, target_triple)
                    if not isinstance(parameter_type, ir.VoidType):
                        parameter_types.append(parameter_type)
            return ir.FunctionType(
                return_type,
                parameter_types,
                var_arg=is_var_arg,
            ).as_pointer()
        pointee = resolve_node_type(inner, target_triple)
        if isinstance(pointee, ir.VoidType):
            return voidptr_t
        return ir.PointerType(pointee)
    if isinstance(node_type, c_ast.TypeDecl):
        if isinstance(node_type.type, c_ast.IdentifierType):
            return get_ir_type(node_type.type.names, target_triple)
        if isinstance(node_type.type, c_ast.Struct):
            struct_node = node_type.type
            if struct_node.decls is not None:
                member_types = []
                for declaration in struct_node.decls:
                    member_types.append(resolve_node_type(declaration.type, target_triple))
                struct_type = ir.LiteralStructType(member_types)
                struct_type.members = [item.name for item in struct_node.decls]
                struct_type.member_decl_types = [
                    item.type for item in struct_node.decls
                ]
                return struct_type
            return int8_t
        if isinstance(node_type.type, c_ast.Union):
            return _resolve_union_type(node_type.type, target_triple)
        if isinstance(node_type.type, c_ast.Enum):
            return int32_t
        return int64_t
    if isinstance(node_type, c_ast.ArrayDecl):
        return voidptr_t
    return int64_t


def _resolve_union_member_type(declaration_type, target_triple=""):
    if isinstance(declaration_type, c_ast.ArrayDecl):
        dimensions = []
        array_node = declaration_type
        while isinstance(array_node, c_ast.ArrayDecl):
            dimension = 0
            if isinstance(array_node.dim, c_ast.Constant):
                dimension = int(array_node.dim.value.rstrip("uUlL"), 0)
            dimensions.append(dimension)
            array_node = array_node.type
        member_type = resolve_node_type(array_node, target_triple)
        for dimension in reversed(dimensions):
            member_type = ir.ArrayType(member_type, dimension)
        return member_type
    return resolve_node_type(declaration_type, target_triple)


def _resolve_union_type(union_node, target_triple=""):
    if union_node.decls is None:
        return int8_t
    if len(union_node.decls) == 0:
        union_type = ir.LiteralStructType([])
        union_type.members = []
        union_type.member_types = {}
        union_type.member_decl_types = {}
        union_type.is_union = True
        return union_type

    max_size = 0
    max_alignment = 1
    member_types = {}
    for declaration in union_node.decls:
        member_type = _resolve_union_member_type(declaration.type, target_triple)
        member_types[declaration.name] = member_type
        size = member_type.width // 8 if isinstance(member_type, ir.IntType) else 8
        if is_struct_ir_type(member_type):
            size = sum(
                element.width // 8 if isinstance(element, ir.IntType) else 8
                for element in member_type.elements
            )
        if isinstance(member_type, ir.PointerType):
            size = 8
        if is_floating_ir_type(member_type):
            size = ir_type_size(member_type)
        alignment = ir_type_align(member_type)
        max_size = max(max_size, size)
        max_alignment = max(max_alignment, alignment)
    alignment_types = {8: int64_t, 4: int32_t, 2: int16_t, 1: int8_t}
    alignment_type = alignment_types.get(max_alignment, int64_t)
    padding_size = max_size - max_alignment
    if padding_size > 0:
        union_type = ir.LiteralStructType(
            [alignment_type, ir.ArrayType(int8_t, padding_size)]
        )
    else:
        union_type = ir.LiteralStructType([alignment_type])
    union_type.members = list(member_types.keys())
    union_type.member_types = member_types
    union_type.is_union = True
    return union_type
