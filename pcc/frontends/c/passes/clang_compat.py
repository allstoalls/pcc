"""Clang-compatible IR quality passes.

Techniques learned from clang's CodeGen layer (CGExprScalar.cpp, CGCall.cpp,
CGDecl.cpp) and LLVM's Frontend/PerformanceTips.html:

  - TailCallPass:   mark eligible calls as 'tail call'
  - NoundefPass:     add 'noundef' to function parameters
  - BoolNarrowPass:  remove redundant zext i1→i64 + icmp ne i64,0 patterns
"""

from __future__ import annotations

import re

from pcc.frontends.c.passes.base import IRPass
from pcc.frontends.c.passes.context import PassContext


class TailCallPass(IRPass):
    """Mark eligible calls as 'tail call'.

    clang's CGCall.cpp: EmitCall marks calls as 'tail' when the result is
    immediately returned. LLVM's TailCallElim pass then converts tail calls
    to jumps, enabling tail recursion → loop transformation.

    Pattern:  %r = call TYPE @func(...)
              ret TYPE %r
    Becomes:  %r = tail call TYPE @func(...)
              ret TYPE %r
    """
    name = "tail-call"

    _SSA_VALUE_RE = re.compile(r'%(?:"[^"]+"|[-A-Za-z$._0-9]+)')
    _ALLOCA_RE = re.compile(
        r'^\s*(%(?:"[^"]+"|[-A-Za-z$._0-9]+))\s+=\s+alloca\b'
    )
    _PTR_DERIVE_RE = re.compile(
        r'^\s*(%(?:"[^"]+"|[-A-Za-z$._0-9]+))\s+=\s+'
        # `load ptr` is included so a pointer loaded out of an unsafe
        # (alloca-derived) slot is itself treated as unsafe. Without it, a
        # pointer into a local buffer that reaches a returned call via a
        # pointer-typed local (pre-mem2reg: `%p = load ptr, ptr %p.addr`)
        # would not be flagged, and the call would be marked `tail` — which
        # frees the caller frame (and the local buffer) before the callee
        # reads through the pointer. C99 return-of-call passing &local.
        r'(?:bitcast|getelementptr|phi|select|load\s+ptr)\b'
    )
    _CALL_ASSIGN_RE = re.compile(
        r'''
        ^
        (?P<retvar>%\S+)\s+=\s+
        (?P<call>
            call\s+
            \S+(?:\s+\w+)*
            \s+
            (?:\([^@]*\)\s+)?
            @.+ 
        )
        $
        ''',
        re.VERBOSE,
    )

    def _function_block_ranges(self, lines: list[str]) -> list[tuple[int, int]]:
        ranges = []
        start = None
        pending_define = None
        for index, line in enumerate(lines):
            stripped = line.strip()
            if line.startswith("define "):
                pending_define = index
                if line.rstrip().endswith("{"):
                    start = index
                    pending_define = None
                continue
            if pending_define is not None and stripped == "{":
                start = pending_define
                pending_define = None
                continue
            if start is not None and stripped == "}":
                ranges.append((start, index))
                start = None
        return ranges

    def _unsafe_pointer_values(self, func_lines: list[str]) -> set[str]:
        unsafe = set()
        for line in func_lines:
            match = self._ALLOCA_RE.match(line)
            if match:
                unsafe.add(match.group(1))

        changed = True
        while changed:
            changed = False
            for line in func_lines:
                match = self._PTR_DERIVE_RE.match(line)
                if not match:
                    continue
                dest = match.group(1)
                if dest in unsafe:
                    continue
                if any(token in unsafe for token in self._SSA_VALUE_RE.findall(line)):
                    unsafe.add(dest)
                    changed = True
        return unsafe

    def run(self, ir_text: str, ctx: PassContext) -> str:
        lines = ir_text.split('\n')
        count = 0

        for start, end in self._function_block_ranges(lines):
            unsafe_ptrs = self._unsafe_pointer_values(lines[start:end])
            for i in range(start, end):
                line = lines[i].strip()
                next_line = lines[i + 1].strip() if i + 1 < len(lines) else ""

                if 'tail call' in line:
                    continue

                m = self._CALL_ASSIGN_RE.match(line)
                if not m:
                    continue

                ret_var = m.group("retvar")
                call_part = m.group("call")

                ret_m = re.match(
                    r'ret\s+\S+\s+(' + re.escape(ret_var) + r')\s*$',
                    next_line,
                )
                if not ret_m:
                    continue

                if any(token in unsafe_ptrs for token in self._SSA_VALUE_RE.findall(call_part)):
                    continue

                indent = lines[i][:len(lines[i]) - len(lines[i].lstrip())]
                lines[i] = f"{indent}{ret_var} = tail {call_part}"
                count += 1

        if count:
            ctx.bump("tail_call.annotated", count)
            ctx.record(self.name, "annotated", f"{count} calls marked tail")

        return '\n'.join(lines)


class NoundefPass(IRPass):
    """Add 'noundef' attribute to function parameters.

    clang's CGCall.cpp: ConstructAttributeList adds noundef to all
    parameters by default. This tells LLVM the value is never poison/undef.

    Pattern:  define i32 @func(i32 %x, i32 %y)
    Becomes:  define i32 @func(i32 noundef %x, i32 noundef %y)
    """
    name = "noundef"

    # Match parameter in function definition: TYPE %"name"
    _PARAM_RE = re.compile(
        r'(define\s+\S+\s+@\S+\s*\()([^)]*)\)',
    )

    def run(self, ir_text: str, ctx: PassContext) -> str:
        count = 0

        def add_noundef(m):
            nonlocal count
            prefix = m.group(1)
            params = m.group(2)
            if not params.strip():
                return m.group(0)

            new_params = []
            for param in params.split(','):
                param = param.strip()
                if not param or param == '...':
                    new_params.append(param)
                    continue
                if 'noundef' in param:
                    new_params.append(param)
                    continue
                # Pattern: TYPE %name  →  TYPE noundef %name
                pm = re.match(r'(\S+(?:\s*\*)*)\s+(%\S+)', param)
                if pm:
                    count += 1
                    new_params.append(f"{pm.group(1)} noundef {pm.group(2)}")
                else:
                    new_params.append(param)

            return prefix + ', '.join(new_params) + ')'

        ir_text = self._PARAM_RE.sub(add_noundef, ir_text)

        if count:
            ctx.bump("noundef.annotated", count)
            ctx.record(self.name, "annotated", f"{count} params")
        return ir_text
