from __future__ import annotations

from dataclasses import dataclass
import json
import re


_NAME = r"[A-Za-z_.$][A-Za-z_0-9.$-]*"
_INT = r"i(?:1|8|16|32|64)"
_VALUE = rf"(?:%{_NAME}|-?[0-9]+|true|false)"
_FUNC_RE = re.compile(
    rf"^(?P<header>define[^\n]*?@(?P<name>{_NAME})\([^\n]*\)[^\n]*\{{)[ \t]*\n"
    r"(?P<body>.*?)^\}", re.M | re.S,
)
_HEADER_RE = re.compile(
    rf"(?P<prefix>define\s+(?:(?:internal|private|dso_local)\s+)*)"
    rf"(?P<ret>void|{_INT})\s+@(?P<name>{_NAME})\((?P<args>[^()]*)\)\s*\{{"
)
_CALL_RE = re.compile(
    rf"(?:(?P<result>%{_NAME})\s*=\s*)?(?:tail\s+)?call\s+"
    rf"(?P<ret>void|{_INT})\s+@(?P<name>{_NAME})\((?P<args>[^()]*)\)"
)
_ARG_RE = re.compile(rf"(?P<type>{_INT})\s+(?P<value>{_VALUE})")
_LABEL_RE = re.compile(rf"(?P<name>{_NAME}):")
_BRANCH_RE = re.compile(
    rf"(?:br label %{_NAME}|br i1 {_VALUE}, label %{_NAME}, label %{_NAME})"
)
# An allowlist is deliberate: no frame, roots, local storage, pointer values,
# unknown calls, exception edges, metadata or implicit ABI state can slip in.
_SCALAR_RE = re.compile(
    rf"%{_NAME}\s*=\s*(?:"
    rf"(?:add|sub|mul|and|or|xor|shl|lshr|ashr) {_INT} {_VALUE}, {_VALUE}|"
    rf"icmp (?:eq|ne|ugt|uge|ult|ule|sgt|sge|slt|sle) {_INT} {_VALUE}, {_VALUE}|"
    rf"select i1 {_VALUE}, (?P<select_type>{_INT}) {_VALUE}, (?P=select_type) {_VALUE}|"
    rf"(?:trunc|zext|sext) {_INT} {_VALUE} to {_INT})"
)
_STORE_RE = re.compile(
    rf"store (?P<type>{_INT}) {_VALUE}, (?:ptr|(?P=type)\*) @{_NAME}"
    r"(?:, align [0-9]+)?"
)


@dataclass(frozen=True)
class TailcallCandidate:
    function: str
    rewritten: bool
    reason: str

    def to_json(self) -> dict[str, object]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class TailcallRewriteResult:
    ir_text: str
    candidates: tuple[TailcallCandidate, ...]

    @property
    def rewritten(self) -> bool:
        return any(c.rewritten for c in self.candidates)

    def report_json(self) -> str:
        return json.dumps({
            "schema": "pcc.tailcall.rewrite.v1",
            "candidates": [c.to_json() for c in self.candidates],
        }, indent=2, sort_keys=True)


def _code(line: str) -> str:
    return line.split(";", 1)[0].strip()


def _has_self_call(body: str, name: str) -> bool:
    return any(re.search(rf"\bcall\b[^\n]*@{re.escape(name)}\(", _code(line))
               for line in body.splitlines())


def analyze_self_tailcalls(ir_text: str) -> list[TailcallCandidate]:
    out: list[TailcallCandidate] = []
    for match in _FUNC_RE.finditer(ir_text):
        name = match.group("name")
        lines = [_code(line) for line in match.group("body").splitlines()]
        lines = [line for line in lines if line]
        for call, ret in zip(lines, lines[1:]):
            if (re.search(rf"\bcall\b[^\n]*@{re.escape(name)}\(", call)
                    and ret.startswith("ret ")):
                out.append(TailcallCandidate(name, False, "self tail call detected"))
                break
    return out


def _typed_args(text: str) -> list[tuple[str, str]] | None:
    args: list[tuple[str, str]] = []
    if not text.strip():
        return args
    for item in text.split(","):
        match = _ARG_RE.fullmatch(item.strip())
        if match is None:
            return None
        args.append((match.group("type"), match.group("value")))
    return args


def _rewrite_function(header: str, body: str, *, value_returns: bool,
                      marker: str) -> tuple[str | None, str]:
    signature = _HEADER_RE.fullmatch(header)
    if signature is None:
        return None, "unsupported signature, attributes or ABI"
    ret_type = signature.group("ret")
    if not value_returns and ret_type != "void":
        return None, "self tail call needs value phi-loop rewrite"
    if value_returns and ret_type == "void":
        return None, "accumulator helper requires an integer result"
    args = _typed_args(signature.group("args"))
    if args is None or any(not value.startswith("%") for _, value in args):
        return None, "requires named integer parameters without pointer/root or ABI state"
    name = signature.group("name")
    lines = body.splitlines(keepends=True)
    # Retain original text; indices let the rewrite remove exactly the matched
    # call and return, without a regex spanning intervening side effects.
    blocks: list[tuple[str, int, list[tuple[int, str]]]] = []
    for index, line in enumerate(lines):
        code = _code(line)
        if not code:
            continue
        label = _LABEL_RE.fullmatch(code)
        if label is not None:
            blocks.append((label.group("name"), index, []))
        elif not blocks:
            return None, "requires an explicit named entry block"
        else:
            blocks[-1][2].append((index, code))
    if not blocks or len({label for label, _, _ in blocks}) != len(blocks):
        return None, "requires distinct named blocks"
    entry = blocks[0][0]
    labels = {label for label, _, _ in blocks}
    sites: list[tuple[str, int, int, list[tuple[str, str]]]] = []
    for label, _, instructions in blocks:
        if not instructions:
            return None, "block has no terminator"
        tail_start = len(instructions)
        if len(instructions) >= 2:
            call = _CALL_RE.fullmatch(instructions[-2][1])
            if call is not None and call.group("name") == name:
                expected_ret = ("ret void" if ret_type == "void"
                                else f"ret {ret_type} {call.group('result')}")
                actual_args = _typed_args(call.group("args"))
                if (call.group("ret") != ret_type
                        or (ret_type == "void") != (call.group("result") is None)
                        or instructions[-1][1] != expected_ret
                        or actual_args is None
                        or [ty for ty, _ in actual_args] != [ty for ty, _ in args]):
                    return None, "tail call must match parameter and return types exactly"
                sites.append((label, instructions[-2][0], instructions[-1][0], actual_args))
                tail_start -= 2
        for position, (_, code) in enumerate(instructions[:tail_start]):
            if _SCALAR_RE.fullmatch(code) or _STORE_RE.fullmatch(code):
                if position == len(instructions) - 1:
                    return None, "block has no terminator"
                continue
            is_return = (code == "ret void" if ret_type == "void" else
                         re.fullmatch(rf"ret {ret_type} {_VALUE}", code) is not None)
            if position != len(instructions) - 1:
                return None, "unsupported instruction or non-tail call; frame/root/exception state is not proven empty"
            if not is_return and _BRANCH_RE.fullmatch(code) is None:
                return None, "unsupported terminator or exception control flow"
            targets = re.findall(rf"label %({_NAME})", code)
            if any(target not in labels or target == entry for target in targets):
                return None, "requires a valid CFG with no original entry predecessors"
    if not sites:
        return None, "no immediate matching self call and return"

    used = set(re.findall(rf"%({_NAME})", header + body)) | labels

    def fresh(base: str) -> str:
        result = base
        suffix = 0
        while result in used:
            suffix += 1
            result = f"{base}.{suffix}"
        used.add(result)
        return result

    preheader = fresh("pcc.tailcall.preheader")
    incoming_args = [fresh(f"pcc.tailcall.arg.{index}") for index in range(len(args))]
    parameters = ", ".join(f"{ty} %{incoming}" for (ty, _), incoming in zip(args, incoming_args))
    new_header = f"{signature.group('prefix')}{ret_type} @{name}({parameters}) {{\n"
    phis = []
    for index, ((ty, value), incoming) in enumerate(zip(args, incoming_args)):
        edges = [f"[ %{incoming}, %{preheader} ]"]
        edges.extend(f"[ {actual[index][1]}, %{label} ]" for label, _, _, actual in sites)
        phis.append(f"  {value} = phi {ty} {', '.join(edges)}\n")
    replacements = {call: f"  ; {marker}\n  br label %{entry}\n" for _, call, _, _ in sites}
    removed = {ret for _, _, ret, _ in sites}
    output = [new_header, f"{preheader}:\n  br label %{entry}\n"]
    for index, line in enumerate(lines):
        if index not in removed:
            output.append(replacements.get(index, line))
        if index == blocks[0][1]:
            output.extend(phis)
    output.append("}")
    return "".join(output), f"rewrote {len(sites)} self-tail-call site(s) with parameter PHIs"


def _rewrite_self_tailcalls(ir_text: str, *, value_returns: bool,
                            marker: str) -> TailcallRewriteResult:
    candidates: list[TailcallCandidate] = []

    def rewrite(match: re.Match[str]) -> str:
        name = match.group("name")
        body = match.group("body")
        if not _has_self_call(body, name):
            return match.group(0)
        rewritten, reason = _rewrite_function(
            match.group("header"), body, value_returns=value_returns, marker=marker,
        )
        candidates.append(TailcallCandidate(name, rewritten is not None, reason))
        return match.group(0) if rewritten is None else rewritten

    return TailcallRewriteResult(_FUNC_RE.sub(rewrite, ir_text), tuple(candidates))


def rewrite_simple_void_self_tailcalls(ir_text: str) -> TailcallRewriteResult:
    """Loopify direct void recursion only when per-call state is proven empty.

    The bounded subset has named integer parameters, integer computations,
    direct integer global stores and branch/return control flow. Each accepted
    call must immediately return. A new entry preheader supplies the initial
    arguments; PHIs in the original entry carry all recursive arguments in
    parallel. Existing PHIs, local storage, pointer values, other calls,
    exception handling and ABI/function attributes are deliberately refused.
    Value-returning recursion remains a separate, unwired helper.
    """
    return _rewrite_self_tailcalls(ir_text, value_returns=False, marker="pcc.tailcall.self")


def format_tailcall_report(ir_text: str) -> str:
    return json.dumps({
        "schema": "pcc.tailcall.v1",
        "candidates": [c.to_json() for c in analyze_self_tailcalls(ir_text)],
    }, indent=2, sort_keys=True)
