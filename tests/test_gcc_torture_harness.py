"""Pure configuration/mocked-call checks; never construct a real compiler."""

from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests import gcc_torture_cases as harness


def owned_compiler_mock():
    compiler = Mock(backend="self", is_cross=False)
    compiler._normalize_opt_level.return_value = 2
    return compiler


class GccTortureOptionPolicyTests(unittest.TestCase):
    def test_modern_default_and_linux_math_linkage(self):
        options = harness.gcc_torture_comparison_options("int main(void) {}", platform="linux")
        self.assertEqual(options.standard, "-std=gnu17")
        self.assertEqual(options.link_args, ("-lm",))
        self.assertEqual(options.unmodeled_directives, ())

    def test_non_linux_does_not_receive_libm(self):
        for platform in ("darwin", "win32"):
            with self.subTest(platform=platform):
                options = harness.gcc_torture_comparison_options("", platform=platform)
                self.assertEqual(options.link_args, ())

    def test_retained_old_language_override(self):
        for directive in ("dg-options", "dg-additional-options"):
            with self.subTest(directive=directive):
                options = harness.gcc_torture_comparison_options(
                    f'/* {{ {directive} "-std=gnu89" }} */'
                )
                self.assertEqual(options.standard, "-std=gnu89")

    def test_additional_standard_follows_base_options(self):
        options = harness.gcc_torture_comparison_options(
            '/* { dg-options "-std=c11" } */\n'
            '/* { dg-additional-options "-std=gnu17" } */'
        )
        self.assertEqual(options.standard, "-std=gnu17")

    def test_last_standard_in_one_option_string_wins(self):
        options = harness.gcc_torture_comparison_options(
            '/* { dg-options "-std=gnu89 -std=c99" } */'
        )
        self.assertEqual(options.standard, "-std=c99")

    def test_ansi_is_c89_on_both_paths(self):
        options = harness.gcc_torture_comparison_options('/* { dg-options "-ansi" } */')
        self.assertEqual(options.standard, "-std=c89")

    def test_unknown_standard_is_not_replaced_by_default(self):
        options = harness.gcc_torture_comparison_options(
            '/* { dg-options "-std=unavailable-dialect" } */'
        )
        self.assertEqual(options.standard, "-std=unavailable-dialect")

    def test_conditional_standard_is_explicitly_rejected(self):
        for selector in ("{ target x86_64-*-* }", "{ target { ! lp64 } }"):
            with self.subTest(selector=selector):
                with self.assertRaisesRegex(ValueError, "requires target evaluation"):
                    harness.gcc_torture_comparison_options(
                        '/* { dg-options "-std=c99" ' + selector + ' } */'
                    )

    def test_malformed_and_ambiguous_standards_are_rejected(self):
        for text in (
            '/* { dg-options "-std=" } */',
            '/* { dg-options "-std c99" } */',
            '/* { dg-options { "-std=c99" } } */',
            '/* { dg-options "-std=c99" */',
            '/* { dg-options "-std=c99" } { dg-options "-std=c11" } */',
        ):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    harness.gcc_torture_comparison_options(text)

    def test_c_string_and_character_literals_are_not_directives(self):
        options = harness.gcc_torture_comparison_options(
            r'''char *s = "/* { dg-options \"-std=gnu89\" } */"; char c = '/';'''
        )
        self.assertEqual(options.standard, "-std=gnu17")

    def test_nonstandard_options_and_prerequisites_remain_visible(self):
        options = harness.gcc_torture_comparison_options(
            '/* { dg-additional-options "-std=gnu17 -fpermissive" } */\n'
            '/* { dg-options "-mno-stv" { target x86_64-*-* } } */\n'
            '/* { dg-require-effective-target label_values } */'
        )
        self.assertEqual(options.standard, "-std=gnu17")
        self.assertEqual(len(options.unmodeled_directives), 3)
        self.assertIn("-fpermissive", options.unmodeled_directives[0])
        self.assertIn("target x86_64", options.unmodeled_directives[1])
        self.assertIn("label_values", options.unmodeled_directives[2])

    def test_quoted_brace_does_not_end_directive(self):
        options = harness.gcc_torture_comparison_options(
            '/* { dg-options "-DSTR=\\\"}\\\" -std=c99" } */'
        )
        self.assertEqual(options.standard, "-std=c99")
        self.assertEqual(len(options.unmodeled_directives), 1)


class GccTortureMockedCallTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="pcc_torture_policy_")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.case = self.root / "case.c"
        self.case.write_text(
            '/* { dg-additional-options "-std=gnu89" } */\n'
            "int main() { return 0; }\n"
        )
        harness.compile_native.cache_clear()
        harness.run_native.cache_clear()
        self.addCleanup(harness.compile_native.cache_clear)
        self.addCleanup(harness.run_native.cache_clear)

    def test_reference_compile_uses_dialect_and_link_order(self):
        expected = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(harness, "_host_cc", return_value="reference-cc"), \
             patch.object(harness.sys, "platform", "linux"), \
             patch.object(harness.subprocess, "run", return_value=expected) as run:
            self.assertIs(harness.compile_native(self.case, self.root), expected)
        argv = run.call_args.args[0]
        self.assertEqual(argv[0:2], ["reference-cc", "-std=gnu89"])
        self.assertEqual(argv[-1], "-lm")
        self.assertLess(argv.index(str(self.case)), argv.index("-lm"))
        self.assertIn("-Wno-error=implicit-int", argv)
        self.assertIn("-Wno-error=implicit-function-declaration", argv)

    def test_reference_run_only_after_successful_compile(self):
        compiled = subprocess.CompletedProcess([], 0, "", "")
        linked = subprocess.CompletedProcess([], 0, "", "")
        executed = subprocess.CompletedProcess([], 7, "program output", "program stderr")
        with patch.object(harness, "_host_cc", return_value="reference-cc"), \
             patch.object(harness.sys, "platform", "linux"), \
             patch.object(harness.subprocess, "run", side_effect=[compiled, linked, executed]) as run:
            result = harness.run_native(self.case, self.root)
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (executed.returncode, executed.stdout, executed.stderr))
        self.assertTrue(result.executed)
        self.assertEqual([stage.stage for stage in result.stages], ["compile", "link", "run"])
        self.assertEqual(run.call_count, 3)
        self.assertIn("-std=gnu89", run.call_args_list[0].args[0])
        self.assertIn("-c", run.call_args_list[0].args[0])
        self.assertEqual(run.call_args_list[1].args[0][-1], "-lm")
        self.assertEqual(len(run.call_args_list[2].args[0]), 1)

    def test_reference_unsupported_option_failure_is_unchanged(self):
        self.case.write_text('/* { dg-options "-std=unavailable-dialect" } */')
        rejected = subprocess.CompletedProcess([], 1, "", "unsupported language standard")
        with patch.object(harness, "_host_cc", return_value="reference-cc"), \
             patch.object(harness.subprocess, "run", return_value=rejected) as run:
            result = harness.run_native(self.case, self.root)
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (rejected.returncode, rejected.stdout, rejected.stderr))
        self.assertFalse(result.executed)
        self.assertEqual(result.stages[-1].stage, "compile")
        self.assertEqual(run.call_count, 1)
        self.assertIn("-std=unavailable-dialect", run.call_args.args[0])

    def test_pcc_compile_receives_matching_preprocessor_standard(self):
        compiler = Mock()
        connection = Mock()
        with patch.object(harness, "CEvaluator", return_value=compiler):
            harness._pcc_worker_entry("compile", str(self.case), 20, connection)
        self.assertEqual(
            compiler.compile_translation_units.call_args.kwargs["cpp_args"],
            ["-std=gnu89"],
        )
        compiler.run_translation_units_with_system_cc.assert_not_called()
        connection.send.assert_called_once_with({"returncode": 0, "stdout": "", "stderr": "", "stages": [
            {"stage": "compile", "returncode": 0, "stdout": "", "stderr": "", "completed": True},
        ]})
        connection.close.assert_called_once()

    def test_pcc_explicit_system_link_receives_matching_standard_and_libm(self):
        # Retain the original node identity while checking the migrated owner.
        compiler = owned_compiler_mock()
        executed = subprocess.CompletedProcess([], 9, "out", "err")
        connection = Mock()
        with patch.object(harness, "CEvaluator", return_value=compiler), \
             patch.object(harness.sys, "platform", "linux"), \
             patch.dict(harness.os.environ, {"PCC_RUNTIME_ARCHIVE": "/model/runtime.a"}), \
             patch.object(harness.subprocess, "run", return_value=executed):
            harness._pcc_worker_entry("run", str(self.case), 20, connection)
        kwargs = compiler.compile_translation_units.call_args.kwargs
        self.assertEqual(kwargs["cpp_args"], ["-std=gnu89"])
        self.assertFalse(kwargs["use_system_cpp"])
        self.assertIsNone(compiler.emit_executable.call_args.kwargs["link_args"])
        compiler.run_translation_units_with_system_cc.assert_not_called()
        payload = connection.send.call_args.args[0]
        self.assertEqual((payload["returncode"], payload["stdout"], payload["stderr"]), (9, "out", "err"))
        self.assertEqual([stage["stage"] for stage in payload["stages"]], ["compile", "link", "run"])
        connection.close.assert_called_once()

    def test_pcc_unsupported_option_remains_a_failure(self):
        self.case.write_text('/* { dg-options "-std=unavailable-dialect" } */')
        compiler = Mock()
        compiler.compile_translation_units.side_effect = ValueError(
            "unsupported owned preprocessor option: -std=unavailable-dialect"
        )
        connection = Mock()
        with patch.object(harness, "CEvaluator", return_value=compiler):
            harness._pcc_worker_entry("compile", str(self.case), 20, connection)
        self.assertEqual(
            compiler.compile_translation_units.call_args.kwargs["cpp_args"],
            ["-std=unavailable-dialect"],
        )
        self.assertEqual(connection.send.call_args.args[0]["returncode"], 1)
        self.assertIn("unsupported owned preprocessor", connection.send.call_args.args[0]["stderr"])
        connection.close.assert_called_once()

    def test_unmodeled_directive_is_reported_without_modifying_source(self):
        source = '/* { dg-require-effective-target label_values } */\nint main(void) {}'
        self.case.write_text(source)
        with self.assertWarnsRegex(harness.GccTortureDirectiveWarning, "label_values"):
            options = harness.gcc_torture_case_options(self.case)
        self.assertEqual(options.standard, "-std=gnu17")
        self.assertEqual(self.case.read_text(), source)


class GccTortureSelfLaneMockedTests(unittest.TestCase):
    def setUp(self):
        from tests.c import test_gcc_torture_self

        self.lane = test_gcc_torture_self
        self.directory = tempfile.TemporaryDirectory(prefix="pcc_self_lane_policy_")
        self.addCleanup(self.directory.cleanup)
        self.case = Path(self.directory.name) / "case.c"
        self.case.write_text('/* { dg-additional-options "-std=c99" } */')
        self.lane._run_backend.cache_clear()
        self.addCleanup(self.lane._run_backend.cache_clear)

    def test_explicit_self_lane_receives_shared_dialect_and_linux_libm(self):
        compiler = owned_compiler_mock()
        executed = subprocess.CompletedProcess([], 13, "out", "err")
        with patch.object(self.lane, "CEvaluator", return_value=compiler) as factory, \
             patch.object(harness.sys, "platform", "linux"), \
             patch.dict(harness.os.environ, {"PCC_RUNTIME_ARCHIVE": "/model/runtime.a"}), \
             patch.object(harness.subprocess, "run", return_value=executed) as run:
            result = self.lane._run_self_backend(self.case, timeout=17)
        factory.assert_called_once_with(backend="self", allow_unimplemented_backend=True)
        kwargs = compiler.compile_translation_units.call_args.kwargs
        self.assertEqual(kwargs["cpp_args"], ["-std=c99"])
        self.assertFalse(kwargs["use_system_cpp"])
        self.assertIsNone(compiler.emit_executable.call_args.kwargs["link_args"])
        self.assertEqual(run.call_args.kwargs["timeout"], 17)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (13, "out", "err"))
        self.assertTrue(result.executed)
        compiler.run_translation_units_with_system_cc.assert_not_called()

    def test_non_linux_self_lane_has_no_added_math_library(self):
        compiler = owned_compiler_mock()
        with patch.object(self.lane, "CEvaluator", return_value=compiler), \
             patch.object(harness.sys, "platform", "darwin"), \
             patch.dict(harness.os.environ, {"PCC_RUNTIME_ARCHIVE": "/model/runtime.a"}), \
             patch.object(harness.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            self.lane._run_self_backend(self.case)
        kwargs = compiler.compile_translation_units.call_args.kwargs
        self.assertEqual(kwargs["cpp_args"], ["-std=c99"])
        self.assertIsNone(compiler.emit_executable.call_args.kwargs["link_args"])
        compiler.run_translation_units_with_system_cc.assert_not_called()

    def test_self_lane_does_not_mask_unsupported_standard(self):
        self.case.write_text('/* { dg-options "-std=unavailable-dialect" } */')
        compiler = owned_compiler_mock()
        compiler.compile_translation_units.side_effect = ValueError(
            "unsupported owned preprocessor option: -std=unavailable-dialect"
        )
        with patch.object(self.lane, "CEvaluator", return_value=compiler), \
             patch.dict(harness.os.environ, {"PCC_RUNTIME_ARCHIVE": "/model/runtime.a"}):
            result = self.lane._run_self_backend(self.case)
        kwargs = compiler.compile_translation_units.call_args.kwargs
        self.assertEqual(kwargs["cpp_args"], ["-std=unavailable-dialect"])
        self.assertEqual(result.returncode, 1)
        self.assertIn("unsupported owned preprocessor", result.stderr)
        self.assertFalse(result.executed)
        self.assertEqual(result.stages[-1].stage, "compile")
        compiler.run_translation_units_with_system_cc.assert_not_called()

    def test_conditional_standard_blocks_before_compiler_construction(self):
        self.case.write_text(
            '/* { dg-options "-std=c99" { target x86_64-*-* } } */'
        )
        with patch.object(self.lane, "CEvaluator") as factory:
            result = self.lane._run_self_backend(self.case)
        factory.assert_not_called()
        self.assertEqual(result.returncode, 1)
        self.assertIn("requires target evaluation", result.stderr)

    def test_historically_named_llvm_lane_delegates_to_shared_pcc_worker(self):
        expected = harness.PccCompileResult(5, "out", "err")
        with patch.object(self.lane, "run_pcc", return_value=expected) as worker:
            result = self.lane._run_llvm_backend(self.case, timeout=11)
        worker.assert_called_once_with(self.case, self.lane.REPO_ROOT, 11)
        self.assertIs(result, expected)

    def test_backend_selection_model_has_self_default_and_no_llvm_fallback(self):
        from pcc.backend import resolve_backend

        with patch.dict(harness.os.environ, {}, clear=True):
            self.assertEqual(resolve_backend().kind, "self")
            with self.assertRaisesRegex(ValueError, "unknown backend 'llvm'"):
                resolve_backend("llvm")


if __name__ == "__main__":
    unittest.main()
