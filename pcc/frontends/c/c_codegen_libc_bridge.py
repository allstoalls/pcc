"""Bridge pcc.frontends.c.c_libc_registry to c_codegen's IR tuple shape."""
from __future__ import annotations

from pcc.frontends.c.c_libc_registry import LibcSignature, iter_signatures, lookup_signature


def _ptr(ir, base=None):
    return (base or ir.IntType(8)).as_pointer()


def _type_from_name(ir, name: str):
    name = name.strip()
    if name == "void":
        return ir.VoidType()
    if name in ("int", "int32_t"):
        return ir.IntType(32)
    if name in ("long", "ssize_t", "size_t", "time_t", "int64_t"):
        return ir.IntType(64)
    if name == "double":
        return ir.DoubleType()
    if name == "float":
        return ir.FloatType()
    if name.endswith("*") or name in ("void*", "const void*", "const char*", "FILE*"):
        return _ptr(ir)
    if name == "...":
        return None
    return _ptr(ir)


def signature_to_codegen_tuple(ir, sig: LibcSignature):
    args = []
    var_arg = False
    for arg in sig.arg_types:
        if arg == "...":
            var_arg = True
        else:
            args.append(_type_from_name(ir, arg))
    return (_type_from_name(ir, sig.return_type), args, var_arg)


def registry_to_codegen_map(ir, platform: str | None = None):
    return {sig.name: signature_to_codegen_tuple(ir, sig) for sig in iter_signatures(platform)}


def lookup_codegen_signature(ir, name: str, platform: str | None = None):
    sig = lookup_signature(name, platform)
    return None if sig is None else signature_to_codegen_tuple(ir, sig)
