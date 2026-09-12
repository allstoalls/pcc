"""pcc.llvm_capi.ir — text-first IR builder (β4.1 Tier 1-3).

Drop-in replacement for ``llvmlite.ir`` on the pcc codegen path.
Constructs LLVM IR as text incrementally (no object graph), then
``str(module)`` yields a complete IR string which downstream passes
(``llvmlite.binding.parse_assembly`` or ``pcc.llvm_capi.parse``)
consume.

API parity (Tier 1-3) with ``llvmlite.ir`` — same class names, same
method signatures — so codegen call sites are **zero-change** when
switching via ``PCC_USE_LLVMCAPI=1``.

**Scope exclusions** (β4.3 work, not here):
- Metadata canonicalization is semantic rather than byte-identical to llvmlite
- Rare ir.* long-tail: use ``__getattr__`` shim that raises clear errors

The implementation philosophy matches ``pcc.parse.py_parse`` /
``c_parse_driver``: static typed Python, no heavy reflection, ready
for self-host compilation.
"""

from __future__ import annotations

import os
import sys
from typing import Iterable, Optional

from pcc.stdlib._float_bits import (
    _bits_to_float64 as _shared_bits_to_float64,
    _float64_to_bits as _shared_float64_to_bits,
    _round_to_float16 as _shared_round_to_float16,
    _round_to_float32 as _shared_round_to_float32,
)


def _hex64(value: int) -> str:
    digits = "0123456789ABCDEF"
    out = ""
    shift = 60
    while shift >= 0:
        out += digits[(value >> shift) & 15]
        shift -= 4
    return "0x" + out


def _float64_to_bits_ir(f: float) -> int:
    """Return the canonical IEEE 754 binary64 bit pattern."""
    return _shared_float64_to_bits(f)


def _coerce_float64_ir(value) -> float:
    """Coerce a CPython/pcc float-like value to a native double."""
    return value


def _bits_to_float64_ir(bits: int) -> float:
    return _shared_bits_to_float64(bits)


def _env_flag_enabled(name: str) -> bool:
    value = str(os.environ.get(name, "") or "").strip().lower()
    return value in ("1", "true", "yes", "on")


_DEBUG_IR_RENDER_ENABLED = _env_flag_enabled("PCC_DEBUG_IR_RENDER")
_DEBUG_IR_CALL_TRACE_ENABLED = _env_flag_enabled("PCC_DEBUG_IR_CALL")
_DIRECT_INLINE_ERROR_EDGE_CAPTURE_ENABLED = _env_flag_enabled(
    "PCC_DIRECT_INLINE_ERROR_EDGE_CAPTURE"
)


def _debug_ir_render_enabled() -> bool:
    return _DEBUG_IR_RENDER_ENABLED


def _debug_ir_call_trace_enabled() -> bool:
    return _DEBUG_IR_CALL_TRACE_ENABLED


def _debug_ir_render_enabled_uncached() -> bool:
    value = str(os.environ.get("PCC_DEBUG_IR_RENDER", "") or "").strip().lower()
    return value in ("1", "true", "yes", "on")


def _debug_ir_call_enabled_uncached() -> bool:
    value = str(os.environ.get("PCC_DEBUG_IR_CALL", "") or "").strip().lower()
    return value in ("1", "true", "yes", "on")


def _join_text(parts, sep: str) -> str:
    all_text = True
    i = 0
    while i < len(parts):
        if not isinstance(parts[i], str):
            all_text = False
            break
        i += 1
    if all_text:
        out_parts = parts
    else:
        out_parts = []
        i = 0
        while i < len(parts):
            part = parts[i]
            if isinstance(part, str):
                out_parts.append(part)
            else:
                rendered = str(part)
                if not isinstance(rendered, str):
                    raise TypeError(
                        "IR text join requires text: index="
                        + str(i)
                        + " value_type="
                        + str(type(part).__name__)
                    )
                out_parts.append(rendered)
            i += 1
    if not out_parts:
        return ""
    # A single native join over several thousand IR fragments can allocate
    # while a large compiler object graph is live.  Bound that temporary
    # sequence and avoid the thousands of intermediate strings produced by a
    # pairwise tree: join fixed-size chunks, then join the small chunk list.
    chunk_limit = 128
    if len(out_parts) <= chunk_limit:
        return sep.join(out_parts)
    chunks = []
    chunk = []
    i = 0
    while i < len(out_parts):
        chunk.append(out_parts[i])
        if len(chunk) == chunk_limit:
            chunks.append(sep.join(chunk))
            chunk = []
        i += 1
    if chunk:
        chunks.append(sep.join(chunk))
    return sep.join(chunks)


def _round_to_float32_ir(f: float) -> float:
    return _shared_round_to_float32(f)


def _round_to_float16_ir(f: float) -> float:
    return _shared_round_to_float16(f)


# ---------------------------------------------------------------------------
# Type hierarchy — each Type knows how to render itself as LLVM text.
# ---------------------------------------------------------------------------


class Type:
    """Base class for all LLVM types."""

    # Subclasses set a short name used by repr. LLVM text comes from __str__.
    _name: str = "type"

    def __str__(self) -> str:  # LLVM text form, e.g. ``i32``, ``i8*``
        raise NotImplementedError

    def __repr__(self) -> str:
        return "<" + str(self._name) + " " + str(self) + ">"

    def __eq__(self, other) -> bool:
        return type(self) is type(other) and str(self) == str(other)

    def __hash__(self) -> int:
        return hash((type(self), str(self)))

    def as_pointer(self, addrspace: int = 0) -> "PointerType":
        """Return a ``PointerType`` wrapping this type."""
        return PointerType(self, addrspace=addrspace)


class _SingletonType(Type):
    """Scalar types with no parameters — interned per subclass."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance


class VoidType(_SingletonType):
    _name = "void"

    def __str__(self) -> str:
        return "void"


class IntType(Type):
    """Integer type with a given bit width.

    Interned per width — ``IntType(64) is IntType(64)`` holds true.
    Matches llvmlite, and keeps codegen ``value.type is _I64``
    identity checks working.
    """

    _name = "int"
    _cache: dict = {}

    def __new__(cls, width: int):
        existing = cls._cache.get(width)
        if existing is not None:
            return existing
        obj = super().__new__(cls)
        obj.width = width
        cls._cache[width] = obj
        return obj

    def __init__(self, width: int) -> None:
        # Keep this visible to pcc's native class-field collector.
        # __new__ handles CPython interning, but the self-host scaffold
        # constructs IntType instances through __init__ directly.
        self.width = width

    def __str__(self) -> str:
        width_text = str(self.width)
        return _join_text(["i", width_text], "")

    def __call__(self, value: int) -> "Constant":
        """``IntType(32)(0)`` shorthand for ``Constant(IntType(32), 0)``."""
        return Constant(self, value)


_I1 = IntType(1)


class HalfType(_SingletonType):
    _name = "half"

    def __str__(self) -> str:
        return "half"


class FloatType(_SingletonType):
    _name = "float"

    def __str__(self) -> str:
        return "float"


class DoubleType(_SingletonType):
    _name = "double"

    def __str__(self) -> str:
        return "double"


class PointerType(Type):
    """Pointer to a pointee type. LLVM 15+ uses opaque pointers (``ptr``)
    — we emit the opaque form by default to match llvmlite's current
    output; the pointee is tracked only for type-checking in the builder."""

    _name = "ptr"

    def __init__(self, pointee: Type, addrspace: int = 0) -> None:
        self.pointee = pointee
        self.addrspace = addrspace

    def __str__(self) -> str:
        # Opaque pointer form (LLVM 15+). addrspace qualifier inline.
        if self.addrspace:
            return "ptr addrspace(" + str(self.addrspace) + ")"
        return "ptr"

    def __eq__(self, other) -> bool:
        return (
            isinstance(other, PointerType)
            and self.addrspace == other.addrspace
            and self.pointee == other.pointee
        )

    def __hash__(self) -> int:
        return hash((PointerType, self.addrspace, self.pointee))


def _same_llvm_text_type(a: Type, b: Type) -> bool:
    return str(a) == str(b)


def _same_tracked_type(a: Type, b: Type) -> bool:
    if isinstance(a, PointerType) and isinstance(b, PointerType):
        return a.addrspace == b.addrspace and _same_tracked_type(a.pointee, b.pointee)
    return type(a) is type(b) and str(a) == str(b)


class ArrayType(Type):
    _name = "array"

    def __init__(self, element: Type, count: int) -> None:
        self.element = element
        self.count = count

    def __str__(self) -> str:
        count_text = str(self.count)
        element_text = str(self.element)
        if not count_text and count_text != "":
            raise TypeError("ArrayType.__str__ NULL count_text")
        if not element_text and element_text != "":
            raise TypeError("ArrayType.__str__ NULL element_text")
        result = _join_text(["[", count_text, " x ", element_text, "]"], "")
        if not result and result != "":
            raise TypeError("ArrayType.__str__ NULL join result")
        return result

    def gep(self, indices) -> Type:
        """Type-level GEP — array has uniform element type, so any
        index into it yields ``element`` (recursed if nested)."""
        idxs = list(indices)
        if not idxs:
            return self
        result = self.element
        if len(idxs) > 1:
            if isinstance(result, (BaseStructType, ArrayType)):
                return result.gep(idxs[1:])
        return result


class BaseStructType(Type):
    """Base class for both literal (anonymous) and identified (named)
    struct types. Shared ``gep`` helper computes the element type for
    a constant-expression GEP on the struct type; pcc codegen uses
    this to resolve typed accesses at compile time."""

    elements: tuple = ()

    def gep(self, indices) -> Type:
        """Type-level GEP — return the field type at index ``indices[0]``
        (pcc only uses single-index GEP on structs, matching llvmlite's
        static check). Extra indices recurse into nested struct types."""
        idxs = list(indices)
        if not idxs:
            return self
        first = idxs[0]
        # Accept either a Python int or a Constant.
        if isinstance(first, Value):
            try:
                first = int(first.value)  # Constant-like
            except (AttributeError, TypeError, ValueError):
                # Unknown index — best-effort fall through
                return self
        if first < 0 or first >= len(self.elements):
            return self
        result = self.elements[first]
        if len(idxs) > 1 and isinstance(result, (BaseStructType, ArrayType)):
            return result.gep(idxs[1:])
        return result


class LiteralStructType(BaseStructType):
    """Anonymous struct with inline field types (``{i32, ptr}``)."""

    _name = "struct"

    def __init__(self, elements: Iterable[Type], packed: bool = False) -> None:
        self.elements = tuple(elements)
        self.packed = packed

    def __str__(self) -> str:
        body_parts = []
        for e in self.elements:
            body_parts.append(str(e))
        body = _join_text(body_parts, ", ")
        if self.packed:
            return "<{ " + body + " }>"
        return "{ " + body + " }"


class IdentifiedStructType(BaseStructType):
    """Named struct — e.g. ``%struct.Point = type { i32, i32 }``.

    In LLVM IR, identified struct types are module-level declarations
    referenced by their ``%name`` prefix. Codegen uses
    ``Context.get_identified_type(name)`` to intern these per-name,
    then ``set_body(...)`` fills in the elements.
    """

    _name = "id_struct"

    def __init__(self, context: "Context", name: str) -> None:
        self.context = context
        self.name = name
        self.elements: tuple = ()
        self._body_set = False
        self.packed = False

    @property
    def is_opaque(self) -> bool:
        """llvmlite parity: ``True`` until ``set_body`` is called."""
        return not self._body_set

    def set_body(self, body, packed: bool = False) -> None:
        """Set the struct body. ``body`` is an iterable of Types.

        **Signature difference from llvmlite**: llvmlite uses
        ``set_body(*elements)`` (vararg, audit-flagged). We take a
        single iterable. Callers should route through
        ``pcc.llvm_capi.compat.set_struct_body(ty, body)`` which
        adapts to whichever backend is active.
        """
        self.elements = tuple(body)
        self.packed = packed
        self._body_set = True

    def get_declaration(self) -> str:
        """Top-level type declaration text for the module."""
        if not self._body_set:
            return "%" + str(self.name) + " = type opaque"
        body_parts = []
        for e in self.elements:
            body_parts.append(str(e))
        body = _join_text(body_parts, ", ")
        if self.packed:
            return "%" + str(self.name) + " = type <{ " + body + " }>"
        return "%" + str(self.name) + " = type { " + body + " }"

    def __str__(self) -> str:
        # As an operand, refer by its name — actual body is emitted
        # at module top via ``get_declaration``.
        return "%" + str(self.name)


def _type_gep_result(base_ty: Type, indices) -> Type:
    idxs = list(indices)
    result: Type = base_ty
    i = 0
    while i < len(idxs):
        if isinstance(result, ArrayType):
            result = result.element
        elif isinstance(result, BaseStructType):
            first = idxs[i]
            if isinstance(first, Value):
                try:
                    first = int(first.value)
                except (AttributeError, TypeError, ValueError):
                    return result
            if first < 0 or first >= len(result.elements):
                return result
            result = result.elements[first]
        else:
            return result
        i += 1
    return result


class Context:
    """LLVM context — interns identified struct types per name."""

    def __init__(self) -> None:
        self.identified_types: dict[str, IdentifiedStructType] = {}

    def get_identified_type(self, name: str) -> IdentifiedStructType:
        t = self.identified_types.get(name)
        if t is None:
            t = IdentifiedStructType(self, name)
            self.identified_types[name] = t
        return t


# Module-level global context — matches llvmlite's ``global_context``.
global_context = Context()


class FunctionType(Type):
    """Function type: ``return_ty (arg_ty, ...)`` — never appears naked in
    IR text but used to emit function prototypes."""

    _name = "fn"

    def __init__(
        self,
        return_type: Type,
        args: Iterable[Type],
        var_arg: bool = False,
    ) -> None:
        self.return_type = return_type
        self.args = tuple(args)
        self.var_arg = var_arg

    def __str__(self) -> str:
        arg_parts = []
        for a in self.args:
            arg_parts.append(str(a))
        args_text = _join_text(arg_parts, ", ")
        if self.var_arg:
            args_text = args_text + ", ..." if args_text else "..."
        return str(self.return_type) + " (" + args_text + ")"


# ---------------------------------------------------------------------------
# Values — anything that can appear as an operand. All carry a ``.type``.
# ---------------------------------------------------------------------------


class Value:
    """A named SSA value: carries a type and a reference form (``%tmp3``
    or ``@globvar`` or a constant literal).

    Optionally carries ``_instr`` — a back-reference to the
    ``InstructionRecord`` this Value's defining instruction produced.
    Setting ``.flags`` rewrites that record to include the fast-math
    flags (matches llvmlite's mutable ``flags`` list on instrs)."""

    __slots__ = (
        "type",
        "_ref",
        "_instr",
        "_flags",
        "_is_unsigned",
        "_pcc_unsigned_pointee",
        "_pcc_unsigned_return",
        "_direct_value_id",
        "_direct_name",
    )

    def __init__(self, ty: Type, ref: str) -> None:
        # ``""`` is a valid operand spelling for void sentinels; a NULL
        # pcc-native string is not.  Static StrType lowering can otherwise
        # let NULL pass an ``isinstance(..., str)`` check and poison a later
        # call-argument join far from the creator.
        if not ref and ref != "":
            raise TypeError("IR Value ref must be text, not NULL")
        self.type = ty
        self._ref = ref  # text used when this value is referenced as an operand
        self._instr = None
        self._flags: list[str] = []
        self._is_unsigned = False
        self._pcc_unsigned_pointee = False
        self._pcc_unsigned_return = False
        self._direct_value_id = -1
        self._direct_name = ""

    def __str__(self) -> str:
        return self._ref

    @property
    def flags(self) -> list[str]:
        return list(self._flags)

    @flags.setter
    def flags(self, value) -> None:
        self._flags = list(value)
        self._refresh_flags()

    def _refresh_flags(self) -> None:
        """Rewrite the defining instruction's text to include the
        current fast-math flags after the opcode. No-op if we don't
        have a back-reference to the record (e.g. for constants)."""
        if self._instr is None:
            return
        old = self._instr.text
        # Find the opcode position: optional "%name = " prefix, then opcode.
        eq = old.find(" = ")
        if eq >= 0:
            body = old[eq + 3 :]
            head = old[:eq] + " = "
        else:
            body = old
            head = ""
        # First token is opcode; inject flags between opcode and rest.
        split = body.find(" ")
        if split >= 0:
            op = body[:split]
            tail = body[split + 1 :]
        else:
            op = body
            tail = ""
        flag_parts = []
        for f in self._flags:
            flag_parts.append(" " + str(f))
        flag_text = _join_text(flag_parts, "").lstrip()
        if flag_text:
            new = str(head) + str(op) + " " + flag_text + " " + str(tail)
        else:
            new = str(head) + str(op) + " " + str(tail)
        self._instr.block._replace_record_text(self._instr, new)
        if self._instr._direct_record_id >= 0:
            direct_builder = self._instr.block.parent._direct_indexed_builder
            if direct_builder is not None:
                DirectIndexedFunctionBuilder.set_arithmetic_flags(
                    direct_builder,
                    self._instr._direct_record_id,
                    self._flags,
                )

    def bitcast(self, target_ty: Type) -> "Value":
        if _same_tracked_type(self.type, target_ty):
            return self
        expr = (
            "bitcast ("
            + str(self.type)
            + " "
            + str(self._ref)
            + " to "
            + str(target_ty)
            + ")"
        )
        return Value(target_ty, expr)

    def gep(self, indices, inbounds: bool = True) -> "Value":
        indices_list = list(indices)
        idx_parts = []
        for i in indices_list:
            idx_parts.append(str(i.type) + " " + str(i))
        idx_text = _join_text(idx_parts, ", ")
        base_ty = self.type.pointee if isinstance(self.type, PointerType) else self.type
        result_pointee = _type_gep_result(base_ty, indices_list[1:])
        inb = "inbounds " if inbounds else ""
        expr = (
            "getelementptr "
            + inb
            + "("
            + str(base_ty)
            + ", "
            + str(self.type)
            + " "
            + str(self._ref)
            + ", "
            + idx_text
            + ")"
        )
        return Value(PointerType(result_pointee), expr)


class Constant(Value):
    """Compile-time constant. ``ref`` is the constant's LLVM-text form."""

    def __init__(self, ty: Type, value) -> None:
        self.type = ty
        self.value = value
        self._direct_value_id = -1
        self._direct_name = ""
        self._ref = self._format(ty, value)
        self._instr = None
        self._flags: list[str] = []

    def inttoptr(self, target_ty: Type) -> Value:
        """Constant-expression inttoptr: returns a Value whose ref is
        the inline ``inttoptr (<ty> <val> to <target>)`` form.
        Matches llvmlite's chained-constant API used by codegen
        for label-address tables."""
        expr = (
            "inttoptr ("
            + str(self.type)
            + " "
            + str(self._ref)
            + " to "
            + str(target_ty)
            + ")"
        )
        return Value(target_ty, expr)

    def bitcast(self, target_ty: Type) -> Value:
        """Constant-expression bitcast. Same shape as ``inttoptr``."""
        if _same_tracked_type(self.type, target_ty):
            return self
        expr = (
            "bitcast ("
            + str(self.type)
            + " "
            + str(self._ref)
            + " to "
            + str(target_ty)
            + ")"
        )
        return Value(target_ty, expr)

    @staticmethod
    def _format_int(value) -> str:
        if value == 0:
            return "0"
        neg = value < 0
        if neg:
            value = 0 - value
        out = ""
        while value > 0:
            digit = value % 10
            if digit == 0:
                ch = "0"
            elif digit == 1:
                ch = "1"
            elif digit == 2:
                ch = "2"
            elif digit == 3:
                ch = "3"
            elif digit == 4:
                ch = "4"
            elif digit == 5:
                ch = "5"
            elif digit == 6:
                ch = "6"
            elif digit == 7:
                ch = "7"
            elif digit == 8:
                ch = "8"
            else:
                ch = "9"
            out = ch + out
            value = value // 10
        if neg:
            return "-" + out
        return out

    @staticmethod
    def _format_i8_array(value) -> str | None:
        parts = ['c"']
        digits = "0123456789ABCDEF"
        for v in value:
            if isinstance(v, Value) or not isinstance(v, int):
                return None
            if v < 0 or v > 255:
                return None
            hi = (v >> 4) & 15
            lo = v & 15
            parts.append("\\")
            parts.append(digits[hi])
            parts.append(digits[lo])
        parts.append('"')
        return _join_text(parts, "")

    @staticmethod
    def _format(ty: Type, value) -> str:
        # Already a Value/Constant — use its pre-formatted ref text.
        if isinstance(value, Value):
            return value._ref
        if value is None:
            # ``null`` / zeroinitializer depending on type
            if isinstance(ty, (PointerType,)):
                return "null"
            return "zeroinitializer"
        if isinstance(ty, IntType):
            if isinstance(value, bool):
                return "1" if value else "0"
            return Constant._format_int(value)
        if isinstance(ty, (FloatType, DoubleType, HalfType)):
            # Do not coerce with ``value * 1.0``: during self-host this path can
            # receive a boxed pcc float in a DynType slot, and dynamic ``*``
            # would dispatch through ``__mul__`` while the compiler is merely
            # trying to render a literal. The helper's float return type forces
            # pcc to unbox to a native double.
            f = _coerce_float64_ir(value)
            if isinstance(ty, FloatType):
                f = _round_to_float32_ir(f)
            elif isinstance(ty, HalfType):
                f = _round_to_float16_ir(f)
            raw = _float64_to_bits_ir(f)
            return _hex64(raw)
        if isinstance(ty, ArrayType):
            # [count x elem] [ <elem1>, <elem2>, ... ]
            # value is a sequence of Python-side values
            elem_ty = ty.element
            if isinstance(elem_ty, IntType) and elem_ty.width == 8:
                c_string = Constant._format_i8_array(value)
                if c_string is not None:
                    return c_string
            parts = []
            if isinstance(elem_ty, IntType):
                for v in value:
                    if isinstance(v, Value):
                        parts.append(v._ref)
                    elif isinstance(v, bool):
                        parts.append("1" if v else "0")
                    else:
                        parts.append(Constant._format_int(v))
            else:
                for v in value:
                    if isinstance(v, Value):
                        parts.append(v._ref)
                    else:
                        parts.append(Constant(elem_ty, v)._ref)
            # Each operand is printed as ``<elem_ty> <val>``
            elem_text = str(elem_ty)
            body_parts = []
            for p in parts:
                body_parts.append(elem_text + " " + str(p))
            body = _join_text(body_parts, ", ")
            return "[" + body + "]"
        if isinstance(ty, BaseStructType):
            # struct constant: { <ty1> <val1>, <ty2> <val2>, ... }
            parts = []
            for elem_ty, val in zip(ty.elements, value):
                c = Constant(elem_ty, val)
                parts.append(str(elem_ty) + " " + str(c._ref))
            return "{ " + _join_text(parts, ", ") + " }"
        # Fallback: stringify the raw value
        return str(value)


class Undefined:
    """Singleton sentinel for ``undef``."""

    def __str__(self) -> str:
        return "undef"


Undefined = Undefined()  # module-level singleton instance


# ---------------------------------------------------------------------------
# Metadata — the finite surface used by C ``-g`` debug information.
# ---------------------------------------------------------------------------


def _escape_metadata_string(value: str) -> str:
    out = []
    digits = "0123456789ABCDEF"
    for byte in value.encode("utf-8"):
        if 32 <= byte <= 126 and byte not in (34, 92):
            out.append(chr(byte))
        else:
            out.append("\\" + digits[(byte >> 4) & 15] + digits[byte & 15])
    return _join_text(out, "")


class DIToken:
    """Bare DWARF enumeration token such as ``DW_LANG_C99``."""

    def __init__(self, value: str) -> None:
        self.value = value


class MetaDataString:
    """String operand in a generic ``!{...}`` metadata node."""

    def __init__(self, parent: "Module", string: str) -> None:
        self.parent = parent
        self.string = string

    def render_operand(self) -> str:
        return '!"' + _escape_metadata_string(self.string) + '"'


class _MetadataNode:
    def __init__(self, parent: "Module", name: int) -> None:
        self.parent = parent
        self.name = name

    def get_reference(self) -> str:
        return "!" + str(self.name)

    def __str__(self) -> str:
        return self.get_reference()


def _render_metadata_operand(value) -> str:
    if isinstance(value, _MetadataNode):
        return value.get_reference()
    if isinstance(value, MetaDataString):
        return value.render_operand()
    if isinstance(value, Constant):
        return str(value.type) + " " + str(value)
    if value is None:
        return "null"
    raise TypeError("invalid generic metadata operand: " + repr(value))


class _MetadataTuple(_MetadataNode):
    def __init__(self, parent: "Module", name: int, operands) -> None:
        super().__init__(parent, name)
        self.operands = tuple(operands)

    def render(self) -> str:
        rendered = [_render_metadata_operand(value) for value in self.operands]
        return self.get_reference() + " = !{ " + _join_text(rendered, ", ") + " }"


def _render_di_operand(value) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, DIToken):
        return str(value.value)
    if isinstance(value, str):
        return '"' + _escape_metadata_string(value) + '"'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Constant):
        return str(value.type) + " " + str(value)
    if isinstance(value, _MetadataNode):
        return value.get_reference()
    raise TypeError("invalid debug metadata operand: " + repr(value))


class _DebugMetadataNode(_MetadataNode):
    def __init__(
        self,
        parent: "Module",
        name: int,
        kind: str,
        fields: dict,
        is_distinct: bool,
    ) -> None:
        super().__init__(parent, name)
        self.kind = kind
        self.fields = tuple(sorted(fields.items()))
        self.is_distinct = is_distinct

    def render(self) -> str:
        operands = []
        for key, value in self.fields:
            operands.append(str(key) + ": " + _render_di_operand(value))
        distinct = "distinct " if self.is_distinct else ""
        return (
            self.get_reference()
            + " = "
            + distinct
            + "!"
            + str(self.kind)
            + "("
            + _join_text(operands, ", ")
            + ")"
        )


def _render_metadata_definition(node: _MetadataNode) -> str:
    """Render one member of ``Module.metadata`` without dynamic fallback.

    ``Module.metadata`` is deliberately a closed collection in this IR
    implementation: ``add_metadata`` appends ``_MetadataTuple`` and
    ``add_debug_info`` appends ``_DebugMetadataNode``.  Calling
    ``node.render()`` through the common base loses that concrete type in the
    typed frontend and would pull in CPython merely to choose between these
    two native implementations.  Keep the closed dispatch explicit, and fail
    if a future metadata kind is added without extending the renderer.
    """
    if isinstance(node, _MetadataTuple):
        return node.render()
    if isinstance(node, _DebugMetadataNode):
        return node.render()
    raise TypeError("unsupported metadata definition: " + repr(node))


class _NamedMetadata:
    def __init__(self, parent: "Module", name: str) -> None:
        self.parent = parent
        self.name = name
        self.operands = []

    def add(self, node: _MetadataNode) -> None:
        if not isinstance(node, _MetadataNode):
            raise TypeError("named metadata requires a metadata node")
        self.operands.append(node)

    def render(self) -> str:
        refs = [node.get_reference() for node in self.operands]
        return "!" + self.name + " = !{ " + _join_text(refs, ", ") + " }"


# ---------------------------------------------------------------------------
# Function + Block containers
# ---------------------------------------------------------------------------


class Argument(Value):
    """Formal parameter of a Function.

    llvmlite convention: unnamed args get ``%.1``, ``%.2``, ... (note
    the leading ``.`` — these are *named* identifiers, not numbered
    unnamed ones). This keeps them out of the anonymous-temp number
    space so named locals inside the body don't collide with them.

    Codegen typically assigns a human name via ``arg.name = "x"``
    after construction — the property setter flips ``_ref`` to
    ``%x`` so instruction operands pick up the new name too.
    """

    def __init__(self, ty: Type, index: int) -> None:
        self.type = ty
        self.index = index
        self._direct_value_id = -1
        self._direct_name = "%." + str(index + 1)
        self._name = ""
        # Match llvmlite: args are ``%.1``, ``%.2``, ... (1-based)
        self._ref = "%." + str(index + 1)

    @property
    def name(self) -> str:
        return self._name

    @name.setter
    def name(self, value: str) -> None:
        self._name = value
        self._ref = "%" + str(value) if value else "%." + str(self.index + 1)
        self._direct_name = str(value) if value else "%." + str(self.index + 1)


class InstructionRecord:
    """A single emitted instruction — text form + opname tag.

    Codegen sometimes needs to know "is this an alloca?" or "is this
    a terminator?" when hoisting or positioning new instructions.
    llvmlite's Instruction has ``opname``; we expose the same property
    derived from the first token of the emitted text."""

    def __init__(self, text: str, opname: str, block: "Block") -> None:
        self.text = text
        self.opname = opname
        self.block = block
        self._metadata: dict[str, _MetadataNode] = {}
        # One compact-plane row ID on the existing record preserves final
        # insertion order without allocating a parallel instruction object.
        # -1 is the explicit text-fallback marker.
        self._direct_record_id = -1

    def __str__(self) -> str:
        if self.text:
            return self.text
        # Direct-only workers need only the opname for the two placement and
        # strict-fallback scans that stringify an instruction.  Reaching back
        # through the dynamically typed builder plane here makes this method a
        # libpython-only strict stub in pcc1; detailed projection remains an
        # explicit cold API on DirectIndexedFunctionBuilder.
        return self.opname

    def set_metadata(self, name: str, node: "_MetadataNode") -> None:
        """Attach instruction metadata using llvmlite's public API shape.

        C debug lowering adds locations only after the function body is
        complete, so the instruction text is stable here.  Keep the node map
        nevertheless: a repeated attachment replaces the prior suffix instead
        of emitting two ``!dbg`` operands.
        """
        if not isinstance(node, _MetadataNode):
            raise TypeError("instruction metadata requires a metadata node")
        prior = self._metadata.get(name)
        text = self.text
        if prior is not None:
            old_suffix = ", !" + str(name) + " " + prior.get_reference()
            if not text.endswith(old_suffix):
                raise ValueError(
                    "instruction metadata text is out of sync: " + str(name)
                )
            text = text[: -len(old_suffix)]
        self._metadata[name] = node
        text += ", !" + str(name) + " " + node.get_reference()
        self.block._replace_record_text(self, text)


def _opname_of(line: str) -> str:
    """Extract the LLVM opname from an emitted instruction text line."""
    stripped = line.strip()
    # Skip past an optional ``%name =`` assignment prefix
    if stripped.startswith("%"):
        eq = stripped.find(" = ")
        if eq >= 0:
            stripped = stripped[eq + 3 :]
    # The self-hosted pcc-Python runtime has to keep these opname strings in
    # long-lived InstructionRecord fields. Returning stable literals for the
    # common opcodes avoids allocating a short slice solely to store metadata.
    if stripped.startswith("call "):
        return "call"
    if stripped.startswith("br "):
        return "br"
    if stripped.startswith("ret void"):
        return "ret void"
    if stripped.startswith("ret "):
        return "ret"
    if stripped.startswith("switch "):
        return "switch"
    if stripped.startswith("alloca "):
        return "alloca"
    if stripped.startswith("load "):
        return "load"
    if stripped.startswith("store "):
        return "store"
    if stripped.startswith("unreachable"):
        return "unreachable"
    if stripped.startswith("phi "):
        return "phi"
    # First whitespace-delimited token is the opcode
    idx = stripped.find(" ")
    return stripped[:idx] if idx >= 0 else stripped


def _render_instruction_lines(
    lines: list[str],
    start: int,
    end: int,
) -> str:
    chunks: list[str] = []
    parts: list[str] = []
    line_count = 0
    i = start
    while i < end:
        parts.append("  ")
        # Block.append/insert already enforce text. Avoid str(existing_str):
        # pcc1-native can lose that borrowed result while rendering a large
        # module-top initializer.
        parts.append(lines[i])
        parts.append("\n")
        line_count += 1
        if line_count == 128:
            chunks.append(_join_text(parts, ""))
            parts = []
            line_count = 0
        i += 1
    if parts:
        chunks.append(_join_text(parts, ""))
    return _join_text(chunks, "")


class Block:
    """A basic block — a list of ``InstructionRecord`` objects plus an
    auto-assigned label. Instructions are kept as records that the
    Function serializer assembles into the final IR text."""

    def __init__(self, parent: "Function", name: str) -> None:
        self.parent = parent
        # llvmlite also exposes the containing function as ``.function``
        # on BasicBlock — alias for codegen that uses that name.
        self.function = parent
        self.name = name
        self._instrs: list[InstructionRecord] = []
        self._text_lines: list[str] = []
        self._terminated = False

    @property
    def is_terminated(self) -> bool:
        """True if the block ends with a terminator instruction."""
        return self._terminated

    @property
    def instructions(self) -> list[InstructionRecord]:
        """Emitted instructions as records (``.opname``, ``.text``)."""
        return list(self._instrs)

    def append(self, line: str) -> None:
        if line is None or not isinstance(line, str):
            raise TypeError(
                "IR block append requires text: function="
                + str(self.parent.name)
                + " block="
                + str(self.name)
                + " value_type="
                + str(type(line).__name__)
            )
        op = _opname_of(line)
        self._instrs.append(InstructionRecord(line, op, self))
        self._text_lines.append(line)

    def insert(self, idx: int, line: str) -> None:
        if line is None or not isinstance(line, str):
            raise TypeError(
                "IR block insert requires text: function="
                + str(self.parent.name)
                + " block="
                + str(self.name)
                + " value_type="
                + str(type(line).__name__)
            )
        self._instrs.insert(
            idx,
            InstructionRecord(line, _opname_of(line), self),
        )
        self._text_lines.insert(idx, line)

    def _replace_record_text(self, rec: InstructionRecord, text: str) -> None:
        rec.text = text
        i = 0
        while i < len(self._instrs):
            if self._instrs[i] is rec:
                self._text_lines[i] = text
                return
            i += 1

    def render(self) -> str:
        """Render as ``name:\\n  instr1\\n  instr2\\n``."""
        header = str(self.name) + ":\n"
        if _debug_ir_render_enabled() and self.parent.name == "user_prog_Parser_make":
            try:
                sys.stderr.write(
                    "[pcc.ir.block] "
                    + str(self.name)
                    + " lines="
                    + str(len(self._text_lines))
                    + "\n"
                )
                j = 0
                while j < len(self._text_lines):
                    line = self._text_lines[j]
                    if not isinstance(line, str):
                        sys.stderr.write(
                            "[pcc.ir.block] nonstr line i="
                            + str(j)
                            + " type="
                            + str(type(line).__name__)
                            + "\n"
                        )
                        break
                    if self.name == "t.owned.cont.15":
                        sys.stderr.write(
                            "[pcc.ir.block] line i="
                            + str(j)
                            + " len="
                            + str(len(line))
                            + " text="
                            + line[:80]
                            + "\n"
                        )
                    j += 1
            except Exception:
                pass
        if (
            _debug_ir_render_enabled()
            and self.name == "entry"
            and self.parent.name == "user_pcc_parse_py_lex__is_digit_code"
        ):
            try:
                first_text = "<none>"
                if len(self._instrs) > 0:
                    first_text = str(self._instrs[0].text)
                sys.stderr.write(
                    "[pcc.ir.render] block entry instrs="
                    + str(len(self._instrs))
                    + " first="
                    + first_text
                    + "\n"
                )
            except Exception:
                pass
        body = _render_instruction_lines(self._text_lines, 0, len(self._text_lines))
        if _debug_ir_render_enabled() and self.parent.name == "user_prog_Parser_make":
            try:
                sys.stderr.write(
                    "[pcc.ir.block] rendered "
                    + str(self.name)
                    + " type="
                    + str(type(body).__name__)
                    + " len="
                    + str(len(body))
                    + "\n"
                )
            except Exception:
                pass
        if (
            _debug_ir_render_enabled()
            and self.name == "entry"
            and self.parent.name == "user_pcc_parse_py_lex__is_digit_code"
        ):
            try:
                first_part = "<none>"
                first_part_len = -1
                first_render_len = -1
                second_part = "<none>"
                second_part_len = -1
                second_render_len = -1
                render2_len = -1
                render3_len = -1
                if len(self._instrs) > 0:
                    first_part = "  " + str(self._instrs[0].text)
                    first_part_len = len(first_part)
                    first_render_len = len(
                        _render_instruction_lines(self._text_lines, 0, 1)
                    )
                    if len(self._instrs) > 1:
                        second_part = "  " + str(self._instrs[1].text)
                        second_part_len = len(second_part)
                        second_render_len = len(
                            _render_instruction_lines(self._text_lines, 1, 2)
                        )
                    render2_len = len(_render_instruction_lines(self._text_lines, 0, 2))
                    render3_len = len(_render_instruction_lines(self._text_lines, 0, 3))
                sys.stderr.write(
                    "[pcc.ir.render] block entry stream=1 body_len="
                    + str(len(body))
                    + " parts="
                    + str(len(self._instrs))
                    + " first_part_len="
                    + str(first_part_len)
                    + " first_render_len="
                    + str(first_render_len)
                    + " second_part_len="
                    + str(second_part_len)
                    + " second_render_len="
                    + str(second_render_len)
                    + " render2_len="
                    + str(render2_len)
                    + " render3_len="
                    + str(render3_len)
                    + " first_part="
                    + first_part
                    + " second_part="
                    + second_part
                    + "\n"
                )
            except Exception:
                pass
        return _join_text([header, body], "")


class Function(Value):
    """A function in a Module. Holds a signature, blocks, and a name
    counter for temp %N identifiers.

    Subclasses ``Value`` because in LLVM IR a Function is also a typed
    SSA value: a global pointer to a function type. Codegen call sites
    that take ``Value`` accept ``Function`` directly without a separate
    downcast.
    """

    def __init__(
        self,
        module: Module,
        function_type: FunctionType,
        name: str = "",
    ) -> None:
        # As a value, a function has a pointer type — matches llvmlite
        # (``Function.type`` is ``PointerType(FunctionType, 0)``).
        Value.__init__(self, PointerType(function_type), "@" + str(name))
        self.module = module
        self.ftype = function_type
        # llvmlite also exposes ``function_type``; alias both so codegen
        # code that reads either attribute works.
        self.function_type = function_type
        self.name = name
        self.blocks: list[Block] = []
        # Formal parameter objects
        args_list: list[Argument] = []
        i = 0
        n_args = len(function_type.args)
        while i < n_args:
            args_list.append(Argument(function_type.args[i], i))
            i += 1
        self.args = tuple(args_list)
        # Anonymous-temp counter continues from args (%.1..%.N are args,
        # next temp is %.{N+1}). llvmlite uses the ``.`` prefix on all
        # auto-numbered identifiers — matches lets canonical-IR diff
        # stay clean.
        self._name_counter = len(self.args)
        self._block_counter = 0
        # Track explicit names used by blocks and the small number of
        # callers that require llvmlite-style stable deduplication. SSA
        # instruction names are allocated from ``_name_counter`` instead;
        # pcc-Python's dict is too expensive for per-instruction use in
        # self-hosted codegen.
        self._name_registry: dict[str, int] = {}
        self.linkage = ""
        self.attributes = FunctionAttributes()
        self.calling_convention = ""
        self._metadata: dict[str, _MetadataNode] = {}
        self._direct_indexed_builder = None
        self._direct_indexed_function_cache = None
        self._direct_first_libpython_callee = ""
        # Mark external if no blocks ever appended — subset of llvmlite's
        # behavior (``declare`` vs ``define``).
        module._functions.append(self)
        module.globals[name] = self

    def append_basic_block(self, name: str = "") -> Block:
        if not name:
            name = "bb" + str(self._block_counter)
            self._block_counter += 1
        else:
            # Dedup against existing blocks + SSA names in this
            # function. Matches llvmlite: repeated
            # ``append_basic_block("then")`` yields ``then``, ``then.1``...
            name = self._unique(name)
        blk = Block(self, name)
        self.blocks.append(blk)
        return blk

    def _fresh(self) -> str:
        """Generate a fresh ``.N`` identifier for anonymous temps
        (llvmlite convention: ``%.N``)."""
        self._name_counter += 1
        return "." + str(self._name_counter)

    def _unique(self, name: str) -> str:
        """Return a unique variant of ``name`` within this function.
        First use returns ``name`` unchanged; subsequent uses return
        ``name.1``, ``name.2``, etc. Matches llvmlite's behavior."""
        n = self._name_registry.get(name, 0)
        self._name_registry[name] = n + 1
        if n == 0:
            return name
        # Keep scanning upward in case ``name.K`` has already been
        # registered explicitly elsewhere.
        candidate = str(name) + "." + str(n)
        while candidate in self._name_registry:
            n += 1
            candidate = str(name) + "." + str(n)
        self._name_registry[candidate] = 1
        return candidate

    def __str__(self) -> str:
        """Function-as-operand: render as ``@name`` so it can be used
        directly as a call target or inside constant expressions."""
        return "@" + str(self.name)

    @property
    def return_value(self) -> "Value":
        """Sentinel value representing the function's return slot. Only
        its ``.type`` is meaningful — used by codegen to check the
        declared return type without looking up ``ftype``."""
        return Value(self.ftype.return_type, "<return>")

    @property
    def is_declaration(self) -> bool:
        return not self.blocks

    @property
    def entry_basic_block(self) -> "Block":
        """First block in the function — matches llvmlite's attribute."""
        if not self.blocks:
            raise ValueError("function has no blocks")
        return self.blocks[0]

    @property
    def basic_blocks(self) -> list["Block"]:
        """All basic blocks — matches llvmlite's attribute."""
        return list(self.blocks)

    def set_metadata(self, name: str, node: _MetadataNode) -> None:
        if not isinstance(node, _MetadataNode):
            raise TypeError("function metadata requires a metadata node")
        self._metadata[name] = node

    def render(self) -> str:
        """Render the function as LLVM IR text."""
        fty = self.ftype
        ret_ty = fty.return_type
        # Argument list text
        arg_parts = []
        i = 0
        while i < len(self.args):
            arg = self.args[i]
            if i < len(fty.args):
                arg_ty = fty.args[i]
            else:
                arg_ty = arg.type
            if arg._name:
                arg_parts.append(str(arg_ty) + " %" + str(arg._name))
            else:
                arg_parts.append(str(arg_ty) + " " + str(arg._ref))
            i += 1
        if fty.var_arg:
            arg_parts.append("...")
        args_text = _join_text(arg_parts, ", ")
        linkage = str(self.linkage) + " " if self.linkage else ""

        # Personality + attributes (declarations don't carry them).
        pers_text = ""
        attrs_text = ""
        metadata_text = ""
        if self.attributes.personality is not None:
            pers = self.attributes.personality
            pers_ty = PointerType(pers.ftype)
            pers_text = " personality " + str(pers_ty) + " @" + str(pers.name)
        if self.attributes._attrs:
            attrs_text = " " + _join_text(sorted(self.attributes._attrs), " ")
        if self._metadata:
            metadata_parts = []
            for key, node in self._metadata.items():
                metadata_parts.append("!" + str(key) + " " + node.get_reference())
            metadata_text = " " + _join_text(metadata_parts, " ")

        if not self.blocks:
            arg_type_parts = []
            i = 0
            while i < len(fty.args):
                t = fty.args[i]
                arg_type_parts.append(str(t))
                i += 1
            arg_type_only = _join_text(arg_type_parts, ", ")
            if fty.var_arg:
                arg_type_only = arg_type_only + ", ..." if arg_type_only else "..."
            return (
                "declare "
                + linkage
                + str(ret_ty)
                + " @"
                + str(self.name)
                + "("
                + arg_type_only
                + ")"
                + attrs_text
                + metadata_text
                + "\n"
            )
        if (
            _debug_ir_render_enabled()
            and self.name == "user_pcc_parse_py_lex__is_digit_code"
        ):
            try:
                entry_instrs = -1
                first_text = "<none>"
                if len(self.blocks) > 0:
                    entry_instrs = len(self.blocks[0]._instrs)
                    if entry_instrs > 0:
                        first_text = str(self.blocks[0]._instrs[0].text)
                sys.stderr.write(
                    "[pcc.ir.render] function "
                    + str(self.name)
                    + " blocks="
                    + str(len(self.blocks))
                    + " entry_instrs="
                    + str(entry_instrs)
                    + " first="
                    + first_text
                    + "\n"
                )
            except Exception:
                pass
        body_parts = []
        i = 0
        while i < len(self.blocks):
            b = self.blocks[i]
            rendered_block = b.render()
            if _debug_ir_render_enabled() and self.name == "user_prog_Parser_make":
                try:
                    if not isinstance(rendered_block, str):
                        sys.stderr.write(
                            "[pcc.ir.function] nonstr block i="
                            + str(i)
                            + " name="
                            + str(b.name)
                            + " type="
                            + str(type(rendered_block).__name__)
                            + "\n"
                        )
                except Exception:
                    pass
            body_parts.append(rendered_block)
            i += 1
        body = _join_text(body_parts, "\n")
        if _debug_ir_render_enabled() and self.name == "user_prog_Parser_make":
            try:
                sys.stderr.write(
                    "[pcc.ir.function] body type="
                    + str(type(body).__name__)
                    + " len="
                    + str(len(body))
                    + "\n"
                )
            except Exception:
                pass
        return _join_text(
            [
                "define ",
                linkage,
                str(ret_ty),
                " @",
                str(self.name),
                "(",
                args_text,
                ")",
                attrs_text,
                pers_text,
                metadata_text,
                " {\n",
                body,
                "}\n",
            ],
            "",
        )


def _value_ref(value) -> str:
    """Render an operand reference without relying on virtual ``__str__``.

    Self-hosted pcc currently exercises this path before all Python object
    dispatch edge cases are closed.  Function operands have an unambiguous LLVM
    spelling, so keep that critical case explicit.
    """
    if isinstance(value, Function):
        return "@" + str(value.name)
    try:
        name = value.name
        value.ftype
        if name:
            return "@" + str(name)
    except AttributeError:
        pass
    try:
        ref = value._ref
        if ref:
            return ref
    except AttributeError:
        pass
    try:
        index = value.index
        if index is not None:
            return "%." + str(index + 1)
    except AttributeError:
        pass
    return str(value)


def _looks_like_function(value) -> bool:
    if isinstance(value, Function):
        return True
    try:
        ftype = value.ftype
        name = value.name
    except AttributeError:
        return False
    return name is not None and _looks_like_function_type(ftype)


def _is_exact_function(value) -> bool:
    # Keep this check in an untyped helper.  Writing ``type(fn) is Function``
    # directly to the right of ``isinstance(fn, Function)`` lets the typed
    # frontend fold the second predicate after narrowing, which would admit
    # subclasses and bypass their dynamic attribute overrides.
    return type(value) is Function


def _looks_like_pointer_type(value) -> bool:
    if isinstance(value, PointerType):
        return True
    try:
        pointee = value.pointee
    except AttributeError:
        return False
    return pointee is not None


def _looks_like_function_type(value) -> bool:
    if isinstance(value, FunctionType):
        return True
    try:
        return_type = value.return_type
        args = value.args
    except AttributeError:
        return False
    return return_type is not None and args is not None


def _looks_like_void_type(value) -> bool:
    if isinstance(value, VoidType):
        return True
    return str(value) == "void"


class FunctionAttributes:
    """Function attributes set (``noreturn``, ``alwaysinline``, etc.) plus
    a ``personality`` slot for EH-emitting functions. Rendered inline
    with the function definition."""

    def __init__(self) -> None:
        # NOTE: backed by a list (sorted on read) instead of a set —
        # pcc1 self-host has a UAF in py_set.add on freshly-constructed
        # sets under tight heap. See
        # docs/investigations/pcc1-stage2-runtime-abi-set-segfault.md.
        self._attrs: list[str] = []
        self.personality: Optional["Function"] = None

    def add(self, attr: str) -> None:
        if attr not in self._attrs:
            self._attrs.append(attr)

    def __bool__(self) -> bool:
        return bool(self._attrs) or self.personality is not None


# ---------------------------------------------------------------------------
# GlobalVariable
# ---------------------------------------------------------------------------


class GlobalVariable(Value):
    """``@name = [linkage] global <ty> <init>`` (or external / internal)."""

    def __init__(
        self,
        module: "Module",
        ty: Type,
        name: str,
        addrspace: int = 0,
    ) -> None:
        # Native method parameters are borrowed.  Snapshot the heap-valued
        # inputs into local GC slots before PointerType/string/list/dict work
        # can allocate; the final module index must not reload a stale raw
        # ``name`` or ``module`` parameter.
        stable_module = module
        stable_type: Type = ty
        stable_name: str = name
        self.type = PointerType(stable_type, addrspace=addrspace)
        self.value_type = stable_type
        self.name = stable_name
        self.linkage = ""
        self.storage_class = ""
        self.global_constant = False
        self.initializer: Optional[Value] = None
        self.addrspace = addrspace
        self.section = ""
        self.align: Optional[int] = None
        self.unnamed_addr = False
        self._ref = "@" + stable_name
        self._direct_value_id = -1
        self._direct_name = ""
        stable_module._globals.append(self)
        stable_module.globals[stable_name] = self

    def gep(self, indices, inbounds: bool = True) -> Value:
        """Constant-expression GEP on this global — emits inline as
        ``getelementptr (inbounds) (<ty>, ptr @name, i32 i0, i32 i1, ...)``.
        Used by codegen to materialize a pointer-to-first-element of
        a global array or struct without a separate builder call."""
        indices_list = []
        for idx in indices:
            indices_list.append(idx)
        idx_parts = []
        for v in indices_list:
            idx_parts.append(str(v.type) + " " + str(v))
        idx_text = _join_text(idx_parts, ", ")
        inb = "inbounds " if inbounds else ""
        result_pointee = _type_gep_result(self.value_type, indices_list[1:])
        expr = (
            "getelementptr "
            + inb
            + "("
            + str(self.value_type)
            + ", "
            + str(self.type)
            + " @"
            + str(self.name)
            + ", "
            + idx_text
            + ")"
        )
        return Value(PointerType(result_pointee), expr)

    def render(self) -> str:
        linkage = str(self.linkage) + " " if self.linkage else ""
        storage_class = (
            str(self.storage_class) + " " if self.storage_class else ""
        )
        kind = "constant" if self.global_constant else "global"
        init_text = ""
        if self.initializer is not None:
            init_text = " " + str(self.initializer)
        else:
            init_text = " zeroinitializer" if self.linkage != "external" else ""
        align_text = ", align " + str(self.align) if self.align else ""
        return (
            "@"
            + str(self.name)
            + " = "
            + linkage
            + storage_class
            + kind
            + " "
            + str(self.value_type)
            + init_text
            + align_text
            + "\n"
        )


# ---------------------------------------------------------------------------
# Module
# ---------------------------------------------------------------------------


class Module:
    """Top-level IR container. Holds globals, functions, triple + layout
    strings, and named metadata."""

    def __init__(self, name: str = "", context: Optional[object] = None) -> None:
        self.name = name
        # Match llvmlite defaults so canonical-form diffs stay empty
        # when the caller doesn't explicitly set these.
        self.triple = "unknown-unknown-unknown"
        self.data_layout = ""
        self._functions: list[Function] = []
        self._globals: list[GlobalVariable] = []
        self.globals: dict = {}
        # Named metadata (e.g. ``!llvm.dbg.cu = !{!0}``) — β4.3 surface.
        self._named_metadata: dict[str, _NamedMetadata] = {}
        self.namedmetadata = self._named_metadata
        self.metadata: list[_MetadataNode] = []
        self.context = context or global_context
        self._name_counters: dict[str, int] = {}
        self._direct_indexed_supported_records = 0
        self._direct_indexed_fallback_records = 0

    def get_unique_name(self, base: str) -> str:
        """Return a unique variant of ``base`` within this module's
        global namespace. Matches llvmlite's ``Module.get_unique_name``."""
        n = self._name_counters.get(base, 0)
        self._name_counters[base] = n + 1
        if n == 0:
            return base
        return str(base) + "." + str(n)

    @property
    def functions(self) -> list[Function]:
        return list(self._functions)

    @property
    def global_values(self) -> list[GlobalVariable]:
        return list(self._globals)

    def get_global(self, name: str):
        """Retrieve a global by name (Function or GlobalVariable).
        Returns None if absent — matches llvmlite's ``.globals.get``."""
        return self.globals.get(name)

    def add_global(self, gv: GlobalVariable) -> None:
        if gv not in self._globals:
            self._globals.append(gv)
        self.globals[gv.name] = gv

    def add_metadata(self, operands):
        if not isinstance(operands, (list, tuple)):
            raise TypeError("expected a list or tuple of metadata values")
        normalized = []
        for value in operands:
            if isinstance(value, str):
                normalized.append(MetaDataString(self, value))
            else:
                normalized.append(value)
        node = _MetadataTuple(self, len(self.metadata), normalized)
        self.metadata.append(node)
        return node

    def add_named_metadata(self, name: str, node=None) -> _NamedMetadata:
        named = self._named_metadata.get(name)
        if named is None:
            named = _NamedMetadata(self, name)
            self._named_metadata[name] = named
        if node is not None:
            if not isinstance(node, _MetadataNode):
                node = self.add_metadata(node)
            named.add(node)
        return named

    def add_debug_info(
        self,
        node_type: str,
        fields: dict,
        is_distinct: bool = False,
    ):
        node = _DebugMetadataNode(
            self,
            len(self.metadata),
            node_type,
            fields,
            is_distinct,
        )
        self.metadata.append(node)
        return node

    def direct_indexed_module(self):
        """Project this builder state into the self backend's final kernel.

        The backend import is deliberately lazy: ordinary LLVM/text users do
        not acquire a self-backend dependency merely by constructing a Module.
        """
        from .direct_indexed_kernel import build_direct_indexed_module

        return build_direct_indexed_module(self)

    def __str__(self) -> str:
        parts: list[str] = []
        if _debug_ir_render_enabled():
            try:
                sys.stderr.write(
                    "[pcc.ir.module] start funcs="
                    + str(len(self._functions))
                    + " globals="
                    + str(len(self._globals))
                    + "\n"
                )
            except Exception:
                pass
        if self.name:
            parts.append('; ModuleID = "' + str(self.name) + '"')
        # Always emit triple + datalayout (even if empty) to match llvmlite
        parts.append('target triple = "' + str(self.triple) + '"')
        parts.append('target datalayout = "' + str(self.data_layout) + '"')
        parts.append("")
        # Identified struct type declarations — must precede globals
        # and functions that reference them.
        identified_type_values = list(self.context.identified_types.values())
        i = 0
        while i < len(identified_type_values):
            t = identified_type_values[i]
            parts.append(t.get_declaration())
            i += 1
        if self.context.identified_types:
            parts.append("")
        i = 0
        while i < len(self._globals):
            gv = self._globals[i]
            parts.append(gv.render())
            i += 1
        i = 0
        while i < len(self._functions):
            fn = self._functions[i]
            rendered_fn = fn.render()
            if _debug_ir_render_enabled():
                try:
                    if not isinstance(rendered_fn, str):
                        sys.stderr.write(
                            "[pcc.ir.module] nonstr function i="
                            + str(i)
                            + " name="
                            + str(fn.name)
                            + " type="
                            + str(type(rendered_fn).__name__)
                            + "\n"
                        )
                except Exception:
                    pass
            parts.append(rendered_fn)
            i += 1
        if self._named_metadata:
            for named in self._named_metadata.values():
                parts.append(named.render())
        i = 0
        while i < len(self.metadata):
            parts.append(_render_metadata_definition(self.metadata[i]))
            i += 1
        out = _join_text(parts, "\n")
        if _debug_ir_render_enabled():
            try:
                sys.stderr.write(
                    "[pcc.ir.module] end parts="
                    + str(len(parts))
                    + " out="
                    + str(len(out))
                    + "\n"
                )
            except Exception:
                pass
        return out


# ---------------------------------------------------------------------------
# IRBuilder
# ---------------------------------------------------------------------------


class IRBuilder:
    """Builder for emitting instructions into a Block.

    API-compatible subset of ``llvmlite.ir.IRBuilder`` covering the
    Tier 1-3 operations from docs/plans/llvmcapi-beta4-backlog.md.
    """

    # Sentinel for "append at end" insertion position. Using a huge
    # int keeps arithmetic with _anchor safe (``saved + 1``) — llvmlite
    # uses an actual index, not a symbol.
    _END = 10**18

    def __init__(self, block: Optional[Block] = None) -> None:
        # llvmlite uses ``_block`` internally; some pcc helpers poke
        # that name directly (marshal._stash_overflow_slot). Keep a
        # single private storage and expose via both names.
        self._block: Optional[Block] = block
        # Current source location, llvmlite's ``debug_location`` shape.  When
        # set, ``_emit`` stamps ``!dbg`` on every instruction it appends, so a
        # frontend marks statement boundaries once instead of decorating each
        # of the hundreds of emit sites individually.
        self.debug_location: Optional["_MetadataNode"] = None
        # Insertion position within the block — int index. _END means
        # "append to end" (clamped by _emit before use).
        self._pos: int = self._END
        self._fn: Optional[Function] = block.parent if block else None
        self._direct_indexed_capture = _env_flag_enabled(
            "PCC_DIRECT_INDEXED_KERNEL_CAPTURE"
        )
        self._direct_indexed_no_text = (
            self._direct_indexed_capture
            and _env_flag_enabled("PCC_DIRECT_INDEXED_KERNEL_EMIT")
            and not _env_flag_enabled("PCC_DIRECT_INDEXED_KERNEL_VALIDATE")
        )

    @property
    def block(self) -> Optional[Block]:
        return self._block

    @block.setter
    def block(self, value: Optional[Block]) -> None:
        self._block = value

    # llvmlite names its internal insertion cursor ``_anchor``. Some
    # pcc codegen helpers (marshal._stash_overflow_slot) save/restore
    # it around nested emissions. Expose a property alias over ``_pos``
    # so that code keeps working.
    @property
    def _anchor(self) -> int:
        if self._pos == self._END and self._block is not None:
            return len(self._block._instrs)
        return int(self._pos)

    @_anchor.setter
    def _anchor(self, value) -> None:
        self._pos = int(value)

    @property
    def function(self) -> Optional["Function"]:
        """Current function — matches llvmlite's ``IRBuilder.function``."""
        return self._fn

    def _direct_builder_plane(
        self,
    ):
        if not self._direct_indexed_capture or self._fn is None:
            return None
        direct_builder = self._fn._direct_indexed_builder
        if direct_builder is None:
            from .direct_indexed_kernel import DirectIndexedFunctionBuilder

            direct_builder = DirectIndexedFunctionBuilder(self._fn)
            self._fn._direct_indexed_builder = direct_builder
        return direct_builder

    # ------------- positioning -------------

    def position_at_end(self, block: Block) -> None:
        self._block = block
        self._fn = block.parent
        self._pos = self._END

    def position_at_start(self, block: Block) -> None:
        self._block = block
        self._fn = block.parent
        self._pos = 0

    def position_before(self, instr_or_block) -> None:
        """Position before a specific instruction record or at the
        start of a block. Matches llvmlite's API."""
        if isinstance(instr_or_block, Block):
            self.position_at_start(instr_or_block)
            return
        if isinstance(instr_or_block, InstructionRecord):
            # Find the record's index within its block
            blk = instr_or_block.block
            self._block = blk
            self._fn = blk.parent
            for i, rec in enumerate(blk._instrs):
                if rec is instr_or_block:
                    self._pos = i
                    return
            # Fallback — not found, position at end
            self._pos = "end"
            return
        # Fallback — position at start of the inferred block
        try:
            blk = instr_or_block.block
        except AttributeError:
            blk = None
        if blk is None:
            blk = self._block
        if blk is not None:
            self.position_at_start(blk)

    def append_basic_block(self, name: str = "") -> Block:
        """Shortcut — create a block in the current function."""
        if self._fn is None:
            raise ValueError("no current function")
        return self._fn.append_basic_block(name)

    # ------------- emission helpers -------------

    def _emit(self, line: str) -> "InstructionRecord":
        """Append ``line`` to the current block at the current position.

        Keeps ``_pos`` tracking the *next-insert* index correctly:
        after a positioned append (``_pos == len``), we advance ``_pos``
        so subsequent emits continue appending rather than starting to
        insert between already-emitted instructions. This mirrors
        llvmlite's anchor semantics.

        Returns the created ``InstructionRecord`` so callers can
        attach the defining instruction to the returned ``Value`` —
        required for mutable-flags rewrites (fast-math contract flag).
        """
        if self._block is None:
            raise ValueError("IRBuilder has no current block")
        blk = self._block
        n = len(blk._instrs)
        if self._pos >= n:
            blk.append(line)
            if self._pos != self._END:
                self._pos = len(blk._instrs)
            return self._stamp(blk._instrs[-1])
        blk.insert(self._pos, line)
        rec = blk._instrs[self._pos]
        self._pos += 1
        return self._stamp(rec)

    def _emit_direct(self, opname: str) -> "InstructionRecord":
        """Append a compact-only record for a direct emit worker.

        Canonical text is not needed by this worker.  The record still owns
        final insertion order/opname, and the two narrow frontend diagnostics
        project a minimal line from the compact plane on demand.
        """
        if self._block is None:
            raise ValueError("IRBuilder has no current block")
        block = self._block
        record = InstructionRecord("", opname, block)
        count = len(block._instrs)
        if self._pos >= count:
            block._instrs.append(record)
            block._text_lines.append("")
            if self._pos != self._END:
                self._pos = len(block._instrs)
            return record
        block._instrs.insert(self._pos, record)
        block._text_lines.insert(self._pos, "")
        self._pos += 1
        return record

    def _stamp(self, rec: "InstructionRecord") -> "InstructionRecord":
        """Attach the current debug location, if one is set.

        Terminators and instructions that already carry ``!dbg`` are left
        alone: re-stamping would rewrite text that ``set_metadata`` expects to
        end with the previous suffix.
        """
        loc = self.debug_location
        if loc is not None and "dbg" not in rec._metadata:
            rec.set_metadata("dbg", loc)
        return rec

    def _next_name(self, name: str) -> str:
        fn = self._fn
        if fn is None:
            raise ValueError("IRBuilder has no current function")
        fn._name_counter += 1
        counter_text = str(fn._name_counter)
        if name == "":
            return _join_text([".", counter_text], "")
        return _join_text([name, ".", counter_text], "")

    def _next(self, name: str, ty: Type) -> Value:
        """Produce a new local SSA value with ``name`` (or a fresh temp
        if empty). Named temporaries use a function-wide serial suffix,
        trading llvmlite text parity for O(1) self-hosted codegen."""
        fn = self._fn
        if fn is None:
            raise ValueError("IRBuilder has no current function")
        fn._name_counter += 1
        counter_text = str(fn._name_counter)
        # Direct concatenation, not _join_text: the list existed only to be
        # joined with an empty separator, so it was one list allocation per
        # emitted instruction for nothing.  This removes an allocation and adds
        # nothing -- unlike a cache, which pays a probe and a pinned key (two
        # such caches measured 3.8% and 4.7% SLOWER here).  Kept to few operands:
        # for the 9-part binop text a join still beats 8 intermediate strings.
        if name == "":
            ref = "%." + counter_text
        else:
            ref = "%" + name + "." + counter_text
        value = Value(ty, ref)
        value._direct_name = ref if name == "" else ref[1:]
        return value

    # ------------- return / branch / terminator -------------

    def ret(self, value: Value) -> Value:
        direct_builder = self._direct_builder_plane()
        if self._direct_indexed_no_text and direct_builder is not None:
            rec = self._emit_direct("ret")
        else:
            rec = self._emit("ret " + str(value.type) + " " + str(value))
        if direct_builder is not None:
            rec._direct_record_id = DirectIndexedFunctionBuilder.publish_terminator(
                direct_builder,
                "ret",
                value.type,
                value,
            )
        if self._block is not None:
            self._block._terminated = True
        return Value(VoidType(), "")

    def ret_void(self) -> Value:
        direct_builder = self._direct_builder_plane()
        if self._direct_indexed_no_text and direct_builder is not None:
            rec = self._emit_direct("ret void")
        else:
            rec = self._emit("ret void")
        if direct_builder is not None:
            rec._direct_record_id = DirectIndexedFunctionBuilder.publish_terminator(
                direct_builder,
                "ret_void",
            )
        if self._block is not None:
            self._block._terminated = True
        return Value(VoidType(), "")

    def branch(self, target: Block) -> Value:
        direct_builder = self._direct_builder_plane()
        if self._direct_indexed_no_text and direct_builder is not None:
            rec = self._emit_direct("br")
        else:
            rec = self._emit("br label %" + str(target.name))
        if direct_builder is not None:
            rec._direct_record_id = DirectIndexedFunctionBuilder.publish_terminator(
                direct_builder,
                "br",
                target0=str(target.name),
            )
        if self._block is not None:
            self._block._terminated = True
        return Value(VoidType(), "")

    def cbranch(self, cond: Value, t: Block, f: Block) -> Value:
        direct_builder = self._direct_builder_plane()
        if self._direct_indexed_no_text and direct_builder is not None:
            rec = self._emit_direct("br")
        else:
            line = "br i1 "
            line = line + str(cond)
            line = line + ", label %"
            line = line + str(t.name)
            line = line + ", label %"
            line = line + str(f.name)
            rec = self._emit(line)
        if direct_builder is not None:
            cond_ref = _value_ref(cond)
            if cond_ref in ("1", "true"):
                rec._direct_record_id = DirectIndexedFunctionBuilder.publish_terminator(
                    direct_builder,
                    "br",
                    target0=str(t.name),
                    target1=str(f.name),
                )
            elif cond_ref in ("0", "false", "undef", "poison"):
                rec._direct_record_id = DirectIndexedFunctionBuilder.publish_terminator(
                    direct_builder,
                    "br",
                    target0=str(f.name),
                    target1=str(t.name),
                )
            else:
                rec._direct_record_id = DirectIndexedFunctionBuilder.publish_terminator(
                    direct_builder,
                    "br_cond",
                    None,
                    cond,
                    str(t.name),
                    str(f.name),
                )
        if self._block is not None:
            self._block._terminated = True
        return Value(VoidType(), "")

    def unreachable(self) -> Value:
        direct_builder = self._direct_builder_plane()
        if self._direct_indexed_no_text and direct_builder is not None:
            rec = self._emit_direct("unreachable")
        else:
            rec = self._emit("unreachable")
        if direct_builder is not None:
            rec._direct_record_id = DirectIndexedFunctionBuilder.publish_terminator(
                direct_builder,
                "unreachable"
            )
        if self._block is not None:
            self._block._terminated = True
        return Value(VoidType(), "")

    def switch(self, value: Value, default_block: Block) -> "SwitchInstr":
        """Emit a ``switch`` terminator. Returns a ``SwitchInstr`` that
        the caller extends via ``add_case(int_value, target_block)``."""
        sw = SwitchInstr(self, value, default_block)
        direct_builder = self._direct_builder_plane()
        if self._direct_indexed_no_text and direct_builder is not None:
            sw._instr = self._emit_direct("switch")
        else:
            sw._instr = self._emit(sw._render())
        if direct_builder is not None:
            sw._instr._direct_record_id = DirectIndexedFunctionBuilder.publish_terminator(
                direct_builder,
                "switch",
                value.type,
                value,
                str(default_block.name),
            )
        if self._block is not None:
            self._block._terminated = True
        return sw

    # ------------- memory -------------

    def alloca(self, ty: Type, size: Optional[Value] = None, name: str = "") -> Value:
        v = self._next(name, PointerType(ty))
        direct_builder = self._direct_builder_plane()
        if self._direct_indexed_no_text and direct_builder is not None:
            rec = self._emit_direct("alloca")
        else:
            size_text = ""
            if size is not None:
                size_text = ", " + str(size.type) + " " + str(size)
            rec = self._emit(str(v) + " = alloca " + str(ty) + size_text)
        if direct_builder is not None:
            rec._direct_record_id = DirectIndexedFunctionBuilder.publish_alloca(
                direct_builder,
                v,
                ty,
            )
        return v

    def load(self, ptr: Value, name: str = "", align: Optional[int] = None) -> Value:
        pointee = ptr.type.pointee if isinstance(ptr.type, PointerType) else ptr.type
        v = self._next(name, pointee)
        direct_builder = self._direct_builder_plane()
        if self._direct_indexed_no_text and direct_builder is not None:
            rec = self._emit_direct("load")
        else:
            align_text = ", align " + str(align) if align else ""
            line = str(v)
            line = line + " = load "
            line = line + str(pointee)
            line = line + ", "
            line = line + str(ptr.type)
            line = line + " "
            line = line + str(ptr)
            line = line + align_text
            rec = self._emit(line)
        if direct_builder is not None:
            rec._direct_record_id = DirectIndexedFunctionBuilder.publish_load(
                direct_builder,
                v,
                pointee,
                ptr.type,
                ptr,
            )
        return v

    def store(self, value: Value, ptr: Value, align: Optional[int] = None) -> Value:
        stored_ty = (
            ptr.type.pointee if isinstance(ptr.type, PointerType) else value.type
        )
        direct_builder = self._direct_builder_plane()
        if self._direct_indexed_no_text and direct_builder is not None:
            rec = self._emit_direct("store")
        else:
            align_text = ", align " + str(align) if align else ""
            parts = [
                "store ",
                str(stored_ty),
                " ",
                _value_ref(value),
                ", ",
                str(ptr.type),
                " ",
                _value_ref(ptr),
                align_text,
            ]
            rec = self._emit(_join_text(parts, ""))
        if direct_builder is not None:
            rec._direct_record_id = DirectIndexedFunctionBuilder.publish_store(
                direct_builder,
                stored_ty,
                value,
                ptr.type,
                ptr,
            )
        return Value(VoidType(), "")

    def load_atomic(
        self,
        ptr: Value,
        ordering: str,
        align: int,
        name: str = "",
        typ: Optional[Type] = None,
    ) -> Value:
        pointee = (
            typ
            if typ is not None
            else (ptr.type.pointee if isinstance(ptr.type, PointerType) else ptr.type)
        )
        v = self._next(name, pointee)
        self._emit(
            str(v)
            + " = load atomic "
            + str(pointee)
            + ", "
            + str(ptr.type)
            + " "
            + str(ptr)
            + " "
            + str(ordering)
            + ", align "
            + str(align)
        )
        return v

    def store_atomic(
        self,
        value: Value,
        ptr: Value,
        ordering: str,
        align: int,
    ) -> Value:
        self._emit(
            "store atomic "
            + str(value.type)
            + " "
            + str(value)
            + ", "
            + str(ptr.type)
            + " "
            + str(ptr)
            + " "
            + str(ordering)
            + ", align "
            + str(align)
        )
        return Value(VoidType(), "")

    def fence(self, ordering: str, syncscope: Optional[str] = None) -> Value:
        scope_text = 'syncscope("' + str(syncscope) + '") ' if syncscope else ""
        self._emit("fence " + scope_text + str(ordering))
        return Value(VoidType(), "")

    def gep(
        self,
        ptr: Value,
        indices: Iterable[Value],
        inbounds: bool = False,
        name: str = "",
    ) -> Value:
        # Result pointee = drill into the base type using indices[1:]
        # (the first index steps through the pointer itself).
        indices_list = list(indices)
        base_ty = ptr.type.pointee if isinstance(ptr.type, PointerType) else ptr.type
        result_pointee = base_ty
        result_pointee = _type_gep_result(base_ty, indices_list[1:])
        # GEP result is a pointer to the drilled type
        result_ptr_ty = PointerType(result_pointee)
        v = self._next(name, result_ptr_ty)
        direct_builder = self._direct_builder_plane()
        if self._direct_indexed_no_text and direct_builder is not None:
            rec = self._emit_direct("getelementptr")
        else:
            idx_parts = []
            for i in indices_list:
                idx_parts.append(str(i.type) + " " + str(i))
            idx_text = _join_text(idx_parts, ", ")
            inb = "inbounds " if inbounds else ""
            line = str(v)
            line = line + " = getelementptr "
            line = line + inb
            line = line + str(base_ty)
            line = line + ", "
            line = line + str(ptr.type)
            line = line + " "
            line = line + str(ptr)
            line = line + ", "
            line = line + idx_text
            rec = self._emit(line)
        if direct_builder is not None:
            rec._direct_record_id = DirectIndexedFunctionBuilder.publish_gep(
                direct_builder,
                v,
                base_ty,
                ptr.type,
                ptr,
                indices_list,
            )
        return v

    # ------------- casts -------------

    def bitcast(self, value: Value, ty: Type, name: str = "") -> Value:
        try:
            value_ty = value.type
        except AttributeError:
            value_ty = None
        value_ref = _value_ref(value)
        if isinstance(value, Function):
            fn_name = value.name
            fn_type = value.ftype
            value_ty = PointerType(fn_type)
            value_ref = "@" + str(fn_name)
        if value_ty is None:
            value_ty = value.type
        if _same_tracked_type(value_ty, ty):
            return value
        v = self._next(name, ty)
        direct_builder = self._direct_builder_plane()
        if self._direct_indexed_no_text and direct_builder is not None:
            rec = self._emit_direct("bitcast")
        else:
            rec = self._emit(
                str(v)
                + " = bitcast "
                + str(value_ty)
                + " "
                + str(value_ref)
                + " to "
                + str(ty)
            )
        if direct_builder is not None:
            rec._direct_record_id = DirectIndexedFunctionBuilder.publish_cast(
                direct_builder,
                "bitcast",
                v,
                value_ty,
                value,
                ty,
            )
        return v

    def sext(self, value: Value, ty: Type, name: str = "") -> Value:
        if _same_llvm_text_type(value.type, ty):
            return value
        v = self._next(name, ty)
        rec = self._emit(
            str(v) + " = sext " + str(value.type) + " " + str(value) + " to " + str(ty)
        )
        direct_builder = self._direct_builder_plane()
        if direct_builder is not None:
            rec._direct_record_id = DirectIndexedFunctionBuilder.publish_cast(
                direct_builder, "sext", v, value.type, value, ty
            )
        return v

    def zext(self, value: Value, ty: Type, name: str = "") -> Value:
        if _same_llvm_text_type(value.type, ty):
            return value
        v = self._next(name, ty)
        rec = self._emit(
            str(v) + " = zext " + str(value.type) + " " + str(value) + " to " + str(ty)
        )
        direct_builder = self._direct_builder_plane()
        if direct_builder is not None:
            rec._direct_record_id = DirectIndexedFunctionBuilder.publish_cast(
                direct_builder, "zext", v, value.type, value, ty
            )
        return v

    def trunc(self, value: Value, ty: Type, name: str = "") -> Value:
        if _same_llvm_text_type(value.type, ty):
            return value
        v = self._next(name, ty)
        rec = self._emit(
            str(v) + " = trunc " + str(value.type) + " " + str(value) + " to " + str(ty)
        )
        direct_builder = self._direct_builder_plane()
        if direct_builder is not None:
            rec._direct_record_id = DirectIndexedFunctionBuilder.publish_cast(
                direct_builder, "trunc", v, value.type, value, ty
            )
        return v

    def ptrtoint(self, value: Value, ty: Type, name: str = "") -> Value:
        v = self._next(name, ty)
        rec = self._emit(
            str(v)
            + " = ptrtoint "
            + str(value.type)
            + " "
            + str(value)
            + " to "
            + str(ty)
        )
        direct_builder = self._direct_builder_plane()
        if direct_builder is not None:
            rec._direct_record_id = DirectIndexedFunctionBuilder.publish_cast(
                direct_builder, "ptrtoint", v, value.type, value, ty
            )
        return v

    def inttoptr(self, value: Value, ty: Type, name: str = "") -> Value:
        v = self._next(name, ty)
        rec = self._emit(
            str(v)
            + " = inttoptr "
            + str(value.type)
            + " "
            + str(value)
            + " to "
            + str(ty)
        )
        direct_builder = self._direct_builder_plane()
        if direct_builder is not None:
            rec._direct_record_id = DirectIndexedFunctionBuilder.publish_cast(
                direct_builder, "inttoptr", v, value.type, value, ty
            )
        return v

    def sitofp(self, value: Value, ty: Type, name: str = "") -> Value:
        v = self._next(name, ty)
        rec = self._emit(
            str(v)
            + " = sitofp "
            + str(value.type)
            + " "
            + str(value)
            + " to "
            + str(ty)
        )
        direct_builder = self._direct_builder_plane()
        if direct_builder is not None:
            rec._direct_record_id = DirectIndexedFunctionBuilder.publish_cast(
                direct_builder, "sitofp", v, value.type, value, ty
            )
        return v

    def uitofp(self, value: Value, ty: Type, name: str = "") -> Value:
        v = self._next(name, ty)
        rec = self._emit(
            str(v)
            + " = uitofp "
            + str(value.type)
            + " "
            + str(value)
            + " to "
            + str(ty)
        )
        direct_builder = self._direct_builder_plane()
        if direct_builder is not None:
            rec._direct_record_id = DirectIndexedFunctionBuilder.publish_cast(
                direct_builder, "uitofp", v, value.type, value, ty
            )
        return v

    def fptosi(self, value: Value, ty: Type, name: str = "") -> Value:
        v = self._next(name, ty)
        rec = self._emit(
            str(v)
            + " = fptosi "
            + str(value.type)
            + " "
            + str(value)
            + " to "
            + str(ty)
        )
        direct_builder = self._direct_builder_plane()
        if direct_builder is not None:
            rec._direct_record_id = DirectIndexedFunctionBuilder.publish_cast(
                direct_builder, "fptosi", v, value.type, value, ty
            )
        return v

    def fptoui(self, value: Value, ty: Type, name: str = "") -> Value:
        v = self._next(name, ty)
        rec = self._emit(
            str(v)
            + " = fptoui "
            + str(value.type)
            + " "
            + str(value)
            + " to "
            + str(ty)
        )
        direct_builder = self._direct_builder_plane()
        if direct_builder is not None:
            rec._direct_record_id = DirectIndexedFunctionBuilder.publish_cast(
                direct_builder, "fptoui", v, value.type, value, ty
            )
        return v

    def fpext(self, value: Value, ty: Type, name: str = "") -> Value:
        if _same_llvm_text_type(value.type, ty):
            return value
        v = self._next(name, ty)
        rec = self._emit(
            str(v) + " = fpext " + str(value.type) + " " + str(value) + " to " + str(ty)
        )
        direct_builder = self._direct_builder_plane()
        if direct_builder is not None:
            rec._direct_record_id = DirectIndexedFunctionBuilder.publish_cast(
                direct_builder, "fpext", v, value.type, value, ty
            )
        return v

    def fptrunc(self, value: Value, ty: Type, name: str = "") -> Value:
        if _same_llvm_text_type(value.type, ty):
            return value
        v = self._next(name, ty)
        rec = self._emit(
            str(v)
            + " = fptrunc "
            + str(value.type)
            + " "
            + str(value)
            + " to "
            + str(ty)
        )
        direct_builder = self._direct_builder_plane()
        if direct_builder is not None:
            rec._direct_record_id = DirectIndexedFunctionBuilder.publish_cast(
                direct_builder, "fptrunc", v, value.type, value, ty
            )
        return v

    # ------------- integer arithmetic -------------

    def _int_binop(self, op: str, lhs: Value, rhs: Value, name: str) -> Value:
        lhs_ty = lhs.type
        rhs_ty = rhs.type
        if not isinstance(lhs_ty, Type):
            if isinstance(rhs_ty, Type):
                lhs_ty = rhs_ty
            else:
                lhs_ty = IntType(64)
        v = self._next(name, lhs_ty)
        direct_builder = self._direct_builder_plane()
        if self._direct_indexed_no_text and direct_builder is not None:
            rec = self._emit_direct(str(op))
        else:
            parts = [
                str(v),
                " = ",
                str(op),
                " ",
                str(lhs_ty),
                " ",
                _value_ref(lhs),
                ", ",
                _value_ref(rhs),
            ]
            rec = self._emit(_join_text(parts, ""))
        if direct_builder is not None:
            rec._direct_record_id = DirectIndexedFunctionBuilder.publish_binop(
                direct_builder,
                op,
                v,
                lhs_ty,
                lhs,
                rhs,
            )
        return v

    def add(self, a: Value, b: Value, name: str = "") -> Value:
        return self._int_binop("add", a, b, name)

    def sub(self, a: Value, b: Value, name: str = "") -> Value:
        return self._int_binop("sub", a, b, name)

    def mul(self, a: Value, b: Value, name: str = "") -> Value:
        return self._int_binop("mul", a, b, name)

    def sdiv(self, a: Value, b: Value, name: str = "") -> Value:
        return self._int_binop("sdiv", a, b, name)

    def udiv(self, a: Value, b: Value, name: str = "") -> Value:
        return self._int_binop("udiv", a, b, name)

    def srem(self, a: Value, b: Value, name: str = "") -> Value:
        return self._int_binop("srem", a, b, name)

    def urem(self, a: Value, b: Value, name: str = "") -> Value:
        return self._int_binop("urem", a, b, name)

    def and_(self, a: Value, b: Value, name: str = "") -> Value:
        return self._int_binop("and", a, b, name)

    def or_(self, a: Value, b: Value, name: str = "") -> Value:
        return self._int_binop("or", a, b, name)

    def xor(self, a: Value, b: Value, name: str = "") -> Value:
        return self._int_binop("xor", a, b, name)

    def shl(self, a: Value, b: Value, name: str = "") -> Value:
        return self._int_binop("shl", a, b, name)

    def ashr(self, a: Value, b: Value, name: str = "") -> Value:
        return self._int_binop("ashr", a, b, name)

    def lshr(self, a: Value, b: Value, name: str = "") -> Value:
        return self._int_binop("lshr", a, b, name)

    def neg(self, value: Value, name: str = "") -> Value:
        zero = Constant(value.type, 0)
        return self.sub(zero, value, name)

    def not_(self, value: Value, name: str = "") -> Value:
        """Bitwise NOT / logical complement. Emits ``xor value, -1``
        (or for i1: ``xor value, 1``). Matches llvmlite."""
        # For i1 (boolean), XOR with 1 flips the bit. For wider int,
        # XOR with -1 (all ones) gives bitwise complement.
        if isinstance(value.type, IntType) and value.type.width == 1:
            ones = Constant(value.type, 1)
        else:
            ones = Constant(value.type, -1)
        return self.xor(value, ones, name)

    # ------------- floating arithmetic -------------

    def _fp_binop(self, op: str, lhs: Value, rhs: Value, name: str) -> Value:
        v = self._next(name, lhs.type)
        direct_builder = self._direct_builder_plane()
        if self._direct_indexed_no_text and direct_builder is not None:
            rec = self._emit_direct(str(op))
        else:
            rec = self._emit(
                str(v)
                + " = "
                + str(op)
                + " "
                + str(lhs.type)
                + " "
                + str(lhs)
                + ", "
                + str(rhs)
            )
        if direct_builder is not None:
            rec._direct_record_id = DirectIndexedFunctionBuilder.publish_fbinop(
                direct_builder,
                op,
                v,
                lhs.type,
                lhs,
                rhs,
            )
        # Attach the emitted record so flags (fast-math contract etc.)
        # can be added retroactively via ``v.flags = [...]``.
        v._instr = rec
        return v

    def fadd(self, a: Value, b: Value, name: str = "") -> Value:
        return self._fp_binop("fadd", a, b, name)

    def fsub(self, a: Value, b: Value, name: str = "") -> Value:
        return self._fp_binop("fsub", a, b, name)

    def fmul(self, a: Value, b: Value, name: str = "") -> Value:
        return self._fp_binop("fmul", a, b, name)

    def fdiv(self, a: Value, b: Value, name: str = "") -> Value:
        return self._fp_binop("fdiv", a, b, name)

    def frem(self, a: Value, b: Value, name: str = "") -> Value:
        return self._fp_binop("frem", a, b, name)

    def fneg(self, value: Value, name: str = "") -> Value:
        v = self._next(name, value.type)
        direct_builder = self._direct_builder_plane()
        if self._direct_indexed_no_text and direct_builder is not None:
            rec = self._emit_direct("fneg")
        else:
            rec = self._emit(
                str(v) + " = fneg " + str(value.type) + " " + str(value)
            )
        if direct_builder is not None:
            rec._direct_record_id = DirectIndexedFunctionBuilder.publish_fneg(
                direct_builder,
                v,
                value.type,
                value,
            )
        v._instr = rec
        return v

    # ------------- comparisons -------------

    def icmp_signed(self, op: str, a: Value, b: Value, name: str = "") -> Value:
        pred_map = {
            "==": "eq",
            "!=": "ne",
            "<": "slt",
            "<=": "sle",
            ">": "sgt",
            ">=": "sge",
        }
        pred = pred_map.get(op, op)
        v = self._next(name, IntType(1))
        direct_builder = self._direct_builder_plane()
        if self._direct_indexed_no_text and direct_builder is not None:
            rec = self._emit_direct("icmp")
        else:
            line = str(v)
            line = line + " = icmp "
            line = line + str(pred)
            line = line + " "
            line = line + str(a.type)
            line = line + " "
            line = line + str(a)
            line = line + ", "
            line = line + str(b)
            rec = self._emit(line)
        if direct_builder is not None:
            rec._direct_record_id = DirectIndexedFunctionBuilder.publish_icmp(
                direct_builder,
                pred,
                v,
                a.type,
                a,
                b,
            )
        # Attach the record: an inline error edge anchors entry hoists on
        # the condition's own defining instruction.
        v._instr = rec
        return v

    def icmp_unsigned(self, op: str, a: Value, b: Value, name: str = "") -> Value:
        pred_map = {
            "==": "eq",
            "!=": "ne",
            "<": "ult",
            "<=": "ule",
            ">": "ugt",
            ">=": "uge",
        }
        pred = pred_map.get(op, op)
        v = self._next(name, IntType(1))
        direct_builder = self._direct_builder_plane()
        if self._direct_indexed_no_text and direct_builder is not None:
            rec = self._emit_direct("icmp")
        else:
            line = str(v)
            line = line + " = icmp "
            line = line + str(pred)
            line = line + " "
            line = line + str(a.type)
            line = line + " "
            line = line + str(a)
            line = line + ", "
            line = line + str(b)
            rec = self._emit(line)
        if direct_builder is not None:
            rec._direct_record_id = DirectIndexedFunctionBuilder.publish_icmp(
                direct_builder,
                pred,
                v,
                a.type,
                a,
                b,
            )
        # Attach the record: an inline error edge anchors entry hoists on
        # the condition's own defining instruction.
        v._instr = rec
        return v

    def fcmp_ordered(self, op: str, a: Value, b: Value, name: str = "") -> Value:
        pred_map = {
            "==": "oeq",
            "!=": "one",
            "<": "olt",
            "<=": "ole",
            ">": "ogt",
            ">=": "oge",
        }
        pred = pred_map.get(op, op)
        v = self._next(name, IntType(1))
        direct_builder = self._direct_builder_plane()
        if self._direct_indexed_no_text and direct_builder is not None:
            rec = self._emit_direct("fcmp")
        else:
            rec = self._emit(
                str(v)
                + " = fcmp "
                + str(pred)
                + " "
                + str(a.type)
                + " "
                + str(a)
                + ", "
                + str(b)
            )
        if direct_builder is not None:
            rec._direct_record_id = DirectIndexedFunctionBuilder.publish_fcmp(
                direct_builder,
                pred,
                v,
                a.type,
                a,
                b,
            )
        return v

    def fcmp_unordered(self, op: str, a: Value, b: Value, name: str = "") -> Value:
        pred_map = {
            "==": "ueq",
            "!=": "une",
            "<": "ult",
            "<=": "ule",
            ">": "ugt",
            ">=": "uge",
        }
        pred = pred_map.get(op, op)
        v = self._next(name, IntType(1))
        direct_builder = self._direct_builder_plane()
        if self._direct_indexed_no_text and direct_builder is not None:
            rec = self._emit_direct("fcmp")
        else:
            rec = self._emit(
                str(v)
                + " = fcmp "
                + str(pred)
                + " "
                + str(a.type)
                + " "
                + str(a)
                + ", "
                + str(b)
            )
        if direct_builder is not None:
            rec._direct_record_id = DirectIndexedFunctionBuilder.publish_fcmp(
                direct_builder,
                pred,
                v,
                a.type,
                a,
                b,
            )
        return v

    # ------------- call -------------

    def call(
        self,
        fn: Function | Value,
        args: Iterable[Value],
        name: str = "",
        tail: bool = False,
    ) -> Value:
        return _irbuilder_call_from_args_list(self, fn, list(args), name, tail)

    def call4_i32(
        self,
        fn: Function | Value,
        arg0: Value,
        arg1: Value,
        arg2: Value,
        arg3: int,
        name: str = "",
        tail: bool = False,
    ) -> Value:
        """Emit a four-argument call whose final operand is a raw i32.

        Kept as a small compatibility wrapper for call sites that pass a raw
        source line number into runtime helpers such as py_exc_append_frame.
        """
        del tail
        return self.call(
            fn,
            [arg0, arg1, arg2, Constant(IntType(32), arg3)],
            name=name,
        )

    # ------------- select / phi -------------

    def select(
        self,
        cond: Value,
        then_v: Value,
        else_v: Value,
        name: str = "",
    ) -> Value:
        v = self._next(name, then_v.type)
        direct_builder = self._direct_builder_plane()
        if self._direct_indexed_no_text and direct_builder is not None:
            rec = self._emit_direct("select")
        else:
            rec = self._emit(
                str(v)
                + " = select "
                + str(cond.type)
                + " "
                + str(cond)
                + ", "
                + str(then_v.type)
                + " "
                + str(then_v)
                + ", "
                + str(else_v.type)
                + " "
                + str(else_v)
            )
        if direct_builder is not None:
            rec._direct_record_id = DirectIndexedFunctionBuilder.publish_select(
                direct_builder,
                v,
                then_v.type,
                cond,
                then_v,
                else_v,
            )
        return v

    def phi(self, ty: Type, name: str = "") -> "PhiInstr":
        phi_name = self._next_name(name)
        v = PhiInstr(self, ty, phi_name)
        direct_builder = self._direct_builder_plane()
        if self._direct_indexed_no_text and direct_builder is not None:
            rec = self._emit_direct("phi")
        else:
            rec = self._emit(v._placeholder_line)
        v._instr = rec
        if direct_builder is not None:
            rec._direct_record_id = DirectIndexedFunctionBuilder.publish_phi(
                direct_builder,
                v,
                ty,
            )
        return v

    # ------------- landingpad / invoke / extract_value -------------

    def invoke(
        self,
        fn: Function | Value,
        args: Iterable[Value],
        normal_block: Block,
        unwind_block: Block,
        name: str = "",
    ) -> Value:
        args_list = list(args)
        arg_parts = []
        for a in args_list:
            arg_parts.append(str(a.type) + " " + str(a))
        args_text = _join_text(arg_parts, ", ")
        if isinstance(fn, Function):
            callee_ref = "@" + str(fn.name)
            ret_ty = fn.ftype.return_type
            arg_type_parts = []
            for t in fn.ftype.args:
                arg_type_parts.append(str(t))
            arg_types = _join_text(arg_type_parts, ", ")
            sig_text = str(ret_ty) + " (" + arg_types + ")"
        else:
            callee_ref = str(fn)
            fty = fn.type.pointee
            ret_ty = fty.return_type
            arg_type_parts = []
            for t in fty.args:
                arg_type_parts.append(str(t))
            arg_types = _join_text(arg_type_parts, ", ")
            if fty.var_arg:
                arg_types = arg_types + ", ..." if arg_types else "..."
            sig_text = str(ret_ty) + " (" + arg_types + ")"

        if isinstance(ret_ty, VoidType):
            self._emit(
                "invoke "
                + sig_text
                + " "
                + callee_ref
                + "("
                + args_text
                + ") to label %"
                + str(normal_block.name)
                + " unwind label %"
                + str(unwind_block.name)
            )
            if self._block is not None:
                self._block._terminated = True
            return Value(VoidType(), "")
        v = self._next(name, ret_ty)
        self._emit(
            str(v)
            + " = invoke "
            + sig_text
            + " "
            + callee_ref
            + "("
            + args_text
            + ") to label %"
            + str(normal_block.name)
            + " unwind label %"
            + str(unwind_block.name)
        )
        if self._block is not None:
            self._block._terminated = True
        return v

    def atomic_rmw(
        self,
        op: str,
        ptr: Value,
        val: Value,
        ordering: str,
        name: str = "",
    ) -> Value:
        v = self._next(name, val.type)
        self._emit(
            str(v)
            + " = atomicrmw "
            + str(op)
            + " "
            + str(ptr.type)
            + " "
            + str(ptr)
            + ", "
            + str(val.type)
            + " "
            + str(val)
            + " "
            + str(ordering)
        )
        return v

    def cmpxchg(
        self,
        ptr: Value,
        cmp: Value,
        val: Value,
        success_ordering: str,
        failure_ordering: str,
        name: str = "",
    ) -> Value:
        pair_ty = LiteralStructType([cmp.type, _I1])
        v = self._next(name, pair_ty)
        self._emit(
            str(v)
            + " = cmpxchg "
            + str(ptr.type)
            + " "
            + str(ptr)
            + ", "
            + str(cmp.type)
            + " "
            + str(cmp)
            + ", "
            + str(val.type)
            + " "
            + str(val)
            + " "
            + str(success_ordering)
            + " "
            + str(failure_ordering)
        )
        return v

    def syscall6(
        self,
        nr: Value,
        a1: Value,
        a2: Value,
        a3: Value,
        a4: Value,
        a5: Value,
        a6: Value,
        name: str = "",
    ) -> Value:
        """Raw Linux x86_64 syscall as an inline-asm call (musl ABI).

        One fixed shape so the self backend can recognize it exactly:
        rax=nr, args in rdi/rsi/rdx/r10/r8/r9, rcx/r11/memory clobbered.
        """
        v = self._next(name, IntType(64))
        args = [nr, a1, a2, a3, a4, a5, a6]
        arg_parts = []
        for arg in args:
            arg_parts.append("i64 " + _value_ref(arg))
        self._emit(
            str(v)
            + ' = call i64 asm sideeffect "syscall", '
            + '"={rax},{rax},{rdi},{rsi},{rdx},{r10},{r8},{r9},'
            + '~{rcx},~{r11},~{memory}"('
            + _join_text(arg_parts, ", ")
            + ")"
        )
        return v

    def landingpad(
        self,
        ty: Type,
        name: str = "",
        cleanup: bool = False,
    ) -> "LandingPadInstr":
        v = LandingPadInstr(self, ty, self._next_name(name), cleanup)
        self._emit(v._placeholder_line)
        return v

    def extract_value(self, agg: Value, indices, name: str = "") -> Value:
        if isinstance(indices, int):
            indices = [indices]
        # Element type is tricky without struct-type info; default to i32
        # for aggregates we don't know (pcc's uses are typically phi-
        # aggregates of (ptr, i32) where index 1 → i32).
        if isinstance(agg.type, LiteralStructType) and isinstance(indices[0], int):
            elem_ty = agg.type.elements[indices[0]]
        else:
            elem_ty = IntType(32)
        v = self._next(name, elem_ty)
        direct_builder = self._direct_builder_plane()
        if self._direct_indexed_no_text and direct_builder is not None:
            rec = self._emit_direct("extractvalue")
        else:
            idx_parts = []
            for i in indices:
                idx_parts.append(str(i))
            idx_text = _join_text(idx_parts, ", ")
            rec = self._emit(
                str(v)
                + " = extractvalue "
                + str(agg.type)
                + " "
                + str(agg)
                + ", "
                + idx_text
            )
        if direct_builder is not None:
            rec._direct_record_id = DirectIndexedFunctionBuilder.publish_extractvalue(
                direct_builder,
                v,
                agg.type,
                agg,
                indices,
            )
        return v


def _irbuilder_call_from_args_list(
    builder: IRBuilder,
    fn: Function | Value,
    args_list,
    name: str = "",
    tail: bool = False,
) -> Value:
    if _debug_ir_call_trace_enabled():
        try:
            sys.stderr.write("[pcc.ir.call] enter argc=" + str(len(args_list)) + "\n")
        except Exception:
            pass
    # None is the allocation-free sentinel here: pcc-native lowering
    # materializes even ``()`` as py_tuple_new(0).  Function signatures and
    # function pointers materialize their argument sequence below.
    expected_arg_types = None
    expected_arg_count = 0
    is_vararg_call = False
    render_call_text = not builder._direct_indexed_no_text
    if _is_exact_function(fn):
        # Keep exact Functions statically narrowed so pcc1 reads their fields
        # by constant index.  Subclasses stay on the dynamic path below so
        # descriptors and __getattribute__ overrides keep Python semantics.
        # Signature memoization itself was measured and denied: a
        # Function-owned entry cut instructions but improved median wall by
        # only 1.46%, below its 1.08x acceptance line.
        exact_fn: Function = fn
        current_ftype = exact_fn.ftype
        callee_ref = "@" + str(exact_fn.name)
        ret_ty = current_ftype.return_type
        expected_arg_types = []
        arg_type_parts = []
        for t in current_ftype.args:
            expected_arg_types.append(t)
            if render_call_text:
                arg_type_parts.append(str(t))
        expected_arg_count = len(expected_arg_types)
        is_vararg_call = bool(current_ftype.var_arg)
        if render_call_text:
            arg_types = _join_text(arg_type_parts, ", ")
            if current_ftype.var_arg:
                arg_types = arg_types + ", ..." if arg_types else "..."
            sig_text = str(ret_ty) + " (" + arg_types + ")"
        else:
            sig_text = ""
    elif _looks_like_function(fn):
        # Compatibility duck-functions preserve the old generic behavior.
        # Only an exact Function uses the static slot contract above.
        callee_ref = "@" + str(fn.name)
        current_ftype = fn.ftype
        ret_ty = current_ftype.return_type
        expected_arg_types = []
        arg_type_parts = []
        for t in current_ftype.args:
            expected_arg_types.append(t)
            if render_call_text:
                arg_type_parts.append(str(t))
        expected_arg_count = len(expected_arg_types)
        is_vararg_call = bool(current_ftype.var_arg)
        if render_call_text:
            arg_types = _join_text(arg_type_parts, ", ")
            if current_ftype.var_arg:
                arg_types = arg_types + ", ..." if arg_types else "..."
            sig_text = str(ret_ty) + " (" + arg_types + ")"
        else:
            sig_text = ""
    else:
        callee_ref = str(fn)
        if _looks_like_pointer_type(fn.type) and _looks_like_function_type(
            fn.type.pointee
        ):
            fty = fn.type.pointee
            ret_ty = fty.return_type
            expected_arg_types = []
            arg_type_parts = []
            for t in fty.args:
                expected_arg_types.append(t)
                if render_call_text:
                    arg_type_parts.append(str(t))
            expected_arg_count = len(expected_arg_types)
            is_vararg_call = bool(fty.var_arg)
            if render_call_text:
                arg_types = _join_text(arg_type_parts, ", ")
                if fty.var_arg:
                    arg_types = arg_types + ", ..." if arg_types else "..."
                sig_text = str(ret_ty) + " (" + arg_types + ")"
            else:
                sig_text = ""
        else:
            ret_ty = VoidType()
            sig_text = "void ()"

    if not render_call_text:
        direct_builder = builder._direct_builder_plane()
        if direct_builder is None:
            raise RuntimeError("direct call record requested without builder plane")
        direct_types = [] if expected_arg_types is None else expected_arg_types
        if _looks_like_void_type(ret_ty):
            rec = builder._emit_direct("call")
            rec._direct_record_id = DirectIndexedFunctionBuilder.publish_call(
                direct_builder,
                None,
                ret_ty,
                callee_ref,
                callee_ref.startswith("%"),
                args_list,
                direct_types,
                expected_arg_count,
                is_vararg_call,
            )
            return Value(VoidType(), "")
        result = builder._next(name, ret_ty)
        rec = builder._emit_direct("call")
        rec._direct_record_id = DirectIndexedFunctionBuilder.publish_call(
            direct_builder,
            result,
            ret_ty,
            callee_ref,
            callee_ref.startswith("%"),
            args_list,
            direct_types,
            expected_arg_count,
            is_vararg_call,
        )
        return result

    if _debug_ir_call_trace_enabled():
        try:
            sys.stderr.write(
                "[pcc.ir.call] sig callee="
                + str(callee_ref)
                + " expected="
                + str(expected_arg_count)
                + "\n"
            )
        except Exception:
            pass

    arg_parts = []
    i = 0
    while i < len(args_list):
        a = args_list[i]
        if i < expected_arg_count:
            arg_ty = expected_arg_types[i]
        else:
            arg_ty = a.type
        # Keep every owned text result in a named local across both allocating
        # concatenations.  An inline ``str(arg_ty) + ... + _value_ref(a)``
        # leaves a call result as an unrooted SSA temporary in pcc1-native
        # code.  Building a three-item list for _join_text is also unnecessary
        # work in this per-argument hot path.
        arg_ty_text = str(arg_ty)
        arg_ref_text = _value_ref(a)
        arg_text = arg_ty_text + " "
        arg_text = arg_text + arg_ref_text
        arg_parts.append(arg_text)
        i += 1
    args_text = ", ".join(arg_parts)
    tail_prefix = "tail " if tail else ""
    if _looks_like_void_type(ret_ty):
        line = tail_prefix + "call " + sig_text + " " + callee_ref + "("
        line = line + args_text + ")"
        if _debug_ir_call_trace_enabled():
            try:
                sys.stderr.write("[pcc.ir.call] emit void\n")
            except Exception:
                pass
        rec = builder._emit(line)
        direct_builder = builder._direct_builder_plane()
        if direct_builder is not None:
            direct_types = [] if expected_arg_types is None else expected_arg_types
            rec._direct_record_id = DirectIndexedFunctionBuilder.publish_call(
                direct_builder,
                None,
                ret_ty,
                callee_ref,
                callee_ref.startswith("%"),
                args_list,
                direct_types,
                expected_arg_count,
                is_vararg_call,
            )
        return Value(VoidType(), "")
    v = builder._next(name, ret_ty)
    line = str(v) + " = " + tail_prefix + "call " + sig_text + " " + callee_ref
    line = line + "(" + args_text + ")"
    if _debug_ir_call_trace_enabled():
        try:
            sys.stderr.write("[pcc.ir.call] emit value\n")
        except Exception:
            pass
    rec = builder._emit(line)
    direct_builder = builder._direct_builder_plane()
    if direct_builder is not None:
        direct_types = [] if expected_arg_types is None else expected_arg_types
        rec._direct_record_id = DirectIndexedFunctionBuilder.publish_call(
            direct_builder,
            v,
            ret_ty,
            callee_ref,
            callee_ref.startswith("%"),
            args_list,
            direct_types,
            expected_arg_count,
            is_vararg_call,
        )
    return v


def _irbuilder_call_direct_exact_fixed(
    builder: IRBuilder,
    fn: Function,
    arg_count: int,
    arg0=None,
    arg1=None,
) -> Value:
    """Write an exact arity-0/1/2 call without transient argument lists."""
    if _debug_ir_call_trace_enabled():
        if arg_count == 0:
            return _irbuilder_call_from_args_list(builder, fn, [])
        if arg_count == 1:
            return _irbuilder_call_from_args_list(builder, fn, [arg0])
        return _irbuilder_call_from_args_list(builder, fn, [arg0, arg1])

    function_type = fn.ftype
    ret_ty = function_type.return_type
    direct_builder = builder._direct_builder_plane()
    if direct_builder is None:
        raise RuntimeError("direct call record requested without builder plane")

    if _looks_like_void_type(ret_ty):
        result = None
        rec = builder._emit_direct("call")
    else:
        result = builder._next("", ret_ty)
        rec = builder._emit_direct("call")

    record_id = publish_exact_call_fixed(
        direct_builder,
        result,
        fn,
        arg_count,
        arg0,
        arg1,
    )
    rec._direct_record_id = record_id
    if result is None:
        return Value(VoidType(), "")
    return result


# ---------------------------------------------------------------------------
# Phi + LandingPad — mutated after construction via add_incoming / add_clause
# ---------------------------------------------------------------------------


class PhiInstr(Value):
    """Phi node — initially an empty placeholder; ``add_incoming`` appends
    incoming-value clauses before the IR is rendered."""

    def __init__(self, builder: IRBuilder, ty: Type, name: str) -> None:
        self.type = ty
        self._ref = "%" + str(name)
        self._direct_value_id = -1
        self._direct_name = (
            self._ref
            if name.startswith(".") and name[1:].isdigit()
            else str(name)
        )
        self._instr = None
        self._builder = builder
        # A phi is owned by the block in which it was created.  Incoming
        # edges are commonly added only after the builder has emitted a
        # backedge in another block, so refreshing through builder._block
        # would silently edit the wrong block (and leave the phi incomplete).
        self._parent_block = builder._block
        self._incomings: list[tuple[Value, Block]] = []
        self._placeholder_line = ""
        if not builder._direct_indexed_no_text:
            self._refresh()

    def _refresh(self) -> None:
        if self._builder._direct_indexed_no_text:
            return
        pair_parts = []
        for v, b in self._incomings:
            pair_parts.append("[" + str(v) + ", %" + str(b.name) + "]")
        pairs = ""
        if len(pair_parts) == 1:
            pairs = pair_parts[0]
        elif len(pair_parts) >= 2:
            pairs = pair_parts[0] + ", " + pair_parts[1]
            i = 2
            while i < len(pair_parts):
                pairs = pairs + ", " + pair_parts[i]
                i += 1
        if pairs:
            self._placeholder_line = (
                str(self) + " = phi " + str(self.type) + " " + pairs
            )
        else:
            self._placeholder_line = str(self) + " = phi " + str(self.type)
        # Update the emitted line in-place — scan records for the
        # prior placeholder and replace its text field.
        blk: Block = self._parent_block
        if blk is None:
            return
        i = 0
        while i < len(blk._instrs):
            rec = blk._instrs[i]
            if rec.text.startswith(str(self) + " = phi"):
                blk._replace_record_text(rec, self._placeholder_line)
                return
            i += 1

    def add_incoming(self, value: Value, block: Block) -> None:
        _phi_add_incoming_canonical(self, value, block)


class LandingPadInstr(Value):
    """Landingpad — exception personality + clauses."""

    def __init__(self, builder: IRBuilder, ty: Type, name: str, cleanup: bool) -> None:
        self.type = ty
        self._ref = "%" + str(name)
        self._direct_value_id = -1
        self._direct_name = ""
        self._builder = builder
        self._clauses: list[str] = []
        self._cleanup = cleanup
        self._placeholder_line = ""
        self._refresh()

    def _refresh(self) -> None:
        clause_text = _join_text(self._clauses, "\n    ") if self._clauses else ""
        cleanup_text = " cleanup" if self._cleanup else ""
        if clause_text:
            line = (
                str(self)
                + " = landingpad "
                + str(self.type)
                + "\n    "
                + clause_text
                + cleanup_text
            )
        else:
            line = str(self) + " = landingpad " + str(self.type) + cleanup_text
        self._placeholder_line = line
        blk = self._builder.block
        if blk is None:
            return
        i = 0
        while i < len(blk._instrs):
            rec = blk._instrs[i]
            if rec.text.startswith(str(self) + " = landingpad"):
                blk._replace_record_text(rec, line)
                return
            i += 1

    def add_clause(self, clause: "CatchClause | FilterClause") -> None:
        self._clauses.append(clause.render())
        self._refresh()


class CatchClause:
    """Catch clause for a landingpad: ``catch <ty> <val>``."""

    def __init__(self, value: Value) -> None:
        self.value = value

    def render(self) -> str:
        return "catch " + str(self.value.type) + " " + str(self.value)


class SwitchInstr:
    """``switch <ty> <val>, label %<default> [ <case-list> ]``.

    Created by ``IRBuilder.switch``. Cases accumulate via
    ``add_case(int_value, target_block)``; each case appears on its
    own indented line in the final text.
    """

    def __init__(
        self,
        builder: IRBuilder,
        value: Value,
        default: Block,
    ) -> None:
        self.builder = builder
        self.value = value
        self.default = default
        self.cases: list[tuple[int, Block]] = []
        self._instr = None

    def add_case(self, int_value, target: Block) -> None:
        """Register a ``<int> -> %target`` case."""
        _switch_add_case_canonical(self, int_value, target)

    def _render(self) -> str:
        val_ty = self.value.type
        parts = []
        for iv, blk in self.cases:
            parts.append(str(val_ty) + " " + str(iv) + ", label %" + str(blk.name))
        cases_text = _join_text(parts, " ")
        body = " [ " + cases_text + " ]" if cases_text else " [ ]"
        return (
            "switch "
            + str(val_ty)
            + " "
            + str(self.value)
            + ", label %"
            + str(self.default.name)
            + body
        )

    def _refresh(self) -> None:
        blk = self.builder.block
        if blk is None:
            return
        # Replace the placeholder line in the current block's
        # instruction list. Expected prefix: "switch <ty>".
        new_line = self._render()
        if self._instr is not None:
            self._instr.block._replace_record_text(self._instr, new_line)
            return
        idx = 0
        while idx < len(blk._instrs):
            rec = blk._instrs[idx]
            if rec.text.startswith("switch "):
                blk._replace_record_text(rec, new_line)
                return
            idx += 1


class FilterClause:
    """Filter clause: ``filter <ty> [<val>, ...]``."""

    def __init__(self, ty: Type, values: Iterable[Value]) -> None:
        self.ty = ty
        self.values = tuple(values)

    def render(self) -> str:
        val_parts = []
        for v in self.values:
            val_parts.append(str(v.type) + " " + str(v))
        vals_text = _join_text(val_parts, ", ")
        return "filter " + str(self.ty) + " [" + vals_text + "]"


# ---------------------------------------------------------------------------
# ON-mode scaffold ABI helpers.
# ---------------------------------------------------------------------------
#
# The Python frontend's closed-world IR scaffold calls these helpers from the
# compiler being self-hosted. They allocate real pcc.llvm_capi.ir objects and
# keep target-program runtime symbols as IR objects, not native addresses in
# the compiler binary.


def scaffold_IntType(width: int):
    return IntType(width)


def scaffold_PointerType(pointee):
    return PointerType(pointee)


def scaffold_ArrayType(element, count: int):
    return ArrayType(element, count)


def scaffold_Constant_i64(ty, value: int):
    return Constant(ty, value)


def scaffold_Constant_f64(ty, value: float):
    return Constant(ty, value)


def scaffold_Constant_none(ty):
    return Constant(ty, None)


def scaffold_Constant_obj(ty, value):
    return Constant(ty, value)


def scaffold_IRBuilder(block):
    return IRBuilder(block)


def scaffold_Context():
    return Context()


def scaffold_IdentifiedStructType(context, name):
    return IdentifiedStructType(context, name)


def scaffold_Value(ty, ref):
    return Value(ty, str(ref))


def VoidType___init__():
    return VoidType()


def FloatType___init__():
    return FloatType()


def HalfType___init__():
    return HalfType()


def DoubleType___init__():
    return DoubleType()


def FunctionType___init__0(return_type):
    return FunctionType(return_type, ())


def FunctionType___init__1(return_type, arg0):
    return FunctionType(return_type, (arg0,))


def FunctionType___init__1_varargs(return_type, arg0):
    return FunctionType(return_type, (arg0,), var_arg=True)


def FunctionType___init__0_dyn_va(return_type, var_arg):
    return FunctionType(return_type, (), var_arg=bool(var_arg))


def FunctionType___init__1_dyn_va(return_type, arg0, var_arg):
    return FunctionType(return_type, (arg0,), var_arg=bool(var_arg))


def FunctionType___init__2_dyn_va(return_type, arg0, arg1, var_arg):
    return FunctionType(return_type, (arg0, arg1), var_arg=bool(var_arg))


def FunctionType___init__2(return_type, arg0, arg1):
    return FunctionType(return_type, (arg0, arg1))


def FunctionType___init__3(return_type, arg0, arg1, arg2):
    return FunctionType(return_type, (arg0, arg1, arg2))


def FunctionType___init__4(return_type, arg0, arg1, arg2, arg3):
    return FunctionType(return_type, (arg0, arg1, arg2, arg3))


def FunctionType___init__5(return_type, arg0, arg1, arg2, arg3, arg4):
    return FunctionType(return_type, (arg0, arg1, arg2, arg3, arg4))


def FunctionType___init__6(return_type, arg0, arg1, arg2, arg3, arg4, arg5):
    return FunctionType(return_type, (arg0, arg1, arg2, arg3, arg4, arg5))


def FunctionType___init__7(return_type, arg0, arg1, arg2, arg3, arg4, arg5, arg6):
    return FunctionType(return_type, (arg0, arg1, arg2, arg3, arg4, arg5, arg6))


def FunctionType___init__8(
    return_type,
    arg0,
    arg1,
    arg2,
    arg3,
    arg4,
    arg5,
    arg6,
    arg7,
):
    return FunctionType(
        return_type,
        (arg0, arg1, arg2, arg3, arg4, arg5, arg6, arg7),
    )


def FunctionType___init___dyn(return_type, args, var_arg):
    return FunctionType(return_type, args, var_arg=bool(var_arg))


def Function___init___named(module, function_type, name):
    return Function(module, function_type, name=name)


def GlobalVariable___init___named(module, ty, name):
    return GlobalVariable(module, ty, name=name)


def Module___init___named(name):
    return Module(name=name)


def scaffold_Module___init__():
    return Module(name="")


def LiteralStructType___init__0():
    # ``ir.LiteralStructType([])`` -- an empty literal struct.  The C
    # frontend's union lowering opens with one
    # (``pcc/codegen/c_types.py``), so a pcc1 carrying the C frontend needs
    # this arity; without it the scaffold's
    # ``user_pcc_llvm_capi_ir_LiteralStructType___init__0`` stayed undefined
    # and the linked stage died at startup with a dyld symbol error.
    return LiteralStructType(())


def LiteralStructType___init__1(arg0):
    return LiteralStructType((arg0,))


def LiteralStructType___init__2(arg0, arg1):
    return LiteralStructType((arg0, arg1))


def LiteralStructType___init__3(arg0, arg1, arg2):
    return LiteralStructType((arg0, arg1, arg2))


def LiteralStructType___init__4(arg0, arg1, arg2, arg3):
    return LiteralStructType((arg0, arg1, arg2, arg3))


def LiteralStructType___init__5(arg0, arg1, arg2, arg3, arg4):
    return LiteralStructType((arg0, arg1, arg2, arg3, arg4))


def LiteralStructType___init__6(arg0, arg1, arg2, arg3, arg4, arg5):
    return LiteralStructType((arg0, arg1, arg2, arg3, arg4, arg5))


def LiteralStructType___init__7(arg0, arg1, arg2, arg3, arg4, arg5, arg6):
    return LiteralStructType((arg0, arg1, arg2, arg3, arg4, arg5, arg6))


def IRBuilder_call0(builder, fn):
    if builder._direct_indexed_no_text and _is_exact_function(fn):
        return _irbuilder_call_direct_exact_fixed(builder, fn, 0)
    return _irbuilder_call_from_args_list(builder, fn, [])


def IRBuilder_call1(builder, fn, arg0):
    if builder._direct_indexed_no_text and _is_exact_function(fn):
        return _irbuilder_call_direct_exact_fixed(builder, fn, 1, arg0)
    return _irbuilder_call_from_args_list(builder, fn, [arg0])


def IRBuilder_call2(builder, fn, arg0, arg1):
    if builder._direct_indexed_no_text and _is_exact_function(fn):
        return _irbuilder_call_direct_exact_fixed(builder, fn, 2, arg0, arg1)
    return _irbuilder_call_from_args_list(builder, fn, [arg0, arg1])


def IRBuilder_call3(builder, fn, arg0, arg1, arg2):
    return _irbuilder_call_from_args_list(builder, fn, [arg0, arg1, arg2])


def IRBuilder_call4(builder, fn, arg0, arg1, arg2, arg3):
    return _irbuilder_call_from_args_list(builder, fn, [arg0, arg1, arg2, arg3])


def IRBuilder_call4_i32(builder, fn, arg0, arg1, arg2, arg3: int):
    return IRBuilder.call4_i32(builder, fn, arg0, arg1, arg2, arg3)


def IRBuilder_call5(builder, fn, arg0, arg1, arg2, arg3, arg4):
    return _irbuilder_call_from_args_list(builder, fn, [arg0, arg1, arg2, arg3, arg4])


def IRBuilder_call6(builder, fn, arg0, arg1, arg2, arg3, arg4, arg5):
    return _irbuilder_call_from_args_list(
        builder, fn, [arg0, arg1, arg2, arg3, arg4, arg5]
    )


def IRBuilder_call7(builder, fn, arg0, arg1, arg2, arg3, arg4, arg5, arg6):
    return _irbuilder_call_from_args_list(
        builder, fn, [arg0, arg1, arg2, arg3, arg4, arg5, arg6]
    )


def IRBuilder_call8(
    builder,
    fn,
    arg0,
    arg1,
    arg2,
    arg3,
    arg4,
    arg5,
    arg6,
    arg7,
):
    return _irbuilder_call_from_args_list(
        builder,
        fn,
        [arg0, arg1, arg2, arg3, arg4, arg5, arg6, arg7],
    )


def LiteralStructType_dyn(elements):
    """Dynamic-list constructor for the closed-world LiteralStructType
    scaffold path.  Mirrors ``IRBuilder_call_dyn``: a literal per-arity
    extern is impossible when the element count is only known at runtime,
    so the caller passes a pre-built list handle."""
    return LiteralStructType(tuple(elements))


def IRBuilder_call_dyn(builder, fn, args):
    return IRBuilder.call(builder, fn, args)


def IRBuilder_emit_raw(builder, line: str):
    return IRBuilder._emit(builder, line)


def IRBuilder_publish_direct_raw_call(
    builder,
    record,
    dest_ref,
    ret_type,
    callee_ref,
    arg_type_texts,
    arg_ref_texts,
) -> None:
    direct_builder = IRBuilder._direct_builder_plane(builder)
    if direct_builder is None:
        return
    record._direct_record_id = DirectIndexedFunctionBuilder.publish_raw_call(
        direct_builder,
        dest_ref,
        ret_type,
        callee_ref,
        arg_type_texts,
        arg_ref_texts,
    )


def IRBuilder_next_value(builder, name: str, typ):
    return IRBuilder._next(builder, name, typ)


def IRBuilder_current_instruction_count(builder) -> int:
    block = builder._block
    if block is None:
        return 0
    return len(block._instrs)


def IRBuilder_instruction_text_at(builder, index: int) -> str:
    block = builder._block
    if block is None:
        return ""
    if index < 0 or index >= len(block._instrs):
        return ""
    record = block._instrs[index]
    if record.text:
        return record.text
    if record._direct_record_id >= 0:
        direct_builder = block.parent._direct_indexed_builder
        if direct_builder is not None:
            return DirectIndexedFunctionBuilder.diagnostic_record_text(
                direct_builder,
                record._direct_record_id
            )
    return ""


def IRBuilder_can_inline_error_edge(builder) -> bool:
    return bool(
        _DIRECT_INLINE_ERROR_EDGE_CAPTURE_ENABLED
        and builder._direct_indexed_no_text
        and builder._block is not None
        and not builder._block._terminated
    )


def IRBuilder_try_inline_error_edge(
    builder,
    condition,
    error_block,
    source_line: int,
    cleanup_plan_id: int = 0,
    payload: int = -1,
) -> bool:
    if not IRBuilder_can_inline_error_edge(builder) or cleanup_plan_id != 0:
        return False
    direct_builder = IRBuilder._direct_builder_plane(builder)
    if direct_builder is None:
        return False
    DirectIndexedFunctionBuilder.publish_inline_error_edge(
        direct_builder,
        str(builder._block.name),
        condition,
        str(error_block.name),
        int(source_line),
        int(cleanup_plan_id),
        int(payload),
    )
    return True


def IRBuilder_declare_inline_error_landing(builder, block, slot) -> bool:
    """Declare ``block`` as a shared frame landing reading payload ``slot``."""
    direct_builder = IRBuilder._direct_builder_plane(builder)
    if direct_builder is None:
        return False
    DirectIndexedFunctionBuilder.publish_inline_error_landing(
        direct_builder,
        str(block.name),
        slot,
    )
    return True


def IRBuilder_gep0(builder, ptr):
    return ptr


def IRBuilder_gep1(builder, ptr, idx0):
    return IRBuilder.gep(builder, ptr, (idx0,))


def IRBuilder_gep1_inbounds(builder, ptr, idx0):
    return IRBuilder.gep(builder, ptr, (idx0,), inbounds=True)


def IRBuilder_gep2(builder, ptr, idx0, idx1):
    return IRBuilder.gep(builder, ptr, (idx0, idx1))


def IRBuilder_gep2_inbounds(builder, ptr, idx0, idx1):
    return IRBuilder.gep(builder, ptr, (idx0, idx1), inbounds=True)


def IRBuilder_gep3(builder, ptr, idx0, idx1, idx2):
    return IRBuilder.gep(builder, ptr, (idx0, idx1, idx2))


def IRBuilder_gep3_inbounds(builder, ptr, idx0, idx1, idx2):
    return IRBuilder.gep(builder, ptr, (idx0, idx1, idx2), inbounds=True)


def IRBuilder_gep_dyn(builder, ptr, indices):
    return IRBuilder.gep(builder, ptr, indices)


def IRBuilder_gep_dyn_inbounds(builder, ptr, indices):
    return IRBuilder.gep(builder, ptr, indices, inbounds=True)


def _phi_add_incoming_canonical(
    phi: PhiInstr,
    value: Value,
    block: Block,
) -> None:
    phi._incomings.append((value, block))
    if phi._instr is not None and phi._instr._direct_record_id >= 0:
        direct_builder = phi._instr.block.parent._direct_indexed_builder
        if direct_builder is not None:
            DirectIndexedFunctionBuilder.append_phi_incoming(
                direct_builder,
                phi._instr._direct_record_id,
                value,
                str(block.name),
            )
    phi._refresh()


def _switch_add_case_canonical(
    switch_inst: SwitchInstr,
    int_value,
    target: Block,
) -> None:
    if isinstance(int_value, Value):
        try:
            int_value = int(int_value.value)
        except (AttributeError, TypeError, ValueError):
            int_value = int_value._ref
    switch_inst.cases.append((int_value, target))
    if (
        switch_inst._instr is not None
        and switch_inst._instr._direct_record_id >= 0
    ):
        direct_builder = (
            switch_inst._instr.block.parent._direct_indexed_builder
        )
        if direct_builder is not None:
            DirectIndexedFunctionBuilder.append_switch_case(
                direct_builder,
                switch_inst._instr._direct_record_id,
                int_value,
                str(target.name),
            )
    switch_inst._refresh()


def IRBuilder_add_incoming(phi: PhiInstr, value: Value, block: Block):
    _phi_add_incoming_canonical(phi, value, block)


def scaffold_SwitchInstr_add_case_i64(
    switch_inst: SwitchInstr,
    int_value: int,
    target: Block,
):
    _switch_add_case_canonical(switch_inst, int_value, target)


def IRBuilder_as_pointer(ty):
    return PointerType(ty)


def scaffold_IRBuilder_append_basic_block(builder, name):
    return scaffold_Function_append_basic_block(builder._fn, name)


def scaffold_Function_append_basic_block(fn, name):
    if not name:
        name = "bb"
    blk = Block(fn, name)
    fn.blocks.append(blk)
    return blk


# Direct publication calls known class methods as unbound functions and passes
# the receiver explicitly.  That keeps compiled pcc1 on the static method ABI
# without materializing a second family of wrapper functions/native adapters.
# direct_indexed_kernel delays its matching import of this module until its
# definitions are complete, so both import orders remain safe.
from .direct_indexed_kernel import (
    DirectIndexedFunctionBuilder,
    publish_exact_call_fixed,
)


# ---------------------------------------------------------------------------
# Public re-exports matching llvmlite.ir
# ---------------------------------------------------------------------------

__all__ = [
    # Types
    "Type",
    "VoidType",
    "IntType",
    "HalfType",
    "FloatType",
    "DoubleType",
    "PointerType",
    "ArrayType",
    "BaseStructType",
    "LiteralStructType",
    "IdentifiedStructType",
    "FunctionType",
    "Context",
    "global_context",
    # Values / constants
    "Value",
    "Constant",
    "Undefined",
    # Containers
    "Argument",
    "Block",
    "Function",
    "FunctionAttributes",
    "GlobalVariable",
    "Module",
    # Builder + insts
    "IRBuilder",
    "IRBuilder_emit_raw",
    "IRBuilder_next_value",
    "IRBuilder_current_instruction_count",
    "IRBuilder_instruction_text_at",
    "IRBuilder_can_inline_error_edge",
    "IRBuilder_try_inline_error_edge",
    "IRBuilder_declare_inline_error_landing",
    "PhiInstr",
    "LandingPadInstr",
    "SwitchInstr",
    "CatchClause",
    "FilterClause",
]


# ---------------------------------------------------------------------------
# llvmlite sub-module shims
# ---------------------------------------------------------------------------
#
# llvmlite exposes some names via submodules (``ir.values.Constant``,
# ``ir.types.IntType``, etc.). pcc code uses a few of these paths; expose
# the same attribute chains from this single module so existing imports
# keep working without source edits.


class _ValuesShim:
    """Stand-in for ``llvmlite.ir.values`` — just re-exports Value and
    Constant from this module. pcc.codegen.c_codegen uses
    ``ir.values.Constant(...)``."""


values = _ValuesShim()
values.Value = Value
values.Constant = Constant
values.Argument = Argument
values.Function = Function
values.GlobalVariable = GlobalVariable
