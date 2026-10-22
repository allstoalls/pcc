"""CPython parity for exception handling, ported from
``Lib/test/test_exceptions.py``.

All tests use ``libpython_mode="off"``.

Reference contract (from CPython):
  * ``try`` / ``except`` matches by class and subclass
  * ``finally`` always runs, even when an exception propagates
  * ``else`` runs when no exception was raised in ``try``
  * bare ``raise`` re-raises the active exception
  * ``raise X from Y`` sets ``__cause__`` (and suppresses
    ``__context__`` rendering)
  * raising inside ``except X`` sets ``__context__`` automatically

Known pcc gaps marked xfail with citations.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path


def _compile(monkeypatch, src: Path, exe: Path) -> None:
    from pcc.frontends.python.pipeline import compile_python

    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )


def _run(exe: Path, timeout: float = 30.0) -> str:
    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert result.returncode == 0, (
        f"{exe.name} exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result.stdout


def test_exception_try_except_basic(tmp_path, monkeypatch):
    src = tmp_path / "exc_basic.py"
    exe = tmp_path / "exc_basic.out"
    src.write_text(
        textwrap.dedent("""
        def main() -> None:
            try:
                raise ValueError("boom")
            except ValueError as e:
                print("caught", str(e))
            print("after")

        if __name__ == "__main__":
            main()
        """).lstrip(),
        encoding="utf-8",
    )
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["caught boom", "after"]


def test_unbound_name_raises_name_error_at_runtime(tmp_path, monkeypatch):
    src = tmp_path / "name_error_probe.py"
    exe = tmp_path / "name_error_probe.out"
    src.write_text(
        textwrap.dedent("""
        def main() -> None:
            try:
                __PACKAGE_SETUP_SENTINEL__
            except NameError:
                print("caught")
            print("after")

        if __name__ == "__main__":
            main()
        """).lstrip(),
        encoding="utf-8",
    )
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["caught", "after"]


def test_unbound_function_call_raises_name_error_at_runtime(tmp_path, monkeypatch):
    src = tmp_path / "name_call_error_probe.py"
    exe = tmp_path / "name_call_error_probe.out"
    src.write_text(
        textwrap.dedent("""
        def main() -> None:
            try:
                __PACKAGE_SETUP_SENTINEL__(1, 2)
            except NameError:
                print("caught")
            print("after")

        if __name__ == "__main__":
            main()
        """).lstrip(),
        encoding="utf-8",
    )
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["caught", "after"]


def test_exception_try_finally(tmp_path, monkeypatch):
    src = tmp_path / "exc_finally.py"
    exe = tmp_path / "exc_finally.out"
    src.write_text(
        textwrap.dedent("""
        def main() -> None:
            try:
                try:
                    raise RuntimeError("inner")
                finally:
                    print("ran finally")
            except RuntimeError as e:
                print("caught", str(e))

        if __name__ == "__main__":
            main()
        """).lstrip(),
        encoding="utf-8",
    )
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["ran finally", "caught inner"]


def test_exception_try_except_finally_no_raise(tmp_path, monkeypatch):
    src = tmp_path / "exc_no_raise.py"
    exe = tmp_path / "exc_no_raise.out"
    src.write_text(
        textwrap.dedent("""
        def main() -> None:
            try:
                print("try")
            except ValueError:
                print("except")
            finally:
                print("finally")
            print("after")

        if __name__ == "__main__":
            main()
        """).lstrip(),
        encoding="utf-8",
    )
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["try", "finally", "after"]


def test_return_from_except_survives_finally(tmp_path, monkeypatch):
    src = tmp_path / "except_return_finally.py"
    exe = tmp_path / "except_return_finally.out"
    src.write_text(
        textwrap.dedent("""
        def choose() -> str:
            try:
                raise ValueError("boom")
            except ValueError:
                return "handled"
            finally:
                print("cleanup")

        def main() -> None:
            print(choose())

        if __name__ == "__main__":
            main()
        """).lstrip(),
        encoding="utf-8",
    )
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["cleanup", "handled"]


def test_return_from_failed_with_except_survives_outer_finally(tmp_path, monkeypatch):
    src = tmp_path / "failed_with_except_return_finally.py"
    exe = tmp_path / "failed_with_except_return_finally.out"
    missing = tmp_path / "missing.txt"
    src.write_text(
        textwrap.dedent(f"""
        def choose(path: str) -> str:
            try:
                raise RuntimeError("outer")
            except RuntimeError:
                try:
                    with open(path, "r", encoding="utf-8") as fh:
                        return fh.read()
                except Exception:
                    return "fallback"
            finally:
                try:
                    print("cleanup")
                except Exception:
                    pass

        def main() -> None:
            print(choose({str(missing)!r}))

        if __name__ == "__main__":
            main()
        """).lstrip(),
        encoding="utf-8",
    )
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["cleanup", "fallback"]


def test_exception_multiple_except(tmp_path, monkeypatch):
    src = tmp_path / "exc_multi.py"
    exe = tmp_path / "exc_multi.out"
    src.write_text(
        textwrap.dedent("""
        def boom(kind: int) -> None:
            if kind == 0:
                raise ValueError("v")
            else:
                raise RuntimeError("r")

        def trial(kind: int) -> str:
            try:
                boom(kind)
                return "no raise"
            except ValueError:
                return "value"
            except RuntimeError:
                return "runtime"

        def main() -> None:
            print(trial(0))
            print(trial(1))

        if __name__ == "__main__":
            main()
        """).lstrip(),
        encoding="utf-8",
    )
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["value", "runtime"]


def test_exception_subclass_match(tmp_path, monkeypatch):
    src = tmp_path / "exc_subclass.py"
    exe = tmp_path / "exc_subclass.out"
    src.write_text(
        textwrap.dedent("""
        def main() -> None:
            try:
                raise IndexError("oob")
            except LookupError as e:
                print("caught LookupError:", str(e))

        if __name__ == "__main__":
            main()
        """).lstrip(),
        encoding="utf-8",
    )
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip() == "caught LookupError: oob"


def test_exception_else_runs_when_no_raise(tmp_path, monkeypatch):
    src = tmp_path / "exc_else.py"
    exe = tmp_path / "exc_else.out"
    src.write_text(
        textwrap.dedent("""
        def main() -> None:
            try:
                x = 1
            except ValueError:
                print("except")
            else:
                print("else")

        if __name__ == "__main__":
            main()
        """).lstrip(),
        encoding="utf-8",
    )
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip() == "else"


def test_exception_bare_raise_reraises(tmp_path, monkeypatch):
    src = tmp_path / "exc_reraise.py"
    exe = tmp_path / "exc_reraise.out"
    src.write_text(
        textwrap.dedent("""
        def main() -> None:
            try:
                try:
                    raise ValueError("inner")
                except ValueError:
                    print("first catch")
                    raise
            except ValueError as e:
                print("outer caught", str(e))

        if __name__ == "__main__":
            main()
        """).lstrip(),
        encoding="utf-8",
    )
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == [
        "first catch",
        "outer caught inner",
    ]


def test_exception_message_via_str(tmp_path, monkeypatch):
    src = tmp_path / "exc_str.py"
    exe = tmp_path / "exc_str.out"
    src.write_text(
        textwrap.dedent("""
        def main() -> None:
            try:
                raise ValueError("a message")
            except ValueError as e:
                print(str(e))

        if __name__ == "__main__":
            main()
        """).lstrip(),
        encoding="utf-8",
    )
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip() == "a message"


def test_exception_raise_from(tmp_path, monkeypatch):
    src = tmp_path / "exc_raise_from.py"
    exe = tmp_path / "exc_raise_from.out"
    src.write_text(
        textwrap.dedent("""
        def main() -> None:
            try:
                try:
                    raise ValueError("orig")
                except ValueError as orig:
                    raise RuntimeError("wrapped") from orig
            except RuntimeError as e:
                print(str(e))
                cause = e.__cause__
                if cause is not None:
                    print(str(cause))

        if __name__ == "__main__":
            main()
        """).lstrip(),
        encoding="utf-8",
    )
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["wrapped", "orig"]


def test_exception_user_subclass(tmp_path, monkeypatch):
    src = tmp_path / "exc_user_sub.py"
    exe = tmp_path / "exc_user_sub.out"
    src.write_text(
        textwrap.dedent("""
        class MyError(ValueError):
            pass

        def main() -> None:
            try:
                raise MyError("custom")
            except ValueError as e:
                print("caught as ValueError:", str(e))
            try:
                raise MyError("custom2")
            except MyError as e:
                print("caught as MyError:", str(e))

        if __name__ == "__main__":
            main()
        """).lstrip(),
        encoding="utf-8",
    )
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == [
        "caught as ValueError: custom",
        "caught as MyError: custom2",
    ]
