"""dict.update must consume iterable pairs, including one-shot generators."""

import os
import subprocess
import sys

from pcc.py_frontend.pipeline import compile_python


def test_update_iterable_pairs_preserves_partial_updates_and_errors(
    tmp_path, pcc_py_runtime_archive,
):
    source = tmp_path / "update_pairs.py"
    source.write_text('''
import gc
def pairs():
    yield ("alpha", 1)
    gc.collect()
    yield ["beta", 2]
def broken():
    yield ("kept", 3)
    raise RuntimeError("iteration failed")
class Mapping:
    def keys(self):
        return ["mapped", "more"]
    def __getitem__(self, key):
        gc.collect()
        return len(key)
def main():
    data = {"original": 0}
    print(data.update(pairs()))
    data.update((name, index) for index, name in enumerate(["x", "y"]))
    print(sorted(data.items()))
    try:
        data.update([("before", 4), ("bad", 5, 6), ("after", 7)])
    except ValueError:
        print("bad pair", "before" in data, "after" in data)
    try:
        data.update(broken())
    except RuntimeError:
        print("iteration failed", data["kept"])
    try:
        data.update(42)
    except TypeError:
        print("not iterable")
    data.update([("alpha", 9)], beta=10)
    print(data["alpha"], data["beta"])
    data.update(Mapping())
    print(data["mapped"], data["more"])
main()
''', encoding="utf-8")
    reference = subprocess.run([sys.executable, str(source)], capture_output=True,
                               text=True, timeout=10)
    assert reference.returncode == 0, reference.stderr
    output = tmp_path / "update_pairs"
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   runtime_archive=str(pcc_py_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f"GC{gc}: {result.stderr}"
        assert result.stdout == reference.stdout, f"GC{gc}: {result.stdout}"
