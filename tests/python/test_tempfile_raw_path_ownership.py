"""Raw tempfile buffers retain their explicit ABI across conditional calls.

These checks emit and verify single-module IR only. Actual native provenance
is covered separately by test_open_errno_native_five_gc with probe mode 2.
"""

import ast
from pathlib import Path
import re

import pytest

from pcc.frontends.python.owned_runtime_build import (
    _compile_runtime_module,
    runtime_ir_passes,
)
from pcc.frontends.python.pipeline import compile_python
from pcc.ir.optimization.driver import optimize_ir
from tests.owned_ir_validation import verify_ir_text


ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "pcc/runtime"
SOURCE = RUNTIME / "py/py_tempfile.py"
RAW_PATH_HELPERS = (
    "_td_path_template", "_td_windows_path_template", "_td_mkdir_at",
)


def _body(text, name):
    match = re.search(
        r"^define[^\n]*@" + re.escape(name) + r"\([^\n]*\).*?^}",
        text, re.M | re.S,
    )
    assert match, name
    return match.group(0)


def test_raw_path_helper_return_contract_is_explicit():
    functions = {
        node.name: node for node in ast.parse(SOURCE.read_text()).body
        if isinstance(node, ast.FunctionDef)
    }
    for name in RAW_PATH_HELPERS:
        annotation = functions[name].returns
        assert isinstance(annotation, ast.Name) and annotation.id == "c_ptr", name


@pytest.mark.parametrize("target", [
    "arm64-apple-darwin", "aarch64-unknown-linux-gnu", "x86_64-pc-windows-msvc",
], ids=["darwin", "linux", "windows"])
def test_actual_tempfile_path_join_has_no_managed_ownership(tmp_path, monkeypatch, target):
    monkeypatch.setenv("PCC_WITH_THREADS", "1")
    monkeypatch.setenv("PCC_REFCOUNT_KIND", "atomic")
    output = tmp_path / "py_tempfile.ll"
    _compile_runtime_module("py_tempfile", str(SOURCE), str(output), target)
    raw = output.read_text()
    optimized = optimize_ir(raw, runtime_ir_passes(str(RUNTIME)))
    for text in (raw, optimized):
        verify_ir_text(text)
        mkdir = _body(text, "user_py_tempfile__td_mkdir_at")
        assert "@user_py_tempfile__td_path_template(" in mkdir
        assert "@user_py_tempfile__td_windows_path_template(" in mkdir
        assert "phi ptr" in mkdir
        # A malloc result must not be retained, rooted, resolved, barriered,
        # or released as a managed object on either branch or error path.
        assert not re.search(r"\bcall[^\n]*@pcc_gc_", mkdir), mkdir
        template = _body(text, "user_py_tempfile__td_path_template")
        assert "@malloc(" in template
        assert not re.search(r"\bcall[^\n]*@pcc_gc_", template), template
        # Windows path normalization still owns its real managed strings and
        # tuple explicitly; a raw return annotation must not erase this work.
        windows = _body(text, "user_py_tempfile__td_windows_path_template")
        for call in (
            "@malloc(", "@free(", "@pcc_gc_frame_enter(",
            "@user_py_tempfile__td_hold(", "@user_py_tempfile__td_finish(",
        ):
            assert call in windows, call
        assert "@pcc_gc_retain(" not in windows


def test_explicit_raw_call_join_keeps_managed_join_control(tmp_path):
    source = tmp_path / "join_control.py"
    source.write_text('''from pcc.extern import c_ptr
from pcc.unsafe import malloc, free
__pcc_runtime_port__ = True
def first() -> c_ptr:
    return malloc(8)
def second() -> c_ptr:
    return malloc(16)
def raw_join(flag: bool) -> c_ptr:
    result = first() if flag else second()
    return result
def raw_consumer(flag: bool) -> None:
    result = raw_join(flag)
    free(result)
def managed_join(first: str, second: str, flag: bool) -> str:
    return first if flag else second
''')
    output = source.with_suffix(".ll")
    compile_python(
        str(source), str(output), emit_llvm_only=True, python_library=True,
        libpython_mode="off", backend="self", target_triple="arm64-apple-darwin",
    )
    text = output.read_text()
    verify_ir_text(text)
    for name in ("first", "second", "raw_join", "raw_consumer"):
        body = _body(text, "user_join_control_" + name)
        assert not re.search(r"\bcall[^\n]*@pcc_gc_(?!safepoint)", body), body
    assert "@free(" in _body(text, "user_join_control_raw_consumer")
    managed = _body(text, "user_join_control_managed_join")
    assert "ternary.then.retain" in managed
    assert "ternary.else.retain" in managed
