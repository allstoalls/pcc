"""An owned reference returned from a container METHOD CALL must be released.

`list.pop()`, `dict.get()`, `dict.setdefault()`, `dict.pop(k)`, `dict.popitem()`
and `set.pop()` all return a NEW owned reference from the runtime, but their
result type is `DynType`, so the AST ownership classifier
(`_expr_returns_owned_object`) answered "not owned" and no consumer ever
released the temp -- a leak that delays or prevents finalization. Confirmed via
a finalizer round-trip: exactly one `__del__` must fire when the last binding
is dropped; a leak yields 0.

Guard against the opposite error too: `dict.pop(k, default)` on a MISSING key
returns the BORROWED default (its miss edge does not incref), so it must NOT be
classified owned -- doing so would over-release the caller's default. That case
is exercised in a loop so an over-release surfaces as a crash or a >1 count.

Regression for PY-P1-OWNED-METHOD-CALL-RESULT-LEAK.
"""
from __future__ import annotations

import subprocess
import textwrap

import pytest


def _run(tmp_path, backend, body: str) -> list[str]:
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
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
        timeout=240,
        env={"PCC_GC_BACKEND": backend, "PATH": "/usr/bin:/bin"},
    )
    assert native.returncode == 0, (backend, native.stderr)
    return native.stdout.split()


# DELS accumulates across the whole program; every probe returns its OWN
# delta (finalizations it alone caused), so each must be exactly 1.  gc.collect()
# makes the moving collectors (GC3/GC4) finalize deterministically at the fence.
_ROUNDTRIP = """
DELS = [0]


class Canary:
    def __del__(self) -> None:
        DELS[0] = DELS[0] + 1


def list_pop_unused() -> int:
    before = DELS[0]
    box: list = []
    c = Canary()
    box.append(c)
    box.pop()
    c = None
    import gc
    gc.collect()
    return DELS[0] - before


def list_pop_assigned() -> int:
    before = DELS[0]
    box: list = []
    c = Canary()
    box.append(c)
    x = box.pop()
    c = None
    x = None
    import gc
    gc.collect()
    return DELS[0] - before


def dict_get() -> int:
    before = DELS[0]
    d: dict = {}
    c = Canary()
    d["k"] = c
    got = d.get("k", None)
    got = None
    del d["k"]
    c = None
    import gc
    gc.collect()
    return DELS[0] - before


def dict_pop1() -> int:
    before = DELS[0]
    d: dict = {}
    c = Canary()
    d["k"] = c
    x = d.pop("k")
    c = None
    x = None
    import gc
    gc.collect()
    return DELS[0] - before


def dict_setdefault() -> int:
    before = DELS[0]
    d: dict = {}
    c = Canary()
    x = d.setdefault("k", c)
    c = None
    x = None
    d.clear()
    import gc
    gc.collect()
    return DELS[0] - before


def set_pop() -> int:
    before = DELS[0]
    s: set = set()
    c = Canary()
    s.add(c)
    x = s.pop()
    c = None
    x = None
    import gc
    gc.collect()
    return DELS[0] - before


def dict_pop_default_missing() -> int:
    # MISS edge returns the BORROWED default -- must NOT be over-released.
    # Loop so an over-release crashes or drives the delta above 1.
    d: dict = {}
    fallback = Canary()
    i: int = 0
    total: int = 0
    while i < 200:
        got = d.pop("absent", fallback)
        if got is fallback:
            total = total + 1
        got = None
        i = i + 1
    before = DELS[0]
    keep = fallback
    fallback = None
    keep = None
    import gc
    gc.collect()
    return total * 1000 + (DELS[0] - before)


def main() -> None:
    print(list_pop_unused())
    print(list_pop_assigned())
    print(dict_get())
    print(dict_pop1())
    print(dict_setdefault())
    print(set_pop())
    print(dict_pop_default_missing())


main()
"""


@pytest.mark.parametrize("backend", ["0", "3", "4"])
def test_owned_method_call_results_are_released(tmp_path, backend):
    out = _run(tmp_path, backend, _ROUNDTRIP)
    assert len(out) == 7, (backend, out)
    assert out[0] == "1", ("list.pop unused", backend, out)
    assert out[1] == "1", ("list.pop assigned", backend, out)
    assert out[2] == "1", ("dict.get", backend, out)
    assert out[3] == "1", ("dict.pop(k)", backend, out)
    assert out[4] == "1", ("dict.setdefault", backend, out)
    assert out[5] == "1", ("set.pop", backend, out)
    # dict.pop(missing, default): all 200 returns are the borrowed default,
    # and exactly one __del__ fires when the single owner `keep` is dropped.
    # An over-release of the borrowed default would push the low digits > 1.
    assert out[6] == "200001", ("dict.pop default miss over-release", backend, out)
