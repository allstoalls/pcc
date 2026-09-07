"""InstructionCombining (subset) — local peephole rewrites.

Upstream reference:

- ``/private/tmp/llvm-src/llvm-project-20.1.8.src/llvm/lib/Transforms/InstCombine/InstructionCombining.cpp``
- ``/private/tmp/llvm-src/llvm-project-20.1.8.src/llvm/lib/Transforms/InstCombine/InstCombineAddSub.cpp``
- ``/private/tmp/llvm-src/llvm-project-20.1.8.src/llvm/lib/Transforms/InstCombine/InstCombineMulDivRem.cpp``

The subset here focuses on purely local arithmetic/extension
canonicalizations that can be expressed textually:

- ``add x, x`` → ``shl x, 1``
- ``add x, (sub 0, x)`` / ``add (sub 0, x), x`` → ``0``
- ``add x, (sub C, x)`` / ``add (sub C, x), x`` → ``C``
- ``sub x, -c`` → ``add x, c``
- ``sub 0, (sub 0, x)`` → ``x``
- ``sub (shl x, 1), x`` → ``x``
- ``mul x, 2^k`` / ``mul 2^k, x`` → ``shl x, k``
- ``mul x, -1`` / ``mul -1, x`` → ``sub 0, x``
- ``add (shl x, 1), x`` → ``mul x, 3``
- ``add (shl x, N), 1`` / ``add 1, (shl x, N)`` → ``or disjoint (shl x, N), 1`` for ``N > 0``
- ``add (sub x, C1), C2`` / ``add C2, (sub x, C1)`` → ``add x, (C2-C1)``
- ``add (sub C1, x), C2`` / ``add C2, (sub C1, x)`` → ``sub (C1+C2), x``
- ``sub (add x, C1), C2`` → ``add x, (C1-C2)``
- ``zext i1 true/false`` → ``1/0``
- ``sext i1 true/false`` → ``-1/0``

As in upstream, this pass runs after a simplifier phase and then
cleans up dead now-unused local instructions. It is still a subset;
the real InstCombine is far larger and fixed-point driven.
"""

from __future__ import annotations

import re


from .dce import dce_module_text, _collect_function_attrs
from .instsimplify import simplify_module_text
from .integer_fold_contract import (
    FOLD_CONSTANT,
    FOLD_POISON,
    fold_llvm_integer_binary,
    signed_value,
    unsigned_value,
    power_of_two,
)
from .simplifycfg import _function_chunk_module, _module_context_for_function


_BINOP_RE = re.compile(
    r"""
    ^(?P<indent>\s*)
    %(?P<result>[\w\.]+)\s*=\s*
    (?P<op>add|sub|mul|shl|xor|and|or)
    (?P<flags>(?:\s+(?:nsw|nuw|exact|disjoint))*)
    \s+(?P<ty>i\d+)\s+
    (?P<lhs>[^,]+?)\s*,\s*(?P<rhs>.+?)\s*$
    """,
    re.VERBOSE,
)
_ZEXT_CONST_RE = re.compile(
    r"^(?P<indent>\s*)%(?P<result>[\w\.]+)\s*=\s*zext\s+i1\s+(?P<cond>true|false)\s+to\s+(?P<ty>i\d+)\s*$"
)
_SEXT_CONST_RE = re.compile(
    r"^(?P<indent>\s*)%(?P<result>[\w\.]+)\s*=\s*sext\s+i1\s+(?P<cond>true|false)\s+to\s+(?P<ty>i\d+)\s*$"
)
_SSA_NAME_RE = re.compile(r"%([\w\.]+)\b")


def _split_functions(ir_text: str) -> list[tuple[bool, str]]:
    chunks: list[tuple[bool, str]] = []
    current: list[str] = []
    in_function = False
    brace_depth = 0
    for line in ir_text.splitlines(keepends=True):
        stripped = line.lstrip()
        if not in_function and stripped.startswith("define "):
            if current:
                chunks.append((False, "".join(current)))
                current = []
            in_function = True
            brace_depth = line.count("{") - line.count("}")
            current.append(line)
            continue
        if in_function:
            current.append(line)
            brace_depth += line.count("{") - line.count("}")
            if brace_depth <= 0:
                chunks.append((True, "".join(current)))
                current = []
                in_function = False
            continue
        current.append(line)
    if current:
        chunks.append((in_function, "".join(current)))
    return chunks


def _try_int(token: str) -> object:
    token = token.strip()
    try:
        return int(token)
    except ValueError:
        return None


def _int_width(ty: str) -> int | None:
    if not ty.startswith("i"):
        return None
    try:
        width = int(ty[1:])
    except ValueError:
        return None
    return width if width > 0 else None


def _fold_const_binop(op: str, ty: str, lhs: str, rhs: str, flags=""):
    lhs_i = _try_int(lhs)
    rhs_i = _try_int(rhs)
    width = _int_width(ty)
    if lhs_i is None or rhs_i is None or width is None:
        return None
    status, value = fold_llvm_integer_binary(op, width, lhs_i, rhs_i, flags)
    if status == FOLD_POISON:
        return "poison"
    if status == FOLD_CONSTANT:
        return signed_value(value, width)
    return None


def _is_pow2(n: object) -> int | None:
    if n <= 0 or (n & (n - 1)) != 0:
        return None
    shift = 0
    while n > 1:
        n >>= 1
        shift += 1
    return shift


def _is_neg_one(token: str, ty: str) -> bool:
    token = token.strip()
    if token == "-1":
        return True
    if token == "true" and ty == "i1":
        return True
    if not ty.startswith("i"):
        return False
    try:
        width = int(ty[1:])
    except ValueError:
        return False
    if width <= 0:
        return False
    try:
        value = _try_int(token)
    except ValueError:
        return False
    return value == -1 or value == unsigned_value(-1, width)


def _format_line(
    *,
    indent: str,
    result: str,
    op: str,
    flags: str,
    ty: str,
    lhs: str,
    rhs: str,
) -> str:
    return f"{indent}%{result} = {op}{flags} {ty} {lhs}, {rhs}\n"


def _format_scaled_value(
    *,
    indent: str,
    result: str,
    flags: str,
    ty: str,
    base: str,
    scale: object,
) -> str:
    if scale == 0:
        return f"{indent}%{result} = add{flags} {ty} 0, 0\n"
    if scale == -1:
        return _format_line(
            indent=indent,
            result=result,
            op="sub",
            flags=flags,
            ty=ty,
            lhs="0",
            rhs=base,
        )
    if scale == 1:
        return f"{indent}%{result} = add{flags} {ty} 0, {base}\n"
    shift = _is_pow2(scale)
    if shift is not None and shift > 0:
        return _format_line(
            indent=indent,
            result=result,
            op="shl",
            flags=flags,
            ty=ty,
            lhs=base,
            rhs=str(shift),
        )
    return _format_line(
        indent=indent,
        result=result,
        op="mul",
        flags=flags,
        ty=ty,
        lhs=base,
        rhs=str(scale),
    )


def _scaled_operand(
    token: str,
    ty: str,
    binops: dict[str, dict[str, str]],
) -> tuple[str, object] | None:
    token = token.strip()
    if _try_int(token) is not None:
        return None
    if not token.startswith("%"):
        return (token, 1)

    info = binops.get(token[1:])
    if info is None or info["ty"] != ty:
        return (token, 1)

    if info["op"] == "mul":
        lhs_c = _try_int(info["lhs"])
        rhs_c = _try_int(info["rhs"])
        if lhs_c is not None and rhs_c is None:
            return (info["rhs"], lhs_c)
        if rhs_c is not None and lhs_c is None:
            return (info["lhs"], rhs_c)
        return None

    if info["op"] == "shl":
        sh_amt = _try_int(info["rhs"])
        if sh_amt is None or sh_amt < 0:
            return None
        inner = _scaled_operand(info["lhs"], ty, binops)
        if inner is None:
            return None
        return (inner[0], inner[1] * power_of_two(sh_amt))

    if info["op"] == "sub" and info["lhs"] == "0":
        inner = _scaled_operand(info["rhs"], ty, binops)
        if inner is None:
            return None
        return (inner[0], -inner[1])

    return (token, 1)


def _base_plus_const(
    token: str,
    ty: str,
    binops: dict[str, dict[str, str]],
) -> tuple[str, object] | None:
    token = token.strip()
    if _try_int(token) is not None or not token.startswith("%"):
        return None

    info = binops.get(token[1:])
    if info is None or info["ty"] != ty:
        return (token, 0)

    if info["op"] == "add":
        lhs_c = _try_int(info["lhs"])
        rhs_c = _try_int(info["rhs"])
        if lhs_c is not None and rhs_c is None:
            return (info["rhs"], lhs_c)
        if rhs_c is not None and lhs_c is None:
            return (info["lhs"], rhs_c)
    if info["op"] == "sub":
        rhs_c = _try_int(info["rhs"])
        if rhs_c is not None and _try_int(info["lhs"]) is None:
            return (info["lhs"], -rhs_c)

    return (token, 0)


def _bitnot_base(
    token: str,
    ty: str,
    binops: dict[str, dict[str, str]],
) -> str | None:
    token = token.strip()
    if not token.startswith("%"):
        return None
    info = binops.get(token[1:])
    if info is None or info["ty"] != ty or info["op"] != "xor":
        return None
    if _is_neg_one(info["lhs"], ty) and not _is_neg_one(info["rhs"], ty):
        return info["rhs"]
    if _is_neg_one(info["rhs"], ty) and not _is_neg_one(info["lhs"], ty):
        return info["lhs"]
    return None


def _unique_ssa_name(base: str, text: str) -> str:
    existing = set(_SSA_NAME_RE.findall(text))
    if not base:
        base = "tmp"
    if not base.isdigit() and base[0].isdigit():
        base = re.sub(r"^\d+", "", base) or "tmp"
    name = base
    counter = 0
    while name in existing:
        counter += 1
        name = f"{base}.{counter}"
    return name




def _rewrite_function(
    fn_text: str,
    module_context: str = "",
    function_attrs: dict[str, set[str]] | None = None,
) -> tuple[str, bool]:
    lines = fn_text.splitlines(keepends=True)
    replacements: dict[str, str] = {}
    changed = False
    original_text = fn_text

    binops: dict[str, dict[str, str]] = {}
    for idx, line in enumerate(lines):
        stripped = line.rstrip("\n")
        match = _BINOP_RE.match(stripped)
        if match is None:
            continue
        binops[match.group("result")] = {
            "line_idx": idx,
            "indent": match.group("indent"),
            "result": match.group("result"),
            "op": match.group("op"),
            "flags": match.group("flags"),
            "ty": match.group("ty"),
            "lhs": match.group("lhs").strip(),
            "rhs": match.group("rhs").strip(),
        }

    for idx, line in enumerate(list(lines)):
        stripped = line.rstrip("\n")

        match = _BINOP_RE.match(stripped)
        if match is not None:
            info = {
                "indent": match.group("indent"),
                "result": match.group("result"),
                "op": match.group("op"),
                "flags": match.group("flags"),
                "ty": match.group("ty"),
                "lhs": match.group("lhs").strip(),
                "rhs": match.group("rhs").strip(),
            }
            op = info["op"]
            lhs = info["lhs"]
            rhs = info["rhs"]
            flags = info["flags"]
            ty = info["ty"]
            result = info["result"]

            folded_const = _fold_const_binop(op, ty, lhs, rhs, flags)
            if folded_const is not None:
                replacements[result] = str(folded_const)
                lines[idx] = ""
                changed = True
                continue

            if op == "add" and lhs == rhs:
                lines[idx] = _format_line(
                    indent=info["indent"],
                    result=result,
                    op="shl",
                    flags=flags,
                    ty=ty,
                    lhs=lhs,
                    rhs="1",
                )
                changed = True
                continue

            if op == "add":
                lhs_not_base = _bitnot_base(lhs, ty, binops)
                rhs_not_base = _bitnot_base(rhs, ty, binops)
                lhs_affine = _base_plus_const(lhs, ty, binops)
                rhs_affine = _base_plus_const(rhs, ty, binops)
                if (
                    lhs_not_base is not None
                    and rhs_affine is not None
                    and rhs_affine[0] == lhs_not_base
                ):
                    replacements[result] = str(rhs_affine[1] - 1)
                    lines[idx] = ""
                    changed = True
                    continue
                if (
                    rhs_not_base is not None
                    and lhs_affine is not None
                    and lhs_affine[0] == rhs_not_base
                ):
                    replacements[result] = str(lhs_affine[1] - 1)
                    lines[idx] = ""
                    changed = True
                    continue
                shl_name = None
                if lhs == "1" and rhs.startswith("%"):
                    shl_name = rhs[1:]
                elif rhs == "1" and lhs.startswith("%"):
                    shl_name = lhs[1:]
                if shl_name is not None:
                    shl = binops.get(shl_name)
                    if (
                        shl is not None
                        and shl["op"] == "shl"
                        and shl["ty"] == ty
                    ):
                        sh_amt = _try_int(shl["rhs"])
                        if sh_amt is not None and sh_amt > 0:
                            lines[idx] = _format_line(
                                indent=info["indent"],
                                result=result,
                                op="or",
                                flags=" disjoint",
                                ty=ty,
                                lhs=f"%{shl_name}",
                                rhs="1",
                            )
                            changed = True
                            continue
                if (
                    lhs_affine is not None
                    and rhs_affine is not None
                    and lhs_affine[0] == rhs_affine[0]
                    and (lhs_affine[1] != 0 or rhs_affine[1] != 0)
                ):
                    combined_const = lhs_affine[1] + rhs_affine[1]
                    tmp_name = _unique_ssa_name("reass.add", original_text)
                    rhs_info = binops.get(rhs[1:]) if rhs.startswith("%") else None
                    reuse_rhs_name = (
                        rhs_info is not None
                        and rhs_info["op"] in {"add", "sub"}
                        and rhs_info["ty"] == ty
                    )
                    if reuse_rhs_name:
                        tmp_name = rhs[1:]
                        lines[rhs_info["line_idx"]] = _format_line(
                            indent=rhs_info["indent"],
                            result=tmp_name,
                            op="shl",
                            flags=rhs_info["flags"],
                            ty=ty,
                            lhs=lhs_affine[0],
                            rhs="1",
                        )
                    if combined_const == 0:
                        if tmp_name == result:
                            lines[idx] = _format_line(
                                indent=info["indent"],
                                result=result,
                                op="shl",
                                flags=flags,
                                ty=ty,
                                lhs=lhs_affine[0],
                                rhs="1",
                            )
                        else:
                            replacements[result] = f"%{tmp_name}"
                            lines[idx] = ""
                    else:
                        prefix = ""
                        if not reuse_rhs_name:
                            prefix = _format_line(
                                indent=info["indent"],
                                result=tmp_name,
                                op="shl",
                                flags=flags,
                                ty=ty,
                                lhs=lhs_affine[0],
                                rhs="1",
                            )
                        lines[idx] = prefix + _format_line(
                            indent=info["indent"],
                            result=result,
                            op="add",
                            flags=flags,
                            ty=ty,
                            lhs=f"%{tmp_name}",
                            rhs=str(combined_const),
                        )
                    changed = True
                    continue
                lhs_scaled = _scaled_operand(lhs, ty, binops)
                rhs_scaled = _scaled_operand(rhs, ty, binops)
                if (
                    lhs_scaled is not None
                    and rhs_scaled is not None
                    and lhs_scaled[0] == rhs_scaled[0]
                    and (lhs_scaled[1] != 1 or rhs_scaled[1] != 1)
                ):
                    combined_scale = lhs_scaled[1] + rhs_scaled[1]
                    if combined_scale == 0:
                        replacements[result] = "0"
                        lines[idx] = ""
                        changed = True
                        continue
                    if combined_scale == 1:
                        replacements[result] = lhs_scaled[0]
                        lines[idx] = ""
                        changed = True
                        continue
                    lines[idx] = _format_scaled_value(
                        indent=info["indent"],
                        result=result,
                        flags=flags,
                        ty=ty,
                        base=lhs_scaled[0],
                        scale=combined_scale,
                    )
                    changed = True
                    continue

            if op == "add" and lhs.startswith("%") and rhs.startswith("%"):
                lhs_not = binops.get(lhs[1:])
                if (
                    lhs_not is not None
                    and lhs_not["op"] == "xor"
                    and lhs_not["ty"] == ty
                    and (
                        _is_neg_one(lhs_not["lhs"], ty)
                        or _is_neg_one(lhs_not["rhs"], ty)
                    )
                ):
                    non_all_ones = (
                        lhs_not["lhs"]
                        if not _is_neg_one(lhs_not["lhs"], ty)
                        else lhs_not["rhs"]
                    )
                    if non_all_ones == rhs:
                        replacements[result] = "-1"
                        lines[idx] = ""
                        changed = True
                        continue
                rhs_not = binops.get(rhs[1:])
                if (
                    rhs_not is not None
                    and rhs_not["op"] == "xor"
                    and rhs_not["ty"] == ty
                    and (
                        _is_neg_one(rhs_not["lhs"], ty)
                        or _is_neg_one(rhs_not["rhs"], ty)
                    )
                ):
                    non_all_ones = (
                        rhs_not["lhs"]
                        if not _is_neg_one(rhs_not["lhs"], ty)
                        else rhs_not["rhs"]
                    )
                    if non_all_ones == lhs:
                        replacements[result] = "-1"
                        lines[idx] = ""
                        changed = True
                        continue
                lhs_sub = binops.get(lhs[1:])
                rhs_sub = binops.get(rhs[1:])
                if (
                    rhs_sub is not None
                    and rhs_sub["op"] == "sub"
                    and rhs_sub["ty"] == ty
                    and rhs_sub["rhs"] == lhs
                ):
                    replacements[result] = rhs_sub["lhs"]
                    lines[idx] = ""
                    changed = True
                    continue
                if (
                    lhs_sub is not None
                    and lhs_sub["op"] == "sub"
                    and lhs_sub["ty"] == ty
                    and lhs_sub["rhs"] == rhs
                ):
                    replacements[result] = lhs_sub["lhs"]
                    lines[idx] = ""
                    changed = True
                    continue

            if op == "add":
                const_val = None
                neg_name = None
                if lhs.startswith("%") and _try_int(rhs) is not None:
                    neg_name = lhs[1:]
                    const_val = rhs
                elif rhs.startswith("%") and _try_int(lhs) is not None:
                    neg_name = rhs[1:]
                    const_val = lhs
                if neg_name is not None and const_val is not None:
                    neg = binops.get(neg_name)
                    if (
                        neg is not None
                        and neg["op"] == "xor"
                        and neg["ty"] == ty
                        and (
                            _is_neg_one(neg["rhs"], ty)
                            or _is_neg_one(neg["lhs"], ty)
                        )
                    ):
                        non_all_ones = (
                            neg["lhs"]
                            if not _is_neg_one(neg["lhs"], ty)
                            else neg["rhs"]
                        )
                        const_int = _try_int(const_val)
                        if const_int is None or const_int == 0:
                            continue
                        lines[idx] = _format_line(
                            indent=info["indent"],
                            result=result,
                            op="sub",
                            flags=flags,
                            ty=ty,
                            lhs=str(const_int - 1),
                            rhs=non_all_ones,
                        )
                        changed = True
                        continue

            if op == "sub":
                if lhs == rhs:
                    replacements[result] = "0"
                    lines[idx] = ""
                    changed = True
                    continue
                if rhs == "0":
                    replacements[result] = lhs
                    lines[idx] = ""
                    changed = True
                    continue
                lhs_affine = _base_plus_const(lhs, ty, binops)
                rhs_affine = _base_plus_const(rhs, ty, binops)
                lhs_not_base = _bitnot_base(lhs, ty, binops)
                rhs_not_base = _bitnot_base(rhs, ty, binops)
                if (
                    lhs_affine is not None
                    and rhs_affine is not None
                    and lhs_affine[0] == rhs_affine[0]
                ):
                    replacements[result] = str(lhs_affine[1] - rhs_affine[1])
                    lines[idx] = ""
                    changed = True
                    continue
                if (
                    lhs_not_base is not None
                    and rhs_affine is not None
                    and rhs_affine[0] == lhs_not_base
                    and rhs_affine[1] != 0
                ):
                    tmp_name = _unique_ssa_name("0", original_text)
                    sub_flags = " nuw nsw" if rhs_affine[1] == 1 else flags
                    lines[idx] = (
                        _format_line(
                            indent=info["indent"],
                            result=tmp_name,
                            op="shl",
                            flags=flags,
                            ty=ty,
                            lhs=lhs_not_base,
                            rhs="1",
                        )
                        + _format_line(
                            indent=info["indent"],
                            result=result,
                            op="sub",
                            flags=sub_flags,
                            ty=ty,
                            lhs=str(-(rhs_affine[1] + 1)),
                            rhs=f"%{tmp_name}",
                        )
                    )
                    changed = True
                    continue
                if (
                    rhs_not_base is not None
                    and lhs_affine is not None
                    and lhs_affine[0] == rhs_not_base
                    and lhs_affine[1] != 0
                ):
                    tmp_name = lhs[1:] if lhs.startswith("%") else _unique_ssa_name("reass.add", original_text)
                    lhs_info = binops.get(lhs[1:]) if lhs.startswith("%") else None
                    if lhs_info is not None and lhs_info["op"] in {"add", "sub"} and lhs_info["ty"] == ty:
                        lines[lhs_info["line_idx"]] = _format_line(
                            indent=lhs_info["indent"],
                            result=tmp_name,
                            op="shl",
                            flags=lhs_info["flags"],
                            ty=ty,
                            lhs=rhs_not_base,
                            rhs="1",
                        )
                    lines[idx] = (
                        (
                            ""
                            if lhs_info is not None and lhs_info["op"] in {"add", "sub"} and lhs_info["ty"] == ty
                            else _format_line(
                                indent=info["indent"],
                                result=tmp_name,
                                op="shl",
                                flags=flags,
                                ty=ty,
                                lhs=rhs_not_base,
                                rhs="1",
                            )
                        )
                        + _format_line(
                            indent=info["indent"],
                            result=result,
                            op="add",
                            flags=flags,
                            ty=ty,
                            lhs=f"%{tmp_name}",
                            rhs=str(lhs_affine[1] + 1),
                        )
                    )
                    changed = True
                    continue
                if lhs.startswith("%") and rhs.startswith("%"):
                    lhs_sub = binops.get(lhs[1:])
                    if (
                        lhs_sub is not None
                        and lhs_sub["op"] == "sub"
                        and lhs_sub["ty"] == ty
                    ):
                        lhs_sub_const = _try_int(lhs_sub["lhs"])
                        if lhs_sub_const is not None and lhs_sub["rhs"] == rhs:
                            tmp_name = _unique_ssa_name("0", original_text)
                            lines[idx] = (
                                _format_line(
                                    indent=info["indent"],
                                    result=tmp_name,
                                    op="shl",
                                    flags=flags,
                                    ty=ty,
                                    lhs=rhs,
                                    rhs="1",
                                )
                                + _format_line(
                                    indent=info["indent"],
                                    result=result,
                                    op="sub",
                                    flags=flags,
                                    ty=ty,
                                    lhs=str(lhs_sub_const),
                                    rhs=f"%{tmp_name}",
                                )
                            )
                            changed = True
                            continue
                    rhs_sub = binops.get(rhs[1:])
                    if (
                        rhs_sub is not None
                        and rhs_sub["op"] == "sub"
                        and rhs_sub["ty"] == ty
                    ):
                        rhs_sub_const = _try_int(rhs_sub["lhs"])
                        if rhs_sub_const is not None and rhs_sub["rhs"] == lhs:
                            tmp_name = _unique_ssa_name("reass.add", original_text)
                            if rhs_sub_const == -1:
                                lines[idx] = (
                                    _format_line(
                                        indent=info["indent"],
                                        result=tmp_name,
                                        op="shl",
                                        flags=flags,
                                        ty=ty,
                                        lhs=lhs,
                                        rhs="1",
                                    )
                                    + _format_line(
                                        indent=info["indent"],
                                        result=result,
                                        op="or",
                                        flags=" disjoint",
                                        ty=ty,
                                        lhs=f"%{tmp_name}",
                                        rhs="1",
                                    )
                                )
                            else:
                                lines[idx] = (
                                    _format_line(
                                        indent=info["indent"],
                                        result=tmp_name,
                                        op="shl",
                                        flags=flags,
                                        ty=ty,
                                        lhs=lhs,
                                        rhs="1",
                                    )
                                    + _format_line(
                                        indent=info["indent"],
                                        result=result,
                                        op="add",
                                        flags=flags,
                                        ty=ty,
                                        lhs=f"%{tmp_name}",
                                        rhs=str(-rhs_sub_const),
                                    )
                                )
                            changed = True
                            continue
                if _is_neg_one(lhs, ty) and rhs.startswith("%"):
                    rhs_not = binops.get(rhs[1:])
                    if (
                        rhs_not is not None
                        and rhs_not["op"] == "xor"
                        and rhs_not["ty"] == ty
                        and (
                            _is_neg_one(rhs_not["lhs"], ty)
                            or _is_neg_one(rhs_not["rhs"], ty)
                        )
                    ):
                        replacements[result] = (
                            rhs_not["lhs"]
                            if not _is_neg_one(rhs_not["lhs"], ty)
                            else rhs_not["rhs"]
                        )
                        lines[idx] = ""
                        changed = True
                        continue
                const_val = None
                neg_name = None
                if lhs.startswith("%") and _try_int(rhs) is not None:
                    neg_name = lhs[1:]
                    const_val = rhs
                elif rhs.startswith("%") and _try_int(lhs) is not None:
                    neg_name = rhs[1:]
                    const_val = lhs
                if neg_name is not None and const_val is not None:
                    neg = binops.get(neg_name)
                    if (
                        neg is not None
                        and neg["op"] == "xor"
                        and neg["ty"] == ty
                        and (
                            _is_neg_one(neg["rhs"], ty)
                            or _is_neg_one(neg["lhs"], ty)
                        )
                    ):
                        non_all_ones = (
                            neg["lhs"]
                            if not _is_neg_one(neg["lhs"], ty)
                            else neg["rhs"]
                        )
                        const_int = _try_int(const_val)
                        if const_int is not None:
                            if lhs.startswith("%"):
                                if const_int == 0:
                                    replacements[result] = lhs
                                    lines[idx] = ""
                                else:
                                    lines[idx] = _format_line(
                                        indent=info["indent"],
                                        result=result,
                                        op="sub",
                                        flags=flags,
                                        ty=ty,
                                        lhs=str(-(const_int + 1)),
                                        rhs=non_all_ones,
                                    )
                            else:
                                if const_int == 0:
                                    new_name = _unique_ssa_name(f"{neg_name}.neg", original_text)
                                    replacements[result] = f"%{new_name}"
                                    lines[idx] = _format_line(
                                        indent=info["indent"],
                                        result=new_name,
                                        op="add",
                                        flags=flags,
                                        ty=ty,
                                        lhs=non_all_ones,
                                        rhs="1",
                                    )
                                else:
                                    lines[idx] = _format_line(
                                        indent=info["indent"],
                                        result=result,
                                        op="add",
                                        flags=flags,
                                        ty=ty,
                                        lhs=non_all_ones,
                                        rhs=str(const_int + 1),
                                    )
                            changed = True
                            continue
                if lhs.startswith("%") and rhs.startswith("%"):
                    rhs_not = binops.get(rhs[1:])
                    if (
                        rhs_not is not None
                        and rhs_not["op"] == "xor"
                        and rhs_not["ty"] == ty
                        and (
                            _is_neg_one(rhs_not["lhs"], ty)
                            or _is_neg_one(rhs_not["rhs"], ty)
                        )
                    ):
                        non_all_ones = (
                            rhs_not["lhs"]
                            if not _is_neg_one(rhs_not["lhs"], ty)
                            else rhs_not["rhs"]
                        )
                        if non_all_ones == lhs:
                            tmp_name = _unique_ssa_name("reass.add", original_text)
                            lines[idx] = (
                                _format_line(
                                    indent=info["indent"],
                                    result=tmp_name,
                                    op="shl",
                                    flags=flags,
                                    ty=ty,
                                    lhs=lhs,
                                    rhs="1",
                                )
                                + _format_line(
                                    indent=info["indent"],
                                    result=result,
                                    op="or",
                                    flags=" disjoint",
                                    ty=ty,
                                    lhs=f"%{tmp_name}",
                                    rhs="1",
                                )
                            )
                            changed = True
                            continue
                lhs_scaled = _scaled_operand(lhs, ty, binops)
                rhs_scaled = _scaled_operand(rhs, ty, binops)
                if (
                    lhs_scaled is not None
                    and rhs_scaled is not None
                    and lhs_scaled[0] == rhs_scaled[0]
                    and (lhs_scaled[1] != 1 or rhs_scaled[1] != 1)
                ):
                    combined_scale = lhs_scaled[1] - rhs_scaled[1]
                    if combined_scale == 0:
                        replacements[result] = "0"
                        lines[idx] = ""
                        changed = True
                        continue
                    if combined_scale == 1:
                        replacements[result] = lhs_scaled[0]
                        lines[idx] = ""
                        changed = True
                        continue
                    lines[idx] = _format_scaled_value(
                        indent=info["indent"],
                        result=result,
                        flags=flags,
                        ty=ty,
                        base=lhs_scaled[0],
                        scale=combined_scale,
                    )
                    changed = True
                    continue
                if lhs == "0" and rhs.startswith("%"):
                    inner = binops.get(rhs[1:])
                    if inner is not None and inner["ty"] == ty:
                        if inner["op"] == "mul":
                            lhs_c = _try_int(inner["lhs"])
                            rhs_c = _try_int(inner["rhs"])
                            factor = rhs_c if lhs_c is None else lhs_c
                            base = inner["lhs"] if lhs_c is None else inner["rhs"]
                            if factor is not None and _try_int(base) is None:
                                new_result = _unique_ssa_name(f"{rhs[1:]}.neg", original_text)
                                replacements[result] = f"%{new_result}"
                                lines[idx] = _format_scaled_value(
                                    indent=info["indent"],
                                    result=new_result,
                                    flags=flags,
                                    ty=ty,
                                    base=base,
                                    scale=-factor,
                                )
                                changed = True
                                continue
                        if inner["op"] == "shl":
                            sh_amt = _try_int(inner["rhs"])
                            if sh_amt is not None and sh_amt > 0 and _try_int(inner["lhs"]) is None:
                                new_result = _unique_ssa_name(f"{rhs[1:]}.neg", original_text)
                                replacements[result] = f"%{new_result}"
                                lines[idx] = _format_scaled_value(
                                    indent=info["indent"],
                                    result=new_result,
                                    flags=flags,
                                    ty=ty,
                                    base=inner["lhs"],
                                    scale=-power_of_two(sh_amt),
                                )
                                changed = True
                                continue
                    inner_sub = binops.get(rhs[1:])
                    if (
                        inner_sub is not None
                        and inner_sub["op"] == "sub"
                        and inner_sub["ty"] == ty
                        and inner_sub["lhs"] != "0"
                    ):
                        inner_lhs_int = _try_int(inner_sub["lhs"])
                        new_result = _unique_ssa_name(f"{rhs[1:]}.neg", original_text)
                        replacements[result] = f"%{new_result}"
                        if inner_lhs_int is not None:
                            lines[idx] = _format_line(
                                indent=info["indent"],
                                result=new_result,
                                op="add",
                                flags=flags,
                                ty=ty,
                                lhs=inner_sub["rhs"],
                                rhs=str(-inner_lhs_int),
                            )
                        else:
                            lines[idx] = _format_line(
                                indent=info["indent"],
                                result=new_result,
                                op="sub",
                                flags=flags,
                                ty=ty,
                                lhs=inner_sub["rhs"],
                                rhs=inner_sub["lhs"],
                            )
                        changed = True
                        continue
                lhs_int = _try_int(lhs)
                if lhs.startswith("%"):
                    add = binops.get(lhs[1:])
                    rhs_int = _try_int(rhs)
                    if (
                        add is not None
                        and add["op"] == "add"
                        and add["ty"] == ty
                        and rhs_int is not None
                    ):
                        add_rhs_int = _try_int(add["rhs"])
                        add_lhs_int = _try_int(add["lhs"])
                        if add_rhs_int == rhs_int:
                            replacements[result] = add["lhs"]
                            lines[idx] = ""
                            changed = True
                            continue
                        if add_lhs_int == rhs_int:
                            replacements[result] = add["rhs"]
                            lines[idx] = ""
                            changed = True
                            continue
                        if add_rhs_int is not None:
                            lines[idx] = _format_line(
                                indent=info["indent"],
                                result=result,
                                op="add",
                                flags=flags,
                                ty=ty,
                                lhs=add["lhs"],
                                rhs=str(add_rhs_int - rhs_int),
                            )
                            changed = True
                            continue
                        if add_lhs_int is not None:
                            lines[idx] = _format_line(
                                indent=info["indent"],
                                result=result,
                                op="add",
                                flags=flags,
                                ty=ty,
                                lhs=add["rhs"],
                                rhs=str(add_lhs_int - rhs_int),
                            )
                            changed = True
                            continue
                    if add is not None and add["op"] == "add" and add["ty"] == ty:
                        if add["rhs"] == rhs:
                            replacements[result] = add["lhs"]
                            lines[idx] = ""
                            changed = True
                            continue
                        if add["lhs"] == rhs:
                            replacements[result] = add["rhs"]
                            lines[idx] = ""
                            changed = True
                            continue
                if lhs.startswith("%") and rhs.startswith("%"):
                    rhs_add = binops.get(rhs[1:])
                    if rhs_add is not None and rhs_add["op"] == "add" and rhs_add["ty"] == ty:
                        if rhs_add["lhs"] == lhs:
                            lines[idx] = _format_line(
                                indent=info["indent"],
                                result=result,
                                op="sub",
                                flags=flags,
                                ty=ty,
                                lhs="0",
                                rhs=rhs_add["rhs"],
                            )
                            changed = True
                            continue
                        if rhs_add["rhs"] == lhs:
                            lines[idx] = _format_line(
                                indent=info["indent"],
                                result=result,
                                op="sub",
                                flags=flags,
                                ty=ty,
                                lhs="0",
                                rhs=rhs_add["lhs"],
                            )
                            changed = True
                            continue
                if lhs_int is not None and rhs.startswith("%"):
                    folded = binops.get(rhs[1:])
                    if folded is not None and folded["ty"] == ty:
                        if folded["op"] == "add":
                            folded_rhs_int = _try_int(folded["rhs"])
                            folded_lhs_int = _try_int(folded["lhs"])
                            if folded_rhs_int is not None:
                                lines[idx] = _format_line(
                                    indent=info["indent"],
                                    result=result,
                                    op="sub",
                                    flags=flags,
                                    ty=ty,
                                    lhs=str(lhs_int - folded_rhs_int),
                                    rhs=folded["lhs"],
                                )
                                changed = True
                                continue
                            if folded_lhs_int is not None:
                                lines[idx] = _format_line(
                                    indent=info["indent"],
                                    result=result,
                                    op="sub",
                                    flags=flags,
                                    ty=ty,
                                    lhs=str(lhs_int - folded_lhs_int),
                                    rhs=folded["rhs"],
                                )
                                changed = True
                                continue
                        if folded["op"] == "sub":
                            folded_lhs_int = _try_int(folded["lhs"])
                            folded_rhs_int = _try_int(folded["rhs"])
                            if folded_lhs_int is not None:
                                lines[idx] = _format_line(
                                    indent=info["indent"],
                                    result=result,
                                    op="add",
                                    flags=flags,
                                    ty=ty,
                                    lhs=folded["rhs"],
                                    rhs=str(lhs_int - folded_lhs_int),
                                )
                                changed = True
                                continue
                            if folded_rhs_int is not None:
                                lines[idx] = _format_line(
                                    indent=info["indent"],
                                    result=result,
                                    op="sub",
                                    flags=flags,
                                    ty=ty,
                                    lhs=str(lhs_int + folded_rhs_int),
                                    rhs=folded["lhs"],
                                )
                                changed = True
                                continue
                rhs_int = _try_int(rhs)
                if lhs.startswith("%") and rhs_int is not None:
                    folded = binops.get(lhs[1:])
                    if (
                        folded is not None
                        and folded["op"] == "sub"
                        and folded["ty"] == ty
                    ):
                        folded_rhs_int = _try_int(folded["rhs"])
                        if folded_rhs_int is not None:
                            lines[idx] = _format_line(
                                indent=info["indent"],
                                result=result,
                                op="add",
                                flags=flags,
                                ty=ty,
                                lhs=folded["lhs"],
                                rhs=str(-(folded_rhs_int + rhs_int)),
                            )
                            changed = True
                            continue
                if rhs_int is not None and rhs_int < 0:
                    lines[idx] = _format_line(
                        indent=info["indent"],
                        result=result,
                        op="add",
                        flags=flags,
                        ty=ty,
                        lhs=lhs,
                        rhs=str(-rhs_int),
                    )
                    changed = True
                    continue
                if lhs == "0" and rhs.startswith("%"):
                    neg = binops.get(rhs[1:])
                    if (
                        neg is not None
                        and neg["op"] == "sub"
                        and neg["ty"] == ty
                        and neg["lhs"] == "0"
                    ):
                        replacements[result] = neg["rhs"]
                        lines[idx] = ""
                        changed = True
                        continue
                    if (
                        neg is not None
                        and neg["op"] == "xor"
                        and neg["ty"] == ty
                        and (
                            _is_neg_one(neg["rhs"], ty)
                            or _is_neg_one(neg["lhs"], ty)
                        )
                    ):
                        non_all_ones = (
                            neg["lhs"]
                            if not _is_neg_one(neg["lhs"], ty)
                            else neg["rhs"]
                        )
                        new_name = _unique_ssa_name(f"{rhs[1:]}.neg", original_text)
                        replacements[result] = f"%{new_name}"
                        lines[idx] = _format_line(
                            indent=info["indent"],
                            result=new_name,
                            op="add",
                            flags=flags,
                            ty=ty,
                            lhs=non_all_ones,
                            rhs="1",
                        )
                        changed = True
                        continue
                if lhs.startswith("%") and rhs.startswith("%"):
                    neg = binops.get(rhs[1:])
                    if (
                        neg is not None
                        and neg["op"] == "sub"
                        and neg["ty"] == ty
                        and neg["lhs"] == "0"
                    ):
                        lines[idx] = _format_line(
                            indent=info["indent"],
                            result=result,
                            op="add",
                            flags=flags,
                            ty=ty,
                            lhs=lhs,
                            rhs=neg["rhs"],
                        )
                        changed = True
                        continue

                    shl = binops.get(lhs[1:])
                    if (
                        shl is not None
                        and shl["op"] == "shl"
                        and shl["ty"] == ty
                        and shl["rhs"] == "1"
                        and shl["lhs"] == rhs
                    ):
                        replacements[result] = rhs
                        lines[idx] = ""
                        changed = True
                        continue

                if lhs.startswith("%") and rhs.startswith("%"):
                    scaled = binops.get(lhs[1:])
                    if scaled is not None and scaled["ty"] == ty and scaled["lhs"] == rhs:
                        if scaled["op"] == "shl":
                            sh_amt = _try_int(scaled["rhs"])
                            if sh_amt is not None and sh_amt > 0:
                                lines[idx] = _format_scaled_value(
                                    indent=info["indent"],
                                    result=result,
                                    flags=flags,
                                    ty=ty,
                                    base=rhs,
                                    scale=power_of_two(sh_amt) - 1,
                                )
                                changed = True
                                continue
                        if scaled["op"] == "mul":
                            mul_c = _try_int(scaled["rhs"])
                            if mul_c is not None and mul_c > 1:
                                lines[idx] = _format_scaled_value(
                                    indent=info["indent"],
                                    result=result,
                                    flags=flags,
                                    ty=ty,
                                    base=rhs,
                                    scale=mul_c - 1,
                                )
                                changed = True
                                continue

                    scaled_rhs = binops.get(rhs[1:])
                    if scaled_rhs is not None and scaled_rhs["ty"] == ty and scaled_rhs["lhs"] == lhs:
                        if scaled_rhs["op"] == "shl":
                            sh_amt = _try_int(scaled_rhs["rhs"])
                            if sh_amt is not None and sh_amt > 0:
                                lines[idx] = _format_scaled_value(
                                    indent=info["indent"],
                                    result=result,
                                    flags=flags,
                                    ty=ty,
                                    base=lhs,
                                    scale=1 - power_of_two(sh_amt),
                                )
                                changed = True
                                continue
                        if scaled_rhs["op"] == "mul":
                            mul_c = _try_int(scaled_rhs["rhs"])
                            if mul_c is not None:
                                lines[idx] = _format_scaled_value(
                                    indent=info["indent"],
                                    result=result,
                                    flags=flags,
                                    ty=ty,
                                    base=lhs,
                                    scale=1 - mul_c,
                                )
                                changed = True
                                continue

            if op == "mul":
                lhs_int = _try_int(lhs)
                rhs_int = _try_int(rhs)
                if lhs_int == 1 and rhs_int is None:
                    replacements[result] = rhs
                    lines[idx] = ""
                    changed = True
                    continue
                if rhs_int == 1 and lhs_int is None:
                    replacements[result] = lhs
                    lines[idx] = ""
                    changed = True
                    continue
                if lhs_int == 0 or rhs_int == 0:
                    replacements[result] = "0"
                    lines[idx] = ""
                    changed = True
                    continue
                if lhs_int == -1 and rhs_int is None:
                    lines[idx] = _format_line(
                        indent=info["indent"],
                        result=result,
                        op="sub",
                        flags=flags,
                        ty=ty,
                        lhs="0",
                        rhs=rhs,
                    )
                    changed = True
                    continue
                if rhs_int == -1 and lhs_int is None:
                    lines[idx] = _format_line(
                        indent=info["indent"],
                        result=result,
                        op="sub",
                        flags=flags,
                        ty=ty,
                        lhs="0",
                        rhs=lhs,
                    )
                    changed = True
                    continue

                const_val = rhs_int if rhs_int is not None else lhs_int
                other = lhs if rhs_int is not None else rhs
                if const_val is not None:
                    shift = _is_pow2(const_val)
                    if shift is not None and shift > 0 and _try_int(other) is None:
                        lines[idx] = _format_line(
                            indent=info["indent"],
                            result=result,
                            op="shl",
                            flags=flags,
                            ty=ty,
                            lhs=other,
                            rhs=str(shift),
                        )
                        changed = True
                        continue

            if op == "add":
                const_lhs = _try_int(lhs)
                const_rhs = _try_int(rhs)
                if const_lhs == 0:
                    replacements[result] = rhs
                    lines[idx] = ""
                    changed = True
                    continue
                if const_rhs == 0:
                    replacements[result] = lhs
                    lines[idx] = ""
                    changed = True
                    continue
                if const_lhs is not None and rhs.startswith("%"):
                    add_rhs = binops.get(rhs[1:])
                    if (
                        add_rhs is not None
                        and add_rhs["op"] == "add"
                        and add_rhs["ty"] == ty
                    ):
                        add_rhs_rhs_int = _try_int(add_rhs["rhs"])
                        add_rhs_lhs_int = _try_int(add_rhs["lhs"])
                        if add_rhs_rhs_int is not None:
                            lines[idx] = _format_line(
                                indent=info["indent"],
                                result=result,
                                op="add",
                                flags=flags,
                                ty=ty,
                                lhs=add_rhs["lhs"],
                                rhs=str(add_rhs_rhs_int + const_lhs),
                            )
                            changed = True
                            continue
                        if add_rhs_lhs_int is not None:
                            lines[idx] = _format_line(
                                indent=info["indent"],
                                result=result,
                                op="add",
                                flags=flags,
                                ty=ty,
                                lhs=add_rhs["rhs"],
                                rhs=str(add_rhs_lhs_int + const_lhs),
                            )
                            changed = True
                            continue
                    sub_rhs = binops.get(rhs[1:])
                    if (
                        sub_rhs is not None
                        and sub_rhs["op"] == "sub"
                        and sub_rhs["ty"] == ty
                    ):
                        sub_rhs_rhs_int = _try_int(sub_rhs["rhs"])
                        sub_rhs_lhs_int = _try_int(sub_rhs["lhs"])
                        if sub_rhs_rhs_int == const_lhs:
                            replacements[result] = sub_rhs["lhs"]
                            lines[idx] = ""
                            changed = True
                            continue
                        if sub_rhs_rhs_int is not None:
                            lines[idx] = _format_line(
                                indent=info["indent"],
                                result=result,
                                op="add",
                                flags=flags,
                                ty=ty,
                                lhs=sub_rhs["lhs"],
                                rhs=str(const_lhs - sub_rhs_rhs_int),
                            )
                            changed = True
                            continue
                        if sub_rhs_lhs_int is not None:
                            lines[idx] = _format_line(
                                indent=info["indent"],
                                result=result,
                                op="sub",
                                flags=flags,
                                ty=ty,
                                lhs=str(const_lhs + sub_rhs_lhs_int),
                                rhs=sub_rhs["rhs"],
                            )
                            changed = True
                            continue
                if const_rhs is not None and lhs.startswith("%"):
                    add_lhs = binops.get(lhs[1:])
                    if (
                        add_lhs is not None
                        and add_lhs["op"] == "add"
                        and add_lhs["ty"] == ty
                    ):
                        add_lhs_rhs_int = _try_int(add_lhs["rhs"])
                        add_lhs_lhs_int = _try_int(add_lhs["lhs"])
                        if add_lhs_rhs_int is not None:
                            lines[idx] = _format_line(
                                indent=info["indent"],
                                result=result,
                                op="add",
                                flags=flags,
                                ty=ty,
                                lhs=add_lhs["lhs"],
                                rhs=str(add_lhs_rhs_int + const_rhs),
                            )
                            changed = True
                            continue
                        if add_lhs_lhs_int is not None:
                            lines[idx] = _format_line(
                                indent=info["indent"],
                                result=result,
                                op="add",
                                flags=flags,
                                ty=ty,
                                lhs=add_lhs["rhs"],
                                rhs=str(add_lhs_lhs_int + const_rhs),
                            )
                            changed = True
                            continue
                    sub_lhs = binops.get(lhs[1:])
                    if (
                        sub_lhs is not None
                        and sub_lhs["op"] == "sub"
                        and sub_lhs["ty"] == ty
                    ):
                        sub_rhs_int = _try_int(sub_lhs["rhs"])
                        sub_lhs_int = _try_int(sub_lhs["lhs"])
                        if sub_rhs_int == const_rhs:
                            replacements[result] = sub_lhs["lhs"]
                            lines[idx] = ""
                            changed = True
                            continue
                        if sub_rhs_int is not None:
                            lines[idx] = _format_line(
                                indent=info["indent"],
                                result=result,
                                op="add",
                                flags=flags,
                                ty=ty,
                                lhs=sub_lhs["lhs"],
                                rhs=str(const_rhs - sub_rhs_int),
                            )
                            changed = True
                            continue
                        if sub_lhs_int is not None:
                            lines[idx] = _format_line(
                                indent=info["indent"],
                                result=result,
                                op="sub",
                                flags=flags,
                                ty=ty,
                                lhs=str(sub_lhs_int + const_rhs),
                                rhs=sub_lhs["rhs"],
                            )
                            changed = True
                            continue
                if lhs.startswith("%") and rhs.startswith("%"):
                    lhs_sub = binops.get(lhs[1:])
                    rhs_sub = binops.get(rhs[1:])
                    if (
                        lhs_sub is not None
                        and lhs_sub["op"] == "sub"
                        and lhs_sub["ty"] == ty
                        and lhs_sub["lhs"] == "0"
                    ):
                        lines[idx] = _format_line(
                            indent=info["indent"],
                            result=result,
                            op="sub",
                            flags=flags,
                            ty=ty,
                            lhs=rhs,
                            rhs=lhs_sub["rhs"],
                        )
                        changed = True
                        continue
                    lhs_sub_const = _try_int(lhs_sub["lhs"]) if lhs_sub is not None else None
                    if (
                        lhs_sub is not None
                        and lhs_sub["op"] == "sub"
                        and lhs_sub["ty"] == ty
                        and lhs_sub_const is not None
                        and lhs_sub["rhs"] == rhs
                    ):
                        tmp_name = _unique_ssa_name("reass.sub", original_text)
                        lines[idx] = (
                            _format_line(
                                indent=info["indent"],
                                result=tmp_name,
                                op="shl",
                                flags=flags,
                                ty=ty,
                                lhs=rhs,
                                rhs="1",
                            )
                            + _format_line(
                                indent=info["indent"],
                                result=result,
                                op="sub",
                                flags=flags,
                                ty=ty,
                                lhs=str(lhs_sub_const),
                                rhs=f"%{tmp_name}",
                            )
                        )
                        changed = True
                        continue
                    if (
                        rhs_sub is not None
                        and rhs_sub["op"] == "sub"
                        and rhs_sub["ty"] == ty
                        and rhs_sub["lhs"] == "0"
                    ):
                        lines[idx] = _format_line(
                            indent=info["indent"],
                            result=result,
                            op="sub",
                            flags=flags,
                            ty=ty,
                            lhs=lhs,
                            rhs=rhs_sub["rhs"],
                        )
                        changed = True
                        continue
                    rhs_sub_const = _try_int(rhs_sub["lhs"]) if rhs_sub is not None else None
                    if (
                        rhs_sub is not None
                        and rhs_sub["op"] == "sub"
                        and rhs_sub["ty"] == ty
                        and rhs_sub_const is not None
                        and rhs_sub["rhs"] == lhs
                    ):
                        tmp_name = _unique_ssa_name("reass.add", original_text)
                        lines[idx] = (
                            _format_line(
                                indent=info["indent"],
                                result=tmp_name,
                                op="shl",
                                flags=flags,
                                ty=ty,
                                lhs=lhs,
                                rhs="1",
                            )
                            + _format_line(
                                indent=info["indent"],
                                result=result,
                                op="add",
                                flags=flags,
                                ty=ty,
                                lhs=f"%{tmp_name}",
                                rhs=str(-rhs_sub_const),
                            )
                        )
                        changed = True
                        continue
                const_val = None
                neg_name = None
                if lhs.startswith("%") and _try_int(rhs) is not None:
                    neg_name = lhs[1:]
                    const_val = rhs
                elif rhs.startswith("%") and _try_int(lhs) is not None:
                    neg_name = rhs[1:]
                    const_val = lhs
                if neg_name is not None and const_val is not None:
                    neg = binops.get(neg_name)
                    if (
                        neg is not None
                        and neg["op"] == "sub"
                        and neg["ty"] == ty
                        and neg["lhs"] == "0"
                    ):
                        lines[idx] = _format_line(
                            indent=info["indent"],
                            result=result,
                            op="sub",
                            flags=flags,
                            ty=ty,
                            lhs=const_val,
                            rhs=neg["rhs"],
                        )
                        changed = True
                        continue
                    if (
                        neg is not None
                        and neg["op"] == "xor"
                        and neg["ty"] == ty
                        and _try_int(const_val) == 1
                        and (
                            _is_neg_one(neg["rhs"], ty)
                            or _is_neg_one(neg["lhs"], ty)
                        )
                    ):
                        non_all_ones = (
                            neg["lhs"]
                            if not _is_neg_one(neg["lhs"], ty)
                            else neg["rhs"]
                        )
                        const_int = _try_int(const_val)
                        if const_int is not None:
                            lines[idx] = _format_line(
                                indent=info["indent"],
                                result=result,
                                op="sub",
                                flags=flags,
                                ty=ty,
                                lhs="0",
                                rhs=non_all_ones,
                            )
                            changed = True
                            continue
                for shl_name, shl in binops.items():
                    if shl["op"] != "shl" or shl["ty"] != ty:
                        continue
                    sh_amt = _try_int(shl["rhs"])
                    if sh_amt is None or sh_amt <= 0:
                        continue
                    shl_val = f"%{shl_name}"
                    base = shl["lhs"]
                    if (lhs == shl_val and rhs == base) or (rhs == shl_val and lhs == base):
                        lines[idx] = _format_scaled_value(
                            indent=info["indent"],
                            result=result,
                            flags=flags,
                            ty=ty,
                            base=base,
                            scale=power_of_two(sh_amt) + 1,
                        )
                        changed = True
                        break
                    if base.startswith("%"):
                        neg_base = binops.get(base[1:])
                        if (
                            neg_base is not None
                            and neg_base["op"] == "sub"
                            and neg_base["ty"] == ty
                            and neg_base["lhs"] == "0"
                        ):
                            actual_base = neg_base["rhs"]
                            if (lhs == shl_val and rhs == actual_base) or (
                                rhs == shl_val and lhs == actual_base
                            ):
                                lines[idx] = _format_scaled_value(
                                    indent=info["indent"],
                                    result=result,
                                    flags=flags,
                                    ty=ty,
                                    base=actual_base,
                                    scale=1 - power_of_two(sh_amt),
                                )
                                changed = True
                                break
                if changed and lines[idx] != line:
                    continue

                for mul_name, mul in binops.items():
                    if mul["op"] != "mul" or mul["ty"] != ty:
                        continue
                    mul_val = f"%{mul_name}"
                    mul_c = _try_int(mul["rhs"])
                    base = mul["lhs"]
                    if mul_c is None or _try_int(base) is not None:
                        mul_c = _try_int(mul["lhs"])
                        base = mul["rhs"]
                    if mul_c is None or _try_int(base) is not None:
                        continue
                    if (lhs == mul_val and rhs == base) or (rhs == mul_val and lhs == base):
                        lines[idx] = _format_scaled_value(
                            indent=info["indent"],
                            result=result,
                            flags=flags,
                            ty=ty,
                            base=base,
                            scale=mul_c + 1,
                        )
                        changed = True
                        break
                    if base.startswith("%"):
                        neg_base = binops.get(base[1:])
                        if (
                            neg_base is not None
                            and neg_base["op"] == "sub"
                            and neg_base["ty"] == ty
                            and neg_base["lhs"] == "0"
                        ):
                            actual_base = neg_base["rhs"]
                            if (lhs == mul_val and rhs == actual_base) or (
                                rhs == mul_val and lhs == actual_base
                            ):
                                lines[idx] = _format_scaled_value(
                                    indent=info["indent"],
                                    result=result,
                                    flags=flags,
                                    ty=ty,
                                    base=actual_base,
                                    scale=1 - mul_c,
                                )
                                changed = True
                                break
                if changed and lines[idx] != line:
                    continue

            if op == "xor":
                if lhs == rhs:
                    replacements[result] = "0"
                    lines[idx] = ""
                    changed = True
                    continue
                if lhs == "0":
                    replacements[result] = rhs
                    lines[idx] = ""
                    changed = True
                    continue
                if rhs == "0":
                    replacements[result] = lhs
                    lines[idx] = ""
                    changed = True
                    continue
                const_lhs = _is_neg_one(lhs, ty)
                const_rhs = _is_neg_one(rhs, ty)
                if lhs.startswith("%") and const_rhs:
                    prev = binops.get(lhs[1:])
                    if (
                        prev is not None
                        and prev["op"] == "xor"
                        and prev["ty"] == ty
                        and (
                            _is_neg_one(prev["rhs"], ty)
                            or _is_neg_one(prev["lhs"], ty)
                        )
                    ):
                        replacements[result] = (
                            prev["lhs"]
                            if not _is_neg_one(prev["lhs"], ty)
                            else prev["rhs"]
                        )
                        lines[idx] = ""
                        changed = True
                        continue
                if rhs.startswith("%") and const_lhs:
                    prev = binops.get(rhs[1:])
                    if (
                        prev is not None
                        and prev["op"] == "xor"
                        and prev["ty"] == ty
                        and (
                            _is_neg_one(prev["rhs"], ty)
                            or _is_neg_one(prev["lhs"], ty)
                        )
                    ):
                        replacements[result] = (
                            prev["lhs"]
                            if not _is_neg_one(prev["lhs"], ty)
                            else prev["rhs"]
                        )
                        lines[idx] = ""
                        changed = True
                        continue
                if lhs.startswith("%") and rhs.startswith("%"):
                    lhs_prev = binops.get(lhs[1:])
                    if (
                        lhs_prev is not None
                        and lhs_prev["op"] == "xor"
                        and lhs_prev["ty"] == ty
                        and (
                            _is_neg_one(lhs_prev["rhs"], ty)
                            or _is_neg_one(lhs_prev["lhs"], ty)
                        )
                    ):
                        non_all_ones = (
                            lhs_prev["lhs"]
                            if not _is_neg_one(lhs_prev["lhs"], ty)
                            else lhs_prev["rhs"]
                        )
                        if non_all_ones == rhs:
                            replacements[result] = "-1"
                            lines[idx] = ""
                            changed = True
                            continue
                    rhs_prev = binops.get(rhs[1:])
                    if (
                        rhs_prev is not None
                        and rhs_prev["op"] == "xor"
                        and rhs_prev["ty"] == ty
                        and (
                            _is_neg_one(rhs_prev["rhs"], ty)
                            or _is_neg_one(rhs_prev["lhs"], ty)
                        )
                    ):
                        non_all_ones = (
                            rhs_prev["lhs"]
                            if not _is_neg_one(rhs_prev["lhs"], ty)
                            else rhs_prev["rhs"]
                        )
                        if non_all_ones == lhs:
                            replacements[result] = "-1"
                            lines[idx] = ""
                            changed = True
                            continue

            if op == "or":
                if lhs == rhs:
                    replacements[result] = lhs
                    lines[idx] = ""
                    changed = True
                    continue
                if _is_neg_one(lhs, ty) or _is_neg_one(rhs, ty):
                    replacements[result] = "-1"
                    lines[idx] = ""
                    changed = True
                    continue
                if lhs == "0":
                    replacements[result] = rhs
                    lines[idx] = ""
                    changed = True
                    continue
                if rhs == "0":
                    replacements[result] = lhs
                    lines[idx] = ""
                    changed = True
                    continue
                if lhs.startswith("%"):
                    prev = binops.get(lhs[1:])
                    if (
                        prev is not None
                        and prev["op"] == "xor"
                        and prev["ty"] == ty
                        and (
                            _is_neg_one(prev["rhs"], ty)
                            or _is_neg_one(prev["lhs"], ty)
                        )
                    ):
                        non_all_ones = (
                            prev["lhs"]
                            if not _is_neg_one(prev["lhs"], ty)
                            else prev["rhs"]
                        )
                        if non_all_ones == rhs:
                            replacements[result] = "-1"
                            lines[idx] = ""
                            changed = True
                            continue
                if rhs.startswith("%"):
                    prev = binops.get(rhs[1:])
                    if (
                        prev is not None
                        and prev["op"] == "xor"
                        and prev["ty"] == ty
                        and (
                            _is_neg_one(prev["rhs"], ty)
                            or _is_neg_one(prev["lhs"], ty)
                        )
                    ):
                        non_all_ones = (
                            prev["lhs"]
                            if not _is_neg_one(prev["lhs"], ty)
                            else prev["rhs"]
                        )
                        if non_all_ones == lhs:
                            replacements[result] = "-1"
                            lines[idx] = ""
                            changed = True
                            continue

            if op == "and":
                if lhs == rhs:
                    replacements[result] = lhs
                    lines[idx] = ""
                    changed = True
                    continue
                if lhs == "0" or rhs == "0":
                    replacements[result] = "0"
                    lines[idx] = ""
                    changed = True
                    continue
                if _is_neg_one(lhs, ty):
                    replacements[result] = rhs
                    lines[idx] = ""
                    changed = True
                    continue
                if _is_neg_one(rhs, ty):
                    replacements[result] = lhs
                    lines[idx] = ""
                    changed = True
                    continue
                if lhs.startswith("%"):
                    prev = binops.get(lhs[1:])
                    if (
                        prev is not None
                        and prev["op"] == "xor"
                        and prev["ty"] == ty
                        and (
                            _is_neg_one(prev["rhs"], ty)
                            or _is_neg_one(prev["lhs"], ty)
                        )
                    ):
                        non_all_ones = (
                            prev["lhs"]
                            if not _is_neg_one(prev["lhs"], ty)
                            else prev["rhs"]
                        )
                        if non_all_ones == rhs:
                            replacements[result] = "0"
                            lines[idx] = ""
                            changed = True
                            continue
                if rhs.startswith("%"):
                    prev = binops.get(rhs[1:])
                    if (
                        prev is not None
                        and prev["op"] == "xor"
                        and prev["ty"] == ty
                        and (
                            _is_neg_one(prev["rhs"], ty)
                            or _is_neg_one(prev["lhs"], ty)
                        )
                    ):
                        non_all_ones = (
                            prev["lhs"]
                            if not _is_neg_one(prev["lhs"], ty)
                            else prev["rhs"]
                        )
                        if non_all_ones == lhs:
                            replacements[result] = "0"
                            lines[idx] = ""
                            changed = True
                            continue

        match = _ZEXT_CONST_RE.match(stripped)
        if match is not None:
            replacements[match.group("result")] = (
                "1" if match.group("cond") == "true" else "0"
            )
            lines[idx] = ""
            changed = True
            continue

        match = _SEXT_CONST_RE.match(stripped)
        if match is not None:
            replacements[match.group("result")] = (
                "-1" if match.group("cond") == "true" else "0"
            )
            lines[idx] = ""
            changed = True
            continue

    if replacements:
        text = "".join(lines)
        for _ in range(8):
            next_text = text
            for name, rep in replacements.items():
                next_text = re.sub(r"%" + re.escape(name) + r"\b", rep, next_text)
            if next_text == text:
                break
            text = next_text
        lines = text.splitlines(keepends=True)

    if not changed:
        return fn_text, False

    rewritten = "".join(lines)
    cleaned, _ = dce_module_text(
        _function_chunk_module(module_context, rewritten), function_attrs,
    )
    for is_function, chunk in _split_functions(cleaned):
        if is_function:
            return chunk, True
    return rewritten, True


def instcombine_text(ir_text: str) -> tuple[str, bool]:
    """Apply local InstCombine-style peepholes; return (new_ir, changed)."""
    out: list[str] = []
    changed = False
    function_attrs = _collect_function_attrs(ir_text)
    for is_function, chunk in _split_functions(ir_text):
        if not is_function:
            out.append(chunk)
            continue
        current = chunk
        fn_changed = False
        for _ in range(8):
            current, local_changed = _rewrite_function(current, function_attrs=function_attrs)
            if not local_changed:
                break
            fn_changed = True
        out.append(current)
        changed = changed or fn_changed
    if not changed:
        return ir_text, False
    return "".join(out), True
