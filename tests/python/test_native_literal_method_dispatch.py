from __future__ import annotations

import subprocess
import textwrap

from pcc.frontends.python.pipeline import compile_python


def _source() -> str:
    return textwrap.dedent(
        """
        class Machine:
            def alu(self, core: int, op: int, value: int) -> int:
                return core + op + value

            def valu(self, core: int, op: int, value: int) -> int:
                return core * 10 + op + value

            def step(self, name: str, slot):
                dispatch = {
                    "alu": self.alu,
                    "valu": self.valu,
                }
                return dispatch[name](7, *slot)

        def main():
            m = Machine()
            print(m.step("alu", (3, 4)))
            print(m.step("valu", (5, 6)))
            try:
                m.step("flow", (1, 2))
            except KeyError:
                print("missing")

        main()
        """
    ).lstrip()


def test_literal_self_method_dict_dispatch_preserves_stararg_semantics(tmp_path):
    src = tmp_path / "literal_method_dispatch.py"
    src.write_text(_source(), encoding="utf-8")
    exe = tmp_path / "literal_method_dispatch.out"

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)

    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["14", "81", "missing"]


def test_literal_self_method_dict_dispatch_uses_direct_blocks(tmp_path):
    src = tmp_path / "literal_method_dispatch.py"
    src.write_text(_source(), encoding="utf-8")
    ll = tmp_path / "literal_method_dispatch.ll"

    compile_python(
        str(src),
        str(ll),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
        emit_llvm_only=True,
    )
    ir_text = ll.read_text(encoding="utf-8")

    assert "method.dict.alu" in ir_text
    assert "method.dict.valu" in ir_text
    assert "bound.alu.func" not in ir_text
    assert "bound.valu.func" not in ir_text
