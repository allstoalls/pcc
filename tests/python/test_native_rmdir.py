import os
import subprocess

from pcc.py_frontend.pipeline import compile_python


def test_native_rmdir_preserves_nonempty_directories_and_removes_empty_ones(tmp_path, pcc_py_runtime_archive):
    source = tmp_path / "remove_dir.py"
    source.write_text('''
import os
import sys
def main():
    root = sys.argv[1]
    os.makedirs(root, mode=0o700, exist_ok=False)
    with open(root + "/file", "w") as stream:
        stream.write("kept")
    try:
        os.rmdir(root)
    except OSError:
        print("nonempty kept")
    os.unlink(root + "/file")
    os.rmdir(root)
    print(os.path.exists(root))
main()
''')
    output = tmp_path / "remove_dir"
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   runtime_archive=str(pcc_py_runtime_archive))
    for gc in range(5):
        directory = tmp_path / ("directory-" + str(gc))
        result = subprocess.run([str(output), str(directory)], capture_output=True, text=True,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)), timeout=10)
        assert result.returncode == 0, result.stderr
        assert result.stdout == "nonempty kept\nFalse\n"
        assert not directory.exists()
