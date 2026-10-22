"""NotImplemented is its own native singleton, not a second None."""

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


def test_notimplemented_is_a_distinct_singleton(tmp_path, monkeypatch, pcc_runtime_archive):
    # repr/str/print/format, type(NotImplemented) and its name, isinstance,
    # calling the type, identity hashing as a dict key next to None and
    # Ellipsis, and the TypeError Python 3.14+ raises for bool(NotImplemented).
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(pcc_runtime_archive))

    source = textwrap.dedent(
        """
        NotImplementedType = type(NotImplemented)


        class Box:
            def __eq__(self, other):
                return NotImplemented


        def show(label, thunk):
            try:
                print(label, repr(thunk()))
            except Exception as e:
                print(label, type(e).__name__, e)


        def main():
            print(NotImplemented, repr(NotImplemented), str(NotImplemented))
            print([None, NotImplemented, Ellipsis])
            show("format", lambda: f"{NotImplemented}|{NotImplemented!r}")
            show("type", lambda: NotImplementedType.__name__)
            show("not-none", lambda: (NotImplementedType is type(None), NotImplemented is None))
            show("isinstance", lambda: (isinstance(NotImplemented, NotImplementedType), isinstance(None, NotImplementedType)))
            show("call", lambda: NotImplementedType() is NotImplemented)
            show("call-args", lambda: NotImplementedType(1))
            values = {None: "none", NotImplemented: "notimpl", Ellipsis: "ellipsis"}
            show("keys", lambda: (len(values), values[NotImplemented], hash(NotImplemented) == hash(NotImplemented)))
            show("eq", lambda: (NotImplemented == NotImplemented, NotImplemented == None, Box().__eq__(1) is NotImplemented))
            show("bool", lambda: bool(NotImplemented))
            show("if", lambda: "yes" if NotImplemented else "no")


        main()
        """
    )
    expected = (
        "NotImplemented NotImplemented NotImplemented\n"
        "[None, NotImplemented, Ellipsis]\n"
        "format 'NotImplemented|NotImplemented'\n"
        "type 'NotImplementedType'\n"
        "not-none (False, False)\n"
        "isinstance (True, False)\n"
        "call True\n"
        "call-args TypeError NotImplementedType takes no arguments\n"
        "keys (3, 'notimpl', True)\n"
        "eq (True, False, True)\n"
        "bool TypeError NotImplemented should not be used in a boolean context\n"
        "if TypeError NotImplemented should not be used in a boolean context\n"
    )
    assert _cpython_output(source) == expected
    _run_on_five_collectors(tmp_path, "notimplemented_singleton", source, expected)
