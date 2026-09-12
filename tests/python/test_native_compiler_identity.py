import os
import subprocess
from pathlib import Path

from pcc.tools import compiler_identity


def test_native_identity_reads_its_own_signed_image_without_sources(tmp_path, pcc_py_runtime_archive):
    from pcc.py_frontend.pipeline import compile_python_multi

    source = tmp_path / "identity_probe.py"
    source.write_text('''
import sys
from pcc.tools.compiler_identity import native_executable_identity
def main():
    print(native_executable_identity(sys.executable))
main()
''')
    output = tmp_path / "identity_probe"
    compile_python_multi(
        [str(Path(compiler_identity.__file__)), str(source)], str(output),
        module_names=["pcc.tools.compiler_identity", "identity_probe"], entry_module="identity_probe",
        recursive_stdlib=True, backend="self", libpython_mode="off",
        runtime_archive=str(pcc_py_runtime_archive),
    )
    expected = compiler_identity.native_executable_identity(str(output)) + "\n"
    for gc in range(5):
        result = subprocess.run([str(output)], env=dict(os.environ, PCC_GC_BACKEND=str(gc)),
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert result.stdout == expected
