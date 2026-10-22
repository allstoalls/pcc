from __future__ import annotations

import re
import subprocess
import textwrap


def test_owned_object_locals_are_registered_as_gc_frame_roots(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "owned_root.py"
    out = tmp_path / "owned_root.ll"
    src.write_text(
        textwrap.dedent("""
        def make_value() -> int:
            xs = []
            ys = [xs]
            return len(ys)

        print(make_value())
        """).lstrip(),
        encoding="utf-8",
    )

    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    ir_text = out.read_text(encoding="utf-8")

    assert "call void @pcc_gc_release" in ir_text
    assert ".pcc.gc.frame.map.1" in ir_text
    assert "gc.frame.map = alloca" not in ir_text
    assert "call void @pcc_gc_frame_enter" in ir_text
    assert "call void @pcc_gc_frame_leave" in ir_text


def test_borrowed_class_method_parameters_are_rooted_before_calls(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "method_parameter_roots.py"
    out = tmp_path / "method_parameter_roots.ll"
    src.write_text(
        textwrap.dedent("""
        def normalize(value: str) -> str:
            return value

        class Registry:
            def register(self, namespace: str) -> str:
                namespace = normalize(namespace)
                return namespace
        """).lstrip(),
        encoding="utf-8",
    )

    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    ir_text = out.read_text(encoding="utf-8")
    body_match = re.search(
        r"define\s+[^@]*@user_[^(]*Registry_register[^{]*\{(?P<body>.*?)\n\}",
        ir_text,
        re.S,
    )
    assert body_match is not None, ir_text
    body = body_match.group("body")
    call_pos = body.index("@user_method_parameter_roots_normalize")
    prefix = body[:call_pos]
    assert re.search(
        r"@\.pcc\.gc\.frame\.map\.borrowed\.1[^\n]*\n"
        r"\s*%gc\.frame\.slots\.ptr\.[^\n]*%self\.addr",
        prefix,
    ), body
    assert re.search(
        r"@\.pcc\.gc\.frame\.map\.borrowed\.1[^\n]*\n"
        r"\s*%gc\.frame\.slots\.ptr\.[^\n]*%namespace\.addr",
        prefix,
    ), body


def test_owned_module_global_is_rooted_before_attribute_publication(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "module_global_publish_root.py"
    out = tmp_path / "module_global_publish_root.ll"
    src.write_text(
        "DUMMY = lambda data: data\n",
        encoding="utf-8",
    )

    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    ir_text = out.read_text(encoding="utf-8")

    pin_pos = ir_text.find("call void @pcc_gc_pin")
    root_pos = ir_text.find("call void @pcc_gc_store_root", pin_pos)
    publish_pos = ir_text.find("call i64 @py_module_attr_set", root_pos)

    assert 0 <= pin_pos < root_pos < publish_pos, ir_text


def test_borrowed_module_global_default_is_not_released_after_signature_store(
    tmp_path,
):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "borrowed_module_global_default.py"
    out = tmp_path / "borrowed_module_global_default.ll"
    src.write_text(
        textwrap.dedent("""
        DUMMY = lambda data: data

        def transform(data=DUMMY):
            return data

        print(transform("ok"))
        """).lstrip(),
        encoding="utf-8",
    )

    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    ir_text = out.read_text(encoding="utf-8")

    default_load = re.search(
        r"(?P<value>%DUMMY\.[0-9.]+) = load ptr, ptr "
        r"@\.modvar\.borrowed_module_global_default\.DUMMY",
        ir_text,
    )
    assert default_load is not None, ir_text
    value = re.escape(default_load.group("value"))
    assert re.search(
        rf"call void @py_tuple_set_item\([^\n]*ptr {value}\)",
        ir_text,
    ), ir_text
    assert not re.search(
        rf"call void @pcc_gc_release\(ptr {value}\)",
        ir_text,
    ), ir_text


def test_native_with_open_file_is_an_owned_gc_frame_root(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "native_with_file_root.py"
    out = tmp_path / "native_with_file_root.ll"
    src.write_text(
        textwrap.dedent("""
        def write_value(path: str) -> None:
            with open(path, "w", encoding="utf-8") as f:
                f.write("value")
        """).lstrip(),
        encoding="utf-8",
    )

    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    ir_text = out.read_text(encoding="utf-8")

    body_match = re.search(
        r"define\s+[^@]*@user_[^(]*write_value[^{]*\{(?P<body>.*?)\n\}",
        ir_text,
        re.S,
    )
    assert body_match is not None, ir_text
    body = body_match.group("body")
    assert "f.owned" in body
    assert re.search(
        r"@\.pcc\.gc\.frame\.map\.1[^\n]*\n"
        r"\s*%gc\.frame\.slots\.ptr\.[^\n]*%f\.addr",
        body,
    ), body
    assert "@pcc_gc_release" in body


def test_dynamic_for_iterator_is_an_owned_gc_frame_root(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "dynamic_for_iterator_root.py"
    out = tmp_path / "dynamic_for_iterator_root.ll"
    src.write_text(
        textwrap.dedent("""
        def consume(values) -> int:
            total: int = 0
            for value in values:
                total = total + 1
            return total
        """).lstrip(),
        encoding="utf-8",
    )

    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    ir_text = out.read_text(encoding="utf-8")

    body_match = re.search(
        r"define\s+[^@]*@user_[^(]*consume[^{]*\{(?P<body>.*?)\n\}",
        ir_text,
        re.S,
    )
    assert body_match is not None, ir_text
    body = body_match.group("body")
    assert "for.obj.iter.root" in body
    assert re.search(
        r"@\.pcc\.gc\.frame\.map\.1[^\n]*\n"
        r"\s*%gc\.frame\.slots\.ptr\.[^\n]*%for\.obj\.iter\.root",
        body,
    ), body
    assert "@pcc_gc_load_ptr" in body
    assert "@pcc_gc_release" in body


def test_dynamic_for_target_replaces_an_owned_gc_frame_root(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "dynamic_for_target_root.py"
    out = tmp_path / "dynamic_for_target_root.ll"
    src.write_text(
        textwrap.dedent("""
        def consume(values) -> int:
            total: int = 0
            for value in values:
                total = total + len(str(value))
            return total
        """).lstrip(),
        encoding="utf-8",
    )

    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    ir_text = out.read_text(encoding="utf-8")

    body_match = re.search(
        r"define\s+[^@]*@user_[^(]*consume[^{]*\{(?P<body>.*?)\n\}",
        ir_text,
        re.S,
    )
    assert body_match is not None, ir_text
    body = body_match.group("body")
    assert "value.owned" in body
    assert re.search(
        r"@\.pcc\.gc\.frame\.map\.1[^\n]*\n"
        r"\s*%gc\.frame\.slots\.ptr\.[^\n]*%value(?:\.for\.obj)?\.addr",
        body,
    ), body
    # Each owned result from py_obj_next replaces (and releases) the prior
    # target binding; function cleanup releases the final value.
    assert len(re.findall(r"call void @pcc_gc_release", body)) >= 3


def test_temporary_gc_roots_use_lifo_frame_api(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "lifo_temp_roots.py"
    out = tmp_path / "lifo_temp_roots.ll"
    src.write_text(
        textwrap.dedent("""
        def make_list() -> list[int]:
            return [1, 2, 3]

        def use_call() -> int:
            xs = make_list()
            print(xs, make_list())
            return len(xs)

        print(use_call())
        """).lstrip(),
        encoding="utf-8",
    )

    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    ir_text = out.read_text(encoding="utf-8")

    assert "@pcc_gc_frame_enter_lifo" in ir_text
    assert "@pcc_gc_frame_leave_lifo" in ir_text
    assert "call.ret.root" in ir_text
    assert "container.tmp.root" in ir_text
    # The print-argument tuple is now protected through a GC *frame slot*
    # (`pr.args.frame.ptr` / `pr.args.frame.value`) instead of a bespoke
    # `pr.args.root` alloca.  That is the shape this test is named for -- the
    # LIFO frame API -- so assert the frame form; requiring the old name made
    # the test fail on the very migration it was meant to encourage.
    assert "pr.args.frame.ptr" in ir_text
    assert "pr.args.frame.value" in ir_text
    assert "@pcc_gc_frame_enter(" in ir_text
    assert "@pcc_gc_frame_leave(" in ir_text


def test_owned_local_cleanup_release_reads_through_gc_barrier(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "owned_cleanup_barrier.py"
    out = tmp_path / "owned_cleanup_barrier.ll"
    src.write_text(
        textwrap.dedent("""
        def make_value() -> int:
            xs = []
            return 1

        print(make_value())
        """).lstrip(),
        encoding="utf-8",
    )

    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    ir_text = out.read_text(encoding="utf-8")

    body_match = re.search(
        r"define\s+[^@]*@user_[^(]*make_value[^{]*\{(?P<body>.*?)\n\}",
        ir_text,
        re.S,
    )
    assert body_match is not None, ir_text
    body = body_match.group("body")
    release_match = re.search(
        r"xs\.owned\.release[^:]*:(?P<block>.*?)(?:\n\n|$)",
        body,
        re.S,
    )
    assert release_match is not None, body
    release_block = release_match.group("block")
    load_pos = release_block.find("@pcc_gc_load_ptr")
    release_pos = release_block.find("@pcc_gc_release")
    assert load_pos >= 0, release_block
    assert release_pos > load_pos, release_block
    assert "load ptr, ptr %xs.addr" not in release_block


def test_string_subscript_assignment_is_owned_local_in_raw_scaffold(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "subscript_owned_root.py"
    out = tmp_path / "subscript_owned_root.ll"
    src.write_text(
        textwrap.dedent("""
        def pick(raw: str) -> str:
            c = raw[0]
            return c

        print(pick("x"))
        """).lstrip(),
        encoding="utf-8",
    )

    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    ir_text = out.read_text(encoding="utf-8")

    body_match = re.search(
        r"define\s+[^@]*@user_[^(]*pick[^{]*\{(?P<body>.*?)\n\}",
        ir_text,
        re.S,
    )
    assert body_match is not None, ir_text
    body = body_match.group("body")
    assert "c.owned" in body
    assert re.search(
        r"@\.pcc\.gc\.frame\.map\.1[^\n]*\n"
        r"\s*%gc\.frame\.slots\.ptr\.[^\n]*%c\.addr",
        body,
    ), body
    assert not re.search(
        r"@\.pcc\.gc\.frame\.map\.borrowed\.1[^\n]*\n"
        r"\s*%gc\.frame\.slots\.ptr\.[^\n]*%c\.addr",
        body,
    ), body


def test_owned_object_roots_are_left_on_function_error_exit(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "owned_root_err_exit.py"
    out = tmp_path / "owned_root_err_exit.ll"
    src.write_text(
        textwrap.dedent("""
        def callee() -> int:
            return 1

        def make_value() -> int:
            xs = []
            return callee()

        print(make_value())
        """).lstrip(),
        encoding="utf-8",
    )

    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    ir_text = out.read_text(encoding="utf-8")

    err_pos = ir_text.find("err.exit:")
    assert err_pos >= 0, ir_text
    next_block = ir_text.find("\n\n", err_pos)
    err_block = ir_text[err_pos:] if next_block < 0 else ir_text[err_pos:next_block]
    assert "call void @pcc_gc_frame_leave" in err_block


def test_owned_nested_string_binop_operand_is_pinned_across_outer_concat(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "nested_string_binop_pin.py"
    out = tmp_path / "nested_string_binop_pin.ll"
    src.write_text(
        textwrap.dedent("""
        def has_method(top_level_func_names: list[str], class_name: str, method_name: str) -> int:
            if class_name + "_" + method_name in top_level_func_names:
                return 1
            return 0
        """).lstrip(),
        encoding="utf-8",
    )

    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    ir_text = out.read_text(encoding="utf-8")

    body_match = re.search(
        r"define\s+[^@]*@user_[^(]*has_method[^{]*\{(?P<body>.*?)\n\}",
        ir_text,
        re.S,
    )
    assert body_match is not None, ir_text
    body = body_match.group("body")
    concat_positions = [m.start() for m in re.finditer(r"@py_str_concat", body)]
    assert len(concat_positions) >= 2, body
    pin_positions = [m.start() for m in re.finditer(r"@pcc_gc_pin", body)]
    unpin_positions = [m.start() for m in re.finditer(r"@pcc_gc_unpin", body)]
    release_pos = body.find("@pcc_gc_release", concat_positions[1])

    assert any(
        concat_positions[0] < pos < concat_positions[1] for pos in pin_positions
    ), body
    assert any(concat_positions[1] < pos < release_pos for pos in unpin_positions), body


def test_owned_nested_string_binop_argument_is_pinned_across_list_append(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "nested_string_binop_append_pin.py"
    out = tmp_path / "nested_string_binop_append_pin.ll"
    src.write_text(
        textwrap.dedent("""
        def collect_arg(class_name: str, method_name: str) -> int:
            arg_parts: list[str] = []
            arg_parts.append(class_name + " " + method_name)
            return len(arg_parts)
        """).lstrip(),
        encoding="utf-8",
    )

    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    ir_text = out.read_text(encoding="utf-8")

    body_match = re.search(
        r"define\s+[^@]*@user_[^(]*collect_arg[^{]*\{(?P<body>.*?)\n\}",
        ir_text,
        re.S,
    )
    assert body_match is not None, ir_text
    body = body_match.group("body")
    concat_positions = [m.start() for m in re.finditer(r"@py_str_concat", body)]
    assert len(concat_positions) >= 2, body
    append_pos = body.find("@py_list_append", concat_positions[1])
    release_pos = body.find("@pcc_gc_release", append_pos)
    assert append_pos > concat_positions[1], body

    pin_positions = [m.start() for m in re.finditer(r"@pcc_gc_pin", body)]
    unpin_positions = [m.start() for m in re.finditer(r"@pcc_gc_unpin", body)]
    assert any(concat_positions[1] < pos < append_pos for pos in pin_positions), body
    assert append_pos < release_pos, body
    assert any(append_pos < pos for pos in unpin_positions), body


def test_incremental_collect_preserves_live_owned_object_local(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "owned_root_runtime.py"
    exe = tmp_path / "owned_root_runtime.out"
    src.write_text(
        textwrap.dedent("""
        from pcc.extern import extern, c_int32, c_int64

        pcc_gc_set_backend = extern("pcc_gc_set_backend", (c_int64,), c_int64)
        pcc_gc_collect = extern("pcc_gc_collect", (c_int32,), c_int64)

        def live_collect() -> int:
            xs = [41, 1]
            pcc_gc_collect(0)
            return xs[0] + xs[1]

        def main() -> None:
            pcc_gc_set_backend(1)
            print(live_collect())

        if __name__ == "__main__":
            main()
        """).lstrip(),
        encoding="utf-8",
    )

    compile_python(
        str(src),
        str(exe),
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "42"
