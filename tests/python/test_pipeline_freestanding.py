"""Facade and finite-boundary contracts for pipeline_freestanding."""
from __future__ import annotations

import pytest

from pcc.frontends.python import pipeline
from pcc.frontends.python import pipeline_freestanding


def test_pipeline_freestanding_facade_is_thin():
    assert pipeline._source_declares_freestanding_module is (
        pipeline_freestanding.source_declares_freestanding_module
    )
    assert pipeline._freestanding_allowed_external_symbols is (
        pipeline_freestanding.freestanding_allowed_external_symbols
    )
    assert pipeline._source_call_arguments is (
        pipeline_freestanding.source_call_arguments
    )
    assert pipeline._freestanding_module_scope_extern_bindings is (
        pipeline_freestanding.freestanding_module_scope_extern_bindings
    )
    assert pipeline._validate_freestanding_ir is (
        pipeline_freestanding.validate_freestanding_ir
    )


def test_freestanding_directive_must_be_one_unconditional_module_assignment():
    assert pipeline_freestanding.source_declares_freestanding_module(
        "__pcc_freestanding__ = True\n"
    )
    assert not pipeline_freestanding.source_declares_freestanding_module(
        "value = True\n"
    )
    with pytest.raises(pipeline.PyPipelineError, match="module-scope"):
        pipeline_freestanding.source_declares_freestanding_module(
            "__pcc_freestanding__: bool = True\n"
        )
    with pytest.raises(pipeline.PyPipelineError, match="only once"):
        pipeline_freestanding.source_declares_freestanding_module(
            "__pcc_freestanding__ = True\n__pcc_freestanding__ = True\n"
        )


@pytest.mark.parametrize("marker", ["__pcc_freestanding__", "__pcc_runtime_port__"])
@pytest.mark.parametrize("template", [
    '_MARKER = "{marker}"\n',
    "_MARKER = '{marker}'\n",
    '# {marker} = False\nvalue = 1\n',
    '"""{marker} = False"""\nvalue = 1\n',
    '"""doc\ndef phantom():\n{marker} = False\n"""\nvalue = 1\n',
    '_MARKER = "escaped \\\"{marker}"\n',
    'prefix{marker} = True\n',
    '{marker}suffix = True\n',
    'def local():\n    {marker} = False\n',
])
def test_directive_names_in_data_are_not_declarations(marker, template):
    source = template.format(marker=marker)
    assert not pipeline_freestanding._source_declares_module_directive(source, marker)
    assert pipeline_freestanding._source_declares_module_directive(
        source + marker + " = True # canonical\n", marker
    )


@pytest.mark.parametrize("marker", ["__pcc_freestanding__", "__pcc_runtime_port__"])
@pytest.mark.parametrize("template", [
    '{marker} = False\n',
    '{marker} = "True"\n',
    '{marker}: bool = True\n',
    'if True:\n    {marker} = True\n',
    '{marker} = True; other = 1\n',
    'other = {marker} = True\n',
])
def test_literal_masking_preserves_invalid_directive_rejection(marker, template):
    source = '_NAME = "' + marker + '"\n' + template.format(marker=marker)
    with pytest.raises(pipeline.PyPipelineError, match="module-scope"):
        pipeline_freestanding._source_declares_module_directive(source, marker)


def test_bootstrap_modules_can_store_directive_names_as_data():
    from pathlib import Path

    root = Path(pipeline_freestanding.__file__).parent
    for name in ("freestanding_constants.py", "codegen/generation_lowering.py"):
        source = (root / name).read_text()
        assert not pipeline_freestanding.source_declares_freestanding_module(source)


@pytest.mark.integration
def test_directive_lexical_scan_executes_native(tmp_path, pcc_runtime_archive):
    import inspect
    import os
    import subprocess
    from pcc.frontends.python.pipeline_import_scan import _source_module_scope_lines

    helpers = (
        _source_module_scope_lines,
        pipeline_freestanding._source_without_literals_or_comments,
        pipeline_freestanding._contains_directive_name,
        pipeline_freestanding._source_declares_module_directive,
    )
    source = "class PyPipelineError(Exception):\n    pass\n\n"
    source += "\n\n".join(inspect.getsource(helper) for helper in helpers)
    source += '''
def main():
    marker = "__pcc_freestanding__"
    literal = '_MARKER = "__pcc_freestanding__"\\n'
    assert not _source_declares_module_directive(literal, marker)
    assert not _source_declares_module_directive('# __pcc_freestanding__ = False\\n', marker)
    assert not _source_declares_module_directive('prefix__pcc_freestanding__ = True\\n', marker)
    assert _source_declares_module_directive(literal + marker + ' = True\\n', marker)
    count = 0
    try:
        _source_declares_module_directive(marker + ' = False\\n', marker)
    except PyPipelineError:
        count += 1
    try:
        _source_declares_module_directive('if True:\\n    ' + marker + ' = True\\n', marker)
    except PyPipelineError:
        count += 1
    assert count == 2
    print("DIRECTIVE_LEXICAL_OK")
main()
'''
    path = tmp_path / "directive_lexical.py"
    output = path.with_suffix("")
    path.write_text(source)
    pipeline.compile_python(str(path), str(output), backend="self",
                            libpython_mode="off", ir_scaffold_mode="on",
                            runtime_archive=str(pcc_runtime_archive))
    for collector in range(5):
        environment = dict(os.environ, PATH="", PCC_HOST_PYTHON="/usr/bin/false",
                           PCC_HOST_PCC="/usr/bin/false", PCC_GC_BACKEND=str(collector))
        result = subprocess.run([str(output)], env=environment, capture_output=True,
                                text=True, timeout=20)
        assert result.returncode == 0, (collector, result.stdout, result.stderr)
        assert result.stdout == "DIRECTIVE_LEXICAL_OK\n"
        assert result.stderr == ""


def test_freestanding_extern_scanner_admits_only_finite_exact_boundaries():
    source = '''\
from pcc.unsafe import malloc, call_ptr1
from pcc.extern import c_int, c_ptr, extern
__pcc_freestanding__ = True
_main = extern("main", (c_int, c_ptr, c_ptr), c_int)
_wrong = extern("not_owned", (c_ptr,), c_ptr)
'''
    assert pipeline_freestanding.freestanding_module_scope_extern_bindings(
        source
    ) == [
        ("main", "(c_int,c_ptr,c_ptr)", "c_int"),
        ("not_owned", "(c_ptr,)", "c_ptr"),
    ]
    assert pipeline_freestanding.freestanding_allowed_external_symbols(
        source
    ) == {"malloc", "__pcc_verified_indirect_call__", "main"}


def test_freestanding_ir_verifier_accepts_local_and_named_machine_calls_only():
    valid = '''\
define i64 @local(i64 %value) {
entry:
  ret i64 %value
}
define i64 @owner(i64 %value) {
entry:
  %same = call i64 @local(i64 %value)
  %mem = call ptr @malloc(i64 8)
  ret i64 %same
}
'''
    pipeline_freestanding.validate_freestanding_ir(valid, {"malloc"})

    escaped = '''\
define ptr @owner(ptr %value) {
entry:
  %result = call ptr @py_obj_str(ptr %value)
  ret ptr %result
}
'''
    with pytest.raises(pipeline.PyPipelineError, match="managed-runtime"):
        pipeline_freestanding.validate_freestanding_ir(escaped, set())
