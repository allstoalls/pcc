"""The scaffold Module constructor must allocate an object before __init__."""

import os
from pathlib import Path
import subprocess

from pcc.llvm_capi import ir
from pcc.py_frontend.pipeline import compile_python_multi


def test_unnamed_and_named_scaffold_modules_are_real_objects(tmp_path, pcc_py_runtime_archive):
    source = tmp_path / "module_ctor.py"
    source.write_text('''
from pcc.llvm_capi import ir
def main():
    module = ir.Module()
    module.triple = "arm64-apple-darwin"
    module.data_layout = "e-p:64:64"
    print(module.name == "", module.triple, module.data_layout)
    named = ir.Module(name="named")
    print(named.name)
main()
''', encoding="utf-8")
    output = tmp_path / "module_ctor"
    compile_python_multi([str(Path(ir.__file__)), str(source)], str(output),
                         module_names=["pcc.llvm_capi.ir", "module_ctor"],
                         entry_module="module_ctor", recursive_stdlib=True,
                         ir_scaffold_mode="on", backend="self", libpython_mode="off",
                         runtime_archive=str(pcc_py_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f"GC{gc}: {result.stderr}"
        assert result.stdout == "True arm64-apple-darwin e-p:64:64\nnamed\n"
