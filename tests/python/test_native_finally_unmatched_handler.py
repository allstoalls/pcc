"""Native finally execution and exception preservation across cleanup."""
from __future__ import annotations

import subprocess
import sys
import textwrap


def test_finally_runs_on_unmatched_handler_matches_cpython(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "fin.py"
    exe = tmp_path / "fin.out"
    src.write_text(textwrap.dedent("""
        def f_return() -> int:
            try:
                return 1
            finally:
                print("f_return finally")

        def main() -> None:
            try:
                print("normal body")
            finally:
                print("finally-1")
            try:
                raise ValueError("x")
            except ValueError:
                print("handled")
            finally:
                print("finally-2")
            try:
                try:
                    raise KeyError("k")
                except IndexError:
                    print("nope")
                finally:
                    print("finally-3")
            except KeyError:
                print("outer caught k")
            print("ret", f_return())
            try:
                try:
                    raise RuntimeError("r")
                finally:
                    print("finally-5")
            except RuntimeError:
                print("caught r")

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    compile_python(
        str(src), str(exe),
        ir_scaffold_mode="on", libpython_mode="off", backend="self",
    )
    cpython = subprocess.run(
        [sys.executable, str(src)], capture_output=True, text=True, timeout=30,
    ).stdout
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == cpython


def test_finally_preserves_exception_across_cleanup(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "cleanup.py"
    exe = tmp_path / "cleanup.out"
    src.write_text(textwrap.dedent("""
        import gc

        def cleanup() -> None:
            try:
                raise AttributeError("temporary")
            except AttributeError:
                pass
            gc.collect()

        def fail(mode: int) -> int:
            try:
                raise ValueError("original")
            except BaseException:
                raise
            finally:
                cleanup()
                if mode == 1:
                    raise
                if mode == 2:
                    raise TypeError("replacement")
                if mode == 3:
                    return 42

        def nested() -> None:
            try:
                raise KeyError("outer")
            finally:
                try:
                    fail(0)
                except ValueError:
                    cleanup()

        def main() -> None:
            for mode in range(4):
                try:
                    print("returned", fail(mode))
                except ValueError as exc:
                    print("original", str(exc))
                except TypeError as exc:
                    print("replacement", str(exc), str(exc.__context__))
            try:
                nested()
            except KeyError:
                print("outer preserved")

        main()
        """).lstrip(), encoding="utf-8")
    expected = subprocess.run(
        [sys.executable, str(src)], capture_output=True, text=True, timeout=30,
    )
    assert expected.returncode == 0, expected.stderr
    compile_python(
        str(src), str(exe),
        ir_scaffold_mode="on", libpython_mode="off", backend="self",
    )
    for backend in range(5):
        monkeypatch.setenv("PCC_GC_BACKEND", str(backend))
        result = subprocess.run(
            [str(exe)], capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, (backend, result.stderr)
        assert result.stdout == expected.stdout, (backend, result.stdout)
