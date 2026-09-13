"""Generator finalization preserves cleanup, pending errors and resurrection."""
import os
import subprocess
import sys

from pcc.py_frontend.pipeline import compile_python


def test_generator_finalization_under_every_gc(tmp_path, pcc_py_runtime_archive):
    source = tmp_path / "gen_finalize.py"
    source.write_text('''import gc
events = []
rescued = []
def values(label):
    try:
        yield 1
    finally:
        events.append(label)
        gc.collect()
def unstarted():
    gen = values("unstarted")
def started():
    gen = values("normal")
    print(next(gen))
def discard_with_error(original):
    gen = values("error")
    next(gen)
    try:
        raise original
    finally:
        gen = None
def cyclic(box, rescue):
    try:
        yield 1
    finally:
        events.append("cycle")
        if rescue:
            rescued.append(box[0])
        gc.collect()
def make_cycle(rescue):
    box = []
    gen = cyclic(box, rescue)
    box.append(gen)
    next(gen)
def main():
    unstarted()
    gc.collect()
    print(events)
    started()
    gc.collect()
    print(events)
    original = KeyError("original")
    try:
        discard_with_error(original)
    except KeyError as found:
        print(found is original)
    print(events)
    make_cycle(False)
    gc.collect()
    print(events)
    make_cycle(True)
    gc.collect()
    print(events, len(rescued))
    try:
        next(rescued[0])
    except StopIteration:
        print("closed")
    rescued.clear()
    gc.collect()
    print(events)
main()
''', encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True,
                              text=True, timeout=10)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "gen_finalize"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   runtime_archive=str(pcc_py_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=15,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f"GC{gc}: {result.stderr}; {result.stdout}"
        assert result.stdout == expected.stdout, f"GC{gc}: {result.stdout}"
