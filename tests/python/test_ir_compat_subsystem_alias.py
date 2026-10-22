"""Bare ``ir.X`` values through the per-subsystem compat spellings.

The C codegen imports ``from pcc.ir.compat import ir_c as ir``.  In ON
mode that import is a compile-time scaffold: compat is dropped from the
closure and ``pcc.ir.ir`` is linked instead, so ``ir`` has no runtime
binding.  Only the scaffold's recognised ``ir.X`` symbols were lowered;
``isinstance(t, ir.Type)``, ``ir.PhiInstr`` and the ``ir.Undefined``
singleton fell through to a global lookup, which is how pcc1 delegating a C
input died with ``NameError: name 'ir' is not defined`` in
``CCodegen._get_ir_type``.
"""

import os
import subprocess
import sys

_PROGRAM = '''from pcc.ir.compat import ir_c as ir


def classify(value):
    return isinstance(value, ir.Type)


print(classify(ir.IntType(32)), classify(3))
print(ir.Undefined is ir.Undefined)
print(ir.PhiInstr.__name__, ir.MetaDataString.__name__)
'''


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_compat_subsystem_alias_exposes_every_provider_export(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    expected = subprocess.run(
        [sys.executable, "-c", _PROGRAM], capture_output=True, text=True,
        timeout=60, env=dict(os.environ, PYTHONPATH=_repo_root()),
    )
    assert expected.returncode == 0, expected.stderr
    assert expected.stdout == "True False\nTrue\nPhiInstr MetaDataString\n"
    source = tmp_path / "ir_alias.py"
    source.write_text(_PROGRAM, encoding="utf-8")
    (tmp_path / "pcc").symlink_to(
        os.path.join(_repo_root(), "pcc"), target_is_directory=True
    )
    binary = tmp_path / "ir_alias"
    python_program_compiler(
        str(source), str(binary), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60)
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout == expected.stdout
