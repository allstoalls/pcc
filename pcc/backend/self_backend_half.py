from __future__ import annotations

"""Promote LLVM ``half`` (C ``_Float16``) to ``float`` before the target parser.

The self-backend targets have no binary16 register class or arithmetic.  As
in LLVM's ``SoftPromoteHalf`` legalization, a ``half`` SSA value is carried
as a ``float`` whose value is always representable in binary16, and memory
keeps the 16-bit encoding:

* memory, aggregate and global types use ``i16`` (same size and alignment);
  loads and stores convert between the encoding and the float value;
* ``fadd``/``fsub``/``fmul``/``fdiv``/``frem``, ``fptrunc`` and int-to-FP
  conversions compute in float and round to binary16 (round to nearest
  even).  Float carries 24 significand bits >= 2 * 11 + 2, so rounding the
  float result again is the correctly rounded binary16 result; ``double``
  sources are rounded to binary16 directly, never through float;
* comparisons, negation, ``fpext`` and FP-to-int conversions are exact on
  the float value;
* a ``half`` parameter or return travels as a ``float`` register whose low
  16 bits hold the encoding, which is where AArch64 (h0..h7) and x86-64
  (xmm0..xmm7) keep binary16 arguments.

The conversions are integer sequences (the float-to-half rounding follows
F. Giesen's ``float_to_half_fast3_rtne``), so no compiler-rt helper is
needed.  Constructs outside this list that touch ``half`` values raise
``BackendUnavailable``.
"""

from . import BackendUnavailable
from .wide_float import decode_float_bits, encode_float_bits, encode_ratio, infinity_bits, nan_bits
from .self_backend_wide_int import (
    _derived_local,
    _is_int_literal,
    _matching_close,
    _mentions_type_word,
    _split_keyword,
    _text_mentions_type_word,
    _split_leading_type,
    _split_top_level,
    _split_word,
    _symbol_end,
)


_FP_BINOPS = ("fadd", "fsub", "fmul", "fdiv", "frem")
_FAST_MATH = ("nnan", "ninf", "nsz", "arcp", "contract", "afn", "reassoc", "fast")
_EXACT_F16_INTRINSICS = ("llvm.fabs.f16", "llvm.copysign.f16", "llvm.minnum.f16",
                         "llvm.maxnum.f16", "llvm.floor.f16", "llvm.ceil.f16",
                         "llvm.trunc.f16", "llvm.rint.f16", "llvm.nearbyint.f16")
_ROUNDED_F16_INTRINSICS = ("llvm.sqrt.f16",)
_NEGATIVE_ZERO = "0x8000000000000000"


def legalize_half_floats(ir_text: str) -> str:
    """Return ``ir_text`` with every ``half`` value carried as ``float``."""
    if not _text_mentions_type_word(ir_text, "half"):
        return ir_text
    return _HalfModule(ir_text).rewrite()


def _unavailable(detail: str, text: str) -> BackendUnavailable:
    return BackendUnavailable(
        "self backend cannot lower _Float16/half " + detail + ": " + text.strip()
    )


def _replace_type_word(text: str, word: str, replacement: str) -> str:
    """Replace bare type words (not names or strings) in ``text``."""
    pieces: list[str] = []
    index = 0
    start = 0
    length = len(text)
    while index < length:
        ch = text[index]
        if ch == '"':
            close = text.find('"', index + 1)
            index = length if close < 0 else close + 1
            continue
        if ch == "%" or ch == "@" or ch == "!" or ch == "#":
            index = _symbol_end(text, index)
            continue
        if ch.isalnum() or ch == "_" or ch == "." or ch == "$" or ch == "-":
            word_start = index
            while index < length and (text[index].isalnum() or text[index] in "_.$-"):
                index += 1
            if text[word_start:index] == word:
                pieces.append(text[start:word_start])
                pieces.append(replacement)
                start = index
            continue
        index += 1
    pieces.append(text[start:])
    return "".join(pieces)


def _half_bits_of_literal(text: str) -> int:
    """binary16 encoding of an IR half constant (0xH...., 0x<double>, decimal)."""
    value = text.strip()
    if value == "zeroinitializer" or value == "undef" or value == "poison":
        return 0
    if value.startswith("0xH") or value.startswith("0xh"):
        return int(value[3:], 16)
    if (value.startswith("0x") or value.startswith("-0x")) and value.find("p") < 0 and value.find(".") < 0:
        negative = value.startswith("-")
        bits = int(value[3:] if negative else value[2:], 16)
        sign, numerator, denominator, special = decode_float_bits(bits, 64)
        negative = bool(sign) != negative
        if special == "inf":
            return infinity_bits(16, negative)
        if special == "nan":
            return nan_bits(16, negative)
        return encode_ratio(numerator, denominator, 16, negative)
    return encode_float_bits(value, 16)


def _float_literal_of_half_bits(bits: int) -> str:
    """LLVM spelling (binary64 hex) of the float equal to a binary16 value."""
    sign, numerator, denominator, special = decode_float_bits(bits, 16)
    if special == "inf":
        double_bits = infinity_bits(64, bool(sign))
    elif special == "nan":
        double_bits = nan_bits(64, bool(sign))
    else:
        double_bits = encode_ratio(numerator, denominator, 64, bool(sign))
    return "0x" + format(double_bits, "016X")


def _float_operand(text: str) -> str:
    operand = text.strip()
    if operand.startswith("%"):
        return operand
    return _float_literal_of_half_bits(_half_bits_of_literal(operand))


class _HalfModule:
    def __init__(self, ir_text: str) -> None:
        self.lines: list[str] = ir_text.split("\n")
        self.named_types: dict[str, str] = {}

    def rewrite(self) -> str:
        for line in self.lines:
            stripped = line.strip()
            if stripped.startswith("%") and stripped.find(" = type ") > 0:
                name_end = _symbol_end(stripped, 0)
                self.named_types[stripped[:name_end]] = stripped[name_end:].strip()[len("= type "):]
        out: list[str] = []
        index = 0
        while index < len(self.lines):
            line = self.lines[index]
            stripped = line.strip()
            if stripped.startswith("define "):
                end = index + 1
                while end < len(self.lines) and self.lines[end].strip() != "}":
                    end += 1
                if end >= len(self.lines):
                    raise BackendUnavailable("self backend found an unterminated function: " + stripped)
                out.extend(self._rewrite_function(self.lines[index:end + 1]))
                index = end + 1
                continue
            if not _mentions_type_word(stripped, "half"):
                out.append(line)
            elif stripped.startswith("declare "):
                out.append(_rewrite_signature(line, "")[0])
            elif stripped.startswith("@"):
                out.append(_rewrite_global(line))
            elif stripped.startswith("%") and stripped.find(" = type ") > 0:
                if stripped.find(" x half>") >= 0:
                    raise _unavailable("vector type", stripped)
                out.append(_replace_type_word(line, "half", "i16"))
            else:
                raise _unavailable("module item", stripped)
            index += 1
        return "\n".join(out)

    def aggregate_mentions_half(self, type_text: str, depth: int) -> bool:
        if _mentions_type_word(type_text, "half"):
            return True
        if depth > 32:
            return False
        index = type_text.find("%")
        while index >= 0:
            end = _symbol_end(type_text, index)
            body = self.named_types.get(type_text[index:end], "")
            if body and self.aggregate_mentions_half(body, depth + 1):
                return True
            index = type_text.find("%", end)
        return False

    def _rewrite_function(self, lines: list[str]) -> list[str]:
        function_text = "\n".join(lines)
        has_half_types = False
        for body in self.named_types.values():
            if _mentions_type_word(body, "half"):
                has_half_types = True
        if not has_half_types and not _text_mentions_type_word(function_text, "half"):
            return lines
        marker = ".f16"
        serial = 0
        while function_text.find(marker) >= 0:
            serial += 1
            marker = ".f16x" + str(serial)
        function = _HalfFunction(self, marker)
        header, abi_params = _rewrite_signature(lines[0], marker)
        out: list[str] = [header]
        index = 1
        last = len(lines) - 1
        entry_done = not abi_params
        while index < last:
            line = lines[index]
            stripped = line.strip()
            if not entry_done and stripped and not stripped.startswith(";"):
                if stripped.endswith(":") or (stripped.find(":") > 0 and stripped.find(" = ") < 0
                                              and stripped.split(":", 1)[1].strip().startswith(";")):
                    out.append(line)
                    out.extend(function.entry_conversions(abi_params))
                    entry_done = True
                    index += 1
                    continue
                out.extend(function.entry_conversions(abi_params))
                entry_done = True
            out.extend(function.rewrite_line(line))
            index += 1
        out.append(lines[last])
        return out


def _rewrite_signature(line: str, marker: str) -> tuple[str, list[str]]:
    """Promote half params/returns; return the header and the renamed params."""
    if not _mentions_type_word(line, "half"):
        return line, []
    at = line.find("@")
    name_end = _symbol_end(line, at)
    if name_end >= len(line) or line[name_end] != "(":
        raise _unavailable("signature", line)
    close = _matching_close(line, name_end)
    prefix = line[:at]
    words = prefix.split()
    if words and words[-1] == "half":
        words[-1] = "float"
        prefix = " ".join(words) + " "
    elif _mentions_type_word(prefix, "half"):
        raise _unavailable("return type", line)
    renamed: list[str] = []
    params: list[str] = []
    for param in _split_top_level(line[name_end + 1:close]):
        type_text, rest = _split_leading_type(param)
        if type_text != "half":
            if _mentions_type_word(param, "half"):
                raise _unavailable("parameter", line)
            params.append(param)
            continue
        sigil = rest.find("%")
        if sigil >= 0 and marker:
            name = rest[sigil:_symbol_end(rest, sigil)]
            params.append("float " + _derived_local(name, marker + "abi"))
            renamed.append(name)
        else:
            params.append("float")
    if _mentions_type_word(line[close + 1:], "half"):
        raise _unavailable("signature", line)
    return prefix + line[at:name_end] + "(" + ", ".join(params) + ")" + line[close + 1:], renamed


def _rewrite_global(line: str) -> str:
    """``half`` storage in a global becomes its ``i16`` encoding."""
    pieces: list[str] = []
    index = 0
    start = 0
    while True:
        found = _find_type_word(line, "half", index)
        if found < 0:
            break
        rest = line[found + 4:]
        literal_start = found + 4 + (len(rest) - len(rest.lstrip()))
        literal_end = literal_start
        while literal_end < len(line) and line[literal_end] not in ",]}> \t":
            literal_end += 1
        literal = line[literal_start:literal_end]
        pieces.append(line[start:found])
        if literal and (literal[0].isdigit() or literal[0] in "-+" or literal.startswith("0x")
                        or literal in ("zeroinitializer", "undef", "poison", "inf", "nan")):
            pieces.append("i16 " + str(_half_bits_of_literal(literal)))
            start = literal_end
        else:
            pieces.append("i16")
            start = found + 4
        index = start
    pieces.append(line[start:])
    return "".join(pieces)


def _find_type_word(text: str, word: str, start: int) -> int:
    index = start
    length = len(text)
    while index < length:
        ch = text[index]
        if ch == '"':
            close = text.find('"', index + 1)
            index = length if close < 0 else close + 1
            continue
        if ch == "%" or ch == "@" or ch == "!" or ch == "#":
            index = _symbol_end(text, index)
            continue
        if ch.isalnum() or ch == "_" or ch == "." or ch == "$" or ch == "-":
            word_start = index
            while index < length and (text[index].isalnum() or text[index] in "_.$-"):
                index += 1
            if text[word_start:index] == word:
                return word_start
            continue
        index += 1
    return -1


class _HalfFunction:
    def __init__(self, module: _HalfModule, marker: str) -> None:
        self.module = module
        self.marker = marker
        self.counter = 0
        self.out: list[str] = []

    def temp(self) -> str:
        self.counter += 1
        return "%" + self.marker[1:] + "t" + str(self.counter)

    def emit(self, text: str) -> None:
        self.out.append("  " + text)

    def emit_temp(self, text: str) -> str:
        name = self.temp()
        self.emit(name + " = " + text)
        return name

    # -- binary16 <-> float sequences ---------------------------------------

    def bits_to_float(self, result: str, bits32: str) -> None:
        """Define ``result`` (float) from a binary16 encoding held in an i32."""
        sign = self.emit_temp("and i32 " + bits32 + ", 32768")
        sign32 = self.emit_temp("shl i32 " + sign + ", 16")
        exponent0 = self.emit_temp("lshr i32 " + bits32 + ", 10")
        exponent = self.emit_temp("and i32 " + exponent0 + ", 31")
        mantissa = self.emit_temp("and i32 " + bits32 + ", 1023")
        mantissa13 = self.emit_temp("shl i32 " + mantissa + ", 13")
        biased = self.emit_temp("add i32 " + exponent + ", 112")
        biased23 = self.emit_temp("shl i32 " + biased + ", 23")
        normal = self.emit_temp("or i32 " + biased23 + ", " + mantissa13)
        special = self.emit_temp("or i32 " + mantissa13 + ", 2139095040")
        mantissa_float = self.emit_temp("uitofp i32 " + mantissa + " to float")
        subnormal = self.emit_temp("fmul float " + mantissa_float + ", 0x3E70000000000000")
        subnormal_bits = self.emit_temp("bitcast float " + subnormal + " to i32")
        is_subnormal = self.emit_temp("icmp eq i32 " + exponent + ", 0")
        is_special = self.emit_temp("icmp eq i32 " + exponent + ", 31")
        magnitude0 = self.emit_temp("select i1 " + is_special + ", i32 " + special + ", i32 " + normal)
        magnitude = self.emit_temp(
            "select i1 " + is_subnormal + ", i32 " + subnormal_bits + ", i32 " + magnitude0
        )
        bits = self.emit_temp("or i32 " + magnitude + ", " + sign32)
        self.emit(result + " = bitcast i32 " + bits + " to float")

    def float_to_bits(self, value: str) -> str:
        """binary16 encoding (in an i32) of a float, rounded to nearest even."""
        bits = self.emit_temp("bitcast float " + value + " to i32")
        sign = self.emit_temp("and i32 " + bits + ", -2147483648")
        magnitude = self.emit_temp("xor i32 " + bits + ", " + sign)
        is_nan = self.emit_temp("icmp ugt i32 " + magnitude + ", 2139095040")
        special = self.emit_temp("select i1 " + is_nan + ", i32 32256, i32 31744")
        overflow = self.emit_temp("icmp uge i32 " + magnitude + ", 1199570944")
        magnitude_float = self.emit_temp("bitcast i32 " + magnitude + " to float")
        aligned = self.emit_temp("fadd float " + magnitude_float + ", 0x3FE0000000000000")
        aligned_bits = self.emit_temp("bitcast float " + aligned + " to i32")
        subnormal = self.emit_temp("sub i32 " + aligned_bits + ", 1056964608")
        is_subnormal = self.emit_temp("icmp ult i32 " + magnitude + ", 947912704")
        odd0 = self.emit_temp("lshr i32 " + magnitude + ", 13")
        odd = self.emit_temp("and i32 " + odd0 + ", 1")
        rebiased = self.emit_temp("add i32 " + magnitude + ", -939520001")
        rounded = self.emit_temp("add i32 " + rebiased + ", " + odd)
        normal = self.emit_temp("lshr i32 " + rounded + ", 13")
        finite = self.emit_temp(
            "select i1 " + is_subnormal + ", i32 " + subnormal + ", i32 " + normal
        )
        encoded = self.emit_temp("select i1 " + overflow + ", i32 " + special + ", i32 " + finite)
        sign16 = self.emit_temp("lshr i32 " + sign + ", 16")
        return self.emit_temp("or i32 " + encoded + ", " + sign16)

    def double_to_bits(self, value: str) -> str:
        """binary16 encoding (in an i32) of a double, rounded directly."""
        bits = self.emit_temp("bitcast double " + value + " to i64")
        sign = self.emit_temp("and i64 " + bits + ", -9223372036854775808")
        magnitude = self.emit_temp("xor i64 " + bits + ", " + sign)
        is_nan = self.emit_temp("icmp ugt i64 " + magnitude + ", 9218868437227405312")
        special = self.emit_temp("select i1 " + is_nan + ", i64 32256, i64 31744")
        overflow = self.emit_temp("icmp uge i64 " + magnitude + ", 4679240012837945344")
        magnitude_double = self.emit_temp("bitcast i64 " + magnitude + " to double")
        aligned = self.emit_temp("fadd double " + magnitude_double + ", 0x41B0000000000000")
        aligned_bits = self.emit_temp("bitcast double " + aligned + " to i64")
        subnormal = self.emit_temp("sub i64 " + aligned_bits + ", 4733283208366391296")
        is_subnormal = self.emit_temp("icmp ult i64 " + magnitude + ", 4544132024016830464")
        odd0 = self.emit_temp("lshr i64 " + magnitude + ", 42")
        odd = self.emit_temp("and i64 " + odd0 + ", 1")
        rebiased = self.emit_temp("add i64 " + magnitude + ", -4539626225366204417")
        rounded = self.emit_temp("add i64 " + rebiased + ", " + odd)
        normal = self.emit_temp("lshr i64 " + rounded + ", 42")
        finite = self.emit_temp(
            "select i1 " + is_subnormal + ", i64 " + subnormal + ", i64 " + normal
        )
        encoded = self.emit_temp("select i1 " + overflow + ", i64 " + special + ", i64 " + finite)
        sign16 = self.emit_temp("lshr i64 " + sign + ", 48")
        combined = self.emit_temp("or i64 " + encoded + ", " + sign16)
        return self.emit_temp("trunc i64 " + combined + " to i32")

    def round_float(self, result: str, value: str) -> None:
        self.bits_to_float(result, self.float_to_bits(value))

    def abi_float(self, value: str) -> str:
        """A float register whose low 16 bits are the binary16 encoding."""
        return self.emit_temp("bitcast i32 " + self.float_to_bits(value) + " to float")

    def from_abi_float(self, result: str, value: str) -> None:
        raw = self.emit_temp("bitcast float " + value + " to i32")
        bits = self.emit_temp("and i32 " + raw + ", 65535")
        self.bits_to_float(result, bits)

    def entry_conversions(self, params: list[str]) -> list[str]:
        self.out = []
        for name in params:
            self.from_abi_float(name, _derived_local(name, self.marker + "abi"))
        return self.out

    # -- instructions -------------------------------------------------------

    def rewrite_line(self, line: str) -> list[str]:
        stripped = line.strip()
        if not stripped or stripped.startswith(";"):
            return [line]
        if not _mentions_type_word(stripped, "half"):
            self.check_aggregate_access(stripped)
            return [line]
        if stripped.find(" x half>") >= 0:
            raise _unavailable("vector", stripped)
        result = ""
        rest = stripped
        if stripped.startswith("%"):
            name_end = _symbol_end(stripped, 0)
            if not stripped.startswith(" = ", name_end):
                raise _unavailable("instruction", stripped)
            result = stripped[:name_end]
            rest = stripped[name_end + 3:]
        opcode, args = _split_word(rest)
        self.out = []
        if opcode in _FP_BINOPS:
            self.binop(result, opcode, args, stripped)
        elif opcode == "fneg" or opcode == "fcmp" or opcode == "select" or opcode == "phi":
            self.exact(result, opcode, args, stripped)
        elif opcode in ("fptrunc", "fpext", "sitofp", "uitofp", "fptosi", "fptoui", "bitcast"):
            self.cast(result, opcode, args, stripped)
        elif opcode == "load":
            self.load(result, args, stripped)
        elif opcode == "store":
            self.store(args, stripped)
        elif opcode == "alloca" or opcode == "getelementptr":
            return [_replace_type_word(line, "half", "i16")]
        elif opcode == "call" or opcode == "tail" or opcode == "musttail" or opcode == "notail":
            self.call(result, rest, stripped)
        elif opcode == "ret":
            type_text, value = _split_leading_type(args)
            if type_text != "half":
                raise _unavailable("return", stripped)
            self.emit("ret float " + self.abi_float(_float_operand(value)))
        else:
            raise _unavailable("instruction", stripped)
        return self.out

    def check_aggregate_access(self, stripped: str) -> None:
        equals = stripped.find(" = ")
        if equals < 0:
            return
        opcode, args = _split_word(stripped[equals + 3:])
        if opcode != "extractvalue" and opcode != "insertvalue":
            return
        type_text, _ = _split_leading_type(args)
        if self.module.aggregate_mentions_half(type_text, 0):
            raise _unavailable("aggregate member access", stripped)

    def skip_flags(self, args: str) -> tuple[str, str]:
        flags = ""
        body = args
        while True:
            word, remainder = _split_word(body)
            if word in _FAST_MATH:
                flags = flags + word + " "
                body = remainder
                continue
            return flags, body

    def binop(self, result: str, opcode: str, args: str, line: str) -> None:
        flags, body = self.skip_flags(args)
        type_text, operands_text = _split_leading_type(body)
        operands = _split_top_level(operands_text)
        if type_text != "half" or len(operands) != 2 or not result:
            raise _unavailable("operation", line)
        wide = self.emit_temp(opcode + " " + flags + "float " + _float_operand(operands[0])
                              + ", " + _float_operand(operands[1]))
        self.round_float(result, wide)

    def exact(self, result: str, opcode: str, args: str, line: str) -> None:
        if not result:
            raise _unavailable(opcode, line)
        flags, body = self.skip_flags(args)
        if opcode == "fneg":
            type_text, value = _split_leading_type(body)
            if type_text != "half":
                raise _unavailable(opcode, line)
            self.emit(result + " = fneg " + flags + "float " + _float_operand(value))
            return
        if opcode == "fcmp":
            predicate, rest = _split_word(body)
            type_text, operands_text = _split_leading_type(rest)
            operands = _split_top_level(operands_text)
            if type_text != "half" or len(operands) != 2:
                raise _unavailable(opcode, line)
            self.emit(result + " = fcmp " + flags + predicate + " float " + _float_operand(operands[0])
                      + ", " + _float_operand(operands[1]))
            return
        if opcode == "select":
            operands = _split_top_level(body)
            if len(operands) != 3:
                raise _unavailable(opcode, line)
            true_type, true_value = _split_leading_type(operands[1])
            false_type, false_value = _split_leading_type(operands[2])
            if true_type != "half" or false_type != "half" or _mentions_type_word(operands[0], "half"):
                raise _unavailable(opcode, line)
            self.emit(result + " = select " + flags + operands[0] + ", float " + _float_operand(true_value)
                      + ", float " + _float_operand(false_value))
            return
        type_text, entries_text = _split_leading_type(body)
        if type_text != "half":
            raise _unavailable(opcode, line)
        entries: list[str] = []
        for entry in _split_top_level(entries_text):
            if not (entry.startswith("[") and entry.endswith("]")):
                raise _unavailable(opcode, line)
            parts = _split_top_level(entry[1:-1])
            if len(parts) != 2:
                raise _unavailable(opcode, line)
            entries.append("[ " + _float_operand(parts[0]) + ", " + parts[1] + " ]")
        self.emit(result + " = phi " + flags + "float " + ", ".join(entries))

    def cast(self, result: str, opcode: str, args: str, line: str) -> None:
        if not result:
            raise _unavailable("conversion", line)
        source_type, rest = _split_leading_type(args)
        value, target_type = _split_keyword(" " + rest, "to")
        if target_type.find(",") >= 0:
            raise _unavailable("conversion", line)
        if opcode == "fptrunc" and target_type == "half" and not value.startswith("%") \
                and (source_type == "float" or source_type == "double"):
            # A float/double literal is exact in binary64; round it once.
            constant = _float_literal_of_half_bits(_half_bits_of_literal(value))
            self.emit(result + " = fadd float " + constant + ", " + _NEGATIVE_ZERO)
        elif opcode == "fptrunc" and target_type == "half":
            if source_type == "float":
                self.round_float(result, value)
            elif source_type == "double":
                self.bits_to_float(result, self.double_to_bits(value))
            else:
                raise _unavailable("conversion", line)
        elif opcode == "fpext" and source_type == "half":
            if target_type == "float":
                self.emit(result + " = fadd float " + _float_operand(value) + ", " + _NEGATIVE_ZERO)
            elif target_type == "double":
                self.emit(result + " = fpext float " + _float_operand(value) + " to double")
            else:
                raise _unavailable("conversion", line)
        elif (opcode == "sitofp" or opcode == "uitofp") and target_type == "half":
            wide = self.emit_temp(opcode + " " + source_type + " " + value + " to float")
            self.round_float(result, wide)
        elif (opcode == "fptosi" or opcode == "fptoui") and source_type == "half":
            self.emit(result + " = " + opcode + " float " + _float_operand(value) + " to " + target_type)
        elif opcode == "bitcast" and source_type == "half" and target_type == "i16":
            self.emit(result + " = trunc i32 " + self.float_to_bits(_float_operand(value)) + " to i16")
        elif opcode == "bitcast" and source_type == "i16" and target_type == "half":
            self.bits_to_float(result, self.emit_temp("zext i16 " + value + " to i32"))
        else:
            raise _unavailable("conversion", line)

    def memory_operands(self, args: str, line: str) -> tuple[str, list[str]]:
        prefix = ""
        word, remainder = _split_word(args)
        if word == "atomic":
            raise _unavailable("atomic memory access", line)
        if word == "volatile":
            prefix = "volatile "
            args = remainder
        return prefix, _split_top_level(args)

    def load(self, result: str, args: str, line: str) -> None:
        prefix, items = self.memory_operands(args, line)
        if not result or len(items) < 2 or items[0] != "half" or _mentions_type_word(", ".join(items[1:]), "half"):
            raise _unavailable("load", line)
        raw = self.emit_temp("load " + prefix + "i16, " + ", ".join(items[1:]))
        self.bits_to_float(result, self.emit_temp("zext i16 " + raw + " to i32"))

    def store(self, args: str, line: str) -> None:
        prefix, items = self.memory_operands(args, line)
        if len(items) < 2:
            raise _unavailable("store", line)
        value_type, value = _split_leading_type(items[0])
        if value_type != "half" or _mentions_type_word(", ".join(items[1:]), "half"):
            raise _unavailable("store", line)
        bits = self.emit_temp("trunc i32 " + self.float_to_bits(_float_operand(value)) + " to i16")
        self.emit("store " + prefix + "i16 " + bits + ", " + ", ".join(items[1:]))

    def call(self, result: str, text: str, line: str) -> None:
        prefix = ""
        word, rest = _split_word(text)
        if word == "tail" or word == "musttail" or word == "notail":
            prefix = word + " "
            word, rest = _split_word(rest)
        if word != "call":
            raise _unavailable("call", line)
        head_words = ""
        while True:
            word, remainder = _split_word(rest)
            if word in _FAST_MATH or word in ("ccc", "fastcc", "noundef", "nofpclass"):
                head_words = head_words + word + " "
                rest = remainder
                continue
            break
        return_type, rest = _split_leading_type(rest)
        params: list[str] = []
        variadic = False
        signature = ""
        if rest.startswith("("):
            close = _matching_close(rest, 0)
            for param in _split_top_level(rest[1:close]):
                if param == "...":
                    variadic = True
                params.append("float" if param == "half" else param)
                if param != "half" and param != "..." and _mentions_type_word(param, "half"):
                    raise _unavailable("call", line)
            signature = " (" + ", ".join(params) + ")"
            rest = rest[close + 1:].lstrip()
        if not (rest.startswith("@") or rest.startswith("%")):
            raise _unavailable("call", line)
        callee_end = _symbol_end(rest, 0)
        callee = rest[:callee_end]
        rest = rest[callee_end:]
        if not rest.startswith("("):
            raise _unavailable("call", line)
        close = _matching_close(rest, 0)
        args = _split_top_level(rest[1:close])
        trailing = rest[close + 1:]
        if _mentions_type_word(trailing, "half"):
            raise _unavailable("call", line)
        if callee.startswith("@llvm."):
            self.intrinsic(result, callee[1:], return_type, args, line)
            return
        fixed = len(params) - (1 if variadic else 0)
        new_args: list[str] = []
        position = 0
        for arg in args:
            arg_type, value = _split_leading_type(arg)
            if arg_type == "half":
                if variadic and position >= fixed:
                    raise _unavailable("variadic argument", line)
                new_args.append("float " + self.abi_float(_float_operand(value)))
            elif _mentions_type_word(arg, "half"):
                raise _unavailable("call argument", line)
            else:
                new_args.append(arg)
            position += 1
        if return_type == "half":
            call_text = prefix + "call " + head_words + "float" + signature + " " + callee \
                + "(" + ", ".join(new_args) + ")" + trailing
            if result:
                returned = self.emit_temp(call_text)
                self.from_abi_float(result, returned)
            else:
                self.emit(call_text)
            return
        if _mentions_type_word(return_type, "half"):
            raise _unavailable("call result", line)
        call_text = prefix + "call " + head_words + return_type + signature + " " + callee \
            + "(" + ", ".join(new_args) + ")" + trailing
        self.emit((result + " = " if result else "") + call_text)

    def intrinsic(self, result: str, name: str, return_type: str, args: list[str], line: str) -> None:
        operands: list[str] = []
        for arg in args:
            arg_type, value = _split_leading_type(arg)
            if arg_type == "half":
                operands.append("float " + _float_operand(value))
            elif _mentions_type_word(arg, "half"):
                raise _unavailable("intrinsic", line)
            else:
                operands.append(arg)
        exact = name in _EXACT_F16_INTRINSICS
        rounded = name in _ROUNDED_F16_INTRINSICS
        if return_type != "half" or not result or not (exact or rounded):
            raise _unavailable("intrinsic", line)
        call_text = "call float @" + name[:-len(".f16")] + ".f32(" + ", ".join(operands) + ")"
        if exact:
            self.emit(result + " = " + call_text)
        else:
            self.round_float(result, self.emit_temp(call_text))
