"""Host lowering of the exact cache additions across an imported class boundary.

The unchanged operand parser and non-branch machine encoder are outside this probe.
The real cache methods, constructor, result records, constants and public
wrapper are read from production source rather than copied into the fixture.
Generating and verifying their owned IR is not native pcc1 execution evidence.
"""
from __future__ import annotations

import ast
from pathlib import Path
import re

from pcc.backend import x86_64_encode as encoder
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.pipeline_context import build_closed_world_context
from pcc.frontends.python.type_infer import infer_module
from tests.owned_ir_validation import verify_ir_text


def _cache_provider_source():
    source = Path(encoder.__file__).read_text(encoding="utf-8")
    lines = source.splitlines(keepends=True)
    nodes = ast.parse(source).body

    def segment(node):
        first = min([node.lineno] + [item.lineno for item in getattr(node, "decorator_list", ())])
        return "".join(lines[first - 1:node.end_lineno])

    definitions = {node.name: node for node in nodes if isinstance(node, (ast.ClassDef, ast.FunctionDef))}
    constants = {
        "_INSTRUCTION_ENCODING_CACHE_MAX_ENTRIES",
        "_INSTRUCTION_ENCODING_CACHE_MAX_BYTES",
        "_BRANCH_SYNTAX_ENTRY_BYTES",
        "_PC_INDEPENDENT_INSTRUCTION_MNEMONICS",
        "_SET_CONDITIONS",
    }
    selected_constants = [node for node in nodes if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id in constants
                                  for target in node.targets)]
    assert len(selected_constants) == len(constants)
    methods = {node.name: node for node in definitions["_OperandParseCache"].body
               if isinstance(node, ast.FunctionDef)}
    # Only the old operand-parser implementation is omitted. Its annotation
    # names need ordinary class owners but never participate in this probe.
    prelude = "from __future__ import annotations\nfrom dataclasses import dataclass\n\n"
    prelude += "R_X86_64_PLT32 = " + str(encoder.R_X86_64_PLT32) + "\n"
    prelude += "\n".join("class " + name + ":\n    pass\n" for name in ("_Mem", "_Imm", "_SymbolImmediate"))
    records = "\n".join(segment(definitions[name]) for name in (
        "X86EncodeError", "_Reg", "_Target", "EncodedRelocation", "EncodedInstruction", "_BranchSyntax",
    ))
    cache = "class _OperandParseCache:\n" + "\n".join(segment(methods[name]) for name in (
        "__init__", "instruction_encoding", "remember_instruction_encoding",
        "branch_syntax", "remember_branch_syntax",
    ))
    leaf = definitions["_encode_instruction_uncached"]
    # Keep the real argument/return ABI, including every keyword-only input.
    leaf_source = "".join(lines[leaf.lineno - 1:leaf.body[0].lineno - 1])
    leaf_source += "    return EncodedInstruction(b'\\x90')\n"
    modrm = definitions["_modrm_instruction"]
    modrm_source = "".join(lines[modrm.lineno - 1:modrm.body[0].lineno - 1])
    modrm_source += "    return EncodedInstruction(b'\\xff\\xd0')\n"
    return "\n\n".join((prelude, records, *map(segment, selected_constants), cache,
                          leaf_source, modrm_source, segment(definitions["_int_bytes"]),
                          segment(definitions["_branch"]), segment(definitions["encode_instruction"])))


def _body(text, symbol):
    match = re.search(r"^define [^\n]*@" + re.escape(symbol) + r"\([^\n]*\).*?^}",
                      text, re.M | re.S)
    assert match is not None, symbol
    body = match.group(0)
    assert "strict.nolib.stub" not in body
    assert not re.search(r"\bcall [^\n]*@py_cpy_", body)
    return body


def test_instruction_cache_added_shapes_lower_with_real_cross_module_exports(tmp_path, monkeypatch):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "0")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "0")
    provider_name = "pcc.backend.x86_64_encode"
    consumer_name = "pcc.backend.cache_encoding_probe"
    provider = tmp_path / "cache_provider.py"
    consumer = tmp_path / "cache_consumer.py"
    provider.write_text(_cache_provider_source(), encoding="utf-8")
    consumer.write_text(
        "from pcc.backend.x86_64_encode import _OperandParseCache, _Target, encode_instruction\n"
        "def probe():\n"
        "    cache = _OperandParseCache()\n"
        "    first = encode_instruction('nop', pc=0, labels={}, section_name='.text', operand_cache=cache)\n"
        "    second = encode_instruction('nop', pc=17, labels={}, section_name='.data', operand_cache=cache)\n"
        "    cache.remember_branch_syntax('jmp target', 'jmp', [_Target('target')])\n"
        "    branch = encode_instruction('jmp target', pc=42, labels={}, section_name='.text', operand_cache=cache)\n"
        "    return first.code == second.code and first is not second and len(branch.code) == 5\n",
        encoding="utf-8",
    )
    modules, exports, derived = build_closed_world_context(
        [str(provider), str(consumer)], [provider_name, consumer_name],
    )
    cache = exports[provider_name]["_OperandParseCache"]
    assert cache["field_names"] == (
        "entries", "key_bytes", "instruction_encodings", "instruction_encoding_bytes",
        "branch_syntaxes", "branch_syntax_bytes",
    )
    assert dict(cache["field_types"])["instruction_encodings"] == ("dict", ("str",), ("bytes",))
    methods = {method["name"]: method for method in cache["methods"]}
    assert methods["instruction_encoding"]["return_ty"] == ("dyn",)
    assert methods["remember_instruction_encoding"]["param_types"][-1] == ("dyn",)
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
        assert not re.search(r"\bcall [^\n]*@py_cpy_", text)
        assert "strict.nolib.stub" not in text
        texts[module.name] = text
    defining = texts[provider_name]
    for method in methods.values():
        _body(defining, method["symbol"])
    lookup = _body(defining, methods["instruction_encoding"]["symbol"])
    assert re.match(r"define (?:external )?ptr ", lookup)
    wrapper = _body(defining, "user_" + provider_name.replace(".", "_") + "_encode_instruction")
    assert "@py_type_builtin(" in wrapper
    assert re.search(r"\bcall [^\n]*@py_set_from_iterable\(", defining)
    _body(texts[consumer_name], "user_" + consumer_name.replace(".", "_") + "_probe")
