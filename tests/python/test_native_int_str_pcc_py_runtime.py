from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

from pcc1_gate import repo_root

from tests.python.process_timeout import run_process_group_timeout


REPO_ROOT = repo_root()


def test_tagged_int_str_uses_pcc_python_runtime(
    tmp_path, monkeypatch, pcc_runtime_archive
):
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(pcc_runtime_archive))
    src = tmp_path / "int_str_pcc_py.py"
    exe = tmp_path / "int_str_pcc_py.out"
    src.write_text(
        textwrap.dedent(
            """
            def main() -> None:
                print(str(0))
                print(str(42))
                print(str(-7))
                print(str(1000000))
                print(str(1234567890123456))
                print(str(-1234567890123456))

            if __name__ == "__main__":
                main()
            """
        ).lstrip(),
        encoding="utf-8",
    )

    env = {**os.environ, "PCC_RUNTIME_CC": "pcc", "PCC_RUNTIME_HIGH": "py"}
    env.pop("LC_ALL", None)
    build = run_process_group_timeout(
        [
            sys.executable,
            "-m",
            "pcc",
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(src),
            "-o",
            str(exe),
        ],
        timeout=600.0,
        cwd=REPO_ROOT,
        env=env,
    )
    assert build.returncode == 0, build.stdout + build.stderr

    run = run_process_group_timeout([str(exe)], timeout=30.0, env=env)
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.strip().splitlines() == [
        "0",
        "42",
        "-7",
        "1000000",
        "1234567890123456",
        "-1234567890123456",
    ]
