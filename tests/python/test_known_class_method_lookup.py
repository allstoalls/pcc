"""A method no class in a known receiver's MRO declares is looked up at run time."""

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


def test_undeclared_method_is_not_another_classes_method(
    tmp_path,
    monkeypatch,
    pcc_runtime_archive,
):
    # Only P declares __setattr__ and tag, and only Circle declares area.
    # A Q receiver must still reach object.__setattr__ and raise for tag,
    # and a Shape receiver must find area on its run-time class -- never
    # the one other class that happens to declare the name.
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(pcc_runtime_archive))

    source = textwrap.dedent(
        """
        class P:
            def __setattr__(self, name, value):
                object.__setattr__(self, name, "P:" + str(value))

            def tag(self):
                return "P.tag"


        class Q:
            pass


        class Shape:
            def describe(self):
                return "shape with " + str(self.area())


        class Circle(Shape):
            def area(self):
                return 3


        class Blob(Shape):
            pass


        def total(s: Shape):
            return s.area()


        def main():
            for i in range(2):
                q = Q()
                q.__setattr__("w", i)
                print(q.w)
            r = Q()
            r.__setattr__("v", 5)
            print(r.v)
            try:
                r.tag()
            except AttributeError as e:
                print("AttributeError", e)
            print(total(Circle()), Circle().describe())
            try:
                total(Blob())
            except AttributeError as e:
                print("AttributeError", e)


        main()
        """
    )
    expected = (
        "0\n"
        "1\n"
        "5\n"
        "AttributeError 'Q' object has no attribute 'tag'\n"
        "3 shape with 3\n"
        "AttributeError 'Blob' object has no attribute 'area'\n"
    )
    assert _cpython_output(source) == expected
    _run_on_five_collectors(tmp_path, "known_class_lookup", source, expected)
