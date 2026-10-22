"""Object-valued conditional expressions must publish one owned result.

The selected operand can be a borrowed load from an owned local.  The IfExpr
join must retain that branch before the source local is rebound; otherwise the
target local points at a freed object.  The opposite error is a double retain
or release, caught by the exact finalizer count.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import textwrap

import pytest

from pcc1_gate import find_current_pcc1, repo_root, skip_or_fail_no_current_pcc1
from pcc.frontends.python.pipeline import compile_python


_REPO_ROOT = repo_root()


_SOURCE = textwrap.dedent(
    """
    DELS: list[int] = [0]

    class Canary:
        def __init__(self, tag: int) -> None:
            self.tag = tag

        def __del__(self) -> None:
            DELS[0] = DELS[0] + 1

    def choose(flag: bool) -> int:
        current = Canary(7)
        selected = current if flag else current
        current = Canary(9)
        got = selected.tag
        return got

    def main() -> None:
        print(choose(True), DELS[0])
        print(choose(False), DELS[0])

    main()
    """
).lstrip()


def _function_body(ir_text: str, function_name: str) -> str:
    marker = "@user_owned_ifexpr_" + function_name + "("
    start = ir_text.index(marker)
    define_start = ir_text.rfind("define ", 0, start)
    end = ir_text.index("\n}", start)
    return ir_text[define_start : end + 2]


def test_owned_ifexpr_ir_normalizes_branches_and_tracks_target(
    tmp_path: Path,
) -> None:
    source = tmp_path / "owned_ifexpr.py"
    output = tmp_path / "owned_ifexpr.ll"
    source.write_text(_SOURCE, encoding="utf-8")
    compile_python(
        str(source),
        str(output),
        emit_llvm_only=True,
        libpython_mode="off",
        ir_scaffold_mode="on",
    )

    body = _function_body(output.read_text(encoding="utf-8"), "choose")
    assert "ternary.then.retain" in body
    assert "ternary.else.retain" in body
    assert re.search(
        r"phi ptr \[ %ternary\.then\.retain\.[^,]+, %ternary_true\.[^]]+\], "
        r"\[ %ternary\.else\.retain\.[^,]+, %ternary_false\.[^]]+\]",
        body,
    )
    assert re.search(
        r"br i1 true, label %selected\.owned\.release\.",
        body,
    )
    # The selected slot is passed directly, or through an identity alias.
    assert re.search(r"bitcast ptr %selected\.addr\.\d+ to ptr|\(ptr %selected\.addr\.\d+[,)]", body)
    assert "pcc_gc_frame_enter" in body
    assert "selected.owned.release" in body


@pytest.mark.parametrize("gc_backend", ["0", "3", "4"])
def test_owned_ifexpr_local_survives_source_rebind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gc_backend: str,
) -> None:
    monkeypatch.setenv("PCC_GC_BACKEND", gc_backend)
    source = tmp_path / "owned_ifexpr.py"
    binary = tmp_path / "owned_ifexpr"
    source.write_text(_SOURCE, encoding="utf-8")
    compile_python(
        str(source),
        str(binary),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )

    env = os.environ.copy()
    env.pop("LC_ALL", None)
    run = subprocess.run(
        [str(binary)],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["7 2", "7 4"]


@pytest.mark.parametrize("gc_backend", ["0", "3", "4"])
def test_current_pcc1_owned_ifexpr_local_survives_source_rebind(
    tmp_path: Path,
    pcc_runtime_archive: Path,
    gc_backend: str,
) -> None:
    pcc1 = find_current_pcc1(_REPO_ROOT)
    if pcc1 is None:
        skip_or_fail_no_current_pcc1(
            "no current pcc1 binary for the owned IfExpr transfer gate"
        )

    source = tmp_path / "owned_ifexpr_pcc1.py"
    binary = tmp_path / "owned_ifexpr_pcc1"
    source.write_text(_SOURCE, encoding="utf-8")
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_RUNTIME_ARCHIVE"] = str(pcc_runtime_archive)
    env["PCC_GC_BACKEND"] = gc_backend
    compile_run = subprocess.run(
        [
            str(pcc1),
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(source),
            "-o",
            str(binary),
        ],
        cwd=_REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=300,
    )
    assert compile_run.returncode == 0, compile_run.stdout + compile_run.stderr

    native = subprocess.run(
        [str(binary)],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert native.returncode == 0, native.stderr
    assert native.stdout.splitlines() == ["7 2", "7 4"]
