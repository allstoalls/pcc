"""Integer literals above the tagged lane must evaluate to their value.

M5-SELFHOST-BIG-INT-LITERAL: on 2026-08-18 a literal above the tagged
small-int lane evaluated to 0 in pcc-compiled code (while the same value
COMPUTED was correct), which zeroed a mask in _emit_int_literal_object and
left pcc2 unable to print any integer.  On 2026-08-27 the defect no longer
reproduces: host pcc in the strict scaffold mode AND a HEAD-content pcc1
(both llvm and self backends) print every shape below correctly — fixed by
the intervening INT-P0-PROJ slices (runtime int(str), py_obj_as_int_object,
object-projection re-wraps).  This pins the strict-mode host arm; the pcc1
arm rides the stage gates.
"""
from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

_REPO_ROOT = Path(__file__).absolute().parents[2]


def _run_native(tmp_path: Path, source: str) -> subprocess.CompletedProcess:
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )
    return subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=60
    )


def test_over_lane_literals_evaluate_to_their_value(tmp_path):
    native = _run_native(
        tmp_path,
        """
        def main() -> None:
            print(9223372036854775808)
            print(0xFFFFFFFFFFFFFFFF)
            print(-9223372036854775808)
            print(-9223372036854775809)
            print(-18446744073709551615)
            print(170141183460469231731687303715884105727)
            print(4611686018427387904)
            print((1 << 63))
            print((1 << 64) - 1)

        main()
        """,
    )
    assert native.returncode == 0, native.stderr
    assert native.stdout.split() == [
        "9223372036854775808",
        "18446744073709551615",
        "-9223372036854775808",
        "-9223372036854775809",
        "-18446744073709551615",
        "170141183460469231731687303715884105727",
        "4611686018427387904",
        "9223372036854775808",
        "18446744073709551615",
    ], native.stdout


def test_raw_scaffold_module_global_promotes_over_i64_expression(tmp_path):
    from pcc.frontends.python.pipeline import compile_python_multi

    src = tmp_path / "bigint_global_probe.py"
    exe = tmp_path / "bigint_global_probe.out"
    src.write_text(
        "LIMIT = (1 << 64) - 1\n"
        "def main() -> None:\n"
        "    print(LIMIT)\n"
        "    print(0 <= 0 <= LIMIT)\n"
        "    print(LIMIT - 1 == 18446744073709551614)\n"
        "main()\n",
        encoding="utf-8",
    )
    compile_python_multi(
        [str(src)],
        str(exe),
        entry_module="pcc.bigint_global_probe",
        module_names=["pcc.bigint_global_probe"],
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
    )
    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "18446744073709551615",
        "True",
        "True",
    ]
