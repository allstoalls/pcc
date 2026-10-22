from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest


class PyStartupArgsTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="pcc_py_argv_")
        self.addCleanup(shutil.rmtree, self.td, True)

    def _write(self, rel: str, source: str) -> str:
        dst = os.path.join(self.td, rel)
        os.makedirs(os.path.dirname(dst) or self.td, exist_ok=True)
        with open(dst, "w", encoding="utf-8") as fh:
            fh.write(textwrap.dedent(source).lstrip())
        return dst

    def test_compiled_program_sees_host_argv(self):
        from pcc.frontends.python.pipeline import compile_python

        src = self._write(
            "argv_prog.py",
            """
            import sys

            print(len(sys.argv))
            print(sys.argv[1])
            print(sys.argv[2])
            """,
        )
        exe = os.path.join(self.td, "argv.out")
        compile_python(src, exe)
        if sys.platform == "darwin":
            linked = subprocess.check_output(
                ["otool", "-L", exe], text=True,
            )
            self.assertNotIn("Python.framework", linked)
            self.assertNotIn("libpython", linked.lower())
        run = subprocess.run(
            [exe, "alpha", "beta"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(run.returncode, 0)
        self.assertEqual(run.stdout, "3\nalpha\nbeta\n")

    def test_compiled_function_scope_sys_argv_is_native(self):
        from pcc.frontends.python.pipeline import compile_python

        src = self._write(
            "argv_fn_prog.py",
            """
            import sys

            def main() -> None:
                out = []
                i = 1
                while i < len(sys.argv):
                    out.append((sys.argv[i] or "") + "")
                    i += 1
                print(len(out))
                print(out[0])
                print(out[1])

            if __name__ == "__main__":
                main()
            """,
        )
        exe = os.path.join(self.td, "argv_fn.out")
        compile_python(src, exe)
        if sys.platform == "darwin":
            linked = subprocess.check_output(
                ["otool", "-L", exe], text=True,
            )
            self.assertNotIn("Python.framework", linked)
            self.assertNotIn("libpython", linked.lower())
        run = subprocess.run(
            [exe, "--entry", "demo", "world"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(run.returncode, 0)
        self.assertEqual(run.stdout, "3\n--entry\ndemo\n")

    def test_compiled_sys_argv_string_slice_is_native(self):
        from pcc.frontends.python.pipeline import compile_python

        src = self._write(
            "argv_slice_prog.py",
            """
            import sys

            def main() -> None:
                s = sys.argv[1]
                print(s[10:])
                idx = s.find("=")
                print(s[idx + 1 :])

            if __name__ == "__main__":
                main()
            """,
        )
        exe = os.path.join(self.td, "argv_slice.out")
        compile_python(src, exe)
        if sys.platform == "darwin":
            linked = subprocess.check_output(
                ["otool", "-L", exe], text=True,
            )
            self.assertNotIn("Python.framework", linked)
            self.assertNotIn("libpython", linked.lower())
        run = subprocess.run(
            [exe, "--backend=self"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(run.returncode, 0, msg=run.stderr)
        self.assertEqual(run.stdout, "self\nself\n")

    def test_compiled_os_path_subset_is_native(self):
        from pcc.frontends.python.pipeline import compile_python

        src = self._write(
            "os_path_prog.py",
            """
            import os

            def main() -> None:
                home = os.path
                print(os.path.join("a", "b", "c.txt"))
                print(home.basename("/tmp/demo.txt"))
                if os.path.exists("/tmp"):
                    print("yes")
                else:
                    print("no")

            if __name__ == "__main__":
                main()
            """,
        )
        exe = os.path.join(self.td, "os_path.out")
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
        self.assertEqual(run.returncode, 0)
        self.assertEqual(run.stdout, "a/b/c.txt\ndemo.txt\nyes\n")

    def test_compiled_os_path_unsupported_methods_fall_back(self):
        from pcc.frontends.python.pipeline import compile_python

        src = self._write(
            "os_path_fallback_prog.py",
            """
            import os

            def main() -> None:
                home = os.path
                print(os.path.dirname("/tmp/demo.txt"))
                print(home.dirname("/tmp/demo.txt"))

            if __name__ == "__main__":
                main()
            """,
        )
        exe = os.path.join(self.td, "os_path_fallback.out")
        compile_python(src, exe)
        run = subprocess.run(
            [exe],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(run.returncode, 0, msg=run.stderr)
        self.assertEqual(run.stdout, "/tmp\n/tmp\n")

    def test_compiled_os_path_join_bridges_cpython_path_values(self):
        from pcc.frontends.python.pipeline import compile_python

        src = self._write(
            "os_path_join_cpython_prog.py",
            """
            import os
            import tempfile

            def main() -> None:
                with tempfile.TemporaryDirectory() as tmp:
                    print(os.path.join(tmp, "x.txt"))

            if __name__ == "__main__":
                main()
            """,
        )
        exe = os.path.join(self.td, "os_path_join_cpython.out")
        compile_python(src, exe)
        run = subprocess.run(
            [exe],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(run.returncode, 0, msg=run.stderr)
        self.assertTrue(run.stdout.strip().endswith("/x.txt"))

    def test_compiled_os_env_subset_is_native(self):
        from pcc.frontends.python.pipeline import compile_python

        src = self._write(
            "os_env_prog.py",
            """
            import os

            def main() -> None:
                print(os.getenv("PCC_MISSING_ENV_12345", "default"))
                os.putenv("PCC_TMP_NATIVE_ENV", "hello")
                print(os.getenv("PCC_TMP_NATIVE_ENV", "missing"))
                os.unsetenv("PCC_TMP_NATIVE_ENV")
                print(os.getenv("PCC_TMP_NATIVE_ENV", "gone"))

            if __name__ == "__main__":
                main()
            """,
        )
        exe = os.path.join(self.td, "os_env.out")
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
            env={**os.environ, "PCC_TMP_NATIVE_ENV": "seed"},
        )
        self.assertEqual(run.returncode, 0, msg=run.stderr)
        self.assertEqual(run.stdout, "default\nhello\ngone\n")

    def test_compiled_sys_stream_write_is_native(self):
        from pcc.frontends.python.pipeline import compile_python

        src = self._write(
            "sys_stream_write_prog.py",
            """
            import sys

            def main() -> None:
                sys.stdout.write("out")
                sys.stderr.write("err")

            if __name__ == "__main__":
                main()
            """,
        )
        exe = os.path.join(self.td, "sys_stream_write.out")
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
        self.assertEqual(run.stdout, "out")
        self.assertEqual(run.stderr, "err")

    def test_compiled_chr_builtin_is_native(self):
        from pcc.frontends.python.pipeline import compile_python

        src = self._write(
            "chr_builtin_prog.py",
            """
            def main() -> None:
                print(chr(65) + chr(0x2665))

            if __name__ == "__main__":
                main()
            """,
        )
        exe = os.path.join(self.td, "chr_builtin.out")
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
            encoding="utf-8",
            timeout=20,
        )
        self.assertEqual(run.returncode, 0, msg=run.stderr)
        self.assertEqual(run.stdout, "A♥\n")

    def test_compiled_heterogeneous_dict_literal_keeps_list_method_dispatch(self):
        from pcc.frontends.python.pipeline import compile_python

        src = self._write(
            "hetero_dict_prog.py",
            """
            def main() -> None:
                d = {"path": "", "backend": None, "prog_args": []}
                d["backend"] = "self"
                print(d["backend"])
                d["prog_args"].append("x")
                print(len(d["prog_args"]))

            if __name__ == "__main__":
                main()
            """,
        )
        exe = os.path.join(self.td, "hetero_dict.out")
        compile_python(src, exe)
        run = subprocess.run(
            [exe],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(run.returncode, 0, msg=run.stderr)
        self.assertEqual(run.stdout, "self\n1\n")

    def test_compiled_str_split_with_maxsplit_falls_back_cleanly(self):
        from pcc.frontends.python.pipeline import compile_python

        src = self._write(
            "str_split_maxsplit_prog.py",
            """
            import sys

            def main() -> None:
                print(sys.argv[1].split("=", 1)[1])

            if __name__ == "__main__":
                main()
            """,
        )
        exe = os.path.join(self.td, "str_split_maxsplit.out")
        compile_python(src, exe)
        run = subprocess.run(
            [exe, "--backend=self"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(run.returncode, 0, msg=run.stderr)
        self.assertEqual(run.stdout, "self\n")

    def test_compiled_argparse_entry_receives_cli_args(self):
        from pcc.frontends.python.pipeline import compile_python

        src = self._write(
            "argparse_prog.py",
            """
            import argparse
            import sys

            def main(argv=None) -> int:
                ap = argparse.ArgumentParser()
                ap.add_argument("--x", required=True)
                args = ap.parse_args(argv)
                print(args.x)
                return 0

            if __name__ == "__main__":
                sys.exit(main())
            """,
        )
        exe = os.path.join(self.td, "argparse.out")
        compile_python(src, exe, libpython_mode="auto")
        run = subprocess.run(
            [exe, "--x", "hello"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(run.returncode, 0)
        self.assertEqual(run.stdout, "hello\n")


if __name__ == "__main__":
    unittest.main()
