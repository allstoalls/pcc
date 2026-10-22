"""An owned container-method-call result must be released after consumption.

`py_list_pop` returns by TRANSFER (the list's reference moves to the caller);
`py_dict_get` / `py_dict_get_default` return a NEW reference.  The assignment
and discard consumers release such temps only when
`_expr_returns_owned_object` classifies the expression as owned — and typed
container method calls were missing from that classifier, so the store's own
incref plus the never-released temp leaked one reference per call:
the object's `__del__` never fired.

Repros mirror docs/goal/evidence/2026-08-27-owned-method-call-result-leak.md:
subscript get, append/clear/delitem are balanced; only the method-call
results leak.  Task: PY-P1-OWNED-METHOD-CALL-RESULT-LEAK.
"""
from __future__ import annotations

import subprocess
import textwrap

import pytest


_PROGRAM = """
DELS = [0]


class Canary:
    def __init__(self) -> None:
        self.alive = 1

    def __del__(self) -> None:
        DELS[0] = DELS[0] + 1


def case_pop_discard() -> int:
    box: list = []
    box.append(Canary())
    before = DELS[0]
    box.pop()
    import gc
    gc.collect()
    return DELS[0] - before


def case_pop_rebind() -> int:
    box: list = []
    box.append(Canary())
    before = DELS[0]
    x = box.pop()
    x = None
    import gc
    gc.collect()
    return DELS[0] - before


def case_dict_get_rebind() -> int:
    d: dict = {}
    d["k"] = Canary()
    before = DELS[0]
    got = d.get("k")
    got = None
    del d["k"]
    import gc
    gc.collect()
    return DELS[0] - before


def case_subscript_control() -> int:
    box: list = []
    box.append(Canary())
    before = DELS[0]
    got = box[0]
    got = None
    box.clear()
    import gc
    gc.collect()
    return DELS[0] - before


def main() -> None:
    print(case_pop_discard())
    print(case_pop_rebind())
    print(case_dict_get_rebind())
    print(case_subscript_control())


main()
"""


@pytest.mark.parametrize("backend", ["0", "3"])
def test_owned_method_results_are_released(tmp_path, backend, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python

    monkeypatch.setenv("PCC_GC_BACKEND", backend)
    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(textwrap.dedent(_PROGRAM).lstrip(), encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )
    native = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=120,
        env={"PCC_GC_BACKEND": backend, "PATH": "/usr/bin:/bin"},
    )
    assert native.returncode == 0, (backend, native.stderr)
    # Exactly one finalizer per case: a leaked retain reports 0, an
    # over-release fires early (or crashes under the moving collectors).
    assert native.stdout.split() == ["1", "1", "1", "1"], (
        backend, native.stdout,
    )
