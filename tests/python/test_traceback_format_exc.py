"""traceback.format_exc() / traceback.print_exc() native lowering.

S-P0-SELF-TRACEBACK-FORMAT-EXC: `import traceback` registers a native
builtin module alias (no CPython fallback), and format_exc/print_exc
lower to py_exc_traceback_format_exc / py_exc_traceback_print_exc over
the PyFrameRecord trail — including inside name-less `except X:`
handlers, where the handler-binding rewrite
(`_rewrite_traceback_handler_bindings`) keeps the handled exception
alive for the handler body like CPython's exc_info does.

CPython reference behavior (verified with python3):
  * inside a handler, format_exc() returns
    'Traceback (most recent call last):\n  File "...", line N, in
    <outer>\n ... in <raise site>\n<Cls>: <msg>\n'
    (outermost frame first, raise site last);
  * outside any handler it returns 'NoneType: None\n';
  * print_exc() writes the same text to stderr and returns None.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest


class TracebackFormatExcTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="pcc_py_tb_fmt_")
        self.addCleanup(shutil.rmtree, self.td, True)

    def _write(self, rel: str, source: str) -> str:
        dst = os.path.join(self.td, rel)
        os.makedirs(os.path.dirname(dst) or self.td, exist_ok=True)
        with open(dst, "w", encoding="utf-8") as fh:
            fh.write(textwrap.dedent(source).lstrip())
        return dst

    def _compile(self, src: str, exe: str) -> None:
        from pcc.frontends.python.pipeline import compile_python

        compile_python(
            src,
            exe,
            libpython_mode="off",
            ir_scaffold_mode="on",
            backend="self",
        )

    def _run(self, argv):
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def _assert_matches_cpython_stdout(self, src: str, exe: str) -> None:
        run = self._run([exe])
        self.assertEqual(run.returncode, 0, msg=run.stderr)
        ref = self._run([sys.executable, src])
        self.assertEqual(ref.returncode, 0, msg=ref.stderr)
        self.assertEqual(run.stdout, ref.stdout)

    def test_format_exc_in_nameless_handler_matches_cpython(self):
        # The handler has NO ``as`` binding — the rewrite must retain
        # the handled exception so format_exc() can read its trail.
        src = self._write(
            "tb_nameless.py",
            """
            import traceback

            def boom() -> None:
                raise ValueError("x")

            def main() -> None:
                try:
                    boom()
                except ValueError:
                    s = traceback.format_exc()
                    print("Traceback (most recent call last):" in s)
                    print("ValueError: x" in s)
                    print("in boom" in s)
                    print("in main" in s)

            main()
            """,
        )
        exe = os.path.join(self.td, "tb_nameless.out")
        self._compile(src, exe)
        self._assert_matches_cpython_stdout(src, exe)

    def test_format_exc_frame_order_and_heading(self):
        # pcc-only inspection of the formatted text: CPython prints the
        # outermost frame first ("most recent call last"), so the
        # ``main`` frame line must come before the ``boom`` raise-site
        # line, and the heading line closes the text.
        src = self._write(
            "tb_order.py",
            """
            import traceback

            def boom() -> None:
                raise ValueError("x")

            def main() -> None:
                try:
                    boom()
                except ValueError:
                    print(traceback.format_exc())

            main()
            """,
        )
        exe = os.path.join(self.td, "tb_order.out")
        self._compile(src, exe)
        run = self._run([exe])
        self.assertEqual(run.returncode, 0, msg=run.stderr)
        s = run.stdout
        self.assertTrue(
            s.startswith("Traceback (most recent call last):\n"), msg=s
        )
        self.assertIn('  File "' + src + '", line ', s)
        self.assertIn(", in main\n", s)
        self.assertIn(", in boom\n", s)
        self.assertLess(s.index(", in main\n"), s.index(", in boom\n"), msg=s)
        # format_exc text ends with the heading newline; print() adds
        # one more.
        self.assertTrue(s.endswith("ValueError: x\n\n"), msg=s)

    def test_format_exc_outside_handler_is_nonetype_none(self):
        src = self._write(
            "tb_outside.py",
            """
            import traceback

            s = traceback.format_exc()
            print(s == "NoneType: None\\n")
            print(len(s))
            """,
        )
        exe = os.path.join(self.td, "tb_outside.out")
        self._compile(src, exe)
        self._assert_matches_cpython_stdout(src, exe)

    def test_print_exc_writes_stderr_and_returns_none(self):
        src = self._write(
            "tb_print_exc.py",
            """
            import traceback

            def main() -> None:
                try:
                    raise KeyError("k")
                except KeyError:
                    r = traceback.print_exc()
                    print(r)
                    print("handled")

            main()
            """,
        )
        exe = os.path.join(self.td, "tb_print_exc.out")
        self._compile(src, exe)
        run = self._run([exe])
        self.assertEqual(run.returncode, 0, msg=run.stderr)
        self.assertEqual(run.stdout, "None\nhandled\n")
        self.assertIn("Traceback (most recent call last):\n", run.stderr)
        self.assertIn("KeyError", run.stderr)

    def test_from_import_format_exc_named_handler(self):
        src = self._write(
            "tb_from_import.py",
            """
            from traceback import format_exc

            def main() -> None:
                try:
                    raise KeyError("k")
                except KeyError as e:
                    s = format_exc()
                    print("Traceback (most recent call last):" in s)
                    print("KeyError" in s)

            main()
            """,
        )
        exe = os.path.join(self.td, "tb_from_import.out")
        self._compile(src, exe)
        self._assert_matches_cpython_stdout(src, exe)


if __name__ == "__main__":
    unittest.main()
