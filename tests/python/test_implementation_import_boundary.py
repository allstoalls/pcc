"""Bounded discovery, codegen and execution-owner models; no native builds."""
import ast
import re
from pathlib import Path
import struct
import sys
import types
import unittest
from unittest.mock import patch

from pcc.frontends.python import pipeline_import_scan as scan

ROOT = Path(__file__).resolve().parents[2]
CALLER = "pcc/frontends/c/evaluator/owned_execution.py"
HOST_ADAPTER = "pcc.backend.host_owned_load"


class ImportBoundaryTests(unittest.TestCase):
    def test_local_alias_implementation_matrix(self):
        for alias in ("sys", "runtime", "_runtime"):
            imported = "import sys" + (" as " + alias if alias != "sys" else "")
            for operator in ("==", "!="):
                for selected in ("pcc", "cpython", "pypy"):
                    source = ("def run():\n    " + imported + "\n    if " + alias
                              + ".implementation.name " + operator + " '" + selected
                              + "':\n        from package.host import call\n"
                              + "    else:\n        import package.native\n")
                    for implementation in ("pcc", "cpython", "pypy"):
                        filtered = scan._without_inactive_implementation_imports(
                            source, implementation=implementation)
                        active = (selected == implementation) == (operator == "==")
                        self.assertEqual("from package.host import call" in filtered, active)
                        self.assertIn("import package.native", filtered)
                        self.assertEqual(filtered.count("\n"), source.count("\n"))
                        ast.parse(filtered)

    def test_unknown_or_rebound_aliases_preserve_dependencies(self):
        cases = [
            "def run(sys):\n    if sys.implementation.name == 'cpython':\n        import host\n",
            "import sys\nsys = replacement\nif sys.implementation.name == 'cpython':\n    import host\n",
            "import sys\ndef run():\n    if sys.implementation.name == 'cpython':\n        import host\n",
            "import sys\nif sys.implementation.name == 'cpython' and enabled:\n    import host\n",
            "import sys\nif sys.implementation.name == choose():\n    import host\n",
            "import sys\nif (sys.implementation.name == 'cpython'):\n    import host\n",
            "import sys\nif sys.implementation.name == 'cpython' or enabled:\n    import host\n",
            "import sys; mutate()\nif sys.implementation.name == 'cpython':\n    import host\n",
            "import sys\nif sys.implementation.name != 'cpython':\n    import host\n",
            "import sys\nif sys.implementation.name == 'cpython': print(\n    'host')\nimport live\n",
        ]
        for source in cases:
            self.assertEqual(scan._without_inactive_implementation_imports(source), source)

    def test_strings_comments_inline_nested_and_multiline_imports(self):
        source = '''text = """import sys
if sys.implementation.name == 'cpython':
    import fictional
"""
def run():
    import sys as runtime  # source owner
    # comments do not change the binding

    if runtime.implementation.name == 'cpython':
        """A real first statement."""
        from package.host import (
            call,
            other,
        )
        if condition:
            import nested
    import reachable
    import sys as inline_runtime
    if inline_runtime.implementation.name == 'cpython': import inline_host
    import after_inline
'''
        filtered = scan._without_inactive_implementation_imports(source)
        self.assertIn("import fictional", filtered)
        for absent in ("from package.host", "import nested", "import inline_host"):
            self.assertNotIn(absent, filtered)
        self.assertIn("import reachable", filtered)
        self.assertIn("import after_inline", filtered)
        ast.parse(filtered)

    def test_type_checking_and_implementation_masks_compose(self):
        source = "import typing\nif typing.TYPE_CHECKING:\n    import annotation\nimport sys\nif sys.implementation.name == 'cpython':\n    import host\nimport live\n"
        filtered = scan._without_inactive_runtime_imports(source)
        self.assertNotIn("import annotation", filtered)
        self.assertNotIn("import host", filtered)
        self.assertIn("import live", filtered)

    def test_dedented_continuation_stays_with_inactive_suite(self):
        source = "import sys\nif sys.implementation.name == 'cpython':\n    from host import (\nvalue,\n)\n    import nested\nimport live\n"
        filtered = scan._without_inactive_implementation_imports(source)
        self.assertNotIn("from host", filtered)
        self.assertNotIn("import nested", filtered)
        self.assertIn("import live", filtered)
        ast.parse(filtered)

    def test_current_caller_and_explicit_unproven_guard_dependency_edges(self):
        from pcc.frontends.python.pipeline_dependency_closure import _package_import_targets
        before = (
            "import sys\ndef evaluate(load_in_process):\n"
            "    if load_in_process and sys.implementation.name != 'cpython':\n"
            "        raise RuntimeError('host pointer execution unavailable')\n"
            "    if load_in_process:\n"
            "        from pcc.backend.host_owned_load import call_function\n"
        )
        after = (ROOT / CALLER).read_text()
        original_imports = scan._without_inactive_runtime_imports(before)
        native_imports = scan._without_inactive_runtime_imports(after)
        host_imports = scan._without_inactive_implementation_imports(after, implementation="cpython")
        self.assertIn("from " + HOST_ADAPTER, original_imports)
        self.assertNotIn("from " + HOST_ADAPTER, native_imports)
        self.assertNotIn("import ctypes", native_imports)
        self.assertIn("from " + HOST_ADAPTER, host_imports)
        self.assertIn("import ctypes", host_imports)
        self.assertIn("evaluator.emit_executable", native_imports)
        self.assertIn("subprocess.run", native_imports)
        targets = _package_import_targets(str(ROOT / CALLER), "pcc.frontends.c.evaluator.owned_execution", root_dir=str(ROOT))
        names = [name for _, name in targets]
        self.assertNotIn(HOST_ADAPTER, names)
        self.assertIn("pcc.frontends.c.evaluator.c_evaluator", names)
        self.assertIn("pcc.frontends.python.pipeline_ir_text", names)

    def test_small_native_ir_omits_unavailable_host_import(self):
        from pcc.frontends.python.py_lift import parse_and_lift
        from pcc.frontends.python.type_infer import infer_module
        from pcc.frontends.python.codegen.layer1 import L1CodeGen
        source = '''def run():
    import sys as execution_runtime
    if execution_runtime.implementation.name == "cpython":
        from unavailable_host_adapter import call_function
        return call_function()
    return 17
'''
        module = infer_module(parse_and_lift(source, "boundary_probe.py", "boundary_probe"))
        generator = L1CodeGen(module, emit_cpy_main_exitcode=False, ir_scaffold_mode="on")
        generator._strict_no_libpython = True
        result = str(generator.generate())
        self.assertNotIn("unavailable_host_adapter", result)
        match = re.search(r"^define[^\n]*@user_boundary_probe_run\([^\n]*\) \{", result, re.MULTILINE)
        self.assertIsNotNone(match)
        body = result[match.start():].split("\n}\n", 1)[0]
        self.assertNotIn("@py_cpy_", body)
        self.assertNotRegex(body, r"\bcall[^\n]*call_function")
        self.assertIn("inttoptr i64 35 to ptr", body)

    def test_current_caller_execution_owner_controls(self):
        from pcc.frontends.c.evaluator import owned_execution
        import pcc.backend.host_owned_load as loader
        evaluate = owned_execution.evaluate_units
        pointer_units = [("pointer.c", 'target triple = "arm64-apple-darwin"\n' + "define ptr @entry() {\nentry:\n  ret ptr null\n}\n", ("ptr", None), [])]
        scalar_units = [("scalar.c", 'target triple = "arm64-apple-darwin"\n' + "define i32 @entry() {\nentry:\n  ret i32 -1234\n}\n", ("int", 32, True), [])]
        controls = []
        class Evaluator:
            is_cross = False
            target_triple = "arm64-apple-darwin"
            def compile_translation_units(self, units, **kwargs):
                self.wrapper = units[0].source
                return []
            def emit_executable(self, units, path, **kwargs):
                self.emitted = path
        for implementation in ("cpython", "pcc"):
            descriptor = dict(vars(sys.implementation))
            descriptor["name"] = implementation
            with patch.object(sys, "implementation", types.SimpleNamespace(**descriptor)):
                evaluator = Evaluator()
                with patch.object(loader, "call_function", return_value=991) as call:
                    if implementation == "cpython":
                        self.assertEqual(evaluate(evaluator, pointer_units, entry="entry", optimize=False), 991)
                        self.assertEqual(call.call_count, 1)
                        self.assertEqual(call.call_args.args[3], "__pcc_evaluate_entry")
                    else:
                        with self.assertRaisesRegex(owned_execution.BackendUnavailable, "owned host pointer execution is unavailable"):
                            evaluate(evaluator, pointer_units, entry="entry", optimize=False)
                        call.assert_not_called()
                def completed(command, **kwargs):
                    self.assertEqual(command, [evaluator.emitted])
                    Path(evaluator.emitted).with_name("result.bin").write_bytes(struct.pack("<i", -1234))
                    return types.SimpleNamespace(returncode=0)
                with patch.object(owned_execution.subprocess, "run", side_effect=completed), patch.object(loader, "call_function", side_effect=AssertionError("unexpected host adapter")):
                    self.assertEqual(evaluate(evaluator, scalar_units, entry="entry", optimize=False), -1234)
                    self.assertIn("fwrite", evaluator.wrapper)
                controls.append({"source": "current", "implementation": implementation,
                                 "pointer": "host adapter dispatched" if implementation == "cpython" else "explicit capability error",
                                 "scalar": "emitted-file result transport model passed"})
        self.assertEqual(len(controls), 2)

if __name__ == "__main__":
    unittest.main(verbosity=2)
