"""Dual-track object model stress tests."""
import os
import subprocess
import textwrap
from pathlib import Path
import pytest

REPO = Path(__file__).absolute().parents[2]
_PCC1_ENV = os.environ.get("PCC1_BINARY")
if _PCC1_ENV:
    PCC1 = Path(_PCC1_ENV)
else:
    _PCC1_CANDIDATES = (
        REPO / "build" / "bootstrap-pytest-self" / "pcc1",
        REPO / "build" / "bootstrap" / "pcc1",
    )
    PCC1 = next((p for p in _PCC1_CANDIDATES if p.exists()), _PCC1_CANDIDATES[-1])

pytestmark = pytest.mark.pcc_gate(probe="pcc1")


def _run_test(tmp_path, monkeypatch, source, compiler):
    src = tmp_path / "stress.py"; exe = tmp_path / "stress.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    if compiler == "pcc1":
        if not PCC1.exists(): pytest.fail("no pcc1 even after auto-provisioning")
        subprocess.run(
            [str(PCC1), str(src), "-o", str(exe), "--ir-scaffold=on", "--python-libpython=off"],
            check=True,
        )
    else:
        from pcc.frontends.python.pipeline import compile_python
        compile_python(str(src), str(exe), ir_scaffold_mode="on", libpython_mode="off")
    return subprocess.run([str(exe)], capture_output=True, text=True).stdout.strip()

@pytest.mark.parametrize("compiler", ["python", "pcc1"])
def test_inheritance_stress(tmp_path, monkeypatch, compiler):
    source = """
        class A:
            def __init__(self): self.a = 1
        class B(A):
            def __init__(self): super().__init__(); self.b = 2
        def main():
            o = B(); print(o.a, o.b)
        if __name__ == "__main__": main()
    """
    assert _run_test(tmp_path, monkeypatch, source, compiler) == "1 2"
