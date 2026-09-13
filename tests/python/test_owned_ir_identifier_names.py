"""Owned passes preserve full unquoted IR names, including '-' and '$'."""

import os
from pathlib import Path
import subprocess

from pcc.native_ir.ir_mutator import MutableModule
from pcc.py_frontend.compiled_owned_passes import run_owned_passes
from pcc.backend.self_backend_parse import parse_self_backend_module
from pcc.backend.self_backend_verify import verify_parsed_module


IR = '''target triple = "arm64-apple-darwin23.6.0"
define internal i32 @worker-name$(i1 %choose-path$, i32 %input-value$) {
entry-block$:
  %value-slot$ = alloca i32
  br i1 %choose-path$, label %yes-path$, label %no-path$
yes-path$:
  %sum-value$ = add i32 %input-value$, 1
  store i32 %sum-value$, ptr %value-slot$
  br label %merge-path$
no-path$:
  store i32 7, ptr %value-slot$
  br label %merge-path$
merge-path$:
  %result-value$ = load i32, ptr %value-slot$
  ret i32 %result-value$
}
define i32 @main() {
entry:
  %answer-value$ = call i32 @worker-name$(i1 true, i32 41)
  ret i32 %answer-value$
}
'''


def test_owned_parser_preserves_function_block_argument_and_result_names():
    module = MutableModule.parse(IR)
    function = module.functions[0]
    assert function.name == "worker-name$"
    assert [arg.name for arg in function.args] == ["choose-path$", "input-value$"]
    assert [block.name for block in function.blocks] == [
        "entry-block$", "yes-path$", "no-path$", "merge-path$",
    ]
    assert module.serialize() == IR


def test_owned_passes_keep_identifiers_whole_through_phi_insertion_and_inlining():
    # The backend currently admits '$' in global symbols but rejects '-'.
    # Keep its separate ABI restriction explicit; local/block names still
    # exercise both characters through the owned transformations.
    source = IR.replace("worker-name$", "worker$name")
    promoted = run_owned_passes(source, ["mem2reg", "sroa"], True)
    assert "alloca" not in promoted
    assert " phi i32 " in promoted
    verify_parsed_module(parse_self_backend_module(promoted))
    result = run_owned_passes(
        promoted, ["instsimplify", "instcombine", "simplifycfg", "dce", "inline"], True,
    )
    verify_parsed_module(parse_self_backend_module(result))
    assert "call i32 @worker$name" not in result


def test_compiled_owned_parser_reads_full_identifier_names(tmp_path, pcc_py_runtime_archive):
    from pcc.native_ir import ir_mutator, text_tokens
    from pcc.py_frontend.pipeline import compile_python_multi

    source = tmp_path / "parse_names.py"
    source.write_text(
        "from pcc.native_ir.ir_mutator import MutableModule\n"
        "def main():\n"
        "    module = MutableModule.parse(" + repr(IR) + ")\n"
        "    function = module.functions[0]\n"
        "    print(function.name)\n"
        "    print(function.args[0].name)\n"
        "    print(function.blocks[0].name)\n"
        "    print(module.serialize() == " + repr(IR) + ")\n"
        "main()\n", encoding="utf-8",
    )
    output = tmp_path / "parse_names"
    compile_python_multi(
        [str(Path(text_tokens.__file__)), str(Path(ir_mutator.__file__)), str(source)],
        str(output), module_names=["pcc.native_ir.text_tokens", "pcc.native_ir.ir_mutator", "parse_names"],
        entry_module="parse_names", recursive_stdlib=True, backend="self",
        libpython_mode="off", runtime_archive=str(pcc_py_runtime_archive),
    )
    for gc in range(5):
        result = subprocess.run([str(output)], capture_output=True, text=True,
                                timeout=15, env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f"GC{gc}: {result.stderr}"
        assert result.stdout == "worker-name$\nchoose-path$\nentry-block$\nTrue\n"
