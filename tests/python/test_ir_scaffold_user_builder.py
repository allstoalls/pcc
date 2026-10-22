"""Ordinary user attributes must not acquire the compiler's IR provider."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from pcc.frontends.python.pipeline import compile_python


_SOURCE = '''
from pcc.ir.compat import ir

class TextBuilder:
    def __init__(self, prefix: str):
        self.prefix = prefix

    def add(self, suffix: str) -> str:
        return self.prefix + suffix

    def as_pointer(self) -> str:
        return self.prefix

class Container:
    def __init__(self):
        self.builder = TextBuilder("C")

    def render(self) -> str:
        return self.builder.add("D")

class FakeIR:
    def IntType(self, number: int) -> int:
        return number + 1

def render_dynamic(builder):
    return builder.add("Z")

def call_fake_ir(ir):
    return ir.IntType(8)

def main():
    builder = TextBuilder("A")
    saved = builder
    builder = TextBuilder("B")
    print(builder.add("!"))
    builder = saved
    print(builder.as_pointer())
    print(Container().render())
    ir = FakeIR()
    print(ir.IntType(4))
    print(render_dynamic(TextBuilder("Y")))
    print(call_fake_ir(ir))

main()
'''


def test_user_builder_names_emit_user_calls(tmp_path):
    source = tmp_path / "builder.py"
    source.write_text(_SOURCE, encoding="utf-8")
    emitted = tmp_path / "builder.ll"
    compile_python(
        str(source), str(emitted), emit_llvm_only=True, ir_scaffold_mode="on"
    )
    text = emitted.read_text(encoding="utf-8")
    assert "user_pcc_ir_ir_IRBuilder_add" not in text
    assert "user_pcc_ir_ir_IRBuilder_as_pointer" not in text
    assert "user_pcc_ir_ir_scaffold_IntType" not in text


def test_irbuilder_annotation_does_not_replace_runtime_receiver(tmp_path):
    source = tmp_path / "annotated.py"
    source.write_text(
        "from pcc.ir.compat import ir\n"
        "class UserBuilder:\n"
        "    def add(self, a, b):\n"
        "        return a + b\n"
        "def render(builder: ir.IRBuilder):\n"
        "    return builder.add(2, 3)\n"
        "print(render(UserBuilder()))\n",
        encoding="utf-8",
    )
    emitted = tmp_path / "annotated.ll"
    compile_python(str(source), str(emitted), emit_llvm_only=True, ir_scaffold_mode="on")
    assert "user_pcc_ir_ir_IRBuilder_add" not in emitted.read_text(encoding="utf-8"), "annotation redirected an ordinary method"


def test_provider_import_does_not_prove_local_or_field_builder(tmp_path):
    source = tmp_path / "dynamic.py"
    source.write_text(
        "from pcc.ir.compat import ir\n"
        "class UserBuilder:\n"
        "    def add(self, a, b):\n"
        "        return a + b\n"
        "class Wrapper:\n"
        "    def __init__(self, builder):\n"
        "        self.builder = builder\n"
        "    def render(self):\n"
        "        return self.builder.add(2, 3)\n"
        "def render(factory):\n"
        "    builder = factory()\n"
        "    return builder.add(4, 5)\n"
        "print(render(UserBuilder), Wrapper(UserBuilder()).render())\n",
        encoding="utf-8",
    )
    emitted = tmp_path / "dynamic.ll"
    compile_python(str(source), str(emitted), emit_llvm_only=True, ir_scaffold_mode="on")
    assert "user_pcc_ir_ir_IRBuilder_add" not in emitted.read_text(encoding="utf-8"), "provider import redirected an ordinary builder"


def test_branch_join_does_not_keep_only_the_last_builder_identity(tmp_path):
    source = tmp_path / "join.py"
    source.write_text(
        "from pcc.ir.compat import ir\n"
        "def render(flag, factory, a, b):\n"
        "    if flag:\n"
        "        builder = factory()\n"
        "    else:\n"
        "        builder = ir.IRBuilder()\n"
        "    return builder.add(a, b)\n",
        encoding="utf-8",
    )
    emitted = tmp_path / "join.ll"
    compile_python(str(source), str(emitted), emit_llvm_only=True, ir_scaffold_mode="on")
    assert "user_pcc_ir_ir_IRBuilder_add" not in emitted.read_text(encoding="utf-8"), "branch join promoted an uncertain builder"


@pytest.mark.parametrize(
    "loop",
    (
        "    while flag:\n        builder = ir.IRBuilder()\n",
        "    for item in values:\n        builder = ir.IRBuilder()\n",
    ),
)
def test_zero_iteration_loop_keeps_builder_identity_uncertain(tmp_path, loop):
    source = tmp_path / "loop.py"
    source.write_text(
        "from pcc.ir.compat import ir\n"
        "def render(flag, values, factory, a, b):\n"
        "    builder = factory()\n"
        + loop
        + "    return builder.add(a, b)\n",
        encoding="utf-8",
    )
    emitted = tmp_path / "loop.ll"
    compile_python(str(source), str(emitted), emit_llvm_only=True, ir_scaffold_mode="on")
    assert "user_pcc_ir_ir_IRBuilder_add" not in emitted.read_text(encoding="utf-8"), "zero-iteration loop promoted an uncertain builder"


def test_exception_join_does_not_keep_last_handler_builder(tmp_path):
    source = tmp_path / "handler.py"
    source.write_text(
        "from pcc.ir.compat import ir\n"
        "def render(flag, factory, a, b):\n"
        "    builder = factory()\n"
        "    try:\n"
        "        if flag:\n"
        "            builder = factory()\n"
        "    except ValueError:\n"
        "        builder = ir.IRBuilder()\n"
        "    return builder.add(a, b)\n",
        encoding="utf-8",
    )
    emitted = tmp_path / "handler.ll"
    compile_python(str(source), str(emitted), emit_llvm_only=True, ir_scaffold_mode="on")
    assert "user_pcc_ir_ir_IRBuilder_add" not in emitted.read_text(encoding="utf-8"), "handler join promoted an uncertain builder"


def test_exception_join_preserves_an_unmodified_builder(tmp_path):
    source = tmp_path / "handler_preserve.py"
    source.write_text(
        "from pcc.ir.compat import ir\n"
        "def render(a, b, maybe):\n"
        "    builder = ir.IRBuilder()\n"
        "    try:\n"
        "        value = maybe()\n"
        "    except ValueError:\n"
        "        value = None\n"
        "    return builder.add(a, b)\n",
        encoding="utf-8",
    )
    emitted = tmp_path / "handler_preserve.ll"
    compile_python(str(source), str(emitted), emit_llvm_only=True, ir_scaffold_mode="on")
    assert "user_pcc_ir_ir_IRBuilder_add" in emitted.read_text(encoding="utf-8"), "unmodified builder lost its exact identity"


def test_with_binding_replaces_prior_builder_identity(tmp_path):
    source = tmp_path / "with_binding.py"
    source.write_text(
        "from pcc.ir.compat import ir\n"
        "class UserBuilder:\n"
        "    def add(self, a, b):\n"
        "        return a + b\n"
        "class Box:\n"
        "    def __enter__(self):\n"
        "        return UserBuilder()\n"
        "    def __exit__(self, *args):\n"
        "        return False\n"
        "def render(a, b):\n"
        "    builder = ir.IRBuilder()\n"
        "    with Box() as builder:\n"
        "        pass\n"
        "    return builder.add(a, b)\n",
        encoding="utf-8",
    )
    emitted = tmp_path / "with_binding.ll"
    compile_python(str(source), str(emitted), emit_llvm_only=True, ir_scaffold_mode="on")
    assert "user_pcc_ir_ir_IRBuilder_add" not in emitted.read_text(encoding="utf-8"), "with binding preserved a stale IRBuilder fact"


def test_import_binding_replaces_prior_builder_identity(tmp_path):
    source = tmp_path / "import_binding.py"
    source.write_text(
        "from pcc.ir.compat import ir\n"
        "def render(a, b):\n"
        "    builder = ir.IRBuilder()\n"
        "    from pcc.ir.compat import ir as builder\n"
        "    return builder.add(a, b)\n",
        encoding="utf-8",
    )
    emitted = tmp_path / "import_binding.ll"
    compile_python(str(source), str(emitted), emit_llvm_only=True, ir_scaffold_mode="on")
    assert "user_pcc_ir_ir_IRBuilder_add" not in emitted.read_text(encoding="utf-8"), "import preserved a stale IRBuilder fact"


def test_module_ir_reassignment_disables_provider_symbol_lowering(tmp_path):
    source = tmp_path / "rebound_ir.py"
    source.write_text(
        "from pcc.ir.compat import ir\n"
        "class FakeIR:\n"
        "    def IntType(self, number):\n"
        "        return number + 1\n"
        "def factory():\n"
        "    return FakeIR()\n"
        "ir = factory()\n"
        "print(ir.IntType(4))\n",
        encoding="utf-8",
    )
    emitted = tmp_path / "rebound_ir.ll"
    compile_python(str(source), str(emitted), emit_llvm_only=True, ir_scaffold_mode="on")
    assert "user_pcc_ir_ir_scaffold_IntType" not in emitted.read_text(encoding="utf-8"), "module reassignment kept an obsolete provider binding"


def test_user_builder_names_execute_natively(tmp_path, pcc_runtime_archive):
    source = tmp_path / "builder.py"
    source.write_text(_SOURCE, encoding="utf-8")
    expected = subprocess.run(
        [sys.executable, str(source)], capture_output=True, text=True, timeout=10
    )
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "builder"
    compile_python(
        str(source),
        str(binary),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
        runtime_archive=str(pcc_runtime_archive),
    )
    for gc in range(5):
        result = subprocess.run(
            [str(binary)],
            capture_output=True,
            text=True,
            timeout=15,
            env=dict(os.environ, PCC_GC_BACKEND=str(gc)),
        )
        assert result.returncode == 0, (gc, result.stderr)
        assert result.stdout == expected.stdout, (gc, result.stdout, expected.stdout)
