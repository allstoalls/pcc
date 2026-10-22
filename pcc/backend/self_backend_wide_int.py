from __future__ import annotations

"""Expand 128-bit integer SSA values into pairs of 64-bit values.

C's ``__int128`` reaches the self backend as LLVM ``i128``, but the AArch64
and x86-64 emitters model every scalar as one 64-bit register.  Before this
pass an ``i128`` value was silently truncated: a switch compared only the low
word, ``(__int128)1 << 64`` produced garbage and an ``i128`` parameter used
one register.

LLVM's answer is type legalization (``DAGTypeLegalizer::ExpandIntegerResult``
in ``llvm/lib/CodeGen/SelectionDAG/LegalizeIntegerTypes.cpp``): an illegal
wide integer becomes a (lo, hi) pair of legal halves, every operation is
rewritten on the halves, and division and FP conversion become compiler-rt
libcalls.  This pass does the same on IR text before the target-neutral
parser runs, so every self-backend target inherits it:

* an ``i128`` SSA value ``%v`` becomes the i64 values ``%v.w128l`` and
  ``%v.w128h`` (the marker is made unique per function);
* memory keeps ``i128`` (16 bytes, 16-byte aligned, see ``TypeDesc``); loads
  and stores are split at byte offsets 0 and 8 (both targets are little
  endian);
* an ``i128`` parameter becomes two consecutive ``i64`` parameters and an
  ``i128`` return becomes ``{ i64, i64 }``.  That matches Darwin arm64 and
  x86-64 SysV for register-resident arguments (x0:x1 / rdi:rsi, results in
  x0:x1 / rax:rdx); the compiler-rt helpers are always called that way.  An
  ``i128`` argument that would straddle the last argument register, or the
  AAPCS64 even-pair rule outside Darwin, is not modelled: callers and callees
  compiled by pcc agree, foreign objects with such signatures do not;
* division, remainder and int/FP conversions call ``__divti3``,
  ``__udivti3``, ``__modti3``, ``__umodti3``, ``__floattidf`` and friends,
  which libSystem (compiler-rt) and libgcc provide;
* any other construct that would define or consume an ``i128`` SSA value
  raises ``BackendUnavailable`` instead of emitting truncated code.

Constants are split with exact integer arithmetic behind ``object``-typed
locals: pcc's own build types ``int`` as an i64 lane, and these values are
128-bit.
"""

from . import BackendUnavailable


_IDENT_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.$-"
_DIGITS = "0123456789"
_PAIR_TYPE = "{ i64, i64 }"
_INT_FLAGS = ("nuw", "nsw", "exact", "disjoint")
_VALUE_ATTRS = ("noundef", "signext", "zeroext", "inreg")
_CALL_WORDS = (
    "nnan", "ninf", "nsz", "arcp", "contract", "afn", "reassoc", "fast",
    "ccc", "fastcc", "coldcc", "swiftcc", "tailcc", "noalias", "nonnull",
    "noundef", "signext", "zeroext", "inreg",
)
_SHIFT_OPCODES = ("shl", "lshr", "ashr")
_BITWISE_OPCODES = ("and", "or", "xor")
_DIVISION_HELPERS = {
    "sdiv": "__divti3",
    "udiv": "__udivti3",
    "srem": "__modti3",
    "urem": "__umodti3",
}
_TO_FLOAT_HELPERS = {
    "sitofp:double": "__floattidf",
    "sitofp:float": "__floattisf",
    "uitofp:double": "__floatuntidf",
    "uitofp:float": "__floatuntisf",
}
_FROM_FLOAT_HELPERS = {
    "fptosi:double": "__fixdfti",
    "fptosi:float": "__fixsfti",
    "fptoui:double": "__fixunsdfti",
    "fptoui:float": "__fixunssfti",
}
_UNSIGNED_PREDICATES = {
    "ugt": "ugt", "uge": "uge", "ult": "ult", "ule": "ule",
    "sgt": "ugt", "sge": "uge", "slt": "ult", "sle": "ule",
}


def legalize_wide_integers(ir_text: str) -> str:
    """Return ``ir_text`` with every ``i128`` SSA value split into i64 halves."""
    if not _text_mentions_type_word(ir_text, "i128"):
        return ir_text
    return _WideModule(ir_text).rewrite()


def _unavailable(detail: str, text: str) -> BackendUnavailable:
    return BackendUnavailable(
        "self backend cannot lower 128-bit integer " + detail + ": " + text.strip()
    )


def _is_ident_char(ch: str) -> bool:
    return _IDENT_CHARS.find(ch) >= 0


def _quoted_end(text: str, quote_index: int) -> int:
    """Index just past the string literal opening at ``quote_index``."""
    close = text.find('"', quote_index + 1)
    if close < 0:
        return len(text)
    return close + 1


def _mentions_i128(text: str) -> bool:
    """Whether ``i128`` occurs as a type word, not inside a name or string."""
    return _mentions_type_word(text, "i128")


def _text_mentions_type_word(text: str, word: str) -> bool:
    """``_mentions_type_word`` for a whole module or function: only the lines
    holding an occurrence of ``word`` are scanned, so text that merely names
    a value ``%half`` or ``@foo_i128`` costs a substring search, not a
    character walk over every line."""
    index = text.find(word)
    while index >= 0:
        line_start = text.rfind("\n", 0, index) + 1
        line_end = text.find("\n", index)
        if line_end < 0:
            line_end = len(text)
        if _mentions_type_word(text[line_start:line_end], word):
            return True
        index = text.find(word, line_end)
    return False


def _mentions_type_word(text: str, word: str) -> bool:
    """Whether ``word`` occurs as a bare word, not inside a name or string."""
    if text.find(word) < 0:
        return False
    width = len(word)
    index = 0
    length = len(text)
    while index < length:
        ch = text[index]
        if ch == '"':
            index = _quoted_end(text, index)
            continue
        if ch == "%" or ch == "@" or ch == "!" or ch == "#":
            index += 1
            if index < length and text[index] == '"':
                index = _quoted_end(text, index)
                continue
            while index < length and _is_ident_char(text[index]):
                index += 1
            continue
        if _is_ident_char(ch):
            start = index
            while index < length and _is_ident_char(text[index]):
                index += 1
            if index - start == width and text.startswith(word, start):
                return True
            continue
        index += 1
    return False


def _symbol_end(text: str, start: int) -> int:
    """End of the ``%name``/``@name`` token whose sigil is at ``start``."""
    index = start + 1
    if index < len(text) and text[index] == '"':
        return _quoted_end(text, index)
    while index < len(text) and _is_ident_char(text[index]):
        index += 1
    return index


def _matching_close(text: str, open_index: int) -> int:
    """Index of the bracket closing the one at ``open_index``."""
    depth = 0
    index = open_index
    while index < len(text):
        ch = text[index]
        if ch == '"':
            index = _quoted_end(text, index)
            continue
        if ch == "(" or ch == "[" or ch == "{" or ch == "<":
            depth += 1
        elif ch == ")" or ch == "]" or ch == "}" or ch == ">":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    raise BackendUnavailable("self backend found an unbalanced IR bracket: " + text)


def _split_top_level(text: str) -> list[str]:
    """Split on commas outside brackets and strings; strip every piece."""
    items: list[str] = []
    depth = 0
    start = 0
    index = 0
    while index < len(text):
        ch = text[index]
        if ch == '"':
            index = _quoted_end(text, index)
            continue
        if ch == "(" or ch == "[" or ch == "{" or ch == "<":
            depth += 1
        elif ch == ")" or ch == "]" or ch == "}" or ch == ">":
            depth -= 1
        elif ch == "," and depth == 0:
            items.append(text[start:index].strip())
            start = index + 1
        index += 1
    tail = text[start:].strip()
    if tail or items:
        items.append(tail)
    return items


def _split_word(text: str) -> tuple[str, str]:
    stripped = text.lstrip()
    space = stripped.find(" ")
    if space < 0:
        return stripped, ""
    return stripped[:space], stripped[space + 1:].lstrip()


def _split_leading_type(text: str) -> tuple[str, str]:
    """Split one LLVM type off the front of ``text``."""
    stripped = text.lstrip()
    if not stripped:
        return "", ""
    first = stripped[0]
    if first == "{" or first == "[" or first == "<":
        end = _matching_close(stripped, 0) + 1
    elif first == "%":
        end = _symbol_end(stripped, 0)
    else:
        end = 0
        while end < len(stripped) and _is_ident_char(stripped[end]):
            end += 1
    type_text = stripped[:end]
    rest = stripped[end:]
    if type_text == "ptr" and rest.lstrip().startswith("addrspace("):
        rest = rest.lstrip()
        close = rest.find(")")
        type_text = type_text + " " + rest[:close + 1]
        rest = rest[close + 1:]
    while rest.startswith("*"):
        type_text = type_text + "*"
        rest = rest[1:]
    return type_text, rest.lstrip()


def _split_keyword(text: str, keyword: str) -> tuple[str, str]:
    """Split ``text`` at the first top-level `` keyword `` (e.g. `` to ``)."""
    depth = 0
    index = 0
    needle = " " + keyword + " "
    while index < len(text):
        ch = text[index]
        if ch == '"':
            index = _quoted_end(text, index)
            continue
        if ch == "(" or ch == "[" or ch == "{" or ch == "<":
            depth += 1
        elif ch == ")" or ch == "]" or ch == "}" or ch == ">":
            depth -= 1
        elif depth == 0 and text.startswith(needle, index):
            return text[:index].strip(), text[index + len(needle):].strip()
        index += 1
    raise BackendUnavailable("self backend expected '" + keyword + "' in IR: " + text)


def _is_int_literal(text: str) -> bool:
    start = 1 if text.startswith("-") else 0
    if start >= len(text):
        return False
    index = start
    while index < len(text):
        if _DIGITS.find(text[index]) < 0:
            return False
        index += 1
    return True


def _i64_text(value: object) -> str:
    """Canonical signed spelling of a 64-bit pattern in ``[0, 2**64)``."""
    number: object = value
    if number >= (1 << 63):
        number = number - (1 << 64)
    return str(number)


def _split_wide_constant(text: str) -> tuple[str, str]:
    number: object = int(text)
    if number < 0:
        number = number + (1 << 128)
    low: object = number % (1 << 64)
    high: object = (number // (1 << 64)) % (1 << 64)
    return _i64_text(low), _i64_text(high)


def _int_constant_value(text: str, width: int, signed: bool) -> str:
    """Decimal value of an iN constant extended to 128 bits."""
    number: object = 0
    if text == "true":
        number = 1
    elif _is_int_literal(text):
        number = int(text)
    modulus: object = 1 << width
    number = number % modulus
    if signed and number >= (1 << (width - 1)):
        number = number - modulus
    return str(number)


def _shift_count(text: str) -> int:
    """A constant shift amount reduced modulo 128 (wider counts are poison)."""
    number: object = int(text)
    return number % 128


def _int_width(type_text: str) -> int:
    if not type_text.startswith("i") or not _is_int_literal(type_text[1:]):
        return 0
    return int(type_text[1:])


def _derived_local(name: str, suffix: str) -> str:
    if name.startswith('%"'):
        return name[:-1] + suffix + '"'
    body = name[1:]
    if _is_int_literal(body):
        # A numbered value: "%12.w128l" would not lex as one local name.
        return '%"' + body + suffix + '"'
    return name + suffix


def _unique_marker(function_text: str) -> str:
    marker = ".w128"
    serial = 0
    while function_text.find(marker) >= 0:
        serial += 1
        marker = ".w128x" + str(serial)
    return marker


def _strip_value_attrs(text: str) -> str:
    """Drop leading parameter attributes, leaving the operand spelling."""
    rest = text.strip()
    while True:
        word, remainder = _split_word(rest)
        if word in _VALUE_ATTRS and remainder:
            rest = remainder
            continue
        return rest


def _split_return_prefix(prefix: str) -> tuple[str, bool]:
    """Replace a trailing ``i128`` return type in a define/declare prefix."""
    words = prefix.split()
    if not words or words[-1] != "i128":
        return prefix, False
    words.pop()
    while words and words[-1] in _VALUE_ATTRS:
        words.pop()
    words.append(_PAIR_TYPE)
    return " ".join(words) + " ", True


def _rewrite_signature(line: str, marker: str) -> str:
    at = line.find("@")
    if at < 0:
        raise BackendUnavailable("self backend expected a function name in: " + line)
    name_end = _symbol_end(line, at)
    if name_end >= len(line) or line[name_end] != "(":
        raise BackendUnavailable("self backend expected a parameter list in: " + line)
    close = _matching_close(line, name_end)
    prefix, _ = _split_return_prefix(line[:at])
    params: list[str] = []
    for param in _split_top_level(line[name_end + 1:close]):
        type_text, rest = _split_leading_type(param)
        if type_text != "i128":
            params.append(param)
            continue
        name = ""
        sigil = rest.find("%")
        if sigil >= 0:
            name = rest[sigil:_symbol_end(rest, sigil)]
        if name:
            params.append("i64 " + _derived_local(name, marker + "l"))
            params.append("i64 " + _derived_local(name, marker + "h"))
        else:
            params.append("i64")
            params.append("i64")
    return prefix + line[at:name_end] + "(" + ", ".join(params) + ")" + line[close + 1:]


def _function_symbol(line: str) -> str:
    at = line.find("@")
    if at < 0:
        return ""
    name = line[at + 1:_symbol_end(line, at)]
    if name.startswith('"') and name.endswith('"'):
        return name[1:-1]
    return name


class _WideModule:
    def __init__(self, ir_text: str) -> None:
        self.lines: list[str] = ir_text.split("\n")
        self.named_types: dict[str, str] = {}
        self.symbols: dict[str, bool] = {}
        self.helper_lines: dict[str, str] = {}
        self.helper_order: list[str] = []
        self.has_wide_named_types = False

    def rewrite(self) -> str:
        for line in self.lines:
            stripped = line.strip()
            if stripped.startswith("define ") or stripped.startswith("declare "):
                self.symbols[_function_symbol(stripped)] = True
            elif stripped.startswith("%") and stripped.find(" = type ") > 0:
                name_end = _symbol_end(stripped, 0)
                body = stripped[name_end:].strip()[len("= type "):]
                self.named_types[stripped[:name_end]] = body
                if _mentions_i128(body):
                    self.has_wide_named_types = True
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
            if stripped.startswith("declare ") and _mentions_i128(stripped):
                out.append(_rewrite_signature(line, ".w128"))
            else:
                out.append(line)
            index += 1
        for name in self.helper_order:
            if name not in self.symbols:
                out.append(self.helper_lines[name])
        return "\n".join(out)

    def require_helper(self, name: str, declaration: str) -> None:
        if name not in self.helper_lines:
            self.helper_lines[name] = declaration
            self.helper_order.append(name)

    def aggregate_mentions_i128(self, type_text: str, depth: int) -> bool:
        if _mentions_i128(type_text):
            return True
        if depth > 32:
            return False
        index = type_text.find("%")
        while index >= 0:
            end = _symbol_end(type_text, index)
            body = self.named_types.get(type_text[index:end], "")
            if body and self.aggregate_mentions_i128(body, depth + 1):
                return True
            index = type_text.find("%", end)
        return False

    def _rewrite_function(self, lines: list[str]) -> list[str]:
        function_text = "\n".join(lines)
        if not self.has_wide_named_types and not _text_mentions_type_word(function_text, "i128"):
            return lines
        marker = _unique_marker(function_text)
        function = _WideFunction(self, marker)
        out: list[str] = []
        header = lines[0]
        out.append(_rewrite_signature(header, marker) if _mentions_i128(header) else header)
        index = 1
        last = len(lines) - 1
        while index < last:
            line = lines[index]
            stripped = line.strip()
            if stripped.startswith("switch ") and stripped.find("[") >= 0 and stripped.find("]") < 0:
                # LLVM may print one switch case per line; join the table.
                pieces: list[str] = [stripped]
                while index + 1 < last and pieces[-1].find("]") < 0:
                    index += 1
                    pieces.append(lines[index].strip())
                line = "  " + " ".join(pieces)
            out.extend(function.rewrite_line(line))
            index += 1
        out.append(lines[last])
        return out


class _WideFunction:
    def __init__(self, module: _WideModule, marker: str) -> None:
        self.module = module
        self.marker = marker
        self.counter = 0
        self.out: list[str] = []

    # -- operands ---------------------------------------------------------

    def temp(self) -> str:
        self.counter += 1
        return "%" + self.marker[1:] + "t" + str(self.counter)

    def low(self, name: str) -> str:
        return _derived_local(name, self.marker + "l")

    def high(self, name: str) -> str:
        return _derived_local(name, self.marker + "h")

    def pair(self, operand: str) -> tuple[str, str]:
        text = _strip_value_attrs(operand)
        if text.startswith("%"):
            if _symbol_end(text, 0) != len(text):
                raise _unavailable("operand", text)
            return self.low(text), self.high(text)
        if text == "undef" or text == "poison" or text == "zeroinitializer":
            return "0", "0"
        if _is_int_literal(text):
            return _split_wide_constant(text)
        raise _unavailable("operand", text)

    def emit(self, text: str) -> None:
        self.out.append("  " + text)

    def emit_temp(self, text: str) -> str:
        name = self.temp()
        self.emit(name + " = " + text)
        return name

    def copy(self, destination: str, source: str) -> None:
        self.emit(destination + " = or i64 " + source + ", 0")

    def define_pair(self, result: str, low: str, high: str) -> None:
        self.copy(self.low(result), low)
        self.copy(self.high(result), high)

    # -- dispatch ---------------------------------------------------------

    def rewrite_line(self, line: str) -> list[str]:
        stripped = line.strip()
        if not stripped or stripped.startswith(";"):
            return [line]
        if not _mentions_i128(stripped):
            self.check_aggregate_access(stripped)
            return [line]
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
        if opcode in _SHIFT_OPCODES or opcode in _BITWISE_OPCODES or opcode in _DIVISION_HELPERS \
                or opcode == "add" or opcode == "sub" or opcode == "mul":
            self.binop(result, opcode, args, stripped)
        elif opcode == "icmp":
            self.icmp(result, args, stripped)
        elif opcode == "select":
            self.select(result, args, stripped)
        elif opcode == "phi":
            self.phi(result, args, stripped)
        elif opcode in ("trunc", "zext", "sext", "ptrtoint", "inttoptr", "bitcast",
                        "sitofp", "uitofp", "fptosi", "fptoui", "freeze"):
            self.cast(result, opcode, args, stripped)
        elif opcode == "load":
            if not self.load(result, args, stripped):
                return [line]
        elif opcode == "store":
            if not self.store(args, stripped):
                return [line]
        elif opcode == "call" or opcode == "tail" or opcode == "musttail" or opcode == "notail":
            if not self.call(result, rest, stripped):
                return [line]
        elif opcode == "ret":
            self.ret(args, stripped)
        elif opcode == "switch":
            self.switch(args, stripped)
        elif opcode == "getelementptr":
            self.gep(result, args, stripped)
        elif opcode == "alloca":
            return [line]
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
        if self.module.aggregate_mentions_i128(type_text, 0):
            raise _unavailable("aggregate member access", stripped)

    # -- arithmetic -------------------------------------------------------

    def binop(self, result: str, opcode: str, args: str, line: str) -> None:
        body = args
        while True:
            word, remainder = _split_word(body)
            if word in _INT_FLAGS:
                body = remainder
                continue
            break
        type_text, operands_text = _split_leading_type(body)
        operands = _split_top_level(operands_text)
        if type_text != "i128" or len(operands) != 2 or not result:
            raise _unavailable("operation", line)
        a_low, a_high = self.pair(operands[0])
        if opcode in _SHIFT_OPCODES:
            self.shift(result, opcode, a_low, a_high, _strip_value_attrs(operands[1]))
            return
        b_low, b_high = self.pair(operands[1])
        r_low = self.low(result)
        r_high = self.high(result)
        if opcode in _BITWISE_OPCODES:
            self.emit(r_low + " = " + opcode + " i64 " + a_low + ", " + b_low)
            self.emit(r_high + " = " + opcode + " i64 " + a_high + ", " + b_high)
        elif opcode == "add":
            self.emit(r_low + " = add i64 " + a_low + ", " + b_low)
            carry = self.emit_temp("icmp ult i64 " + r_low + ", " + a_low)
            carry64 = self.emit_temp("zext i1 " + carry + " to i64")
            partial = self.emit_temp("add i64 " + a_high + ", " + b_high)
            self.emit(r_high + " = add i64 " + partial + ", " + carry64)
        elif opcode == "sub":
            self.emit(r_low + " = sub i64 " + a_low + ", " + b_low)
            borrow = self.emit_temp("icmp ult i64 " + a_low + ", " + b_low)
            borrow64 = self.emit_temp("zext i1 " + borrow + " to i64")
            partial = self.emit_temp("sub i64 " + a_high + ", " + b_high)
            self.emit(r_high + " = sub i64 " + partial + ", " + borrow64)
        elif opcode == "mul":
            self.multiply(r_low, r_high, a_low, a_high, b_low, b_high)
        else:
            helper = _DIVISION_HELPERS[opcode]
            self.module.require_helper(
                helper, "declare " + _PAIR_TYPE + " @" + helper + "(i64, i64, i64, i64)"
            )
            self.call_pair_helper(
                result, helper,
                "i64 " + a_low + ", i64 " + a_high + ", i64 " + b_low + ", i64 " + b_high,
            )

    def multiply(self, r_low: str, r_high: str, a_low: str, a_high: str,
                 b_low: str, b_high: str) -> None:
        # The high word of a_low * b_low, from four 32x32->64 products; the
        # cross terms only contribute to the high word modulo 2**64.
        a0 = self.emit_temp("and i64 " + a_low + ", 4294967295")
        a1 = self.emit_temp("lshr i64 " + a_low + ", 32")
        b0 = self.emit_temp("and i64 " + b_low + ", 4294967295")
        b1 = self.emit_temp("lshr i64 " + b_low + ", 32")
        p00 = self.emit_temp("mul i64 " + a0 + ", " + b0)
        p01 = self.emit_temp("mul i64 " + a0 + ", " + b1)
        p10 = self.emit_temp("mul i64 " + a1 + ", " + b0)
        p11 = self.emit_temp("mul i64 " + a1 + ", " + b1)
        p00_high = self.emit_temp("lshr i64 " + p00 + ", 32")
        p01_low = self.emit_temp("and i64 " + p01 + ", 4294967295")
        p10_low = self.emit_temp("and i64 " + p10 + ", 4294967295")
        middle0 = self.emit_temp("add i64 " + p00_high + ", " + p01_low)
        middle = self.emit_temp("add i64 " + middle0 + ", " + p10_low)
        p01_high = self.emit_temp("lshr i64 " + p01 + ", 32")
        p10_high = self.emit_temp("lshr i64 " + p10 + ", 32")
        middle_high = self.emit_temp("lshr i64 " + middle + ", 32")
        upper0 = self.emit_temp("add i64 " + p11 + ", " + p01_high)
        upper1 = self.emit_temp("add i64 " + upper0 + ", " + p10_high)
        upper = self.emit_temp("add i64 " + upper1 + ", " + middle_high)
        self.emit(r_low + " = mul i64 " + a_low + ", " + b_low)
        cross0 = self.emit_temp("mul i64 " + a_low + ", " + b_high)
        cross1 = self.emit_temp("mul i64 " + a_high + ", " + b_low)
        high0 = self.emit_temp("add i64 " + upper + ", " + cross0)
        self.emit(r_high + " = add i64 " + high0 + ", " + cross1)

    def shift(self, result: str, opcode: str, a_low: str, a_high: str, amount: str) -> None:
        r_low = self.low(result)
        r_high = self.high(result)
        if _is_int_literal(amount):
            self.constant_shift(r_low, r_high, opcode, a_low, a_high, _shift_count(amount))
            return
        amount_low, _ = self.pair(amount)
        # Branch-free: count = amount & 127; `big` selects the cross-word case.
        # (x >> 1) >> (63 - s) is x >> (64 - s) without ever shifting by 64.
        count = self.emit_temp("and i64 " + amount_low + ", 127")
        big = self.emit_temp("icmp ugt i64 " + count + ", 63")
        small = self.emit_temp("and i64 " + count + ", 63")
        inverse = self.emit_temp("xor i64 " + small + ", 63")
        if opcode == "shl":
            low_small = self.emit_temp("shl i64 " + a_low + ", " + small)
            low_half = self.emit_temp("lshr i64 " + a_low + ", 1")
            carry = self.emit_temp("lshr i64 " + low_half + ", " + inverse)
            high_shifted = self.emit_temp("shl i64 " + a_high + ", " + small)
            high_small = self.emit_temp("or i64 " + high_shifted + ", " + carry)
            self.emit(r_low + " = select i1 " + big + ", i64 0, i64 " + low_small)
            self.emit(r_high + " = select i1 " + big + ", i64 " + low_small + ", i64 " + high_small)
            return
        high_op = "ashr" if opcode == "ashr" else "lshr"
        high_small = self.emit_temp(high_op + " i64 " + a_high + ", " + small)
        low_shifted = self.emit_temp("lshr i64 " + a_low + ", " + small)
        high_double = self.emit_temp("shl i64 " + a_high + ", 1")
        carry = self.emit_temp("shl i64 " + high_double + ", " + inverse)
        low_small = self.emit_temp("or i64 " + low_shifted + ", " + carry)
        fill = "0"
        if opcode == "ashr":
            fill = self.emit_temp("ashr i64 " + a_high + ", 63")
        self.emit(r_low + " = select i1 " + big + ", i64 " + high_small + ", i64 " + low_small)
        self.emit(r_high + " = select i1 " + big + ", i64 " + fill + ", i64 " + high_small)

    def constant_shift(self, r_low: str, r_high: str, opcode: str, a_low: str,
                       a_high: str, count: int) -> None:
        if count == 0:
            self.copy(r_low, a_low)
            self.copy(r_high, a_high)
            return
        if opcode == "shl":
            if count >= 64:
                self.copy(r_low, "0")
                self.emit(r_high + " = shl i64 " + a_low + ", " + str(count - 64))
                return
            self.emit(r_low + " = shl i64 " + a_low + ", " + str(count))
            high_shifted = self.emit_temp("shl i64 " + a_high + ", " + str(count))
            carry = self.emit_temp("lshr i64 " + a_low + ", " + str(64 - count))
            self.emit(r_high + " = or i64 " + high_shifted + ", " + carry)
            return
        high_op = "ashr" if opcode == "ashr" else "lshr"
        if count >= 64:
            self.emit(r_low + " = " + high_op + " i64 " + a_high + ", " + str(count - 64))
            if opcode == "ashr":
                self.emit(r_high + " = ashr i64 " + a_high + ", 63")
            else:
                self.copy(r_high, "0")
            return
        low_shifted = self.emit_temp("lshr i64 " + a_low + ", " + str(count))
        carry = self.emit_temp("shl i64 " + a_high + ", " + str(64 - count))
        self.emit(r_low + " = or i64 " + low_shifted + ", " + carry)
        self.emit(r_high + " = " + high_op + " i64 " + a_high + ", " + str(count))

    def icmp(self, result: str, args: str, line: str) -> None:
        predicate, body = _split_word(args)
        type_text, operands_text = _split_leading_type(body)
        operands = _split_top_level(operands_text)
        if type_text != "i128" or len(operands) != 2 or not result:
            raise _unavailable("comparison", line)
        a_low, a_high = self.pair(operands[0])
        b_low, b_high = self.pair(operands[1])
        if predicate == "eq" or predicate == "ne":
            low_diff = self.emit_temp("xor i64 " + a_low + ", " + b_low)
            high_diff = self.emit_temp("xor i64 " + a_high + ", " + b_high)
            diff = self.emit_temp("or i64 " + low_diff + ", " + high_diff)
            self.emit(result + " = icmp " + predicate + " i64 " + diff + ", 0")
            return
        if predicate not in _UNSIGNED_PREDICATES:
            raise _unavailable("comparison", line)
        # The high words decide unless they are equal; low words are unsigned.
        high_equal = self.emit_temp("icmp eq i64 " + a_high + ", " + b_high)
        low_cmp = self.emit_temp(
            "icmp " + _UNSIGNED_PREDICATES[predicate] + " i64 " + a_low + ", " + b_low
        )
        high_cmp = self.emit_temp("icmp " + predicate + " i64 " + a_high + ", " + b_high)
        self.emit(result + " = select i1 " + high_equal + ", i1 " + low_cmp + ", i1 " + high_cmp)

    def select(self, result: str, args: str, line: str) -> None:
        operands = _split_top_level(args)
        if len(operands) != 3 or not result:
            raise _unavailable("select", line)
        condition_type, condition = _split_leading_type(operands[0])
        true_type, true_value = _split_leading_type(operands[1])
        false_type, false_value = _split_leading_type(operands[2])
        if condition_type != "i1" or true_type != "i128" or false_type != "i128":
            raise _unavailable("select", line)
        t_low, t_high = self.pair(true_value)
        f_low, f_high = self.pair(false_value)
        self.emit(self.low(result) + " = select i1 " + condition + ", i64 " + t_low + ", i64 " + f_low)
        self.emit(self.high(result) + " = select i1 " + condition + ", i64 " + t_high + ", i64 " + f_high)

    def phi(self, result: str, args: str, line: str) -> None:
        type_text, entries_text = _split_leading_type(args)
        if type_text != "i128" or not result:
            raise _unavailable("phi", line)
        low_entries: list[str] = []
        high_entries: list[str] = []
        for entry in _split_top_level(entries_text):
            if not (entry.startswith("[") and entry.endswith("]")):
                raise _unavailable("phi", line)
            parts = _split_top_level(entry[1:-1])
            if len(parts) != 2:
                raise _unavailable("phi", line)
            low, high = self.pair(parts[0])
            low_entries.append("[ " + low + ", " + parts[1] + " ]")
            high_entries.append("[ " + high + ", " + parts[1] + " ]")
        self.emit(self.low(result) + " = phi i64 " + ", ".join(low_entries))
        self.emit(self.high(result) + " = phi i64 " + ", ".join(high_entries))

    # -- conversions ------------------------------------------------------

    def cast(self, result: str, opcode: str, args: str, line: str) -> None:
        if not result:
            raise _unavailable("conversion", line)
        source_type, rest = _split_leading_type(args)
        if opcode == "freeze":
            if source_type != "i128":
                raise _unavailable("freeze", line)
            low, high = self.pair(rest)
            self.define_pair(result, low, high)
            return
        value, target_type = _split_keyword(" " + rest, "to")
        if target_type.find(",") >= 0:
            raise _unavailable("conversion", line)
        if source_type == "i128" and target_type == "i128":
            if opcode != "bitcast":
                raise _unavailable("conversion", line)
            low, high = self.pair(value)
            self.define_pair(result, low, high)
            return
        if source_type == "i128":
            low, high = self.pair(value)
            target_width = _int_width(target_type)
            if opcode == "trunc" and target_width == 64:
                self.copy(result, low)
            elif opcode == "trunc" and 0 < target_width < 64:
                self.emit(result + " = trunc i64 " + low + " to " + target_type)
            elif opcode == "inttoptr":
                self.emit(result + " = inttoptr i64 " + low + " to " + target_type)
            elif (opcode == "sitofp" or opcode == "uitofp") and \
                    (opcode + ":" + target_type) in _TO_FLOAT_HELPERS:
                helper = _TO_FLOAT_HELPERS[opcode + ":" + target_type]
                self.module.require_helper(
                    helper, "declare " + target_type + " @" + helper + "(i64, i64)"
                )
                self.emit(result + " = call " + target_type + " @" + helper
                          + "(i64 " + low + ", i64 " + high + ")")
            else:
                raise _unavailable("conversion", line)
            return
        if target_type != "i128":
            raise _unavailable("conversion", line)
        source_width = _int_width(source_type)
        if (opcode == "zext" or opcode == "sext") and 0 < source_width <= 64:
            operand = _strip_value_attrs(value)
            signed = opcode == "sext"
            if _is_int_literal(operand) or operand == "true" or operand == "false":
                low, high = _split_wide_constant(
                    _int_constant_value(operand, source_width, signed)
                )
                self.define_pair(result, low, high)
                return
            if source_width == 64:
                self.copy(self.low(result), operand)
            else:
                self.emit(self.low(result) + " = " + opcode + " " + source_type + " "
                          + operand + " to i64")
            if signed:
                self.emit(self.high(result) + " = ashr i64 " + self.low(result) + ", 63")
            else:
                self.copy(self.high(result), "0")
            return
        if opcode == "ptrtoint":
            self.emit(self.low(result) + " = ptrtoint " + source_type + " " + value + " to i64")
            self.copy(self.high(result), "0")
            return
        if (opcode == "fptosi" or opcode == "fptoui") and \
                (opcode + ":" + source_type) in _FROM_FLOAT_HELPERS:
            helper = _FROM_FLOAT_HELPERS[opcode + ":" + source_type]
            self.module.require_helper(
                helper, "declare " + _PAIR_TYPE + " @" + helper + "(" + source_type + ")"
            )
            self.call_pair_helper(result, helper, source_type + " " + value)
            return
        raise _unavailable("conversion", line)

    def call_pair_helper(self, result: str, helper: str, args: str) -> None:
        returned = _derived_local(result, self.marker + "r")
        self.emit(returned + " = call " + _PAIR_TYPE + " @" + helper + "(" + args + ")")
        self.emit(self.low(result) + " = extractvalue " + _PAIR_TYPE + " " + returned + ", 0")
        self.emit(self.high(result) + " = extractvalue " + _PAIR_TYPE + " " + returned + ", 1")

    # -- memory -----------------------------------------------------------

    def memory_operands(self, args: str, line: str) -> tuple[str, list[str]]:
        prefix = ""
        word, remainder = _split_word(args)
        if word == "atomic":
            raise _unavailable("atomic memory access", line)
        if word == "volatile":
            prefix = "volatile "
            args = remainder
        return prefix, _split_top_level(args)

    def high_address(self, pointer: str) -> str:
        return self.emit_temp("getelementptr i8, " + pointer + ", i64 8")

    def load(self, result: str, args: str, line: str) -> bool:
        prefix, items = self.memory_operands(args, line)
        if not items or items[0] != "i128":
            return False
        if not result or len(items) < 2 or not items[1].startswith("ptr "):
            raise _unavailable("load", line)
        for extra in items[2:]:
            if not extra.startswith("align "):
                raise _unavailable("load", line)
        high_pointer = self.high_address(items[1])
        self.emit(self.low(result) + " = load " + prefix + "i64, " + items[1] + ", align 8")
        self.emit(self.high(result) + " = load " + prefix + "i64, ptr " + high_pointer + ", align 8")
        return True

    def store(self, args: str, line: str) -> bool:
        prefix, items = self.memory_operands(args, line)
        if not items:
            return False
        value_type, value = _split_leading_type(items[0])
        if value_type != "i128":
            return False
        if len(items) < 2 or not items[1].startswith("ptr "):
            raise _unavailable("store", line)
        for extra in items[2:]:
            if not extra.startswith("align "):
                raise _unavailable("store", line)
        low, high = self.pair(value)
        high_pointer = self.high_address(items[1])
        self.emit("store " + prefix + "i64 " + low + ", " + items[1] + ", align 8")
        self.emit("store " + prefix + "i64 " + high + ", ptr " + high_pointer + ", align 8")
        return True

    def gep(self, result: str, args: str, line: str) -> None:
        items = _split_top_level(args)
        rewritten: list[str] = []
        index = 0
        while index < len(items):
            item = items[index]
            if index >= 2:
                type_text, value = _split_leading_type(item)
                if type_text == "i128":
                    low, _ = self.pair(value)
                    item = "i64 " + low
            rewritten.append(item)
            index += 1
        text = "getelementptr " + ", ".join(rewritten)
        if _mentions_i128(", ".join(rewritten[2:])):
            raise _unavailable("address computation", line)
        self.emit((result + " = " if result else "") + text)

    # -- calls and control flow -------------------------------------------

    def call(self, result: str, text: str, line: str) -> bool:
        prefix = ""
        word, rest = _split_word(text)
        if word == "tail" or word == "musttail" or word == "notail":
            prefix = word + " "
            word, rest = _split_word(rest)
        if word != "call":
            raise _unavailable("call", line)
        call_words: list[str] = []
        while True:
            word, remainder = _split_word(rest)
            if word in _CALL_WORDS:
                call_words.append(word)
                rest = remainder
                continue
            break
        return_type, rest = _split_leading_type(rest)
        fixed_params: list[str] = []
        has_function_type = False
        variadic = False
        if rest.startswith("("):
            close = _matching_close(rest, 0)
            has_function_type = True
            for param in _split_top_level(rest[1:close]):
                if param == "...":
                    variadic = True
                else:
                    fixed_params.append(param)
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
        wide_return = return_type == "i128"
        wide_args = False
        for arg in args:
            arg_type, _ = _split_leading_type(arg)
            if arg_type == "i128":
                wide_args = True
        if not wide_return and not wide_args:
            for param in fixed_params:
                if param == "i128":
                    raise _unavailable("call", line)
            return False
        if callee.startswith("@llvm.") or _mentions_i128(trailing):
            raise _unavailable("call", line)
        new_args: list[str] = []
        position = 0
        for arg in args:
            arg_type, value = _split_leading_type(arg)
            if arg_type == "i128":
                if variadic and position >= len(fixed_params):
                    raise _unavailable("variadic argument", line)
                low, high = self.pair(value)
                new_args.append("i64 " + low)
                new_args.append("i64 " + high)
            else:
                new_args.append(arg)
            position += 1
        signature = ""
        if has_function_type:
            params: list[str] = []
            for param in fixed_params:
                if param == "i128":
                    params.append("i64")
                    params.append("i64")
                else:
                    params.append(param)
            if variadic:
                params.append("...")
            signature = " (" + ", ".join(params) + ")"
        head = prefix + "call "
        for call_word in call_words:
            if not (wide_return and call_word in _VALUE_ATTRS):
                head = head + call_word + " "
        head = head + (_PAIR_TYPE if wide_return else return_type)
        call_text = head + signature + " " + callee + "(" + ", ".join(new_args) + ")" + trailing
        if wide_return and result:
            returned = _derived_local(result, self.marker + "r")
            self.emit(returned + " = " + call_text)
            self.emit(self.low(result) + " = extractvalue " + _PAIR_TYPE + " " + returned + ", 0")
            self.emit(self.high(result) + " = extractvalue " + _PAIR_TYPE + " " + returned + ", 1")
        elif result:
            self.emit(result + " = " + call_text)
        else:
            self.emit(call_text)
        return True

    def ret(self, args: str, line: str) -> None:
        type_text, value = _split_leading_type(args)
        if type_text != "i128":
            raise _unavailable("return", line)
        low, high = self.pair(value)
        first = self.emit_temp("insertvalue " + _PAIR_TYPE + " undef, i64 " + low + ", 0")
        second = self.emit_temp("insertvalue " + _PAIR_TYPE + " " + first + ", i64 " + high + ", 1")
        self.emit("ret " + _PAIR_TYPE + " " + second)

    def switch(self, args: str, line: str) -> None:
        type_text, rest = _split_leading_type(args)
        open_index = rest.find("[")
        close_index = rest.rfind("]")
        if type_text != "i128" or open_index < 0 or close_index < open_index:
            raise _unavailable("switch", line)
        head = _split_top_level(rest[:open_index])
        if len(head) != 2 or not head[1].startswith("label "):
            raise _unavailable("switch", line)
        value_low, value_high = self.pair(head[0])
        default_label = head[1]
        case_lows: list[str] = []
        case_highs: list[str] = []
        case_labels: list[str] = []
        cases = rest[open_index + 1:close_index].strip()
        while cases:
            case_type, cases = _split_leading_type(cases)
            comma = cases.find(",")
            if case_type != "i128" or comma < 0:
                raise _unavailable("switch", line)
            low, high = self.pair(cases[:comma])
            cases = cases[comma + 1:].lstrip()
            if not cases.startswith("label "):
                raise _unavailable("switch", line)
            cases = cases[len("label "):].lstrip()
            label_end = _symbol_end(cases, 0)
            case_lows.append(low)
            case_highs.append(high)
            case_labels.append(cases[:label_end])
            cases = cases[label_end:].strip()
        same_high = True
        for high in case_highs:
            if high != case_highs[0]:
                same_high = False
        if case_lows and same_high:
            # Common case (small constants): keep a jump-table-friendly switch
            # on the low word and route other high words to the default with a
            # low-word value no case uses.
            sentinel = 0
            while str(sentinel) in case_lows:
                sentinel += 1
            high_match = self.emit_temp("icmp eq i64 " + value_high + ", " + case_highs[0])
            selector = self.emit_temp(
                "select i1 " + high_match + ", i64 " + value_low + ", i64 " + str(sentinel)
            )
            table: list[str] = []
            index = 0
            while index < len(case_lows):
                table.append("i64 " + case_lows[index] + ", label " + case_labels[index])
                index += 1
            self.emit("switch i64 " + selector + ", " + default_label + " [ " + " ".join(table) + " ]")
            return
        # General case: number the matching case in this block, so PHIs in
        # the successors keep seeing the same predecessor.
        selector = "0"
        index = len(case_lows) - 1
        while index >= 0:
            low_diff = self.emit_temp("xor i64 " + value_low + ", " + case_lows[index])
            high_diff = self.emit_temp("xor i64 " + value_high + ", " + case_highs[index])
            diff = self.emit_temp("or i64 " + low_diff + ", " + high_diff)
            match = self.emit_temp("icmp eq i64 " + diff + ", 0")
            selector = self.emit_temp(
                "select i1 " + match + ", i64 " + str(index + 1) + ", i64 " + selector
            )
            index -= 1
        table = []
        index = 0
        while index < len(case_labels):
            table.append("i64 " + str(index + 1) + ", label " + case_labels[index])
            index += 1
        self.emit("switch i64 " + selector + ", " + default_label + " [ " + " ".join(table) + " ]")
