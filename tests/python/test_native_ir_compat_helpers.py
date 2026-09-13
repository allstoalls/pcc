"""Runtime helpers imported from the scaffold facade bind real owned code."""
import os
from pathlib import Path
import subprocess
from pcc.py_frontend.pipeline import compile_python_multi


def test_ir_compat_helpers_execute_with_aliases(tmp_path, pcc_py_runtime_archive):
    repo = Path(__file__).resolve().parents[2]
    source = tmp_path / 'ir_helpers.py'
    source.write_text('''from pcc.llvm_capi.compat import ir, add_raw_function_attribute as add_attribute, set_struct_body

def main():
    module = ir.Module(name="helpers")
    integer = ir.IntType(32)
    function = ir.Function(module, ir.FunctionType(integer, []), name="main")
    block = function.append_basic_block("entry")
    builder = ir.IRBuilder(block)
    builder.ret(ir.values.Constant(integer, 42))
    add_attribute(function, "noinline")
    alias = add_attribute
    alias(function, "nounwind")
    text = str(module)
    print("noinline" in text, "nounwind" in text, "ret i32 42" in text)
    context = ir.Context()
    pair = ir.IdentifiedStructType(context, "Pair")
    set_struct_body(pair, (integer, integer), packed=True)
    print(pair.get_declaration())
main()
''', encoding='utf-8')
    binary = tmp_path / 'ir_helpers'
    compile_python_multi([str(repo / 'pcc/llvm_capi/ir.py'), str(source)], str(binary),
                         module_names=['pcc.llvm_capi.ir', 'ir_helpers'], entry_module='ir_helpers',
                         recursive_stdlib=True, backend='self', libpython_mode='off',
                         runtime_archive=str(pcc_py_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=15,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f'GC{gc}: {result.stderr}'
        assert result.stdout == 'True True True\n%Pair = type <{ i32, i32 }>\n', f'GC{gc}: {result.stdout}'
