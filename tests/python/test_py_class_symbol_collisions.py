from __future__ import annotations
import re
import textwrap


def test_class_method_symbol_avoids_top_level_wrapper_collision(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "symbol_collision.py"
    out = tmp_path / "symbol_collision.ll"
    src.write_text(
        textwrap.dedent(
            """
            class Builder:
                def call4_i32(self) -> int:
                    return 7

            def Builder_call4_i32(b: Builder) -> int:
                return b.call4_i32()
            """
        ).lstrip()
    , encoding="utf-8")

    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    ir_text = out.read_text(encoding="utf-8")

    assert (
        "@user_symbol_collision_Builder__method_call4_i32("
        in ir_text
    )
    assert "@user_symbol_collision_Builder_call4_i32(" in ir_text


def test_extern_class_method_symbol_preserves_wrapper_collision(tmp_path):
    from pcc.frontends.python.pipeline import compile_python_multi

    lib_src = tmp_path / "lib.py"
    main_src = tmp_path / "main.py"
    out = tmp_path / "program.ll"
    lib_src.write_text(
        textwrap.dedent(
            """
            class Builder:
                def call4_i32(self) -> int:
                    return 7

            def Builder_call4_i32(b: Builder) -> int:
                return b.call4_i32()
            """
        ).lstrip()
    , encoding="utf-8")
    main_src.write_text(
        textwrap.dedent(
            """
            from lib import Builder, Builder_call4_i32

            def main() -> None:
                b = Builder()
                print(Builder_call4_i32(b))
            """
        ).lstrip()
    , encoding="utf-8")

    compile_python_multi(
        [str(lib_src), str(main_src)],
        str(out),
        emit_llvm_only=True,
        entry_module="main",
        module_names=["lib", "main"],
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    ir_text = out.read_text(encoding="utf-8")

    assert "@user_lib_Builder__method_call4_i32(" in ir_text
    assert "@user_lib_Builder_call4_i32(" in ir_text
    # The wrapper calls the method symbol, not itself; the printer may spell
    # the callee's function type (``call ptr (ptr) @...``).
    assert re.search(
        r"call ptr (?:\([^)]*\) )?@user_lib_Builder__method_call4_i32\(", ir_text
    )


def test_extern_subclass_preserves_untyped_inherited_slot_order(tmp_path):
    from pcc.frontends.python.pipeline import compile_python_multi

    lib_src = tmp_path / "ir_mod.py"
    main_src = tmp_path / "main.py"
    out = tmp_path / "program.ll"
    lib_src.write_text(
        textwrap.dedent(
            """
            class FunctionType:
                def __init__(self, return_type):
                    self.return_type = return_type

            class Value:
                def __init__(self, ty, ref):
                    self.type = ty
                    self._ref = ref
                    self._instr = None
                    self._flags = []
                    self._is_unsigned = False
                    self._pcc_unsigned_pointee = False
                    self._pcc_unsigned_return = False

            class Function(Value):
                def __init__(self, function_type: FunctionType):
                    Value.__init__(self, None, "")
                    self.module = None
                    self.ftype = function_type
                    self.function_type = function_type
            """
        ).lstrip()
    , encoding="utf-8")
    main_src.write_text(
        textwrap.dedent(
            """
            from ir_mod import Function

            def get_return_type(fn: Function):
                return fn.function_type.return_type
            """
        ).lstrip()
    , encoding="utf-8")

    compile_python_multi(
        [str(lib_src), str(main_src)],
        str(out),
        emit_llvm_only=True,
        entry_module="main",
        module_names=["ir_mod", "main"],
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    ir_text = out.read_text(encoding="utf-8")

    assert re.search(r"py_instance_get_field\(ptr %fn[^,]*, i32 9\)", ir_text)
    assert not re.search(r"py_instance_get_field\(ptr %fn[^,]*, i32 5\)", ir_text)
