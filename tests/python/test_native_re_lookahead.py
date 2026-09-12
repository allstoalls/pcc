"""The native regex engine supports lookahead assertions.

``(?=...)`` and ``(?!...)`` were rejected by both the engine's parser and the
frontend's static subset checker, so ``re.compile(r"...(?!\\s*\\()...")`` fell
back to CPython.  Two of those live in ``pcc/passes/ir_metadata.py`` as
class-body constants, which run during module initialization -- and module
initialization is outside the strict no-libpython stub projection, so the
whole self-host compile failed with "module pcc.passes.ir_metadata generated
IR still calls py_cpy_* helpers".

The assertion compiles to its own sub-program terminated by a dedicated
success opcode: reusing the ordinary match terminator would publish the
sub-match's end position as the enclosing match's end.  A capturing group
inside a lookahead is refused rather than guessed at -- it would have to
survive a failed negative assertion.

CPython's ``re`` is the oracle.
"""

from __future__ import annotations

import subprocess

import pytest


_SOURCE = r'''
import re

_LOAD_RE = re.compile(
    r"^(\s+%\S+\s+=\s+load\s+)"
    r"(i8|i16|i32|i64|float|double)(?!\s*\()"
    r"(,\s+\S+\s+%\S+)((?:\s*,.*)?)$",
    re.MULTILINE,
)


def main() -> None:
    print(re.sub(r"a(?!b)", "X", "ab ac ad"))
    print(re.sub(r"a(?=b)", "X", "ab ac ad"))
    print(re.findall(r"\w+(?=,)", "one,two three,four"))
    print(re.findall(r"\w+(?!,)", "ab,cd"))
    m = re.search(r"foo(?=bar)", "foobar")
    if m is None:
        print("none")
    else:
        print(m.group(0))
    print(re.search(r"foo(?=bar)", "foobaz") is None)
    print(re.search(r"foo(?!bar)", "foobaz") is not None)
    print(re.findall(r"x(?=y)(?!yz)", "xy xyz"))
    ir = "  %v = load i32, ptr %p, align 4\n  %w = load i32(ptr) @f\n"
    print(len(_LOAD_RE.findall(ir)))
    print(_LOAD_RE.search(ir) is not None)


main()
'''


def test_lookahead_matches_cpython_without_libpython(tmp_path):
    from pcc.py_frontend.pipeline import compile_python

    src = tmp_path / "re_lookahead.py"
    exe = tmp_path / "re_lookahead.out"
    src.write_text(_SOURCE, encoding="utf-8")
    # libpython off: the patterns must go through the native engine, not a
    # CPython fallback that would make this test vacuous.
    compile_python(str(src), str(exe), libpython_mode="off", backend="self")
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=180, check=True
    )
    reference = subprocess.run(
        ["python3", str(src)], text=True, capture_output=True, timeout=180, check=True
    )
    assert run.stdout == reference.stdout
    assert run.stdout.splitlines() == [
        "ab Xc Xd",
        "Xb ac ad",
        "['one', 'three']",
        "['a', 'cd']",
        "foo",
        "True",
        "True",
        "['x']",
        "1",
        "True",
    ]


def test_ir_metadata_module_init_needs_no_libpython():
    """The module whose class-body regexes forced the fallback."""
    import os
    import re as host_re
    from pathlib import Path

    from pcc.parse.py_lift import parse_and_lift
    from pcc.py_frontend import type_infer
    from pcc.py_frontend.codegen.layer1 import L1CodeGen

    path = Path(__file__).resolve().parents[2] / "pcc/passes/ir_metadata.py"
    typed = type_infer.infer_module(
        parse_and_lift(
            path.read_text(encoding="utf-8"), str(path), "pcc.passes.ir_metadata"
        )
    )
    codegen = L1CodeGen(typed, False, "on")
    codegen._strict_no_libpython = True
    codegen._prefer_native_callable_values = True
    codegen._module_source_path = str(path)
    codegen._target_triple = ""
    ir_text = str(codegen.generate(typed))
    assert host_re.search(r"\bcall [^\n]*@py_cpy_", ir_text) is None
    assert "@py_re_compile_obj" in ir_text
    assert os.path.basename(str(path)) == "ir_metadata.py"


@pytest.mark.parametrize(
    "pattern,supported",
    [
        (r"a(?!b)c", True),
        (r"a(?=b)c", True),
        (r"a(?=x(?!y))z", True),
        # A capture inside a lookahead must be refused, in the checker and in
        # the engine, so the two stay in step.
        (r"a(?=(b))c", False),
        (r"a(?=b", False),
    ],
)
def test_frontend_checker_agrees_on_lookahead(pattern, supported):
    from pcc.parse.py_lift import parse_and_lift
    from pcc.py_frontend import type_infer
    from pcc.py_frontend.codegen.layer1 import L1CodeGen

    typed = type_infer.infer_module(parse_and_lift("x = 1\n", "<probe>", "probe"))
    codegen = L1CodeGen(typed, False, "on")
    assert codegen._re_engine_subset_supported(pattern) is supported
