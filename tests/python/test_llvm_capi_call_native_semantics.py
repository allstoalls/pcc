from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).absolute().parents[2]
_IR_PY = _REPO_ROOT / "pcc" / "ir" / "ir.py"
_FLOAT_BITS_PY = _REPO_ROOT / "pcc" / "ir" / "support" / "_float_bits.py"


@pytest.mark.integration
def test_compiled_call_signature_replacement_and_subclass_match_host(
    tmp_path: Path,
) -> None:
    """Execute the exact/duck split in a native ir.py, not only host Python."""
    from pcc.frontends.python.pipeline import compile_python_multi

    probe = tmp_path / "ir_call_probe.py"
    probe.write_text(
        textwrap.dedent(
            """
            from pcc.ir.ir import DoubleType
            from pcc.ir.ir import FloatType
            from pcc.ir.ir import Function
            from pcc.ir.ir import FunctionType
            from pcc.ir.ir import HalfType
            from pcc.ir.ir import IRBuilder
            from pcc.ir.ir import Module
            from pcc.ir.ir import Value
            from pcc.ir.ir import VoidType


            class DynamicFunction(Function):
                def __init__(
                    self,
                    module,
                    stored_ftype,
                    dynamic_ftype,
                ) -> None:
                    self._dynamic_ftype = dynamic_ftype
                    self._dynamic_name = "dynamic_callee"
                    Function.__init__(
                        self,
                        module,
                        stored_ftype,
                        name="stored_callee",
                    )

                @property
                def ftype(self):
                    return self._dynamic_ftype

                @ftype.setter
                def ftype(self, value) -> None:
                    self._stored_ftype = value

                @property
                def name(self) -> str:
                    return self._dynamic_name

                @name.setter
                def name(self, value: str) -> None:
                    self._stored_name = value


            def main() -> None:
                module = Module("native-call-semantics")
                half = HalfType()
                float_ty = FloatType()
                double = DoubleType()
                signature = FunctionType(VoidType(), [float_ty])
                callee = Function(module, signature, name="callee")
                caller = Function(
                    module,
                    FunctionType(VoidType(), []),
                    name="caller",
                )
                block = caller.append_basic_block("entry")
                builder = IRBuilder(block)
                builder.call(callee, [Value(float_ty, "%one")])
                signature.args = [double]
                builder.call(callee, [Value(double, "%two")])
                signature.args = [half]
                builder.call(callee, [Value(half, "%three")])
                builder.ret_void()
                print(block.render())

                dynamic = DynamicFunction(
                    module,
                    FunctionType(VoidType(), [float_ty]),
                    FunctionType(double, [double]),
                )
                dynamic_caller = Function(
                    module,
                    FunctionType(VoidType(), []),
                    name="dynamic_caller",
                )
                dynamic_block = dynamic_caller.append_basic_block("entry")
                dynamic_builder = IRBuilder(dynamic_block)
                dynamic_builder.call(dynamic, [Value(double, "%dynamic")])
                dynamic_builder.ret_void()
                print(dynamic_block.render())


            main()
            """
        ).lstrip(),
        encoding="utf-8",
    )

    executable = tmp_path / "ir_call_probe"
    compile_python_multi(
        [str(probe), str(_IR_PY), str(_FLOAT_BITS_PY)],
        str(executable),
        module_names=[
            "ir_call_probe",
            "pcc.ir.ir",
            "pcc.ir.support._float_bits",
        ],
        entry_module="ir_call_probe",
        backend="self",
        ir_scaffold_mode="on",
        libpython_mode="off",
    )

    run_env = os.environ.copy()
    run_env["PCC_GC_BACKEND"] = "0"
    run_env.pop("PCC_DEBUG_IR_CALL", None)
    run_env.pop("PCC_DEBUG_IR_RENDER", None)
    host = subprocess.run(
        [sys.executable, str(probe)],
        cwd=_REPO_ROOT,
        env=run_env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    native = subprocess.run(
        [str(executable)],
        cwd=_REPO_ROOT,
        env=run_env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert host.returncode == 0, host.stderr
    assert native.returncode == 0, native.stderr
    assert native.stderr == host.stderr == ""
    assert native.stdout == host.stdout
    assert "call void (float) @callee(float %one)" in native.stdout
    assert "call void (double) @callee(double %two)" in native.stdout
    assert "call void (half) @callee(half %three)" in native.stdout
    assert "call double (double) @dynamic_callee(double %dynamic)" in native.stdout
    assert "@stored_callee" not in native.stdout
