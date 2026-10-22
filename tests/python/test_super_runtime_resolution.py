"""``super()`` follows CPython's receiver rules and run-time MRO lookup natively."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap


def _run_on_five_collectors(tmp_path, stem, source, expected):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / (stem + ".py")
    exe = tmp_path / (stem + ".out")
    src.write_text(source, encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    for backend in range(5):
        run_env = {**os.environ, "PCC_GC_BACKEND": str(backend)}
        result = subprocess.run(
            [str(exe)],
            capture_output=True,
            text=True,
            timeout=20,
            env=run_env,
        )
        assert result.returncode == 0, (
            f"PCC_GC_BACKEND={backend} rc={result.returncode}\n"
            + result.stdout + result.stderr
        )
        assert result.stdout == expected, f"PCC_GC_BACKEND={backend}"


def _cpython_output(source):
    return subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    ).stdout


def test_super_receiver_rules_and_runtime_lookup_match_cpython(
    tmp_path,
    monkeypatch,
    pcc_runtime_archive,
):
    # Zero-argument super() takes its receiver from the first positional
    # parameter, so a staticmethod can use one and a function with none (or
    # only *args) raises RuntimeError. An explicit receiver is evaluated
    # once. A name no compile-time class defines is found in the MRO when
    # the call runs, a scalar result keeps its declared type, a super
    # attribute can be read without calling it, and a method name shared
    # with str (count) is not mistaken for a str method. Function-local
    # classes reach a closure staticmethod through super().
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(pcc_runtime_archive))

    source = textwrap.dedent(
        """
        calls = []


        class A:
            def greet(self):
                return "A.greet"

            def count(self) -> int:
                return 41

            @staticmethod
            def lonely():
                return super().greet()

            @staticmethod
            def star(*args):
                return super().greet()


        class B(A):
            def greet(self):
                return "B>" + super().greet()

            def count(self) -> int:
                return super().count() + 1

            @staticmethod
            def via(obj):
                return super().greet()

            @staticmethod
            def via_count(obj) -> int:
                return super().count() + 100

            def explicit(self):
                return super(B, make(self)).late()

            def implicit(self):
                return super().late()

            @classmethod
            def from_class(cls):
                return super().greet


        def make(obj):
            calls.append(1)
            return obj


        def late(self):
            return "late:" + type(self).__name__


        def outer(n):
            class P:
                @staticmethod
                def scaled(x):
                    return x * n

            class C(P):
                @staticmethod
                def scaled(x):
                    return super(C, C).scaled(x) + 1

                def run(self, x):
                    return super().scaled(x)

            return C


        def main():
            A.late = late
            b = B()
            print(b.greet(), B.via(b), b.explicit(), b.implicit(), len(calls))
            print(b.count(), B.via_count(b), B.from_class() is A.greet)
            for thunk in (A.lonely, A.star):
                try:
                    thunk()
                except RuntimeError as e:
                    print("RuntimeError", e)
            C = outer(3)
            print(C.scaled(2), C().run(5))


        main()
        """
    )
    expected = (
        "B>A.greet A.greet late:B late:B 1\n"
        "42 141 True\n"
        "RuntimeError super(): no arguments\n"
        "RuntimeError super(): no arguments\n"
        "7 15\n"
    )
    assert _cpython_output(source) == expected
    _run_on_five_collectors(tmp_path, "super_runtime", source, expected)
