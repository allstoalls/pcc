from __future__ import annotations

import os
import subprocess
from pathlib import Path

from pcc.frontends.python.pipeline import compile_python


def test_decimal_type_identity_imports_without_libpython(tmp_path: Path) -> None:
    src = tmp_path / "decimal_identity.py"
    src.write_text(
        "from decimal import Decimal\n"
        "print(isinstance(1, Decimal))\n"
        "try:\n"
        "    Decimal('1.25')\n"
        "except NotImplementedError as exc:\n"
        "    print(str(exc))\n",
        encoding="utf-8",
    )
    exe = tmp_path / "decimal_identity"
    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )

    env = os.environ.copy()
    env.pop("LC_ALL", None)
    run = subprocess.run(
        [str(exe)],
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == [
        "False",
        "pcc-native decimal.Decimal construction is not implemented",
    ]
