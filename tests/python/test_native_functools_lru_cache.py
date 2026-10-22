from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path


def test_top_level_lru_cache_user_function_is_not_noop(tmp_path: Path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(
        textwrap.dedent(
            """
            from functools import lru_cache

            calls = 0

            @lru_cache(maxsize=None)
            def f(x: int) -> int:
                global calls
                calls += 1
                return x + 1

            print(f(4))
            print(f(4))
            print(calls)
            """
        ).lstrip(),
        encoding="utf-8",
    )

    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="auto",
    )
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["5", "5", "1"]
