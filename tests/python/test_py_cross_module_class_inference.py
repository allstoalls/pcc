"""Regression: cross-module class instance type inference + @property.

Gap 1 — ``main.py`` imports a class from ``lib.py`` and uses an instance
field. type_infer must propagate the imported class's ``ClassType`` into
main.py's ``_InferCtx.class_types`` so that ``C("ok")`` returns a typed
instance and ``c.x`` resolves to the declared field type. Without this
the constructor call types as DynType, ``c.x`` lowers to
``py_obj_getattr`` and the multi-file closed-world compile emits
``py_cpy_*`` fallbacks for the attribute access chain.

Gap 2 — ``@property`` return-type propagation. A property declared
``@property def name(self) -> str`` must register its return type so
``c.name``'s static type is ``str``. Without this, downstream
``n.rfind(".")`` etc. on a property result falls back to dyn dispatch
and the binary pulls libpython.

The Gap 1 regression also exercises ``--python-libpython=off``
multi-file compile; an unresolved cross-module class would surface as
``Python pipeline requires libpython fallback for multi-file compile``.

See ``docs/investigations/codegen-mixin-self-cross-module-types.md`` and
``docs/investigations/pcc-py-type-infer-property-return-type.md``.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import textwrap
import unittest


_PY_CPY_DISPATCH_RE = re.compile(
    r"\bcall [^\n]*@py_cpy_(?:getattr|call1|call2|call3|call_kw|call_argv)\b"
)


class CrossModuleClassInferenceTests(unittest.TestCase):
    def _rmtree(self, path):
        import shutil

        shutil.rmtree(path, ignore_errors=True)

    def _compile_multi(
        self,
        files: dict[str, str],
        entry_module: str,
    ) -> tuple[str, int, str]:
        """Compile ``files`` twice (once to binary, once to IR) under
        the closed-world setting that mirrors the bootstrap gate.

        Returns ``(stdout, exit_code, llvm_ir_text)``. A separate
        ``emit_llvm_only=True`` pass captures the IR so the test can
        assert no ``py_cpy_*`` dispatch helpers appear (Gap 1 + Gap 2
        success criterion). If either compile call raises because pcc
        still requires libpython fallback, the test fails with the
        pipeline's error message.
        """
        from pcc.frontends.python.pipeline import compile_python_multi

        td = tempfile.mkdtemp(prefix="pcc_xmod_test_")
        self.addCleanup(self._rmtree, td)
        src_paths = []
        for rel, source in files.items():
            dst = os.path.join(td, rel)
            os.makedirs(os.path.dirname(dst) or td, exist_ok=True)
            with open(dst, "w", encoding="utf-8") as fh:
                fh.write(textwrap.dedent(source).lstrip())
            src_paths.append(dst)
        exe = os.path.join(td, "a.out")
        ir_out = os.path.join(td, "a.ll")
        compile_python_multi(
            src_paths,
            ir_out,
            entry_module=entry_module,
            emit_llvm_only=True,
            ir_scaffold_mode="on",
            libpython_mode="off",
            recursive_stdlib=True,
        )
        compile_python_multi(
            src_paths,
            exe,
            entry_module=entry_module,
            backend="self",
            ir_scaffold_mode="on",
            libpython_mode="off",
            recursive_stdlib=True,
        )
        r = subprocess.run([exe], capture_output=True, text=True, timeout=20)
        self._last_run_stderr = r.stderr
        ir_text = ""
        if os.path.isfile(ir_out):
            with open(ir_out, "r", encoding="utf-8") as fh:
                ir_text = fh.read()
        return r.stdout, r.returncode, ir_text

    def _assert_no_cpy_dispatch(self, ir_text: str, msg: str) -> None:
        hits = _PY_CPY_DISPATCH_RE.findall(ir_text or "")
        self.assertEqual(
            hits,
            [],
            f"{msg}: found {len(hits)} py_cpy_* dispatch calls: {hits[:3]}",
        )

    # -- Gap 1 ----------------------------------------------------------

    def test_gap1_cross_module_class_constructor_returns_typed_instance(self):
        stdout, code, ir_text = self._compile_multi(
            {
                "lib.py": """
                class C:
                    def __init__(self, x: str) -> None:
                        self.x = x
                """,
                "main.py": """
                from lib import C

                def main() -> None:
                    c = C("ok")
                    print(c.x)

                main()
                """,
            },
            entry_module="main",
        )
        self.assertEqual(code, 0, f"binary exited {code}, stdout={stdout!r}")
        self.assertEqual(stdout, "ok\n")
        self._assert_no_cpy_dispatch(ir_text, "Gap 1 cross-module class")

    def test_gap1_module_qualified_constructor_call(self):
        """``import lib; lib.C(...)`` form — the explicit module-attr
        receiver path. type_infer must resolve ``lib.C`` as the class
        even though ``C`` is reached via attribute access on the module
        alias, not a direct ``from-import``."""
        stdout, code, ir_text = self._compile_multi(
            {
                "lib.py": """
                class C:
                    def __init__(self, x: str) -> None:
                        self.x = x
                """,
                "main.py": """
                import lib

                def main() -> None:
                    c = lib.C("ok")
                    print(c.x)

                main()
                """,
            },
            entry_module="main",
        )
        self.assertEqual(code, 0, f"binary exited {code}, stdout={stdout!r}")
        self.assertEqual(stdout, "ok\n")
        self._assert_no_cpy_dispatch(ir_text, "Gap 1 module-qualified")

    def test_mixin_self_scalar_augassign_uses_receiver_field_layout(self):
        """A mixin method's ``self.field += value`` must use the concrete
        receiver's inferred field layout, just like ordinary attribute loads
        and stores.  Dynamic getattr cannot see the composed self-host class's
        fixed field slot and used to turn ``_tmp_counter += 1`` into
        ``py_obj_inplace_op(NULL, 1, add)`` inside pcc1.
        """
        stdout, code, ir_text = self._compile_multi(
            {
                "ops.py": """
                class OpsMixin:
                    def bump(self) -> int:
                        self.count += 1
                        return self.count
                """,
                "main.py": """
                from ops import OpsMixin

                class Counter(OpsMixin):
                    count: int

                    def __init__(self) -> None:
                        self.count = 0

                def main() -> None:
                    counter = Counter()
                    print(counter.bump())
                    print(counter.bump())

                main()
                """,
            },
            entry_module="main",
        )
        self.assertEqual(
            code,
            0,
            f"binary exited {code}, stdout={stdout!r}, "
            f"stderr={getattr(self, '_last_run_stderr', '')!r}",
        )
        self.assertEqual(stdout, "1\n2\n")
        bump_match = re.search(
            r"(?m)^define\b[^\n]*OpsMixin[^\n]*bump[^\n]*\{$",
            ir_text,
        )
        self.assertIsNotNone(
            bump_match,
            "emitted IR is missing the OpsMixin.bump definition",
        )
        bump_start = bump_match.start()
        bump_end = ir_text.index("\n}", bump_start)
        bump_ir = ir_text[bump_start:bump_end]
        self.assertIn("@py_instance_get_field", bump_ir)
        self.assertNotIn(
            "@py_obj_getattr",
            bump_ir,
            "typed mixin self-field augassign must not use dynamic getattr",
        )
        self._assert_no_cpy_dispatch(ir_text, "mixin self scalar augassign")

    def test_module_qualified_module_global_value(self):
        stdout, code, ir_text = self._compile_multi(
            {
                "lib.py": """
                __all__ = ("alpha", "beta")
                """,
                "main.py": """
                import lib

                def main() -> None:
                    print(len(lib.__all__))

                main()
                """,
            },
            entry_module="main",
        )
        self.assertEqual(code, 0, f"binary exited {code}, stdout={stdout!r}")
        self.assertEqual(stdout, "2\n")
        self._assert_no_cpy_dispatch(ir_text, "module-qualified module global")

    def test_unannotated_cross_module_method_keeps_object_return_abi(self):
        stdout, code, ir_text = self._compile_multi(
            {
                "lib.py": """
                class Registry:
                    def __init__(self, value: str) -> None:
                        self.value = value

                    def get(self):
                        return self.value

                    def touch(self):
                        self.value = "changed"
                """,
                "main.py": """
                from lib import Registry

                def main() -> None:
                    registry = Registry("kept")
                    print(registry.get())
                    print(registry.touch() is None)
                    print(registry.get())

                main()
                """,
            },
            entry_module="main",
        )
        self.assertEqual(
            code,
            0,
            f"binary exited {code}, stdout={stdout!r}, "
            f"stderr={getattr(self, '_last_run_stderr', '')!r}",
        )
        self.assertEqual(stdout, "kept\nTrue\nchanged\n")
        get_match = re.search(
            r"(?m)^define\s+ptr\s+@[^\n]*Registry[^\n]*get[^\n]*\{$",
            ir_text,
        )
        self.assertIsNotNone(
            get_match,
            "an unannotated method definition must use the dynamic pointer ABI",
        )
        touch_match = re.search(
            r"(?m)^define\s+ptr\s+@[^\n]*Registry[^\n]*touch[^\n]*\{$",
            ir_text,
        )
        self.assertIsNotNone(
            touch_match,
            "unannotated implicit fallthrough must return the Python None object",
        )
        self._assert_no_cpy_dispatch(ir_text, "unannotated cross-module method")

    def test_unannotated_cross_module_function_keeps_object_return_abi(self):
        stdout, code, ir_text = self._compile_multi(
            {
                "lib.py": """
                def identity(value):
                    return value
                """,
                "main.py": """
                from lib import identity

                def main() -> None:
                    print(identity("function-value"))

                main()
                """,
            },
            entry_module="main",
        )
        self.assertEqual(
            code,
            0,
            f"binary exited {code}, stdout={stdout!r}, "
            f"stderr={getattr(self, '_last_run_stderr', '')!r}",
        )
        self.assertEqual(stdout, "function-value\n")
        identity_match = re.search(
            r"(?m)^define\s+ptr\s+@[^\n]*identity[^\n]*\{$",
            ir_text,
        )
        self.assertIsNotNone(
            identity_match,
            "an unannotated function definition must use the dynamic pointer ABI",
        )
        self._assert_no_cpy_dispatch(ir_text, "unannotated cross-module function")

    def test_constructor_initialized_cross_module_field_keeps_receiver_type(self):
        stdout, code, ir_text = self._compile_multi(
            {
                "lib.py": """
                class Store:
                    def __init__(self, path: str) -> None:
                        self.path = path

                class Context:
                    def __init__(self) -> None:
                        self.store = Store("durable.jsonl")

                    def get(self, name: str):
                        return self.store

                class Kernel:
                    def __init__(self) -> None:
                        self.context = Context()
                """,
                "main.py": """
                from lib import Kernel, Store

                class Runtime:
                    def __init__(self) -> None:
                        self.kernel = Kernel()

                    def resolve(self) -> Store:
                        return self.kernel.context.get("sessionStore")

                def main() -> None:
                    print(Runtime().resolve().path)

                main()
                """,
            },
            entry_module="main",
        )
        self.assertEqual(
            code,
            0,
            f"binary exited {code}, stdout={stdout!r}, "
            f"stderr={getattr(self, '_last_run_stderr', '')!r}",
        )
        self.assertEqual(stdout, "durable.jsonl\n")
        self.assertIsNone(
            re.search(r"\bcall\s+[^\n]*@py_dict_get_default\b", ir_text),
            "a typed user Context.get call must not lower as dict.get",
        )
        self._assert_no_cpy_dispatch(
            ir_text, "constructor-initialized cross-module field"
        )

    # -- Gap 2 ----------------------------------------------------------

    def test_gap2_property_return_type_propagates_single_file(self):
        """Single-file regression: ``c.name``'s static type must be ``str``
        because the ``@property`` declares ``-> str``. The probe also
        checks that inside ``suffix``, ``self.name`` is typed as ``str``
        so a subsequent str-method (``rfind`` would belong here) does not
        fall back to dynamic dispatch. The Gap 2 fix is unblocked
        independent of cross-module work because everything lives in one
        file."""
        stdout, code, ir_text = self._compile_multi(
            {
                "entry.py": """
                class C:
                    def __init__(self) -> None:
                        self._x = "abc.txt"

                    @property
                    def name(self) -> str:
                        return self._x

                    @property
                    def suffix(self) -> str:
                        n = self.name
                        return n

                def main() -> None:
                    c = C()
                    print(c.name)
                    print(c.suffix)

                main()
                """,
            },
            entry_module="entry",
        )
        self.assertEqual(code, 0, f"binary exited {code}, stdout={stdout!r}")
        self.assertEqual(stdout, "abc.txt\nabc.txt\n")
        self._assert_no_cpy_dispatch(ir_text, "Gap 2 property return-type")

    def test_gap2_str_method_on_property_result_single_file(self):
        """Mirrors the pathlib.PurePath.suffix shape: a ``str``-returning
        property whose result feeds into ``str.rfind`` and slice. If the
        property return-type does not propagate, ``n.rfind('.')`` falls
        back to ``py_cpy_getattr(n, 'rfind')`` and the closed-world gate
        rejects the multi-file compile. This is the regression that
        specifically blocks ``pathlib_parts`` from passing today."""
        stdout, code, ir_text = self._compile_multi(
            {
                "entry.py": """
                class C:
                    def __init__(self) -> None:
                        self._raw = "abc.txt"

                    @property
                    def name(self) -> str:
                        return self._raw

                    @property
                    def suffix(self) -> str:
                        n = self.name
                        i = n.rfind(".")
                        if i <= 0:
                            return ""
                        return n[i:]

                def main() -> None:
                    c = C()
                    print(c.name)
                    print(c.suffix)

                main()
                """,
            },
            entry_module="entry",
        )
        self.assertEqual(code, 0, f"binary exited {code}, stdout={stdout!r}")
        self.assertEqual(stdout, "abc.txt\n.txt\n")
        self._assert_no_cpy_dispatch(ir_text, "Gap 2 str method on property")

    # -- Gap 1 + Gap 2 (stdlib flavor, smaller than pathlib_parts) -------

    def test_gap1_gap2_pathlib_purepath_name_only(self):
        """Bridge regression that uses the real ``pcc/stdlib/pathlib``
        skeleton: only ``PurePath(...)`` constructor + ``.name`` property
        — no ``.suffix`` / ``.rfind`` chain. Verifies the two gaps as
        composed on the existing stdlib shim. Drives ``pathlib_parts``
        toward green without yet requiring ``rfind`` on a dyn-typed
        property result."""
        stdout, code, ir_text = self._compile_multi(
            {
                "entry.py": """
                import pathlib

                def main() -> None:
                    p = pathlib.PurePath("/tmp/a.txt")
                    print(p.name)

                main()
                """,
            },
            entry_module="entry",
        )
        self.assertEqual(code, 0, f"binary exited {code}, stdout={stdout!r}")
        self.assertEqual(stdout, "a.txt\n")
        self._assert_no_cpy_dispatch(ir_text, "stdlib pathlib bridge")


if __name__ == "__main__":
    unittest.main()
