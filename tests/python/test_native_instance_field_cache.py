from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path


def _compile_and_run(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    return subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)


def test_instance_field_cache_preserves_field_updates(tmp_path: Path):
    result = _compile_and_run(
        tmp_path,
        """
        class Box:
            def __init__(self, x: int) -> None:
                self.x = x
                self.y = x + 1

            def total(self) -> int:
                i = 0
                acc = 0
                while i < 6:
                    acc = acc + self.x + self.y
                    i = i + 1
                return acc

        box = Box(3)
        print(box.total())
        box.x = 10
        print(box.total())
        """,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["42", "84"]
