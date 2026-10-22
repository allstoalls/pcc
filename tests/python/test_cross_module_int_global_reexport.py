"""A computed raw integer global can be re-exported through a boxed module."""

from pathlib import Path
import subprocess


def test_computed_int_global_reexport(tmp_path: Path):
    from pcc.frontends.python.pipeline import compile_python

    (tmp_path / "provider.py").write_text(
        "from pcc.unsafe import null\nFIRST = 1\nSECOND = 2\nLIMIT = FIRST | SECOND\n"
    )
    (tmp_path / "facade.py").write_text("from provider import LIMIT\n")
    source = tmp_path / "main.py"
    source.write_text("from facade import LIMIT\nprint(LIMIT)\n")
    executable = tmp_path / "main"
    compile_python(str(source), str(executable), backend="self",
                   ir_scaffold_mode="on", libpython_mode="off")
    ran = subprocess.run([str(executable)], capture_output=True, text=True, timeout=15)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert ran.stdout.strip() == "3"
