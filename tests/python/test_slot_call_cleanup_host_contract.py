"""Cleanup cache state uses one initialized, exported native host layout."""

import re

from pcc.frontends.python.codegen.host_contract import L1_CODEGEN_HOST_ATTRS
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.py_ast import Module
from pcc.ir.compat import ir


_FIELDS = ("_slot_call_cleanup_function", "_slot_call_cleanup_blocks")


def _start_function(codegen, name):
    function = ir.Function(
        codegen.module, ir.FunctionType(ir.VoidType(), []), name=name,
    )
    entry = function.append_basic_block("entry")
    target = function.append_basic_block("error")
    codegen.current_function = function
    codegen._current_entry_block = entry
    codegen.builder = ir.IRBuilder(entry)
    root = codegen._new_slot_call_root(name)
    return function, entry, target, root


def test_cleanup_cache_is_initialized_and_scoped_to_real_ir_functions():
    first = L1CodeGen(Module(name="first_cleanup_host", body=[]))
    second = L1CodeGen(Module(name="second_cleanup_host", body=[]))
    for codegen in (first, second):
        assert codegen._slot_call_cleanup_function is None
        assert codegen._slot_call_cleanup_blocks == {}
    assert first._slot_call_cleanup_blocks is not second._slot_call_cleanup_blocks

    function, entry, target, root = _start_function(first, "first")
    cleanup = first._slot_call_cleanup_block((root,), target)
    cache = first._slot_call_cleanup_blocks
    assert first._slot_call_cleanup_function is function
    assert isinstance(function, ir.Function)
    assert first._slot_call_cleanup_block((root,), target) is cleanup
    key, owners = next(iter(cache.items()))
    assert key == (((id(root), id(None), True),), id(target), (), id(entry))
    assert owners[0] is cleanup
    assert owners[1][0][0] is root
    assert owners[1][0][1] is None
    assert owners[1][0][2] is True
    assert owners[2] is target
    assert owners[3] == ()
    assert owners[4] is entry

    next_function, _, next_target, next_root = _start_function(first, "second")
    next_cleanup = first._slot_call_cleanup_block((next_root,), next_target)
    assert first._slot_call_cleanup_function is next_function
    assert first._slot_call_cleanup_blocks is not cache
    assert len(first._slot_call_cleanup_blocks) == 1
    assert next_cleanup is not cleanup
    # Returning to an earlier function cannot reuse its retired cache.
    first.current_function = function
    first._current_entry_block = entry
    first.builder.position_at_end(entry)
    assert first._slot_call_cleanup_block((root,), target) is not cleanup
    assert first._slot_call_cleanup_function is function


def test_cleanup_cache_fields_have_matching_host_export_and_ir_slots(tmp_path):
    from pcc.frontends.python.pipeline_context import (
        build_closed_world_context,
        compile_contextual_per_module_fallback_counts,
    )
    from pcc.frontends.python.type_infer import _InferCtx, infer_module

    host_name = "pcc.frontends.python.codegen.layer1"
    mixin_name = "pcc.frontends.python.codegen.call_object_lowering"
    host_source = tmp_path / "layer1.py"
    host_source.write_text("class L1CodeGen:\n    pass\n")
    mixin_source = tmp_path / "call_object_lowering.py"
    mixin_source.write_text(
        "class CallObjectLoweringMixin:\n"
        "    def store_function(self, value):\n"
        "        self._slot_call_cleanup_function = value\n"
        "    def load_function(self):\n"
        "        return self._slot_call_cleanup_function\n"
        "    def store_blocks(self, value):\n"
        "        self._slot_call_cleanup_blocks = value\n"
        "    def load_blocks(self):\n"
        "        return self._slot_call_cleanup_blocks\n"
    )
    paths = [str(host_source), str(mixin_source)]
    names = [host_name, mixin_name]
    modules, exports, _ = build_closed_world_context(paths, names)
    typed = infer_module(modules[0])
    codegen = L1CodeGen(typed, ir_scaffold_mode="on")
    codegen._strict_no_libpython = True
    (tmp_path / "host-layout.ll").write_text(str(codegen.generate(typed)))
    layout = codegen.class_lowering.classes["L1CodeGen"].field_names
    exported = exports[host_name]["L1CodeGen"]["field_names"]
    inferred = tuple(
        name for name, _ in _InferCtx(modules[1]).l1_codegen_host_type().fields
    )
    for field in _FIELDS:
        assert L1_CODEGEN_HOST_ATTRS.count(field) == 1
        assert layout.count(field) == exported.count(field) == inferred.count(field) == 1
        assert layout.index(field) == exported.index(field) == inferred.index(field)

    counts = compile_contextual_per_module_fallback_counts(
        paths, names, [mixin_name], ir_scaffold_mode="on",
        strict_no_libpython=True, emit_ir_dir=str(tmp_path),
    )
    assert counts == {mixin_name: 0}
    text = (tmp_path / (mixin_name.replace(".", "_") + ".ll")).read_text()
    prefix = "user_" + mixin_name.replace(".", "_") + "_CallObjectLoweringMixin_"
    for suffix, field in zip(("function", "blocks"), _FIELDS):
        for action, operation in (("store", "set"), ("load", "get")):
            symbol = prefix + action + "_" + suffix
            match = re.search(
                r"^define[^\n]*@" + re.escape(symbol) + r"\([^\n]*\n(.*?)^\}",
                text, re.MULTILINE | re.DOTALL,
            )
            assert match is not None, symbol
            body = match.group(1)
            slots = re.findall(
                r"@py_instance_" + operation + r"_field\([^\n]*?i32 (\d+)", body,
            )
            assert slots == [str(layout.index(field))], (symbol, body)
            assert "@py_obj_getattr(" not in body
            assert "@py_obj_setattr(" not in body
