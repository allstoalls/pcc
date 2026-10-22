"""Target tests for pcc1 actually running pytest (not forking host pytest).

The existing ``pcc1 --pytest`` flag is fake — it forks ``uv run pytest``
under host CPython; pcc1 itself contributes nothing to test execution.
These tests pin the *honest* contract: pcc1 compiles a closed-world
pcc-native test runner and pytest facade, and the produced native binary
actually runs and reports tests without libpython.

This is intentionally a pytest-compatible subset, not a claim that the
third-party pytest package, pluggy, runtime discovery, assert rewriting,
or inspect-driven fixture machinery all compile under pcc1.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


REPO = Path(__file__).absolute().parents[2]
_PCC1_CANDIDATES = [
    REPO / "build" / "bootstrap-pytest-self" / "pcc1",
    REPO / "build" / "bootstrap" / "pcc1",
    REPO / "build" / "bootstrap-self-claude" / "pcc1",
    REPO / "build" / "bootstrap-llvm-claude" / "pcc1",
    REPO / "build" / "bootstrap-strict-self" / "pcc1",
    REPO / "build" / "bootstrap-self-darwin_arm64" / "pcc1",
    REPO / "build" / "bootstrap-llvm-darwin_arm64" / "pcc1",
]


def _find_pcc1() -> Path | None:
    env_path = os.environ.get("PCC1_BINARY")
    if env_path:
        p = Path(env_path)
        if p.exists() and p.is_file():
            return p
    for p in _PCC1_CANDIDATES:
        if p.exists() and p.is_file():
            return p
    return None


PCC1 = _find_pcc1()
pytestmark = pytest.mark.pcc_gate(probe="pcc1")


@pytest.fixture(scope="module", autouse=True)
def _capable_pcc_runtime(pcc_runtime_archive):
    """Ensure the pcc-Python runtime archive pcc1 links is built before these
    tests (shared ``pcc_runtime_archive`` fixture in conftest). Without it a
    tree missing ``libpy_runtime_pcc_py.a`` fails every pcc1 link with
    undefined ``py_*`` symbols."""
    previous = os.environ.get("PCC_RUNTIME_ARCHIVE")
    os.environ["PCC_RUNTIME_ARCHIVE"] = str(pcc_runtime_archive)
    try:
        yield pcc_runtime_archive
    finally:
        if previous is None:
            os.environ.pop("PCC_RUNTIME_ARCHIVE", None)
        else:
            os.environ["PCC_RUNTIME_ARCHIVE"] = previous


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _compile(tmp_path: Path, src_text: str) -> Path:
    """pcc1-compile ``src_text`` strictly (no libpython). Return the
    produced binary path. Raises if compile fails.
    """
    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(textwrap.dedent(src_text).lstrip(), encoding="utf-8")
    cmd = [
        str(PCC1), str(src), "-o", str(exe),
        "--python-libpython=off",
        "--ir-scaffold=on",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180.0)
    assert proc.returncode == 0, (
        f"pcc1 compile failed (exit {proc.returncode}):\n"
        f"cmd: {' '.join(cmd)}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert exe.exists()
    return exe


def _compile_and_run(tmp_path: Path, src_text: str) -> tuple[int, str, str]:
    """Compile + run; return ``(returncode, stdout, stderr)``.

    Does NOT assert exit code 0 — callers may want to verify
    failure semantics.
    """
    exe = _compile(tmp_path, src_text)
    proc = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=60.0,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _links_libpython(path: Path) -> bool:
    cmd = ["otool", "-L", str(path)] if sys.platform == "darwin" else [
        "ldd",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30.0)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pytest.fail(f"can't run {cmd[0]}; cannot verify libpython linkage")
    assert proc.returncode == 0, (
        f"{cmd[0]} failed while checking libpython linkage:\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    text = (proc.stdout or "") + (proc.stderr or "")
    return "libpython" in text.lower() or "Python.framework" in text


# --------------------------------------------------------------------------
# Group 1 — core runner primitives
# --------------------------------------------------------------------------
# Without these, the rest of the file is meaningless. Any of them
# breaking means basic assert/exception lowering regressed.

def test_pcc1_assert_passes_exits_zero(tmp_path):
    rc, out, _ = _compile_and_run(tmp_path, """
        def main() -> None:
            assert 1 + 1 == 2
            print("ok")
        if __name__ == "__main__":
            main()
    """)
    assert rc == 0 and out.strip() == "ok"


def test_pcc1_assert_failure_exits_nonzero(tmp_path):
    """If asserts silently no-op'd, this would be exit 0 — that would
    be a catastrophic regression. Pin it."""
    rc, _out, _err = _compile_and_run(tmp_path, """
        def main() -> None:
            assert 1 + 1 == 3
            print("UNREACHABLE")
        if __name__ == "__main__":
            main()
    """)
    assert rc != 0


def test_pcc1_runs_multiple_inline_assertions_with_try_except(tmp_path):
    """Each test function can be called by name and wrapped in
    try/except AssertionError, which is the base runner shape."""
    rc, out, _ = _compile_and_run(tmp_path, """
        def test_arith() -> None:
            assert 2 * 3 == 6

        def test_str() -> None:
            assert "ab".upper() == "AB"

        def test_bad() -> None:
            assert 1 == 2

        def main() -> None:
            passed = 0
            failed = 0
            try:
                test_arith()
                passed = passed + 1
            except AssertionError:
                failed = failed + 1
            try:
                test_str()
                passed = passed + 1
            except AssertionError:
                failed = failed + 1
            try:
                test_bad()
                passed = passed + 1
            except AssertionError:
                failed = failed + 1
            print(f"{passed} passed, {failed} failed")

        if __name__ == "__main__":
            main()
    """)
    assert rc == 0 and out.strip() == "2 passed, 1 failed"


def test_pcc1_runs_test_list_via_indirect_calls(tmp_path):
    """The 'real' pytest-shape runner: a list of test functions, iterate
    + call each by reference, without CPython callable wrappers."""
    rc, out, _ = _compile_and_run(tmp_path, """
        def test_arith() -> None:
            assert 2 * 3 == 6

        def test_str() -> None:
            assert "ab".upper() == "AB"

        def main() -> None:
            tests = [test_arith, test_str]
            passed = 0
            for fn in tests:
                try:
                    fn()
                    passed = passed + 1
                except AssertionError:
                    pass
            print(f"{passed} passed")

        if __name__ == "__main__":
            main()
    """)
    assert rc == 0 and out.strip() == "2 passed"


# --------------------------------------------------------------------------
# Group 2 — pcc-native mini-runner
# --------------------------------------------------------------------------
# ``pcc.test_runner`` is a compile-time facade: imports are accepted, and
# ``run_tests([...])`` lowers to a static runner over the explicit list.
def test_pcc1_pcc_test_runner_basic(tmp_path):
    rc, out, _ = _compile_and_run(tmp_path, """
        from pcc.test_runner import run_tests

        def test_one() -> None:
            assert 1 + 1 == 2
        def test_two() -> None:
            assert "x".upper() == "X"

        if __name__ == "__main__":
            run_tests([test_one, test_two])
    """)
    assert rc == 0 and "2 passed" in out


def test_pcc1_pcc_test_runner_parametrize(tmp_path):
    rc, out, _ = _compile_and_run(tmp_path, """
        from pcc.test_runner import parametrize, run_tests

        @parametrize([(1, 2, 3), (2, 3, 5), (3, 5, 8)])
        def test_add(a: int, b: int, r: int) -> None:
            assert a + b == r

        if __name__ == "__main__":
            run_tests([test_add])
    """)
    assert rc == 0 and "3 passed" in out


def test_pcc1_pcc_test_runner_fixture(tmp_path):
    rc, out, _ = _compile_and_run(tmp_path, """
        from pcc.test_runner import fixture, run_tests

        @fixture
        def value() -> int:
            return 42

        def test_with_fixture(value: int) -> None:
            assert value == 42

        if __name__ == "__main__":
            run_tests([test_with_fixture])
    """)
    assert rc == 0 and "1 passed" in out


# --------------------------------------------------------------------------
# Group 3 — pytest facade
# --------------------------------------------------------------------------
# ``import pytest`` is accepted as a closed-world facade. ``pytest.main``
# lowers to static discovery of top-level ``test_*`` functions in the
# compiled source file.
def test_pcc1_compiles_file_that_imports_pytest(tmp_path):
    _ = _compile(tmp_path, """
        import pytest

        def test_one() -> None:
            assert 1 + 1 == 2

        if __name__ == "__main__":
            pytest.main([__file__])
    """)


def test_pcc1_runs_pytest_main_with_passes_and_failures(tmp_path):
    rc, out, _ = _compile_and_run(tmp_path, """
        import pytest

        def test_pass_a() -> None:
            assert 2 + 2 == 4
        def test_pass_b() -> None:
            assert "abc"[::-1] == "cba"
        def test_fail_a() -> None:
            assert 1 == 2

        if __name__ == "__main__":
            pytest.main([__file__])
    """)
    # pytest.main returns nonzero when any test fails.
    assert rc != 0
    # Default pytest output has "N passed, M failed" somewhere.
    assert "2 passed" in out
    assert "1 failed" in out


def test_pcc1_runs_pytest_parametrize(tmp_path):
    rc, out, _ = _compile_and_run(tmp_path, """
        import pytest

        @pytest.mark.parametrize("a,b,expected", [
            (1, 1, 2),
            (2, 3, 5),
            (10, 20, 30),
        ])
        def test_add(a: int, b: int, expected: int) -> None:
            assert a + b == expected

        if __name__ == "__main__":
            pytest.main([__file__])
    """)
    assert rc == 0 and "3 passed" in out


def test_pcc1_runs_pytest_fixture(tmp_path):
    rc, out, _ = _compile_and_run(tmp_path, """
        import pytest

        @pytest.fixture
        def fortytwo() -> int:
            return 42

        def test_uses_fixture(fortytwo: int) -> None:
            assert fortytwo == 42

        if __name__ == "__main__":
            pytest.main([__file__])
    """)
    assert rc == 0 and "1 passed" in out


# --------------------------------------------------------------------------
# Group 4 — observable regression gates
# --------------------------------------------------------------------------
# These fail loudly if someone changes pcc1 in a way that silently turns
# assertions off, swallows exit codes, or breaks the closed-world contract.

def test_pcc1_strict_mode_rejects_unsupported_idiom(tmp_path):
    """Closed-world (--python-libpython=off) must REJECT compilation
    when source needs a CPython fallback. Silent fallback = Issue 1
    regression. This pins the gate."""
    src = tmp_path / "needs_cpython.py"
    exe = tmp_path / "x.out"
    src.write_text(textwrap.dedent("""
        # ``eval`` is explicitly out of scope per docs/python-limitations.md.
        def main() -> None:
            x = eval("1 + 1")
            print(x)
        if __name__ == "__main__":
            main()
    """).lstrip(), encoding="utf-8")
    proc = subprocess.run(
        [str(PCC1), str(src), "-o", str(exe),
         "--python-libpython=off", "--ir-scaffold=on"],
        capture_output=True, text=True, timeout=180.0,
    )
    # Either compile fails (expected) OR succeeds but the binary
    # doesn't link libpython. The contract is: no silent fallback.
    if proc.returncode == 0 and exe.exists():
        assert not _links_libpython(exe), (
            "silent libpython fallback under --python-libpython=off"
        )
    # If compile failed, the diagnostic must mention libpython/fallback.
    else:
        combined = (proc.stderr + proc.stdout).lower()
        assert "libpython" in combined or "fallback" in combined or (
            "eval" in combined
        ), (
            f"compile failed but reason unclear:\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
