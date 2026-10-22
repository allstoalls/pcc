from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest


# The IR printer spells out the callee type (``call ptr (ptr, ptr) @f``);
# drop it so the assertions below read the call shape only.
_EXPLICIT_CALL_TYPE = re.compile(r"\bcall (\S+) \((?:[^()]|\([^()]*\))*\) @")


def _normalize_call_types(ir_text: str) -> str:
    return _EXPLICIT_CALL_TYPE.sub(r"call \1 @", ir_text)


class PyLambdaCpythonCallTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="pcc_py_lambda_cpy_")
        self.addCleanup(shutil.rmtree, self.td, True)

    def test_none_bridge_guard_accepts_nominal_none_type(self):
        from pcc.frontends.python.codegen.cpy_bridge_lowering import (
            _is_none_type_for_cpython_bridge,
        )
        from pcc.frontends.python.py_ast import NoneType as FrontendNoneType

        class NoneType:
            name = "None"

        self.assertTrue(_is_none_type_for_cpython_bridge(NoneType()))
        self.assertTrue(_is_none_type_for_cpython_bridge(FrontendNoneType(name="None")))

    def _write(self, rel: str, source: str) -> str:
        dst = os.path.join(self.td, rel)
        os.makedirs(os.path.dirname(dst) or self.td, exist_ok=True)
        with open(dst, "w", encoding="utf-8") as fh:
            fh.write(textwrap.dedent(source).lstrip())
        return dst

    def test_lambda_returning_cpython_object_stays_tagged(self):
        from pcc.frontends.python.pipeline import compile_python

        src = self._write(
            "lambda_cpy.py",
            """
            import pcc.frontends.python.type_infer as type_infer
            import pcc.frontends.python.py_lift as py_lift

            src = "def f(x: int) -> int:\\n    return x + 1\\n"
            mod = py_lift.parse_and_lift(src, "lambda_cpy.py", "repro")
            fn = lambda: type_infer.infer_module(mod)
            typed = fn()
            print(typed.name)
            """,
        )
        exe = os.path.join(self.td, "lambda_cpy.out")
        compile_python(src, exe, libpython_mode="auto")
        run = subprocess.run(
            [exe],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(run.returncode, 0)
        self.assertEqual(run.stdout, "repro\n")

    def test_cpython_string_method_chain_stays_tagged(self):
        from pcc.frontends.python.pipeline import compile_python

        src = self._write(
            "cpy_str_chain.py",
            """
            import subprocess

            def main() -> None:
                out = subprocess.check_output(
                    ["printf", "alpha beta\\n"],
                    text=True,
                ).strip()
                parts = out.split()
                print(parts[0])
                print(parts[1])

            if __name__ == "__main__":
                main()
            """,
        )
        exe = os.path.join(self.td, "cpy_str_chain.out")
        compile_python(src, exe)
        run = subprocess.run(
            [exe],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(run.returncode, 0, msg=run.stderr)
        self.assertEqual(run.stdout, "alpha\nbeta\n")

    def test_cpython_with_exit_gets_none_triple(self):
        from pcc.frontends.python.pipeline import compile_python

        src = self._write(
            "cpy_with_exit.py",
            """
            import tempfile

            def main() -> None:
                with tempfile.TemporaryDirectory(prefix="pcc_with_") as tmp:
                    if tmp:
                        print("ok")

            if __name__ == "__main__":
                main()
            """,
        )
        exe = os.path.join(self.td, "cpy_with_exit.out")
        compile_python(src, exe)
        run = subprocess.run(
            [exe],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(run.returncode, 0, msg=run.stderr)
        self.assertEqual(run.stdout, "ok\n")
        self.assertEqual(run.stderr, "")

    def test_none_argument_marshaled_to_cpython_call(self):
        from pcc.frontends.python.pipeline import compile_python

        src = self._write(
            "cpy_none_arg.py",
            """
            import operator

            def main() -> None:
                print(operator.is_(None, None))

            main()
            """,
        )
        ll = os.path.join(self.td, "cpy_none_arg.ll")
        compile_python(src, ll, emit_llvm_only=True, libpython_mode="auto")
        with open(ll, "r", encoding="utf-8") as fh:
            ir_text = fh.read()
        self.assertIn("@py_cpy_from_pcc_obj", ir_text)
        self.assertIn("@py_cpy_call2", ir_text)
        self.assertNotIn("cannot marshal NoneType", ir_text)

    def test_slice_inside_tuple_key_lowers_to_cpython_slice_object(self):
        from pcc.frontends.python.pipeline import compile_python

        src = self._write(
            "cpy_tuple_slice_key.py",
            """
            import ctypes

            def main() -> None:
                ctypes.pythonapi[:, None]

            main()
            """,
        )
        ll = os.path.join(self.td, "cpy_tuple_slice_key.ll")
        compile_python(src, ll, emit_llvm_only=True, libpython_mode="auto")
        with open(ll, "r", encoding="utf-8") as fh:
            ir_text = _normalize_call_types(fh.read())
        self.assertRegex(
            ir_text,
            r"slice\.expr[^=\n]* = call ptr @py_slice_new",
        )
        self.assertRegex(
            ir_text,
            r"cpy\.from_pcc\.dynobj[^=\n]* = call ptr @py_cpy_from_pcc_obj"
            r"\(ptr %slice\.expr",
        )
        self.assertRegex(ir_text, r"cpy\.getitem[^=\n]* = call ptr @py_cpy_getitem")
        self.assertNotIn("does not handle expression Slice", ir_text)

    def test_top_level_cpython_system_exit_zero_is_clean(self):
        from pcc.frontends.python.pipeline import compile_python

        src = self._write(
            "cpy_sys_exit_zero.py",
            """
            import sys

            sys.exit(0)
            """,
        )
        exe = os.path.join(self.td, "cpy_sys_exit_zero.out")
        compile_python(src, exe)
        if sys.platform == "darwin":
            linked = subprocess.check_output(
                ["otool", "-L", exe], text=True,
            )
            self.assertNotIn("Python.framework", linked)
            self.assertNotIn("libpython", linked.lower())
        run = subprocess.run(
            [exe],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(run.returncode, 0, msg=run.stderr)
        self.assertEqual(run.stdout, "")
        self.assertNotIn("Exception ignored", run.stderr)
        self.assertNotIn("SystemError", run.stderr)

    def test_top_level_cpython_system_exit_code_propagates(self):
        from pcc.frontends.python.pipeline import compile_python

        src = self._write(
            "cpy_sys_exit_code.py",
            """
            import sys

            sys.exit(7)
            """,
        )
        exe = os.path.join(self.td, "cpy_sys_exit_code.out")
        compile_python(src, exe)
        if sys.platform == "darwin":
            linked = subprocess.check_output(
                ["otool", "-L", exe], text=True,
            )
            self.assertNotIn("Python.framework", linked)
            self.assertNotIn("libpython", linked.lower())
        run = subprocess.run(
            [exe],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(run.returncode, 7, msg=run.stderr)
        self.assertEqual(run.stdout, "")
        self.assertEqual(run.stderr, "")

    def test_imported_sys_exit_alias_is_native(self):
        from pcc.frontends.python.pipeline import compile_python

        src = self._write(
            "cpy_sys_exit_alias.py",
            """
            from sys import exit as _exit

            _exit(5)
            """,
        )
        exe = os.path.join(self.td, "cpy_sys_exit_alias.out")
        compile_python(src, exe)
        if sys.platform == "darwin":
            linked = subprocess.check_output(
                ["otool", "-L", exe], text=True,
            )
            self.assertNotIn("Python.framework", linked)
            self.assertNotIn("libpython", linked.lower())
        run = subprocess.run(
            [exe],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(run.returncode, 5, msg=run.stderr)
        self.assertEqual(run.stdout, "")
        self.assertEqual(run.stderr, "")


if __name__ == "__main__":
    unittest.main()
