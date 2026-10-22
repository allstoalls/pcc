"""GC effectiveness and performance matrix."""
import os
import subprocess
import textwrap
import time
import pytest

@pytest.mark.parametrize("backend", [0, 1, 2, 3, 4])
def test_gc_matrix(tmp_path, backend):
    from pcc.frontends.python.pipeline import compile_python
    source = """
        import gc
        class Node:
            def __init__(self, v): self.next = None
        def main():
            for _ in range(100):
                n = Node(0); n.next = n; gc.collect()
            print("done")
        if __name__ == "__main__": main()
    """
    src = tmp_path / "gc.py"; exe = tmp_path / "gc.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    compile_python(str(src), str(exe), ir_scaffold_mode="on")
    env = os.environ.copy(); env["PCC_GC_BACKEND"] = str(backend)
    res = subprocess.run([str(exe)], capture_output=True, text=True, env=env, timeout=60)
    assert res.returncode == 0 and "done" in res.stdout
