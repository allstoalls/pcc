"""`self.foo(...)` on an overridden method resolves once, instead of six times.

A method that any subclass overrides cannot be called directly, so it took the
general attribute protocol: an MRO walk for `__getattribute__`, one for
`__getattr__`, a field-index scan and one for the name itself; then a
bound-method object holding a two-element captures tuple; then the caller's
argument tuple; then a *second* tuple inside `_instance_bound_method_entry` to
prepend the receiver.  Four allocations and four MRO-shaped scans per call, all
refcounted down again afterwards.  Profiling pcc1 compiling a module put about
40% of self time in that protocol and another 13% in the refcount and
deallocation traffic it creates -- a large part of why pcc1 is slower than
CPython running the same compiler source.

Only the receiver's runtime class is genuinely dynamic.
`PCC_DIRECT_VIRTUAL_METHOD_CALLS=1` keeps that one MRO lookup, hands the
runtime an argument tuple that already holds the receiver at index 0, and
calls the resolved function.  Override semantics are unchanged: the lookup
still starts at the receiver's own class.

The compiler takes this route only when the closed-world class graph cannot
redirect the name -- no `__getattribute__`, `__getattr__` or `__slots__`
anywhere, no field shadowing it, no native extension classes in the unit, a
plain-name receiver, and no keyword or starred arguments.
"""
from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).absolute().parents[2]
FLAG = "PCC_DIRECT_VIRTUAL_METHOD_CALLS"

OVERRIDDEN = textwrap.dedent(
    '''
    class Base:
        def __init__(self, n: int) -> None:
            self.n = n

        def step(self, k: int) -> int:
            return self.n + k

        def run(self, rounds: int) -> int:
            total = 0
            for i in range(rounds):
                total += self.step(i)
            return total


    class Child(Base):
        def step(self, k: int) -> int:
            return self.n + k * 2


    class GrandChild(Child):
        def step(self, k: int) -> int:
            return self.n + k * 3


    def main() -> None:
        print(Base(1).run(50) + Child(1).run(50) + GrandChild(1).run(50))
        # The receiver's own class decides, through a base's method body.
        print(Base(7).step(2), Child(7).step(2), GrandChild(7).step(2))


    main()
    '''
)

INTERCEPTED = textwrap.dedent(
    '''
    class Base:
        def __init__(self, n: int) -> None:
            self.n = n

        def step(self, k: int) -> int:
            return self.n + k

        def run(self, rounds: int) -> int:
            total = 0
            for i in range(rounds):
                total += self.step(i)
            return total


    class Child(Base):
        def step(self, k: int) -> int:
            return self.n + k * 2

        def __getattr__(self, name: str):
            raise AttributeError(name)


    def main() -> None:
        print(Base(1).run(50) + Child(1).run(50))


    main()
    '''
)

SHADOWED = textwrap.dedent(
    '''
    class Base:
        def step(self) -> int:
            return 1
        def run(self) -> int:
            return self.step()

    class Child(Base):
        def step(self) -> int:
            return 2

    def main() -> None:
        child = Child()
        child.step = lambda: 99
        print(child.run())

    main()
    '''
)


def _compile(tmp_path: Path, name: str, source: str, *, enabled: bool) -> Path:
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / f"{name}.py"
    exe = tmp_path / f"{name}.out"
    src.write_text(source, encoding="utf-8")
    saved = os.environ.get(FLAG)
    try:
        if enabled:
            os.environ[FLAG] = "1"
        else:
            os.environ.pop(FLAG, None)
        compile_python(
            str(src), str(exe), ir_scaffold_mode="on", libpython_mode="off"
        )
    finally:
        if saved is None:
            os.environ.pop(FLAG, None)
        else:
            os.environ[FLAG] = saved
    return exe


def _run(exe: Path, backend: int = 0) -> str:
    env = dict(os.environ)
    env["PCC_GC_BACKEND"] = str(backend)
    env["PCC_GC_REFCOUNT_PROVENANCE_PROBE"] = "2"
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=120, env=env
    )
    assert result.returncode == 0, result.stderr
    assert "unmanaged pointer" not in result.stderr
    assert "freed twice" not in result.stderr
    return result.stdout.strip()


def test_override_semantics_are_identical_with_and_without_the_flag(tmp_path):
    off = _run(_compile(tmp_path, "ov_off", OVERRIDDEN, enabled=False))
    on = _run(_compile(tmp_path, "ov_on", OVERRIDDEN, enabled=True))
    assert off == on
    # Each class's own step() must win, through the shared run() body.
    assert on.splitlines()[1] == "9 11 13"


def test_a_class_that_intercepts_attributes_keeps_the_general_protocol(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    ir_path = tmp_path / "intercepted.ll"
    src = tmp_path / "intercepted.py"
    src.write_text(INTERCEPTED, encoding="utf-8")
    saved = os.environ.get(FLAG)
    os.environ[FLAG] = "1"
    try:
        compile_python(
            str(src),
            str(ir_path),
            emit_llvm_only=True,
            ir_scaffold_mode="on",
            libpython_mode="off",
        )
    finally:
        if saved is None:
            os.environ.pop(FLAG, None)
        else:
            os.environ[FLAG] = saved
    ir = ir_path.read_text(encoding="utf-8")
    # `__getattr__` anywhere in the graph disqualifies every receiver: the name
    # a call resolves is no longer decided by the MRO alone.  Every runtime
    # function is *declared* in each module, so look for a call.
    assert "call ptr (ptr, ptr, ptr) @py_instance_method_call_direct" not in ir
    assert "@py_obj_getattr" in ir


def test_runtime_instance_shadow_falls_back_under_all_collectors(
    tmp_path, monkeypatch, pcc_runtime_archive,
):
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(pcc_runtime_archive))
    exe = _compile(tmp_path, "shadowed_on", SHADOWED, enabled=True)
    for backend in range(5):
        assert _run(exe, backend) == "99"


@pytest.mark.parametrize("enabled", [False, True])
def test_the_flag_is_part_of_the_compile_cache_identity(enabled) -> None:
    from pcc.frontends.python import compile_cache

    assert FLAG in compile_cache._CODEGEN_ENV_NAMES
