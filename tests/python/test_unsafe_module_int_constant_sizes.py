"""``pcc.unsafe`` constant-size intrinsics accept a module-scope int literal.

``stack_alloc(SIZE)`` and the global-definition intrinsics need a compile-time
integer.  pcc-owned modules get theirs through native export tables; an
out-of-tree package (for example the standalone gateway) binds its sizes as
plain module constants.  A single module-scope ``NAME = <int literal>`` binding
folds; any rebinding, augmentation or ``global`` declaration fails closed.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from pcc.frontends.python.pipeline import compile_python
from pcc.frontends.python.pipeline_modes import PyPipelineError


def _compile(tmp_path: Path, name: str, source: str, run: bool) -> str:
    src = tmp_path / f"{name}.py"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    if not run:
        compile_python(str(src), str(tmp_path / f"{name}.ll"), emit_llvm_only=True,
                       libpython_mode="off", ir_scaffold_mode="on", backend="self")
        return ""
    exe = tmp_path / name
    compile_python(str(src), str(exe), libpython_mode="off", ir_scaffold_mode="on", backend="self")
    done = subprocess.run([str(exe)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_module_scope_int_literal_folds_into_stack_alloc(tmp_path: Path) -> None:
    out = _compile(
        tmp_path,
        "module_const_size",
        """
        from pcc.unsafe import load_i64, stack_alloc, store_i64

        _BUFFER_BYTES = 64
        _NEGATIVE_MARK = -8


        def fill() -> int:
            buf = stack_alloc(_BUFFER_BYTES)
            tail = stack_alloc(_BUFFER_BYTES // 2 + 1)
            store_i64(buf, 0, 7)
            store_i64(buf, 56, 9)
            store_i64(tail, 24, 1)
            return load_i64(buf, 0) + load_i64(buf, 56) + load_i64(tail, 24) + _NEGATIVE_MARK


        print(fill())
        """,
        run=True,
    )
    assert out == "9\n"


@pytest.mark.parametrize(
    "rebinding",
    [
        "_BUFFER_BYTES = 128\n",
        "_BUFFER_BYTES += 1\n",
        "def grow():\n    global _BUFFER_BYTES\n    _BUFFER_BYTES = 128\n",
        "def grow():\n    try:\n        pass\n    except Exception:\n        global _BUFFER_BYTES\n        _BUFFER_BYTES = 128\n",
    ],
)
def test_rebound_module_constant_fails_closed(tmp_path: Path, rebinding: str) -> None:
    """A rebound constant is never folded: the compile either fails with the
    constant-argument diagnostic or, when strict mode stubs the function, the
    stub carries no folded stack slot."""
    src = tmp_path / "rebound_const.py"
    src.write_text(
        "from pcc.unsafe import stack_alloc\n\n_BUFFER_BYTES = 64\n"
        + rebinding
        + "\n\ndef fill():\n    return stack_alloc(_BUFFER_BYTES)\n",
        encoding="utf-8",
    )
    ll = tmp_path / "rebound_const.ll"
    try:
        compile_python(str(src), str(ll), emit_llvm_only=True, libpython_mode="off",
                       ir_scaffold_mode="on", backend="self")
    except (PyPipelineError, NotImplementedError) as exc:
        assert "statically imported integer constants" in str(exc)
        return
    text = ll.read_text(encoding="utf-8")
    fill_index = text.index("_fill(")
    body = text[fill_index:text.index("\n}\n", fill_index)]
    assert "strict.nolib" in body
    assert "alloca [64 x i8]" not in body and "alloca [128 x i8]" not in body
