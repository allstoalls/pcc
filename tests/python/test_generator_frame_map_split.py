"""Pure source-name/splitter contract; no compiler or native emission.

Execute the actual name expression from the generator map producer with the
production _fresh implementation. The old test Host._fresh preserved leading
punctuation and therefore could not expose this cross-boundary name failure.
"""
from __future__ import annotations

import ast
from pathlib import Path

from pcc.frontends.python.codegen.ir_decl_helpers import IrDeclHelperMixin
from pcc.frontends.python.pipeline_ir_split import (
    self_backend_local_frame_map_line,
    split_self_backend_ir_module_for_object_shards,
)

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "pcc/frontends/python/codegen/generator_lowering.py"


class NameHost(IrDeclHelperMixin):
    def __init__(self, counter=0):
        self._tmp_counter = counter


def map_name(host):
    """Run the producer's exact naming expression, with no fake sanitizer."""
    tree = ast.parse(GENERATOR.read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "allocate_generator_operand_root")
    constructor = next(node for node in ast.walk(function)
                       if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id == "frame_map"
                               for target in node.targets))
    assert isinstance(constructor.value, ast.Call)
    expression = next(keyword.value for keyword in constructor.value.keywords
                      if keyword.arg == "name")
    return eval(compile(ast.Expression(expression), str(GENERATOR), "eval"), {"host": host})


def module_text(name, *, mutable=False):
    definition = "global" if mutable else "constant"
    return f'''@{name} = internal {definition} i32 16
@state = private global i64 0
declare void @pcc_gc_frame_enter(ptr, ptr)
declare void @pcc_gc_frame_leave(ptr)

define internal void @first() {{
entry:
  %roots = alloca [16 x ptr]
  call void @pcc_gc_frame_enter(ptr @{name}, ptr %roots)
  call void @pcc_gc_frame_leave(ptr %roots)
  ret void
}}

define internal void @second() {{
entry:
  %roots = alloca [16 x ptr]
  call void @pcc_gc_frame_enter(ptr @{name}, ptr %roots)
  call void @pcc_gc_frame_leave(ptr %roots)
  ret void
}}
'''


def shards(text):
    return split_self_backend_ir_module_for_object_shards(
        text, export_prefix="__pco1_", shard_bytes=1,
    )


def function_shards(texts):
    return [text for text in texts if "define " in text]


def test_generator_map_name_keeps_reserved_prefix_with_real_fresh():
    host = NameHost(11866)
    name = map_name(host)
    assert name == ".pcc.gc.frame.map.gen.operands.11867"
    assert self_backend_local_frame_map_line(f"@{name} = internal constant i32 16")
    second = map_name(host)
    assert second == ".pcc.gc.frame.map.gen.operands.11868"
    assert second != name


def test_generated_map_initializer_remains_in_every_object_function_shard():
    name = map_name(NameHost(11866))
    result = shards(module_text(name))
    functions = function_shards(result)
    assert len(functions) == 2
    definition = f"@{name} = internal constant i32 16"
    for shard in functions:
        assert shard.count(definition) == 1
        assert f"call void @pcc_gc_frame_enter(ptr @{name}," in shard
        assert f"@__pco1_{name}" not in shard
    # Unrelated mutable state must keep its single exported definition.
    assert sum(text.count("@__pco1_state = global i64 0") for text in result) == 1
    assert all("@__pco1_state = global i64 0" not in text for text in functions)


def test_original_sanitized_hint_reproduces_missing_map_in_function_shards():
    # Negative control for the precise logged symbol, without weakening the
    # splitter to treat ordinary globals as duplicate local definitions.
    name = NameHost(11866)._fresh(".pcc.gc.frame.map.gen.operands")
    assert name == "pcc.gc.frame.map.gen.operands.11867"
    assert not self_backend_local_frame_map_line(f"@{name} = internal constant i32 16")
    result = shards(module_text(name))
    functions = function_shards(result)
    exported = "__pco1_" + name
    assert sum(f"@{exported} = constant i32 16" in text for text in result) == 1
    for shard in functions:
        assert f"call void @pcc_gc_frame_enter(ptr @{exported}," in shard
        assert f"@{exported} =" not in shard


def test_mutable_map_named_global_is_never_copied_as_local_metadata():
    name = map_name(NameHost())
    assert not self_backend_local_frame_map_line(f"@{name} = internal global i32 16")
    result = shards(module_text(name, mutable=True))
    exported = "__pco1_" + name
    assert sum(f"@{exported} = global i32 16" in text for text in result) == 1
    assert all(f"@{exported} =" not in text for text in function_shards(result))
