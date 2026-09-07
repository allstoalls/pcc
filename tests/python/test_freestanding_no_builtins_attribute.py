"""A freestanding module must not have libc calls synthesized inside it.

A freestanding module or runtime port *is* the libc implementation: it defines
``memset``, ``memcpy``, ``bzero`` and friends.  Any conforming optimizer is
entitled to recognize the byte-fill loop inside ``@memset`` and rewrite it into
a call to ``memset`` -- itself.  A real compiler prevents that with
``-ffreestanding``/``-fno-builtin``; the IR spelling is the ``"no-builtins"``
function attribute.

pcc emitted no function attributes at all, which is why nothing caught it: the
owned pass tier does not perform that transform, so the normal path never
exercised the hazard.  LLVM's ``default<O2>`` does.  Applied to all 170 runtime
archive members it produced a runtime whose every program hung at startup,
spinning inside ``bzero``, with no walkable stack because O2 had dropped the
frame pointers.  The defect was in pcc's emission, not in the optimizer that
found it, and it would bite the first owned pass that learns to recognize a
memset shape.

llvmlite appears here only as the optimizer that demonstrates the hazard.
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

from pcc.py_frontend.pipeline import compile_python


_APPLICATION_SHAPE = """
    def fill(size: int) -> int:
        total = 0
        i = 0
        while i < size:
            total += i
            i += 1
        return total


    print(fill(4))
    """


REPO_ROOT = Path(__file__).resolve().parents[2]
# The real production module rather than a hand-written fixture: it is the
# module the failure was found in, and a freestanding fixture has its own
# authoring rules (every function needs @c_abi_export, integer comparisons
# need pcc.i64) that would only test the fixture.
MEM_STR_SRC = REPO_ROOT / "pcc" / "py_runtime" / "py" / "freestanding_mem_str.py"


def _emit_path(tmp_path: Path, src: Path, name: str) -> str:
    out = tmp_path / (name + ".ll")
    compile_python(
        str(src),
        str(out),
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
        emit_llvm_only=True,
        python_library=True,
    )
    return out.read_text(encoding="utf-8")


def _emit(tmp_path: Path, source: str, name: str) -> str:
    src = tmp_path / (name + ".py")
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    out = tmp_path / (name + ".ll")
    compile_python(
        str(src),
        str(out),
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
        emit_llvm_only=True,
        python_library=True,
    )
    return out.read_text(encoding="utf-8")


def _defined_functions(ir_text: str) -> dict:
    out = {}
    for line in ir_text.splitlines():
        if not line.startswith("define "):
            continue
        matched = re.search(r"@([A-Za-z_][A-Za-z0-9_.$]*)\s*\(", line)
        if matched is not None:
            out[matched.group(1)] = line
    return out


def test_a_freestanding_module_marks_its_definitions_no_builtins(tmp_path: Path) -> None:
    ir_text = _emit_path(tmp_path, MEM_STR_SRC, "fs_mem")
    defined = _defined_functions(ir_text)
    assert "memset" in defined and "bzero" in defined, sorted(defined)
    for name, line in defined.items():
        assert '"no-builtins"' in line, (name, line)


def test_an_ordinary_module_is_left_alone(tmp_path: Path) -> None:
    """The marker is a freestanding-runtime contract, not a global default.

    Marking ordinary application code would forbid the optimizer from using a
    real libc ``memset`` where that is exactly what is wanted.
    """
    ir_text = _emit(tmp_path, _APPLICATION_SHAPE, "app_mod")
    assert '"no-builtins"' not in ir_text


def test_declarations_do_not_carry_the_attribute(tmp_path: Path) -> None:
    ir_text = _emit_path(tmp_path, MEM_STR_SRC, "fs_mem_decl")
    for line in ir_text.splitlines():
        if line.startswith("declare "):
            assert '"no-builtins"' not in line, line


def test_llvm_o2_no_longer_rewrites_the_definition_into_a_call_to_itself(
    tmp_path: Path,
) -> None:
    """The behavioural half: run the optimizer that produced the hang.

    Without the attribute LLVM turned ``@bzero``'s delegation into
    ``llvm.memset.p0.i64``, which lowers to a call to ``memset`` -- and in a
    freestanding link ``memset`` is the very function being optimized.
    """
    import llvmlite.binding as llvm

    from pcc.llvm_capi import binding as capi

    ir_text = _emit_path(tmp_path, MEM_STR_SRC, "fs_mem_o2")
    llvm.parse_assembly(ir_text).verify()
    optimized = capi.run_passes_on_ir(ir_text, "default<O2>")
    llvm.parse_assembly(optimized).verify()

    defined_names = set(_defined_functions(optimized))
    assert "memset" in defined_names, sorted(defined_names)
    for name in defined_names:
        body = re.search(
            r"define[^\n]*@" + re.escape(name) + r"\(.*?\n\}", optimized, re.S
        )
        assert body is not None, name
        called = set(re.findall(r"call [^\n]*@([\w.]+)", body.group(0)))
        # Neither a direct self-call nor the memset intrinsic, which lowers to
        # one.  A pure intrinsic such as llvm.smin is fine.
        assert name not in called, (name, sorted(called))
        assert not any(
            c.startswith("llvm.memset") or c.startswith("llvm.memcpy")
            for c in called
        ), (name, sorted(called))
