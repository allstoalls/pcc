"""Host lowering of the exact x86 packed classification generator and consumer.

The real owned ``struct`` provider and its float-bits dependency participate in
the closed-world context. The unchanged symbol-identity lookup is replaced by
an explicit ordinary tuple-returning callback. This is owned-IR shape evidence,
not native execution, lifetime parity, or whole-linker qualification.
"""

import ast
from pathlib import Path
import re
import textwrap

from pcc.backend import elf_x86_64 as elf
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.pipeline_context import build_closed_world_context
from pcc.frontends.python.type_infer import infer_module
from tests.owned_ir_validation import verify_ir_text


def _provider_source():
    source = Path(elf.__file__).read_text(encoding="utf-8")
    nodes = ast.parse(source).body
    constants = {"_RELA", "R_X86_64_GOTTPOFF"}
    selected = [
        node for node in nodes
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id in constants
                for target in node.targets)
    ]
    assert len(selected) == len(constants)
    helper = next(node for node in nodes if isinstance(node, ast.FunctionDef)
                  and node.name == "_x86_packed_classification_rows")
    consumers = [
        node for root in nodes for node in ast.walk(root)
        if isinstance(node, ast.If)
        and any(isinstance(statement, ast.For)
                and isinstance(statement.iter, ast.Call)
                and isinstance(statement.iter.func, ast.Name)
                and statement.iter.func.id == helper.name
                for statement in node.body)
    ]
    assert len(consumers) == 1
    branch = consumers[0]
    assert len(branch.body) == 4
    loop = branch.body[2]
    assert isinstance(loop, ast.For)
    assert isinstance(loop.target, ast.Tuple) and len(loop.target.elts) == 3
    lines = source.splitlines(keepends=True)
    consumer = textwrap.dedent("".join(
        lines[branch.body[0].lineno - 1:branch.body[-1].end_lineno]
    ))
    prelude = "from __future__ import annotations\nimport struct\n"
    # Only this pre-existing lookup is a stand-in. The generator, its Struct
    # global, the pop/delete ownership transfer, and the for body are exact.
    callback = (
        "def _symbol_identity(objects, definitions, object_index, symbol_index):\n"
        "    return (object_index, symbol_index)\n"
    )
    probe = (
        "def probe(relocations, objects, definitions, object_index, "
        "got_reloc_types, got_seen, got_identities):\n"
        + textwrap.indent(consumer, "    ")
        + "    return got_identities\n"
    )
    return "\n\n".join((
        prelude,
        *(ast.get_source_segment(source, node) for node in selected),
        ast.get_source_segment(source, helper),
        callback,
        probe,
    ))


def _body(text, symbol):
    match = re.search(r"^define [^\n]*@" + re.escape(symbol)
                      + r"\([^\n]*\).*?^}", text, re.M | re.S)
    assert match is not None, symbol
    return match.group(0)


def test_exact_packed_generator_and_consumer_lower_with_owned_struct(tmp_path, monkeypatch):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "0")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "0")
    root = Path(elf.__file__).resolve().parents[2]
    provider_name = "pcc.backend.elf_x86_64"
    provider = tmp_path / "elf_packed_provider.py"
    provider.write_text(_provider_source(), encoding="utf-8")
    modules, exports, derived = build_closed_world_context(
        [str(root / "pcc/stdlib/_pcc_float_bits.py"),
         str(root / "pcc/stdlib/struct.py"), str(provider)],
        ["pcc.stdlib._pcc_float_bits", "struct", provider_name],
    )
    texts = {}
    for module in modules:
        external = {name: value for name, value in exports.items() if name != module.name}
        typed = infer_module(module, external_exports=external, derived_class_map=derived)
        codegen = L1CodeGen(typed, emit_cpy_main_exitcode=False, ir_scaffold_mode="on")
        codegen._strict_no_libpython = True
        codegen._prefer_native_callable_values = True
        codegen._native_module_exports = exports
        codegen._sibling_module_inits = tuple(external)
        text = str(codegen.generate(typed))
        (tmp_path / (module.name + ".ll")).write_text(text, encoding="utf-8")
        verify_ir_text(text)
        assert "strict.nolib.stub" not in text
        assert not re.search(r"\bcall [^\n]*@py_cpy_", text)
        texts[module.name] = text
    defining = texts[provider_name]
    prefix = "user_" + provider_name.replace(".", "_") + "_"
    helper = _body(defining, prefix + "_x86_packed_classification_rows")
    resume = _body(defining, prefix + "_x86_packed_classification_rows__gen_resume")
    consumer = _body(defining, prefix + "probe")
    assert re.search(r"\bcall [^\n]*@py_gen_new\(", helper)
    assert re.search(r"\bcall [^\n]*@py_tuple_new\(", resume)
    assert re.search(r"\bcall [^\n]*@py_gen_set_state\(", resume)
    assert "@" + prefix + "_x86_packed_classification_rows(" in consumer
    assert re.search(r"\bcall [^\n]*@py_list_pop\(", consumer)
    struct_methods = {method["name"]: method for method in exports["struct"]["Struct"]["methods"]}
    unpack_from = struct_methods["unpack_from"]["symbol"]
    _body(texts["struct"], unpack_from)
    assert "@" + unpack_from + "(" in resume
