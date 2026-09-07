"""Regression tests for ``compile_python_multi`` — the multi-file
compile infrastructure (#138.5) that feeds the three-stage bootstrap.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from tests.runtime_build_cache import cached_c_runtime


class MultiFileCompileTests(unittest.TestCase):
    def _run_multi(self, files, entry_module, module_names=None):
        """Write ``files`` (dict of relpath → source) to a tempdir,
        multi-compile them, run the resulting binary, and return
        (exe_path, stdout, exit_code).

        ``files`` is a dict to preserve source file → content mapping;
        the parallel ``module_names`` list (when supplied) tells the
        pipeline the dotted module name each source file should
        simulate. Default: filename stem.
        """
        from pcc.py_frontend.pipeline import compile_python_multi

        td = tempfile.mkdtemp(prefix="pcc_multi_test_")
        self.addCleanup(self._rmtree, td)
        src_paths = []
        for rel, source in files.items():
            dst = os.path.join(td, rel)
            os.makedirs(os.path.dirname(dst) or td, exist_ok=True)
            with open(dst, "w", encoding="utf-8") as fh:
                fh.write(textwrap.dedent(source).lstrip())
            src_paths.append(dst)
        exe = os.path.join(td, "a.out")
        compile_python_multi(
            src_paths,
            exe,
            entry_module=entry_module,
            module_names=module_names,
        )
        r = subprocess.run([exe], capture_output=True, text=True, timeout=20)
        return exe, r.stdout, r.returncode

    def _rmtree(self, path):
        import shutil

        shutil.rmtree(path, ignore_errors=True)

    def test_from_import_computed_raw_int_uses_importer_owned_scalar_slot(self):
        from pcc.py_frontend.pipeline import compile_python_multi

        td = tempfile.mkdtemp(prefix="pcc_multi_imported_raw_int_")
        self.addCleanup(self._rmtree, td)
        provider = os.path.join(td, "provider.py")
        consumer = os.path.join(td, "consumer.py")
        with open(provider, "w", encoding="utf-8") as fh:
            fh.write(
                "BASE = 1 << 2\n"
                "EXTRA = 1 << 3\n"
                "FLAG = BASE | EXTRA\n"
                "__all__ = ['BASE', 'EXTRA', 'FLAG']\n"
            )
        with open(consumer, "w", encoding="utf-8") as fh:
            fh.write(
                "from pcc.provider import BASE, EXTRA, FLAG\n"
                "COMBINED = FLAG | BASE | EXTRA\n"
                "def read() -> int:\n"
                "    return COMBINED\n"
            )

        out_ll = os.path.join(td, "pair.ll")
        compile_python_multi(
            [consumer, provider],
            out_ll,
            module_names=["pcc.consumer", "pcc.provider"],
            entry_module="pcc.consumer",
            emit_llvm_only=True,
            ir_scaffold_mode="on",
            libpython_mode="off",
            recursive_stdlib=False,
        )
        with open(out_ll, "r", encoding="utf-8") as fh:
            ir_text = fh.read()

        self.assertIn("@.modvar.pcc_provider.FLAG = global i64 0", ir_text)
        self.assertIn("@.modvar.pcc_provider.BASE = global i64 0", ir_text)
        self.assertIn("@.modvar.pcc_provider.EXTRA = global i64 0", ir_text)
        self.assertIn("@.modvar.pcc_consumer.FLAG = global i64 0", ir_text)
        self.assertIn("@.modvar.pcc_consumer.COMBINED = global i64 0", ir_text)
        binding_functions = []
        for function_match in re.finditer(r"(?m)^define [^{]+ \{", ir_text):
            function_body = ir_text[function_match.start() :]
            function_body = function_body[: function_body.index("\n}")]
            if "@.modvar.pcc_consumer.COMBINED" not in function_body:
                continue
            if "store i64" not in function_body and "@pcc_gc_store_root" not in function_body:
                continue
            binding_functions.append(function_body)
        self.assertEqual(len(binding_functions), 1)
        binding_function = binding_functions[0]
        binding = binding_function.rindex("@.modvar.pcc_consumer.COMBINED")
        binding_window = binding_function[max(0, binding - 800) : binding + 400]
        self.assertNotIn("@pcc_gc_pin", binding_window)
        self.assertNotIn("@pcc_gc_store_root", binding_window)
        self.assertRegex(
            binding_window,
            r"store i64 [^,]+, ptr @\.modvar\.pcc_consumer\.COMBINED",
        )
        consumer_fini = ir_text.index(
            "define void @_pcc_py_module_fini_pcc_consumer"
        )
        consumer_fini_end = ir_text.index("\n}", consumer_fini)
        self.assertNotIn(
            "@.modvar.pcc_provider.FLAG",
            ir_text[consumer_fini:consumer_fini_end],
        )

    def test_entry_only_no_siblings(self):
        _, out, code = self._run_multi(
            {"entry.py": "print(1)\nprint(2)\n"},
            entry_module="entry",
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, "1\n2\n")

    def test_native_extension_literal_module_dependency_enters_closure(self):
        from pcc.py_frontend import pipeline

        td = tempfile.mkdtemp(prefix="pcc_multi_extension_literal_import_")
        self.addCleanup(self._rmtree, td)
        pkg_dir = os.path.join(td, "pkg")
        os.makedirs(pkg_dir, exist_ok=True)
        entry = os.path.join(td, "entry.py")
        with open(entry, "w", encoding="utf-8") as fh:
            fh.write("import pkg.native\n")
        with open(os.path.join(pkg_dir, "__init__.py"), "w", encoding="utf-8") as fh:
            fh.write("")
        hidden = os.path.join(pkg_dir, "hidden.py")
        with open(hidden, "w", encoding="utf-8") as fh:
            fh.write("VALUE = 42\n")
        extension = os.path.join(pkg_dir, "native.pcc_native-test.so")
        with open(extension, "wb") as fh:
            fh.write(b"\x00pkg.hidden\x00not.a.real.module\x00")

        with mock.patch.dict(os.environ, {"PCC_PACKAGE_SITE": td}):
            _srcs, modules = pipeline._collect_multi_source_relative_closure(
                [entry],
                ["entry"],
            )

        self.assertIn("pkg.hidden", modules)
        self.assertNotIn("not.a.real.module", modules)

    def test_module_scope_same_package_absolute_import_enters_closure(self):
        from pcc.py_frontend import pipeline

        td = tempfile.mkdtemp(prefix="pcc_multi_absolute_import_")
        self.addCleanup(self._rmtree, td)
        pkg_dir = os.path.join(td, "pkg")
        os.makedirs(pkg_dir, exist_ok=True)
        entry = os.path.join(pkg_dir, "entry.py")
        with open(entry, "w", encoding="utf-8") as fh:
            fh.write("from pkg.provider import Thing\n")
            fh.write("def later():\n    from pkg.lazy import Lazy\n")
        with open(os.path.join(pkg_dir, "provider.py"), "w", encoding="utf-8") as fh:
            fh.write("class Thing:\n    pass\n")
        with open(os.path.join(pkg_dir, "lazy.py"), "w", encoding="utf-8") as fh:
            fh.write("class Lazy:\n    pass\n")

        _srcs, modules = pipeline._collect_multi_source_relative_closure(
            [entry],
            ["pkg.entry"],
        )

        self.assertIn("pkg.provider", modules)
        self.assertNotIn("pkg.lazy", modules)

    def test_multi_compile_backend_self_uses_self_link(self):
        from pcc.py_frontend.pipeline import compile_python_multi

        td = tempfile.mkdtemp(prefix="pcc_multi_backend_self_")
        self.addCleanup(self._rmtree, td)
        src = os.path.join(td, "entry.py")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write("print(1)\n")
        exe = os.path.join(td, "a.out")
        with mock.patch(
            "pcc.py_frontend.pipeline._ensure_runtime",
            return_value="/tmp/libpy_runtime.a",
        ):
            with mock.patch("pcc.py_frontend.pipeline._link_with_clang") as clang_link:
                with mock.patch(
                    "pcc.py_frontend.pipeline._link_with_self_backend_ir_texts"
                ) as self_link:
                    compile_python_multi(
                        [src],
                        exe,
                        entry_module="entry",
                        backend="self",
                    )

        self_link.assert_called_once()
        clang_link.assert_not_called()

    def test_multi_compile_direct_indexed_assembly_runs(self):
        from pcc.py_frontend.pipeline import compile_python_multi

        td = tempfile.mkdtemp(prefix="pcc_multi_direct_indexed_")
        self.addCleanup(self._rmtree, td)
        src = os.path.join(td, "entry.py")
        lib = os.path.join(td, "lib.py")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write("from .lib import add\nprint(add(2, 3))\n")
        with open(lib, "w", encoding="utf-8") as fh:
            fh.write(
                "def add(left: int, right: int) -> int:\n"
                "    return left + right\n"
                "\n"
                "def deferred(value):\n"
                "    return locals()\n"
            )
        exe = os.path.join(td, "a.out")
        profile = {}
        direct_env = {
            "PCC_DIRECT_INDEXED_KERNEL_CAPTURE": "1",
            "PCC_DIRECT_INDEXED_KERNEL_EMIT": "1",
            "PCC_DIRECT_INDEXED_KERNEL_REQUIRE_ZERO_FALLBACK": "1",
            "PCC_DIRECT_INDEXED_KERNEL_FUSE_USES": "1",
            "PCC_DIRECT_INDEXED_KERNEL_RELEASE_FRONTEND": "1",
            "PCC_PYTHON_IR_PASSES": "off",
            "PCC_SELF_LINK": "pcc",
            "PCC_RUNTIME_ARCHIVE": str(cached_c_runtime() / "libpy_runtime.a"),
        }
        with mock.patch.dict(os.environ, direct_env):
            compile_python_multi(
                [src, lib],
                exe,
                entry_module="pkg.entry",
                module_names=["pkg.entry", "pkg.lib"],
                backend="self",
                libpython_mode="off",
                ir_scaffold_mode="on",
                profile=profile,
            )
        result = subprocess.run(
            [exe],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "5\n")
        self.assertEqual(profile["counters"]["multi_direct_native_object_modules"], 2)
        self.assertGreater(profile["counters"]["multi_direct_native_object_bytes"], 0)
        self.assertEqual(profile["counters"]["multi_direct_ir_text_bytes"], 0)
        for phase in (
            "assemble_pool",
            "decode_pco",
            "prepare_link",
            "sign",
            "write",
            "validate",
        ):
            self.assertIn("link_macho_" + phase + "_ms", profile["counters"])

    def test_multi_compile_direct_assembly_path_handoff_runs(self):
        from pcc.py_frontend.pipeline import compile_python_multi

        td = tempfile.mkdtemp(prefix="pcc_multi_direct_asm_path_")
        self.addCleanup(self._rmtree, td)
        src = os.path.join(td, "entry.py")
        lib = os.path.join(td, "lib.py")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write("from .lib import add\nprint(add(20, 22))\n")
        with open(lib, "w", encoding="utf-8") as fh:
            fh.write(
                "def add(left: int, right: int) -> int:\n"
                "    return left + right\n"
            )
        exe = os.path.join(td, "a.out")
        profile = {}
        direct_env = {
            "PCC_DIRECT_INDEXED_KERNEL_CAPTURE": "1",
            "PCC_DIRECT_INDEXED_KERNEL_EMIT": "1",
            "PCC_DIRECT_INDEXED_KERNEL_REQUIRE_ZERO_FALLBACK": "1",
            "PCC_DIRECT_INDEXED_KERNEL_FUSE_USES": "1",
            "PCC_DIRECT_INDEXED_KERNEL_RELEASE_FRONTEND": "1",
            "PCC_DIRECT_INDEXED_NATIVE_OBJECT": "0",
            "PCC_PYTHON_IR_PASSES": "off",
            "PCC_SELF_LINK": "pcc",
            "PCC_RUNTIME_ARCHIVE": str(cached_c_runtime() / "libpy_runtime.a"),
        }
        with mock.patch.dict(os.environ, direct_env):
            compile_python_multi(
                [src, lib],
                exe,
                entry_module="pkg.entry",
                module_names=["pkg.entry", "pkg.lib"],
                backend="self",
                libpython_mode="off",
                ir_scaffold_mode="on",
                profile=profile,
            )

        result = subprocess.run(
            [exe],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "42\n")
        self.assertEqual(profile["counters"]["multi_direct_assembly_modules"], 2)
        self.assertEqual(profile["counters"]["multi_direct_native_object_modules"], 0)
        self.assertEqual(profile["counters"]["multi_direct_ir_text_bytes"], 0)

    def test_multi_compile_deferred_direct_link_runs(self):
        from pcc.py_frontend.pipeline import compile_python_multi

        td = tempfile.mkdtemp(prefix="pcc_multi_deferred_link_")
        self.addCleanup(self._rmtree, td)
        src = os.path.join(td, "entry.py")
        lib = os.path.join(td, "lib.py")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write("from .lib import add\nprint(add(20, 22))\n")
        with open(lib, "w", encoding="utf-8") as fh:
            fh.write(
                "def add(left: int, right: int) -> int:\n"
                "    return left + right\n"
            )
        exe = os.path.join(td, "a.out")
        plan = os.path.join(td, "a.out.pcc-link-plan")
        direct_env = {
            "PCC_DIRECT_INDEXED_KERNEL_CAPTURE": "1",
            "PCC_DIRECT_INDEXED_KERNEL_EMIT": "1",
            "PCC_DIRECT_INDEXED_KERNEL_REQUIRE_ZERO_FALLBACK": "1",
            "PCC_DIRECT_INDEXED_KERNEL_FUSE_USES": "1",
            "PCC_DIRECT_INDEXED_KERNEL_RELEASE_FRONTEND": "1",
            "PCC_DIRECT_INDEXED_NATIVE_OBJECT": "0",
            "PCC_DEFER_SELF_LINK_PLAN": plan,
            "PCC_PYTHON_IR_PASSES": "off",
            "PCC_SELF_LINK": "pcc",
            "PCC_RUNTIME_ARCHIVE": str(cached_c_runtime() / "libpy_runtime.a"),
        }
        with mock.patch.dict(os.environ, direct_env):
            compile_python_multi(
                [src, lib],
                exe,
                entry_module="pkg.entry",
                module_names=["pkg.entry", "pkg.lib"],
                backend="self",
                libpython_mode="off",
                ir_scaffold_mode="on",
            )

        self.assertFalse(os.path.exists(exe))
        self.assertTrue(os.path.isfile(plan))
        helper = os.path.abspath("scripts/run_pcc_deferred_link.py")
        linked = subprocess.run(
            [sys.executable, helper, "--timeout", "30", plan],
            capture_output=True,
            text=True,
            timeout=40,
        )
        self.assertEqual(linked.returncode, 0, linked.stderr)
        result = subprocess.run(
            [exe],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "42\n")

    def test_single_compile_direct_indexed_assembly_runs(self):
        from pcc.py_frontend.pipeline import compile_python

        td = tempfile.mkdtemp(prefix="pcc_single_direct_indexed_")
        self.addCleanup(self._rmtree, td)
        src = os.path.join(td, "entry.py")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write(
                "def add(left: int, right: int) -> int:\n"
                "    return left + right\n"
                "\n"
                "print(add(20, 22))\n"
            )
        exe = os.path.join(td, "a.out")
        direct_env = {
            "PCC_DIRECT_INDEXED_KERNEL_CAPTURE": "1",
            "PCC_DIRECT_INDEXED_KERNEL_EMIT": "1",
            "PCC_DIRECT_INDEXED_KERNEL_REQUIRE_ZERO_FALLBACK": "1",
            "PCC_DIRECT_INDEXED_KERNEL_FUSE_USES": "1",
            "PCC_DIRECT_INDEXED_KERNEL_RELEASE_FRONTEND": "1",
            "PCC_PYTHON_IR_PASSES": "off",
            "PCC_SELF_LINK": "pcc",
            "PCC_RUNTIME_ARCHIVE": str(cached_c_runtime() / "libpy_runtime.a"),
        }
        with mock.patch.dict(os.environ, direct_env):
            compile_python(
                src,
                exe,
                backend="self",
                libpython_mode="off",
                ir_scaffold_mode="on",
            )
        result = subprocess.run(
            [exe],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "42\n")

    def test_unimported_sibling_top_init_does_not_run(self):
        _, out, code = self._run_multi(
            {
                "entry.py": 'print("entry")\n',
                "lib.py": 'print("lib")\n',
            },
            entry_module="entry",
        )
        self.assertEqual(code, 0)
        # Compiled siblings are registered before entry execution, but Python
        # module code runs only when imported.  Eagerly executing every source
        # breaks package-cycle partial-state semantics and makes an unimported
        # module observable.
        self.assertEqual(out, "entry\n")

    def test_sibling_top_init_has_once_guard(self):
        from pcc.py_frontend.pipeline import compile_python_multi

        td = tempfile.mkdtemp(prefix="pcc_multi_once_guard_")
        self.addCleanup(self._rmtree, td)
        entry = os.path.join(td, "entry.py")
        lib = os.path.join(td, "lib.py")
        with open(entry, "w", encoding="utf-8") as fh:
            fh.write("from .lib import greet\ngreet()\n")
        with open(lib, "w", encoding="utf-8") as fh:
            fh.write('print("lib")\ndef greet() -> None:\n    print("greet")\n')

        out_ll = os.path.join(td, "combined.ll")
        compile_python_multi(
            [entry, lib],
            out_ll,
            entry_module="pkg.entry",
            module_names=["pkg.entry", "pkg.lib"],
            emit_llvm_only=True,
        )

        ir_text = open(out_ll, "r", encoding="utf-8").read()
        init_guard = re.search(
            r"@(?P<symbol>(?:__pcp\d+_)?\.pcc\.module\.init\.pkg_lib) "
            r"= (?:internal )?global i32 0\b",
            ir_text,
        )
        self.assertIsNotNone(init_guard)
        self.assertIn("%mod.init.seen", ir_text)
        symbol = re.escape(init_guard.group("symbol"))
        self.assertRegex(ir_text, rf"store i32 1, ptr @{symbol}\b")

    def test_cross_module_function_call(self):
        files = {
            "entry.py": (
                "from .lib import banner\n"
                'print("start")\n'
                "banner()\n"
                'print("done")\n'
            ),
            "lib.py": ("def banner() -> None:\n" '    print("banner called")\n'),
        }
        _, out, code = self._run_multi(
            files,
            entry_module="pkg.entry",
            module_names=["pkg.entry", "pkg.lib"],
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, "start\nbanner called\ndone\n")

    def test_cross_module_none_return_extern_uses_void_abi(self):
        from pcc.py_frontend import pipeline

        td = tempfile.mkdtemp(prefix="pcc_multi_none_return_")
        self.addCleanup(self._rmtree, td)
        entry = os.path.join(td, "entry.py")
        lib = os.path.join(td, "lib.py")
        with open(entry, "w", encoding="utf-8") as fh:
            fh.write("from .lib import touch\n\ntouch()\n")
        with open(lib, "w", encoding="utf-8") as fh:
            fh.write("def touch() -> None:\n" "    return\n")

        out_ll = os.path.join(td, "none_return.ll")
        pipeline.compile_python_multi(
            [entry, lib],
            out_ll,
            entry_module="pkg.entry",
            module_names=["pkg.entry", "pkg.lib"],
            emit_llvm_only=True,
            libpython_mode="off",
        )
        with open(out_ll, "r", encoding="utf-8") as fh:
            ir_text = fh.read()

        sym = "user_pkg_lib_touch"
        self.assertRegex(ir_text, rf"declare (?:external )?void @{sym}\(\)")
        self.assertRegex(ir_text, rf"define (?:external )?void @{sym}\(\)")
        self.assertNotRegex(ir_text, rf"declare (?:external )?ptr @{sym}\(\)")
        self.assertNotRegex(ir_text, rf"call ptr (?:\(\) )?@{sym}\(\)")
        self.assertRegex(ir_text, rf"call void (?:\(\) )?@{sym}\(\)")

    def test_tuple_unpack_rebind_to_borrowed_value_does_not_overrelease(self):
        from pcc.py_frontend import pipeline

        td = tempfile.mkdtemp(prefix="pcc_tuple_unpack_owned_flag_")
        self.addCleanup(self._rmtree, td)
        src = os.path.join(td, "entry.py")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write(
                "import os\n"
                "\n"
                "def main() -> None:\n"
                "    pairs: list[tuple[str, str]] = [\n"
                "        ('a', 'pkg.a'),\n"
                "        ('b', 'pkg.b'),\n"
                "        ('c', 'pkg.c'),\n"
                "    ]\n"
                "    for target_src, target_mod in pairs:\n"
                "        target_src = str(os.path.abspath(target_src))\n"
                "        print(target_mod)\n"
                "    print('ok')\n"
                "\n"
                "main()\n"
            )

        exe = os.path.join(td, "entry_bin")
        pipeline.compile_python(
            src,
            exe,
            backend="self",
            libpython_mode="off",
            ir_scaffold_mode="on",
        )
        env = os.environ.copy()
        env.pop("LC_ALL", None)
        env["PCC_GC_BACKEND"] = "3"
        env["PCC_DEBUG_RELEASES"] = "1"
        run = subprocess.run(
            [exe],
            text=True,
            capture_output=True,
            timeout=20,
            env=env,
        )
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertNotIn("[BAD_INCREF]", run.stderr)
        self.assertEqual(run.stdout.splitlines(), ["pkg.a", "pkg.b", "pkg.c", "ok"])

    def test_borrowed_object_local_rebind_keeps_gc_root(self):
        from pcc.py_frontend import pipeline

        td = tempfile.mkdtemp(prefix="pcc_borrowed_local_root_")
        self.addCleanup(self._rmtree, td)
        src = os.path.join(td, "entry.py")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write(
                "def scan(source: str) -> str:\n"
                "    pending = ''\n"
                "    for raw_line in source.splitlines():\n"
                "        stripped = raw_line.strip()\n"
                "        pending = stripped\n"
                "        pending = pending + 'x'\n"
                "    return pending\n"
                "\n"
                "print(scan('a\\nb'))\n"
            )

        out_ll = os.path.join(td, "borrowed_local_root.ll")
        pipeline.compile_python(
            src,
            out_ll,
            emit_llvm_only=True,
            backend="self",
            libpython_mode="off",
            ir_scaffold_mode="on",
        )
        with open(out_ll, "r", encoding="utf-8") as fh:
            ir_text = fh.read()

        pending_root = re.search(
            r"(?P<slot>%gc\.frame\.slots\.ptr\.[^ ]+) = " r"bitcast ptr %pending\.addr",
            ir_text,
        )
        self.assertIsNotNone(pending_root)
        self.assertRegex(
            ir_text,
            r"call void \(ptr, ptr\) @pcc_gc_frame_enter"
            r"\(ptr %gc\.frame\.map\.ptr\.[^,]+, ptr "
            + re.escape(pending_root.group("slot"))
            + r"\)",
        )
        borrowed_rebind = re.search(
            r"%pending\.local\.copy\.retain[^\n]+ = call ptr \(ptr\) "
            r"@pcc_gc_retain\(ptr %stripped\.[^\n]+\)"
            r"(?P<body>(?:(?!%str\.concat).)*?)"
            r"%str\.concat",
            ir_text,
            flags=re.S,
        )
        self.assertIsNotNone(borrowed_rebind)
        # The retained borrowed value is now transferred directly into the
        # frame-registered slot.  pcc_gc_store_root_take releases the old slot
        # value but does not retain the new one: ownership of the explicit
        # pcc_gc_retain result moves into the slot, so releasing that same temp
        # after the call would be an over-release.  Pin the transfer call, its
        # exact value, and the owned flag at equal strictness.
        body = borrowed_rebind.group("body")
        rooted_store = re.search(
            r"%(?P<gcptr>gc\.ptr\.[^ ]+) = bitcast ptr %pending\.addr[^\n]*\n"
            r"\s*call void \(ptr, ptr\) @pcc_gc_store_root_take\(ptr "
            r"%(?P=gcptr), ptr (?P<temp>%pending\.local\.copy\.retain[^)\n]+)\)",
            body,
        )
        self.assertIsNotNone(rooted_store, body)
        temp = re.escape(rooted_store.group("temp"))
        after_store = body[rooted_store.end():]
        self.assertNotRegex(
            after_store,
            r"call void \(ptr\) @pcc_gc_release\(ptr " + temp + r"\)",
        )
        # The owned state must be recorded, and it must be recorded as owned.
        # Where it is recorded is not part of the contract: the owned mem2reg
        # promotes the ownership flag out of its stack slot, so the
        # pre-promotion `store i1 1` immediately after the transfer becomes a
        # value that materializes at the next join instead.  Assert the
        # decision over the whole function and accept either spelling; pinning
        # one of them, in one window, pins an optimizer implementation detail
        # rather than the invariant.
        self.assertRegex(
            ir_text,
            r"store i1 1, ptr %pending\.owned"
            r"|%pending\.owned[\w.]* = phi i1 \[ 1,"
            r"|br i1 1, label %pending\.owned\.release",
        )
        # And the release path must still be reachable, so the transferred
        # reference is actually given back rather than leaked.
        self.assertIn("pending.owned.release", ir_text)
        self.assertNotIn("@pcc_gc_frame_leave", borrowed_rebind.group("body"))

    def test_cross_module_function_with_args(self):
        files = {
            "entry.py": (
                "from .lib import adder\n"
                "print(adder(3, 4))\n"
                "print(adder(10, 20))\n"
            ),
            "lib.py": ("def adder(a: int, b: int) -> int:\n" "    return a + b\n"),
        }
        _, out, code = self._run_multi(
            files,
            entry_module="pkg.entry",
            module_names=["pkg.entry", "pkg.lib"],
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, "7\n30\n")

    def test_cross_module_exception_subclass_keeps_constructor_args(self):
        files = {
            "entry.py": (
                "from .errors import ProbeError\n"
                "try:\n"
                "    raise ProbeError('hello')\n"
                "except ProbeError as exc:\n"
                "    print(str(exc))\n"
                "    print(exc.args)\n"
            ),
            "errors.py": "class ProbeError(Exception):\n    pass\n",
        }
        _, out, code = self._run_multi(
            files,
            entry_module="pkg.entry",
            module_names=["pkg.entry", "pkg.errors"],
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, "hello\n('hello',)\n")

    def test_package_reexport_chain_stays_native(self):
        """Package ``__init__`` re-exports should not force CPython import calls.

        This mirrors real package APIs where ``pkg`` re-exports names from
        implementation modules via ``from .impl import *`` or explicit
        ``from .leaf import name`` chains.
        """
        from pcc.py_frontend import pipeline

        td = tempfile.mkdtemp(prefix="pcc_multi_reexport_chain_")
        self.addCleanup(self._rmtree, td)
        files = {
            "entry.py": ("from pkg import exported\n" "print(exported(4))\n"),
            "pkg/__init__.py": "from .mid import *\n",
            "pkg/mid.py": "from .leaf import exported\n",
            "pkg/leaf.py": ("def exported(x: int) -> int:\n" "    return x + 2\n"),
        }
        src_paths = []
        for rel, source in files.items():
            dst = os.path.join(td, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "w", encoding="utf-8") as fh:
                fh.write(textwrap.dedent(source).lstrip())
            src_paths.append(dst)
        module_names = ["entry", "pkg", "pkg.mid", "pkg.leaf"]

        out_ll = os.path.join(td, "reexport.ll")
        pipeline.compile_python_multi(
            src_paths,
            out_ll,
            entry_module="entry",
            module_names=module_names,
            emit_llvm_only=True,
            libpython_mode="off",
        )
        with open(out_ll, "r", encoding="utf-8") as fh:
            ir_text = fh.read()
        self.assertFalse(
            pipeline._ir_needs_libpython(ir_text),
            msg="package re-export emitted py_cpy_* fallback",
        )

        exe = os.path.join(td, "reexport.out")
        pipeline.compile_python_multi(
            src_paths,
            exe,
            entry_module="entry",
            module_names=module_names,
            libpython_mode="off",
        )
        r = subprocess.run([exe], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertEqual(r.stdout, "6\n")

    def test_import_from_inside_top_level_if_predeclares_native_export(self):
        """``from .sibling import name`` nested inside a top-level ``if``
        block must still bind the native sibling extern at predeclare
        time, not at module-main emit time. Otherwise a nested ``def``
        inside the same block that calls ``name`` lowers to a
        ``py_cpy_*`` fallback (or a static NameError in pure no-host
        mode) — the original numpy ``_sanity_check`` cap on the
        no-libpython import path. The structure here mirrors
        ``numpy/__init__.py``'s
        ``if not __NUMPY_SETUP__: from ._core import (..., ones, ...);
        def _sanity_check(): ones()``.
        """
        from pcc.py_frontend import pipeline

        td = tempfile.mkdtemp(prefix="pcc_multi_if_import_predeclare_")
        self.addCleanup(self._rmtree, td)
        files = {
            "entry.py": ("import pkg\n"),
            "pkg/__init__.py": (
                "import os\n"
                "\n"
                "if os.environ.get('PCC_NEVER') != '1':\n"
                "    from pkg._core import ones\n"
                "\n"
                "    def _sanity_check():\n"
                "        x = ones()\n"
                "        print(x)\n"
                "\n"
                "    _sanity_check()\n"
            ),
            "pkg/_core/__init__.py": "from pkg._core.numeric import ones\n",
            "pkg/_core/numeric.py": ("def ones() -> int:\n" "    return 1\n"),
        }
        src_paths = []
        module_names = []
        for rel, source in files.items():
            dst = os.path.join(td, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "w", encoding="utf-8") as fh:
                fh.write(textwrap.dedent(source).lstrip())
            src_paths.append(dst)
            mod_name = rel[:-3].replace("/", ".")
            if mod_name.endswith(".__init__"):
                mod_name = mod_name[: -len(".__init__")]
            module_names.append(mod_name)

        out_ll = os.path.join(td, "if_predeclare.ll")
        pipeline.compile_python_multi(
            src_paths,
            out_ll,
            entry_module="entry",
            module_names=module_names,
            emit_llvm_only=True,
            libpython_mode="off",
        )
        with open(out_ll, "r", encoding="utf-8") as fh:
            ir_text = fh.read()
        self.assertFalse(
            pipeline._ir_needs_libpython(ir_text),
            msg="nested-if import emitted py_cpy_* fallback",
        )
        self.assertNotIn(
            "name 'ones' is not defined",
            ir_text,
            msg="static NameError for ones — predeclare missed nested-if",
        )

        exe = os.path.join(td, "if_predeclare.out")
        pipeline.compile_python_multi(
            src_paths,
            exe,
            entry_module="entry",
            module_names=module_names,
            libpython_mode="off",
        )
        r = subprocess.run([exe], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertEqual(r.stdout, "1\n")

    def test_reexported_class_alias_uses_original_class_global(self):
        """Imported class aliases must not invent owner-module class globals.

        ``pkg.api.Base`` below is a re-export of ``pkg.base.Base``.  Codegen
        must keep all extern class references pointed at the original owner;
        otherwise self-backend links fail with undefined ``.class.pkg_api.Base``
        symbols even though ``pkg.base`` is compiled in the same closure.
        """
        from pcc.py_frontend import pipeline

        td = tempfile.mkdtemp(prefix="pcc_multi_class_reexport_")
        self.addCleanup(self._rmtree, td)
        files = {
            "entry.py": ("from pkg.user import make\n" "print(make().value)\n"),
            "pkg/__init__.py": "",
            "pkg/base.py": (
                "class Base:\n"
                "    def __init__(self, value: int):\n"
                "        self.value = value\n"
            ),
            "pkg/api.py": "from .base import Base\n",
            "pkg/user.py": (
                "from .api import Base\n"
                "\n"
                "class Derived(Base):\n"
                "    pass\n"
                "\n"
                "def make() -> Base:\n"
                "    return Derived(42)\n"
            ),
        }
        src_paths = []
        module_names = []
        for rel, source in files.items():
            dst = os.path.join(td, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "w", encoding="utf-8") as fh:
                fh.write(textwrap.dedent(source).lstrip())
            src_paths.append(dst)
            mod_name = rel[:-3].replace("/", ".")
            if mod_name.endswith(".__init__"):
                mod_name = mod_name[: -len(".__init__")]
            module_names.append(mod_name)

        out_ll = os.path.join(td, "class_reexport.ll")
        pipeline.compile_python_multi(
            src_paths,
            out_ll,
            entry_module="entry",
            module_names=module_names,
            emit_llvm_only=True,
            libpython_mode="off",
        )
        with open(out_ll, "r", encoding="utf-8") as fh:
            ir_text = fh.read()
        self.assertIn("@.class.pkg_base.Base = global ptr null", ir_text)
        self.assertNotIn("@.class.pkg_api.Base = external global ptr", ir_text)

        exe = os.path.join(td, "class_reexport.out")
        pipeline.compile_python_multi(
            src_paths,
            exe,
            entry_module="entry",
            module_names=module_names,
            libpython_mode="off",
        )
        r = subprocess.run([exe], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertEqual(r.stdout, "42\n")

    def test_native_module_constant_class_constructor_arg_uses_static_export(self):
        """A native sibling constant passed to a sibling class constructor
        must not become getattr(module-name-string, constant-name).
        """
        from pcc.py_frontend import pipeline

        td = tempfile.mkdtemp(prefix="pcc_multi_module_constant_ctor_arg_")
        self.addCleanup(self._rmtree, td)
        files = {
            "entry.py": (
                "from . import spec as constants\n"
                "from .model import Record\n\n"
                "value = Record(constants.KIND)\n"
                "print(value.kind)\n"
            ),
            "spec.py": "KIND = 0\n",
            "model.py": (
                "class Record:\n"
                "    def __init__(self, kind: int) -> None:\n"
                "        self.kind = kind\n"
            ),
        }
        src_paths = []
        for rel, source in files.items():
            dst = os.path.join(td, rel)
            with open(dst, "w", encoding="utf-8") as fh:
                fh.write(source)
            src_paths.append(dst)
        module_names = ["pkg.entry", "pkg.spec", "pkg.model"]

        out_ll = os.path.join(td, "module_constant_ctor_arg.ll")
        pipeline.compile_python_multi(
            src_paths,
            out_ll,
            entry_module="pkg.entry",
            module_names=module_names,
            emit_llvm_only=True,
            libpython_mode="off",
            ir_scaffold_mode="on",
            backend="self",
        )
        with open(out_ll, "r", encoding="utf-8") as fh:
            ir_text = fh.read()
        self.assertNotIn("classgen.arg.KIND", ir_text)
        self.assertNotRegex(
            ir_text,
            r"py_obj_getattr\([^\n]*@\.pyattr\.KIND",
        )

        exe = os.path.join(td, "module_constant_ctor_arg.out")
        pipeline.compile_python_multi(
            src_paths,
            exe,
            entry_module="pkg.entry",
            module_names=module_names,
            libpython_mode="off",
            ir_scaffold_mode="on",
            backend="self",
        )
        result = subprocess.run([exe], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout, "0\n")

    def test_native_module_class_unbound_method_call_uses_explicit_receiver(self):
        from pcc.py_frontend import pipeline

        td = tempfile.mkdtemp(prefix="pcc_multi_unbound_class_method_")
        self.addCleanup(self._rmtree, td)
        files = {
            "entry.py": ("from pkg.user import run\n" "run()\n"),
            "pkg/__init__.py": "",
            "pkg/base.py": (
                "class Base:\n"
                "    def set(self, index: int, value: int) -> None:\n"
                "        print(value)\n"
            ),
            "pkg/user.py": (
                "from . import base as ma\n"
                "\n"
                "class Child:\n"
                "    def call(self) -> None:\n"
                "        ma.Base.set(self, 1, 5)\n"
                "\n"
                "def run() -> None:\n"
                "    Child().call()\n"
            ),
        }
        src_paths = []
        for rel, source in files.items():
            dst = os.path.join(td, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "w", encoding="utf-8") as fh:
                fh.write(textwrap.dedent(source).lstrip())
            src_paths.append(dst)
        module_names = ["entry", "pkg", "pkg.base", "pkg.user"]

        out_ll = os.path.join(td, "unbound_class_method.ll")
        pipeline.compile_python_multi(
            src_paths,
            out_ll,
            entry_module="entry",
            module_names=module_names,
            emit_llvm_only=True,
            libpython_mode="off",
        )
        with open(out_ll, "r", encoding="utf-8") as fh:
            ir_text = fh.read()
        self.assertFalse(pipeline._ir_needs_libpython(ir_text))

        exe = os.path.join(td, "unbound_class_method.out")
        pipeline.compile_python_multi(
            src_paths,
            exe,
            entry_module="entry",
            module_names=module_names,
            libpython_mode="off",
        )
        r = subprocess.run([exe], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertEqual(r.stdout, "5\n")

    def test_cross_module_static_table_import_stays_native(self):
        """A top-level literal container imported from a native sibling
        must bind to that sibling's native module-global slot, not to
        a CPython ``from ... import`` fallback.
        """
        from pcc.py_frontend import pipeline

        td = tempfile.mkdtemp(prefix="pcc_multi_static_table_")
        self.addCleanup(self._rmtree, td)
        entry = os.path.join(td, "entry.py")
        tables = os.path.join(td, "tables.py")
        with open(entry, "w", encoding="utf-8") as fh:
            fh.write(
                "from .tables import VALUES, direct\n\n"
                "def lookup(name: str) -> int:\n"
                "    if name not in VALUES:\n"
                "        return 0\n"
                "    return VALUES[name]\n\n"
                "print(direct())\n"
                'print(lookup("b"))\n'
            )
        with open(tables, "w", encoding="utf-8") as fh:
            fh.write(
                'VALUES = {"a": 3, "b": 4}\n\n'
                "def direct() -> int:\n"
                '    return VALUES["a"]\n'
            )

        out_ll = os.path.join(td, "static_table.ll")
        pipeline.compile_python_multi(
            [entry, tables],
            out_ll,
            entry_module="pkg.entry",
            module_names=["pkg.entry", "pkg.tables"],
            emit_llvm_only=True,
            libpython_mode="off",
        )
        with open(out_ll, "r", encoding="utf-8") as fh:
            ir_text = fh.read()
        self.assertFalse(
            pipeline._ir_needs_libpython(ir_text),
            msg="cross-module static table import emitted py_cpy_* fallback",
        )

        exe = os.path.join(td, "static_table.out")
        pipeline.compile_python_multi(
            [entry, tables],
            exe,
            entry_module="pkg.entry",
            module_names=["pkg.entry", "pkg.tables"],
            libpython_mode="off",
        )
        r = subprocess.run([exe], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertEqual(r.stdout, "3\n4\n")

    def test_module_tuple_unpack_leaves_are_exported(self):
        """Module-level destructuring publishes every bound leaf.

        Real packages commonly initialize several exported constants from one
        helper call.  A compiled sibling must see those bindings through the
        native module proxy rather than finding only an initializer-local
        alloca.
        """
        from pcc.py_frontend import pipeline

        td = tempfile.mkdtemp(prefix="pcc_multi_module_unpack_")
        self.addCleanup(self._rmtree, td)
        entry = os.path.join(td, "entry.py")
        values = os.path.join(td, "values.py")
        with open(entry, "w", encoding="utf-8") as fh:
            fh.write("from . import values\nprint(values.C)\n")
        with open(values, "w", encoding="utf-8") as fh:
            fh.write(
                "def constants():\n"
                "    return (10, 32)\n\n"
                "A, B = constants()\n"
                "C = B\n"
            )

        exe = os.path.join(td, "module_unpack.out")
        pipeline.compile_python_multi(
            [entry, values],
            exe,
            entry_module="pkg.entry",
            module_names=["pkg.entry", "pkg.values"],
            libpython_mode="off",
            ir_scaffold_mode="on",
            backend="self",
        )
        r = subprocess.run([exe], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertEqual(r.stdout, "32\n")

    def test_conditional_self_unpack_overrides_class_attributes(self):
        _, out, code = self._run_multi(
            {
                "entry.py": """
                    class Pair:
                        left = "class-left"
                        right = "class-right"

                        def __init__(self, values):
                            if values is not None:
                                self.left, self.right = values

                        def show(self):
                            print(self.left)
                            print(self.right)

                    Pair(("instance-left", "instance-right")).show()
                """,
            },
            entry_module="entry",
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, "instance-left\ninstance-right\n")

    def test_module_block_function_definition_binds_name(self):
        """A conditional module-scope ``def`` executes a name binding."""
        _, out, code = self._run_multi(
            {
                "entry.py": (
                    "if False:\n"
                    "    selected = int\n"
                    "else:\n"
                    "    def selected(value):\n"
                    "        return int(value) + 1\n"
                    "print(selected('41'))\n"
                )
            },
            entry_module="entry",
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, "42\n")

    def test_no_libpython_dep(self):
        """Produced binaries must link only libSystem / libc++,
        confirming the multi-file path doesn't pull libpython
        through py_cpy_import for native sibling imports."""
        exe, _, code = self._run_multi(
            {
                "entry.py": ("from .lib import greet\n" "greet()\n"),
                "lib.py": ("def greet() -> None:\n" '    print("hi")\n'),
            },
            entry_module="pkg.entry",
            module_names=["pkg.entry", "pkg.lib"],
        )
        self.assertEqual(code, 0)
        # ``otool -L`` on macOS; ``ldd`` on Linux. Keep this check
        # macOS-only for now so CI on either platform is happy.
        if os.uname().sysname != "Darwin":
            self.skipTest("link-lib check is macOS-specific")
        r = subprocess.run(
            ["otool", "-L", exe],
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.returncode, 0)
        self.assertNotIn("Python", r.stdout)
        self.assertNotIn("libpython", r.stdout)

    def test_relative_import_fallback_uses_absolute_module_name(self):
        """Relative ``from . import helper`` should resolve to the
        package name before falling back to CPython import. Otherwise
        codegen emits ``py_cpy_import('')`` and leaves a pending
        ``ValueError`` behind at runtime."""
        from pcc.py_frontend.pipeline import compile_python_multi

        td = tempfile.mkdtemp(prefix="pcc_multi_ir_")
        self.addCleanup(self._rmtree, td)
        entry = os.path.join(td, "entry.py")
        with open(entry, "w", encoding="utf-8") as fh:
            fh.write("def run() -> None:\n" "    from . import helper\n\n" "run()\n")
        out_ll = os.path.join(td, "entry.ll")
        compile_python_multi(
            [entry],
            out_ll,
            entry_module="pkg.entry",
            module_names=["pkg.entry"],
            emit_llvm_only=True,
            libpython_mode="auto",
        )
        with open(out_ll, "r", encoding="utf-8") as fh:
            ir_text = fh.read()
        self.assertRegex(
            ir_text,
            r'@[\w.$]*\.cpy\.mod\.pkg = (?:internal )?constant \[4 x i8\] c"pkg\\00"',
        )
        self.assertIn("%cpy.fromimport.pkg", ir_text)
        self.assertNotRegex(
            ir_text,
            r"@[\w.$]*\.cpy\.mod\. = (?:internal )?constant",
        )

    def test_relative_package_attribute_pulls_init_into_native_closure(self):
        """``from . import Name`` may import an ``__init__.py`` attribute.

        The automatic multi-file closure must include the package module as
        well as trying ``pkg.Name`` as a possible sibling module.  Otherwise
        an exported exception class silently lowers through ``py_cpy_call1``.
        """
        from pcc.py_frontend.pipeline import compile_python_multi

        td = tempfile.mkdtemp(prefix="pcc_multi_package_attr_")
        self.addCleanup(self._rmtree, td)
        pkg_dir = os.path.join(td, "pkg")
        os.makedirs(pkg_dir, exist_ok=True)
        pkg_init = os.path.join(pkg_dir, "__init__.py")
        entry = os.path.join(pkg_dir, "entry.py")
        with open(pkg_init, "w", encoding="utf-8") as fh:
            fh.write("class PackageError(Exception):\n    pass\n")
        with open(entry, "w", encoding="utf-8") as fh:
            fh.write(
                "from . import PackageError\n\n"
                "def fail() -> None:\n"
                "    raise PackageError('boom')\n"
            )
        out_ll = os.path.join(td, "entry.ll")
        compile_python_multi(
            [entry],
            out_ll,
            entry_module="pkg.entry",
            module_names=["pkg.entry"],
            emit_llvm_only=True,
            backend="self",
            libpython_mode="off",
        )
        with open(out_ll, "r", encoding="utf-8") as fh:
            ir_text = fh.read()
        self.assertIn("; ---- module: pkg ----", ir_text)
        self.assertNotRegex(ir_text, r"\bcall [^\n]*@py_cpy_")

    def test_missing_native_relative_import_raises_importerror_without_libpython(self):
        """A missing same-package optional import should raise native
        ImportError instead of routing through ``py_cpy_import``."""
        from pcc.py_frontend.pipeline import compile_python_multi

        td = tempfile.mkdtemp(prefix="pcc_multi_missing_rel_")
        self.addCleanup(self._rmtree, td)
        pkg_dir = os.path.join(td, "pkg")
        os.makedirs(pkg_dir, exist_ok=True)
        pkg_init = os.path.join(pkg_dir, "__init__.py")
        entry = os.path.join(pkg_dir, "entry.py")
        with open(pkg_init, "w", encoding="utf-8") as fh:
            fh.write("VALUE = 1\n")
        with open(entry, "w", encoding="utf-8") as fh:
            fh.write(
                "try:\n"
                "    from . import missing_optional\n"
                "except ImportError:\n"
                "    print('missing')\n"
                "else:\n"
                "    print('unexpected')\n"
            )
        exe = os.path.join(td, "missing_rel")
        compile_python_multi(
            [pkg_init, entry],
            exe,
            entry_module="pkg.entry",
            module_names=["pkg", "pkg.entry"],
            backend="self",
            libpython_mode="off",
        )
        r = subprocess.run([exe], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "missing\n")

        out_ll = os.path.join(td, "missing_rel.ll")
        compile_python_multi(
            [pkg_init, entry],
            out_ll,
            entry_module="pkg.entry",
            module_names=["pkg", "pkg.entry"],
            emit_llvm_only=True,
            backend="self",
            libpython_mode="off",
        )
        with open(out_ll, "r", encoding="utf-8") as fh:
            ir_text = fh.read()
        self.assertNotIn("call ptr (ptr) @py_cpy_import", ir_text)

    def test_missing_external_optional_import_folds_to_none_without_libpython(self):
        """A missing external optional import caught as ImportError should
        not leave CPython import/call fallback in the strict native closure."""
        from pcc.py_frontend.pipeline import compile_python_multi

        td = tempfile.mkdtemp(prefix="pcc_multi_missing_ext_optional_")
        self.addCleanup(self._rmtree, td)
        entry = os.path.join(td, "entry.py")
        with open(entry, "w", encoding="utf-8") as fh:
            fh.write(
                "try:\n"
                "    import definitely_missing_optional_for_pcc as optional_mod\n"
                "except ImportError:\n"
                "    optional_mod = None\n"
                "if optional_mod is not None:\n"
                "    print(optional_mod.from_path('x'))\n"
                "else:\n"
                "    print('missing')\n"
            )
        exe = os.path.join(td, "missing_ext_optional")
        compile_python_multi(
            [entry],
            exe,
            entry_module="pkg.entry",
            module_names=["pkg.entry"],
            backend="self",
            libpython_mode="off",
        )
        r = subprocess.run([exe], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "missing\n")

        out_ll = os.path.join(td, "missing_ext_optional.ll")
        compile_python_multi(
            [entry],
            out_ll,
            entry_module="pkg.entry",
            module_names=["pkg.entry"],
            emit_llvm_only=True,
            backend="self",
            libpython_mode="off",
        )
        with open(out_ll, "r", encoding="utf-8") as fh:
            ir_text = fh.read()
        self.assertNotIn("call ptr (ptr) @py_cpy_import", ir_text)
        self.assertNotIn("call ptr (ptr, ptr) @py_cpy_getattr", ir_text)

    def test_self_submodule_import_from_parent_uses_native_module_attrs(self):
        """A compiled ``pkg.sub`` can use ``from pkg import sub`` to publish
        into its live module namespace without materialising a host package."""
        from pcc.py_frontend.pipeline import compile_python_multi

        td = tempfile.mkdtemp(prefix="pcc_multi_self_submodule_import_")
        self.addCleanup(self._rmtree, td)
        entry = os.path.join(td, "sub.py")
        with open(entry, "w", encoding="utf-8") as fh:
            fh.write(
                "def roundtrip(value):\n"
                "    from pkg import sub\n"
                "    setattr(sub, 'published', value)\n"
                "    return value\n"
                "print(roundtrip(7))\n"
            )

        exe = os.path.join(td, "self_submodule_import")
        compile_python_multi(
            [entry],
            exe,
            entry_module="pkg.sub",
            module_names=["pkg.sub"],
            backend="self",
            libpython_mode="off",
        )
        result = subprocess.run([exe], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout, "7\n")

        out_ll = os.path.join(td, "self_submodule_import.ll")
        compile_python_multi(
            [entry],
            out_ll,
            entry_module="pkg.sub",
            module_names=["pkg.sub"],
            emit_llvm_only=True,
            backend="self",
            libpython_mode="off",
        )
        with open(out_ll, "r", encoding="utf-8") as fh:
            ir_text = fh.read()
        self.assertNotIn("call ptr (ptr) @py_cpy_import", ir_text)
        self.assertIn("@py_module_attr_set", ir_text)

    def test_dynamic_native_module_attrs_calls_and_sys_modules_stay_native(self):
        from pcc.py_frontend.pipeline import compile_python_multi

        td = tempfile.mkdtemp(prefix="pcc_multi_dynamic_module_attrs_")
        self.addCleanup(self._rmtree, td)
        entry = os.path.join(td, "entry.py")
        helper = os.path.join(td, "helper.py")
        with open(entry, "w", encoding="utf-8") as fh:
            fh.write(
                "import sys\n"
                "from . import helper\n"
                "print(hasattr(helper, 'late'))\n"
                "print(helper.make() != helper.make())\n"
                "if not hasattr(helper, 'late'):\n"
                "    print(sys.modules['pkg.helper'].__path__)\n"
            )
        with open(helper, "w", encoding="utf-8") as fh:
            fh.write("globals()['late'] = 1\n" "globals()['make'] = lambda: {'x': 1}\n")

        exe = os.path.join(td, "dynamic_module_attrs")
        compile_python_multi(
            [entry, helper],
            exe,
            entry_module="pkg.entry",
            module_names=["pkg.entry", "pkg.helper"],
            backend="self",
            libpython_mode="off",
        )
        result = subprocess.run([exe], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout, "True\nFalse\n")

    def test_self_dunder_class_constructor_starstar_kwargs_without_libpython(self):
        """A `self.__class__(..., **kwargs)` clone shape should avoid
        libpython call fallback in no-libpython mode."""
        from pcc.py_frontend.pipeline import compile_python_multi

        td = tempfile.mkdtemp(prefix="pcc_multi_self_dunder_class_ctor_")
        self.addCleanup(self._rmtree, td)
        entry = os.path.join(td, "entry.py")
        with open(entry, "w", encoding="utf-8") as fh:
            fh.write(
                "class EnvironmentConfig:\n"
                "    def __init__(self, distutils_section=None, noopt=None, noarch=None):\n"
                "        self._distutils_section = distutils_section\n"
                '        self._conf_keys = {"noopt": noopt, "noarch": noarch}\n'
                "\n"
                "    def clone(self):\n"
                "        return self.__class__(\n"
                "            distutils_section=self._distutils_section,\n"
                "            **self._conf_keys,\n"
                "        )\n"
                "\n"
                'obj = EnvironmentConfig("build", "0", "1")\n'
                "copy = obj.clone()\n"
                "print(copy._distutils_section)\n"
                'print(copy._conf_keys["noopt"])\n'
                'print(copy._conf_keys["noarch"])\n'
            )

        exe = os.path.join(td, "dunder_class_ctor")
        compile_python_multi(
            [entry],
            exe,
            entry_module="pkg.entry",
            module_names=["pkg.entry"],
            backend="self",
            libpython_mode="off",
        )
        result = subprocess.run([exe], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout, "build\n0\n1\n")

        out_ll = os.path.join(td, "dunder_class_ctor.ll")
        compile_python_multi(
            [entry],
            out_ll,
            entry_module="pkg.entry",
            module_names=["pkg.entry"],
            emit_llvm_only=True,
            backend="self",
            libpython_mode="off",
        )
        with open(out_ll, "r", encoding="utf-8") as fh:
            ir_text = fh.read()
        self.assertNotIn("call ptr (ptr) @py_cpy_call_noargs", ir_text)
        self.assertNotIn("call ptr (ptr, ptr) @py_cpy_call1", ir_text)
        self.assertNotIn("call ptr (ptr, ptr, ptr) @py_cpy_call_kwdict_plus", ir_text)
        self.assertNotIn("call ptr (ptr, ptr, ptr, ptr) @py_cpy_call_kwdict2", ir_text)
        self.assertNotIn("call ptr (ptr, ptr) @py_cpy_import", ir_text)

    def test_os_path_getsize_lowers_without_libpython(self):
        """os.path.getsize is part of the package-import path native
        os.path subset and must not route through CPython fallback."""
        from pcc.py_frontend.pipeline import compile_python_multi

        td = tempfile.mkdtemp(prefix="pcc_multi_os_path_getsize_")
        self.addCleanup(self._rmtree, td)
        data = os.path.join(td, "data.txt")
        with open(data, "w", encoding="utf-8") as fh:
            fh.write("abcdef")
        entry = os.path.join(td, "entry.py")
        with open(entry, "w", encoding="utf-8") as fh:
            fh.write("import os\n" f"print(os.path.getsize({data!r}))\n")

        exe = os.path.join(td, "getsize")
        with mock.patch.dict(
            os.environ,
            {"PCC_RUNTIME_CC": "pcc", "PCC_RUNTIME_HIGH": "py"},
        ):
            compile_python_multi(
                [entry],
                exe,
                entry_module="pkg.entry",
                module_names=["pkg.entry"],
                backend="self",
                libpython_mode="off",
            )
        r = subprocess.run([exe], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertEqual(r.stdout, "6\n")

        out_ll = os.path.join(td, "getsize.ll")
        compile_python_multi(
            [entry],
            out_ll,
            entry_module="pkg.entry",
            module_names=["pkg.entry"],
            emit_llvm_only=True,
            backend="self",
            libpython_mode="off",
        )
        with open(out_ll, "r", encoding="utf-8") as fh:
            ir_text = fh.read()
        self.assertIn("@py_os_path_getsize", ir_text)
        self.assertNotIn("call ptr (ptr, ptr) @py_cpy_getattr", ir_text)
        self.assertNotIn("call ptr (ptr) @py_cpy_call_noargs", ir_text)
        self.assertNotIn("call ptr (ptr, ptr) @py_cpy_call1", ir_text)

    def test_pathlib_path_suffix_lowers_without_libpython(self):
        """Path(...).suffix is common package-import code and should stay
        native when pathlib Path/PurePath are imported as builtin aliases."""
        from pcc.py_frontend.pipeline import compile_python_multi

        td = tempfile.mkdtemp(prefix="pcc_multi_pathlib_suffix_")
        self.addCleanup(self._rmtree, td)
        entry = os.path.join(td, "entry.py")
        with open(entry, "w", encoding="utf-8") as fh:
            fh.write(
                "from pathlib import Path, PurePath\n"
                'print(Path("/tmp/example.F90").suffix.lower())\n'
                'print(PurePath("no_ext").suffix)\n'
            )

        exe = os.path.join(td, "pathlib_suffix")
        with mock.patch.dict(
            os.environ,
            {"PCC_RUNTIME_CC": "pcc", "PCC_RUNTIME_HIGH": "py"},
        ):
            compile_python_multi(
                [entry],
                exe,
                entry_module="pkg.entry",
                module_names=["pkg.entry"],
                backend="self",
                libpython_mode="off",
            )
        r = subprocess.run([exe], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertEqual(r.stdout, ".f90\n\n")

        out_ll = os.path.join(td, "pathlib_suffix.ll")
        compile_python_multi(
            [entry],
            out_ll,
            entry_module="pkg.entry",
            module_names=["pkg.entry"],
            emit_llvm_only=True,
            backend="self",
            libpython_mode="off",
        )
        with open(out_ll, "r", encoding="utf-8") as fh:
            ir_text = fh.read()
        self.assertIn("@py_os_path_splitext", ir_text)
        self.assertIn("@py_tuple_get", ir_text)
        self.assertNotIn("call ptr (ptr, ptr) @py_cpy_getattr", ir_text)
        self.assertNotIn("call ptr (ptr) @py_cpy_call_noargs", ir_text)
        self.assertNotIn("call ptr (ptr, ptr) @py_cpy_call1", ir_text)

    def test_codecs_bom_binary_startswith_lowers_without_libpython(self):
        """NumPy's openhook probes binary BOM prefixes via codecs constants.

        The native path must keep this as bytes/tuple startswith logic rather
        than CPython getattr/call fallback.
        """
        from pcc.py_frontend.pipeline import compile_python_multi

        td = tempfile.mkdtemp(prefix="pcc_multi_codecs_bom_")
        self.addCleanup(self._rmtree, td)
        data = os.path.join(td, "bom.dat")
        with open(data, "wb") as fh:
            fh.write(b"\xef\xbb\xbfabc")
        entry = os.path.join(td, "entry.py")
        with open(entry, "w", encoding="utf-8") as fh:
            fh.write(
                "import codecs\n"
                f"with open({data!r}, 'rb') as f:\n"
                "    raw = f.read(4)\n"
                "print(raw.startswith(codecs.BOM_UTF8))\n"
                "print(raw.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)))\n"
            )

        exe = os.path.join(td, "codecs_bom")
        with mock.patch.dict(
            os.environ,
            {"PCC_RUNTIME_CC": "pcc", "PCC_RUNTIME_HIGH": "py"},
        ):
            compile_python_multi(
                [entry],
                exe,
                entry_module="pkg.entry",
                module_names=["pkg.entry"],
                backend="self",
                libpython_mode="off",
            )
        r = subprocess.run([exe], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertEqual(r.stdout, "True\nFalse\n")

        out_ll = os.path.join(td, "codecs_bom.ll")
        compile_python_multi(
            [entry],
            out_ll,
            entry_module="pkg.entry",
            module_names=["pkg.entry"],
            emit_llvm_only=True,
            backend="self",
            libpython_mode="off",
        )
        with open(out_ll, "r", encoding="utf-8") as fh:
            ir_text = fh.read()
        self.assertIn("@py_bytes_new", ir_text)
        self.assertIn("@py_str_startswith", ir_text)
        self.assertNotIn("@.cpy.attr.BOM_UTF8", ir_text)
        self.assertNotIn("load ptr, ptr @.cpy.modref.codecs", ir_text)
        self.assertNotIn("call ptr (ptr, ptr) @py_cpy_getattr", ir_text)
        self.assertNotIn("call ptr (ptr, ptr) @py_cpy_call1", ir_text)

    def test_fileinput_fileinput_lowers_without_libpython(self):
        """NumPy f2py scans source files through fileinput.FileInput."""
        from pcc.py_frontend.pipeline import compile_python_multi

        td = tempfile.mkdtemp(prefix="pcc_multi_fileinput_")
        self.addCleanup(self._rmtree, td)
        data = os.path.join(td, "lines.txt")
        with open(data, "w", encoding="utf-8") as fh:
            fh.write("alpha\nbeta\n")
        entry = os.path.join(td, "entry.py")
        with open(entry, "w", encoding="utf-8") as fh:
            fh.write(
                "import fileinput\n"
                "def openhook(filename, mode):\n"
                "    return open(filename, mode, encoding='utf-8')\n"
                f"fin = fileinput.FileInput([{data!r}], openhook=openhook)\n"
                "a = fin.readline()\n"
                "print(len(a))\n"
                "print(fin.lineno())\n"
                "print(fin.filelineno())\n"
                "print(fin.isfirstline())\n"
                "b = fin.readline()\n"
                "print(len(b))\n"
                "print(fin.lineno())\n"
                "print(fin.filelineno())\n"
                "print(fin.isfirstline())\n"
                "print(fin.filename())\n"
                "fin.close()\n"
            )

        exe = os.path.join(td, "fileinput_probe")
        with mock.patch.dict(
            os.environ,
            {"PCC_RUNTIME_CC": "pcc", "PCC_RUNTIME_HIGH": "py"},
        ):
            compile_python_multi(
                [entry],
                exe,
                entry_module="pkg.entry",
                module_names=["pkg.entry"],
                backend="self",
                libpython_mode="off",
            )
        r = subprocess.run([exe], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertEqual(
            r.stdout,
            "6\n1\n1\nTrue\n5\n2\n2\nFalse\n" + data + "\n",
        )

        out_ll = os.path.join(td, "fileinput_probe.ll")
        compile_python_multi(
            [entry],
            out_ll,
            entry_module="pkg.entry",
            module_names=["pkg.entry"],
            emit_llvm_only=True,
            backend="self",
            libpython_mode="off",
        )
        with open(out_ll, "r", encoding="utf-8") as fh:
            ir_text = fh.read()
        self.assertIn("@py_fileinput_new", ir_text)
        self.assertIn("@py_fileinput_readline", ir_text)
        self.assertNotIn("load ptr, ptr @.cpy.modref.fileinput", ir_text)
        self.assertNotIn("call ptr (ptr, ptr) @py_cpy_getattr", ir_text)
        self.assertNotIn("call ptr (ptr, ptr) @py_cpy_call1", ir_text)

    def test_native_relative_import_from_concrete_module_still_binds_export(self):
        """The missing-optional-import path must not turn concrete
        sibling-module exports into ImportError."""
        from pcc.py_frontend.pipeline import compile_python_multi

        td = tempfile.mkdtemp(prefix="pcc_multi_rel_export_")
        self.addCleanup(self._rmtree, td)
        pkg_dir = os.path.join(td, "pkg")
        os.makedirs(pkg_dir, exist_ok=True)
        pkg_init = os.path.join(pkg_dir, "__init__.py")
        types_py = os.path.join(pkg_dir, "types.py")
        entry = os.path.join(pkg_dir, "entry.py")
        with open(pkg_init, "w", encoding="utf-8") as fh:
            fh.write("VALUE = 1\n")
        with open(types_py, "w", encoding="utf-8") as fh:
            fh.write(
                "class Marker:\n" "    def value(self) -> int:\n" "        return 7\n"
            )
        with open(entry, "w", encoding="utf-8") as fh:
            fh.write(
                "from .types import Marker\n\n"
                "def main() -> None:\n"
                "    m = Marker()\n"
                "    print(m.value())\n\n"
                "main()\n"
            )
        exe = os.path.join(td, "rel_export")
        compile_python_multi(
            [pkg_init, types_py, entry],
            exe,
            entry_module="pkg.entry",
            module_names=["pkg", "pkg.types", "pkg.entry"],
            backend="self",
            libpython_mode="off",
        )
        r = subprocess.run([exe], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "7\n")

    def test_typing_type_checking_branches_are_compile_time_false(self):
        """typing.TYPE_CHECKING-only imports should not enter runtime lowering."""
        from pcc.py_frontend.pipeline import compile_python_multi

        td = tempfile.mkdtemp(prefix="pcc_multi_type_checking_")
        self.addCleanup(self._rmtree, td)
        entry = os.path.join(td, "entry.py")
        with open(entry, "w", encoding="utf-8") as fh:
            fh.write(
                "from typing import TYPE_CHECKING as TC, TypeVar\n"
                "import typing\n\n"
                "_T_co = TypeVar('_T_co', covariant=True)\n"
                "if TC:\n"
                "    import missing_type_only_a\n"
                "if typing.TYPE_CHECKING:\n"
                "    import missing_type_only_b\n"
                "print('runtime')\n"
            )

        exe = os.path.join(td, "type_checking")
        compile_python_multi(
            [entry],
            exe,
            entry_module="pkg.entry",
            module_names=["pkg.entry"],
            backend="self",
            libpython_mode="off",
        )
        r = subprocess.run([exe], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "runtime\n")

        out_ll = os.path.join(td, "type_checking.ll")
        compile_python_multi(
            [entry],
            out_ll,
            entry_module="pkg.entry",
            module_names=["pkg.entry"],
            emit_llvm_only=True,
            backend="self",
            libpython_mode="off",
        )
        with open(out_ll, "r", encoding="utf-8") as fh:
            ir_text = fh.read()
        self.assertNotIn("call ptr (ptr) @py_cpy_import", ir_text)

    def test_typing_literal_aliases_are_compile_time_metadata(self):
        """typing Literal/Union aliases should not allocate CPython typing objects."""
        from pcc.py_frontend.pipeline import compile_python_multi

        td = tempfile.mkdtemp(prefix="pcc_multi_typing_literal_alias_")
        self.addCleanup(self._rmtree, td)
        entry = os.path.join(td, "entry.py")
        with open(entry, "w", encoding="utf-8") as fh:
            fh.write(
                "from typing import Any, Literal, Sequence, TypeAlias, "
                "TypeAliasType, Union\n"
                "import typing\n\n"
                "_BoolCodes = Literal['bool', '?', 'b1']\n"
                "_MoreCodes = Literal[_BoolCodes, 'i4']\n"
                "_MaybeCodes = Union[_MoreCodes, Sequence[Any]]\n"
                "_OtherCodes = typing.Literal['x', 'y']\n"
                "_ScalarAlias: TypeAlias = tuple[Any, ...] | int\n"
                "_PublicAlias = TypeAliasType('PublicAlias', _ScalarAlias)\n"
                "print('runtime')\n"
            )

        exe = os.path.join(td, "typing_literal_alias")
        compile_python_multi(
            [entry],
            exe,
            entry_module="pkg.entry",
            module_names=["pkg.entry"],
            backend="self",
            libpython_mode="off",
        )
        r = subprocess.run([exe], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "runtime\n")

        out_ll = os.path.join(td, "typing_literal_alias.ll")
        compile_python_multi(
            [entry],
            out_ll,
            entry_module="pkg.entry",
            module_names=["pkg.entry"],
            emit_llvm_only=True,
            backend="self",
            libpython_mode="off",
        )
        with open(out_ll, "r", encoding="utf-8") as fh:
            ir_text = fh.read()
        for line in ir_text.splitlines():
            if "@py_cpy_" in line:
                stripped = line.lstrip()
                self.assertFalse(
                    stripped.startswith("call ") or " call " in line,
                    line,
                )

    def test_typing_metadata_aliases_survive_native_reexports(self):
        """Compiled siblings must not materialize typing-only exports."""
        from pcc.py_frontend.pipeline import compile_python_multi

        td = tempfile.mkdtemp(prefix="pcc_multi_typing_reexport_")
        self.addCleanup(self._rmtree, td)
        provider = os.path.join(td, "provider.py")
        facade = os.path.join(td, "facade.py")
        entry = os.path.join(td, "entry.py")
        with open(provider, "w", encoding="utf-8") as fh:
            fh.write(
                "from typing import Literal, TypeAlias\n\n"
                "AnyShape: TypeAlias = tuple[object, ...]\n"
                "Codes = Literal['x', 'y']\n"
            )
        with open(facade, "w", encoding="utf-8") as fh:
            fh.write("from .provider import AnyShape as AnyShape, Codes as Codes\n")
        with open(entry, "w", encoding="utf-8") as fh:
            fh.write(
                "from .facade import AnyShape, Codes\n\n" "print('typing reexport')\n"
            )

        exe = os.path.join(td, "typing_reexport")
        compile_python_multi(
            [provider, facade, entry],
            exe,
            entry_module="pkg.entry",
            module_names=["pkg.provider", "pkg.facade", "pkg.entry"],
            backend="self",
            libpython_mode="off",
        )
        run = subprocess.run(
            [exe],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout, "typing reexport\n")

    def test_compiled_sibling_module_docstring_is_importable(self):
        from pcc.py_frontend.pipeline import compile_python_multi

        td = tempfile.mkdtemp(prefix="pcc_multi_module_docstring_")
        self.addCleanup(self._rmtree, td)
        provider = os.path.join(td, "provider.py")
        entry = os.path.join(td, "entry.py")
        with open(provider, "w", encoding="utf-8") as fh:
            fh.write('"""provider docs"""\n')
        with open(entry, "w", encoding="utf-8") as fh:
            fh.write("from .provider import __doc__\nprint(__doc__)\n")

        exe = os.path.join(td, "module_docstring")
        compile_python_multi(
            [provider, entry],
            exe,
            entry_module="pkg.entry",
            module_names=["pkg.provider", "pkg.entry"],
            backend="self",
            libpython_mode="off",
        )
        run = subprocess.run(
            [exe],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout, "provider docs\n")

    def test_multi_compile_auto_closes_relative_sibling_sources(self):
        """Explicit multi-file compiles should recursively add missing
        relative-import siblings, so callers can seed just the entry
        module and still keep same-package helpers on the native path."""
        from pcc.py_frontend.pipeline import compile_python_multi

        td = tempfile.mkdtemp(prefix="pcc_multi_auto_closure_")
        self.addCleanup(self._rmtree, td)
        pkg_dir = os.path.join(td, "pkg")
        os.makedirs(pkg_dir, exist_ok=True)
        entry = os.path.join(pkg_dir, "main.py")
        helper = os.path.join(pkg_dir, "helper.py")
        with open(entry, "w", encoding="utf-8") as fh:
            fh.write("from .helper import answer\n\n" "print(answer())\n")
        with open(helper, "w", encoding="utf-8") as fh:
            fh.write("def answer() -> int:\n" "    return 42\n")

        exe = os.path.join(td, "auto_closure.out")
        compile_python_multi(
            [entry],
            exe,
            entry_module="pkg.main",
            module_names=["pkg.main"],
        )
        r = subprocess.run([exe], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertEqual(r.stdout, "42\n")

        if os.uname().sysname == "Darwin":
            lk = subprocess.run(
                ["otool", "-L", exe],
                capture_output=True,
                text=True,
            )
            self.assertEqual(lk.returncode, 0)
            self.assertNotIn("Python", lk.stdout)
            self.assertNotIn("libpython", lk.stdout)

    def test_native_submodule_alias_function_call_stays_native(self):
        """``from .sub import helper`` should keep
        ``helper.answer()`` on the native sibling path when that
        submodule is compiled in the same one-source closure."""
        from pcc.py_frontend.pipeline import compile_python_multi

        td = tempfile.mkdtemp(prefix="pcc_multi_submodule_alias_")
        self.addCleanup(self._rmtree, td)
        pkg_dir = os.path.join(td, "pkg")
        sub_dir = os.path.join(pkg_dir, "sub")
        os.makedirs(sub_dir, exist_ok=True)
        entry = os.path.join(pkg_dir, "main.py")
        sub_init = os.path.join(sub_dir, "__init__.py")
        helper = os.path.join(sub_dir, "helper.py")
        with open(entry, "w", encoding="utf-8") as fh:
            fh.write("from .sub import helper\n\n" "print(helper.answer())\n")
        with open(sub_init, "w", encoding="utf-8") as fh:
            fh.write("")
        with open(helper, "w", encoding="utf-8") as fh:
            fh.write("def answer() -> int:\n" "    return 7\n")

        exe = os.path.join(td, "submodule_alias.out")
        compile_python_multi(
            [entry],
            exe,
            entry_module="pkg.main",
            module_names=["pkg.main"],
        )
        r = subprocess.run([exe], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertEqual(r.stdout, "7\n")

    def test_assign_int_width_mismatch_casting(self):
        """Test that assigning an i32 parameter (from a C-ABI signature override)
        to an i64 stack slot compiles successfully with correct width casting."""
        from pcc.py_frontend.pipeline import compile_python

        td = tempfile.mkdtemp(prefix="pcc_cast_test_")
        self.addCleanup(self._rmtree, td)
        src = os.path.join(td, "prog.py")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write(textwrap.dedent("""
                from pcc.extern import c_abi_export

                @c_abi_export("pcc_gc_collect")
                def pcc_gc_collect(gen: int) -> int:
                    stored_gen: int = gen
                    return stored_gen
            """).lstrip())
        exe = os.path.join(td, "prog.ll")
        compile_python(src, exe, libpython_mode="off", emit_llvm_only=True)


if __name__ == "__main__":
    unittest.main()
