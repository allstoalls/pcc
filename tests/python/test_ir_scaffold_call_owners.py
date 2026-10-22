"""Scaffold migration evidence counts executed IR operations, not declarations.

These focused cases distinguish owned dynamic dispatch from an IR-provider
call. A declaration of a py_cpy helper says nothing about the call owner.
The separately marked native case requires an explicitly supplied pcc1/pcc3.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import textwrap

import pytest


_DIRECT_CALL = re.compile(
    r'^\s*(?:%[^=\n]+\s*=\s*)?(?:(?:tail|musttail|notail)\s+)?'
    r'(?:call|invoke)\s+[^@\n]*@(?:"([^"\n]+)"|([-\w.$]+))\s*\(',
    re.MULTILINE,
)
_DEFINITION = re.compile(r'^define\b[^@\n]*@(?:"([^"\n]+)"|([-\w.$]+))\(', re.MULTILINE)


def _direct_call_targets(body):
    return {match.group(1) or match.group(2) for match in _DIRECT_CALL.finditer(body)}


def _owner_probe_body(text):
    matches = [match for match in _DEFINITION.finditer(text)
               if (match.group(1) or match.group(2)).endswith("_owner_probe")]
    assert len(matches) == 1, "expected the emitted owner_probe function"
    start = text.index("{", matches[0].end()) + 1
    end = text.index("\n}", start)
    return text[start:end]


def _emit_probe(tmp_path, source, mode):
    from pcc.frontends.python.pipeline import compile_python

    path = tmp_path / "owner_case.py"
    output = tmp_path / "owner_case.ll"
    path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    compile_python(str(path), str(output), emit_llvm_only=True,
                   backend="self", libpython_mode="off", ir_scaffold_mode=mode)
    return _owner_probe_body(output.read_text(encoding="utf-8"))


def test_owner_inventory_ignores_declarations_and_counts_invoke():
    text = '''
        declare ptr @py_cpy_call(ptr)
        ; call ptr @py_cpy_comment(ptr null)
        %value = call ptr @py_obj_load_method(ptr null, ptr null, ptr null)
        %result = invoke ptr @"py_obj_call_method"(ptr null, ptr null, ptr null)
            to label %done unwind label %failed
    '''
    assert _direct_call_targets(text) == {"py_obj_load_method", "py_obj_call_method"}
    assert _direct_call_targets("declare ptr @py_cpy_call(ptr)\n") == set()
    assert _direct_call_targets("%value = call ptr @py_cpy_call(ptr null)\n") == {"py_cpy_call"}


@pytest.mark.parametrize("mode", ["off", "on"])
def test_unknown_builder_receiver_uses_owned_dynamic_calls(tmp_path, mode):
    body = _emit_probe(tmp_path, '''
        def owner_probe(builder, left, right):
            return builder.add(left, right)
    ''', mode)
    targets = _direct_call_targets(body)
    assert ({"py_obj_load_method", "py_obj_call_method"} <= targets
            or {"py_obj_getattr", "py_obj_call"} <= targets), targets
    assert not any(target.startswith("py_cpy_") for target in targets), targets
    assert not any(target.startswith("user_pcc_ir_ir_") for target in targets), targets


@pytest.mark.parametrize("mode", ["off", "on"])
def test_unknown_builder_keywords_keep_owned_dispatch(tmp_path, mode):
    body = _emit_probe(tmp_path, '''
        def owner_probe(builder, left):
            return builder.add(left, right=22)
    ''', mode)
    targets = _direct_call_targets(body)
    # Either owned loaded-method form is legal; binding through an owned
    # getattr is also valid. The contract here is the execution owner.
    assert ({"py_obj_load_method", "py_obj_call_method_kwargs"} <= targets
            or {"py_obj_getattr", "py_obj_call"} <= targets), targets
    assert not any(target.startswith("py_cpy_") for target in targets), targets
    assert not any(target.startswith("user_pcc_ir_ir_") for target in targets), targets


def test_proven_provider_constructor_reaches_real_builder_method(tmp_path):
    body = _emit_probe(tmp_path, '''
        from pcc.ir.compat import ir
        def owner_probe(left, right):
            builder = ir.IRBuilder()
            return builder.add(left, right)
    ''', "on")
    targets = _direct_call_targets(body)
    assert "user_pcc_ir_ir_IRBuilder_add" in targets
    assert not any(target.startswith("py_cpy_") for target in targets), targets


_NATIVE_SOURCE = '''
from pcc.ir.compat import ir

class TextBuilder:
    def __init__(self, prefix: str):
        self.prefix = prefix
    def add(self, left: str, right: str = "!") -> str:
        return self.prefix + left + right

class Holder:
    def __init__(self, builder):
        self.builder = builder
    def render(self) -> str:
        return self.builder.add("field", right="?")

def owner_probe(builder, text):
    return builder.add(text, right="!")

def make_ir() -> str:
    module = ir.Module(name="owned-builder")
    ty = ir.IntType(64)
    fn = ir.Function(module, ir.FunctionType(ty, []), name="answer")
    builder = ir.IRBuilder(fn.append_basic_block("entry"))
    builder.ret(ir.Constant(ty, 42))
    return str(module)

def main():
    builder = TextBuilder("A")
    saved = builder
    builder = TextBuilder("B")
    print(owner_probe(builder, "local"))
    print(owner_probe(saved, "alias"))
    print(Holder(TextBuilder("C")).render())
    text = make_ir()
    print("define i64" in text)
    print("ret i64 42" in text)

main()
'''


@pytest.mark.integration
def test_native_compiler_distinguishes_user_builder_and_ir_provider(tmp_path):
    """No implicit bootstrap or host-compiler substitute qualifies this gate."""
    requested = os.environ.get("PCC_SCAFFOLD_NATIVE_COMPILER", "")
    if not requested:
        pytest.fail("set PCC_SCAFFOLD_NATIVE_COMPILER to the qualified pcc1 or pcc3 executable")
    compiler = Path(requested).resolve(strict=True)
    source = tmp_path / "builder_owner.py"
    binary = tmp_path / ("builder_owner.exe" if os.name == "nt" else "builder_owner")
    source.write_text(textwrap.dedent(_NATIVE_SOURCE).lstrip(), encoding="utf-8")
    environment = dict(os.environ)
    environment.pop("LC_ALL", None)
    environment.update(PCC_PYTHON_LIBPYTHON="off", PCC_SELF_LINK="pcc",
                       PCC_IR_SCAFFOLD="on", PCC_NO_AUTO_PCC1="1")
    compiled = subprocess.run(
        [str(compiler), "--backend", "self", "--python-libpython", "off",
         str(source), "-o", str(binary)],
        env=environment, capture_output=True, text=True, timeout=180,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    expected = "Blocal!\nAalias!\nCfield?\nTrue\nTrue\n"
    for gc in range(5):
        result = subprocess.run(
            [str(binary)], env=dict(environment, PCC_GC_BACKEND=str(gc)),
            capture_output=True, text=True, timeout=20,
        )
        assert result.returncode == 0, (gc, result.stdout, result.stderr)
        assert result.stdout.replace("\r\n", "\n") == expected, (gc, result.stdout)
