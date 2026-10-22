from __future__ import annotations

import json
import os
import re
import subprocess
import textwrap


def _generate_ir(source: str) -> str:
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python import type_infer
    from pcc.frontends.python.codegen import layer1

    ast_mod = parse_and_lift(source, "<valueclass-unboxed>", "value_mod")
    typed = type_infer.infer_module(ast_mod)
    cg = layer1.L1CodeGen(typed, ir_scaffold_mode="on")
    return str(cg.generate(typed))


def test_valueclass_local_scalar_payload_avoids_instance_allocation():
    ir_text = _generate_ir(textwrap.dedent("""
            import pcc

            @pcc.valueclass
            class Point:
                x: int
                y: int

            p = Point(1, 2)
            z = p.x + p.y
            print(z)
            """))

    assert not re.search(r"\bcall\b[^\n]*@py_instance_new\b", ir_text)
    assert "extractvalue" in ir_text


def test_eight_field_valueclass_keeps_aggregate_projection():
    ir_text = _generate_ir(textwrap.dedent("""
            import pcc

            @pcc.valueclass
            class Octet:
                a: int
                b: int
                c: int
                d: int
                e: int
                f: int
                g: int
                h: int

            value = Octet(1, 2, 3, 4, 5, 6, 7, 8)
            result = value.a + value.h
            print(result)
            """))

    assert not re.search(r"\bcall\b[^\n]*@py_instance_new\b", ir_text)
    assert "{ i64, i64, i64, i64, i64, i64, i64, i64 }" in ir_text
    assert "extractvalue" in ir_text


def test_valueclass_hot_loop_zero_allocation_oracle_and_escape_semantics(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    source = textwrap.dedent("""
        from typing import Any

        import pcc
        import weakref

        @pcc.valueclass
        class Point:
            x: int
            y: int

        def hot() -> int:
            total: int = 0
            for i in range(1000):
                point = Point(i, i + 1)
                total = total + point.x * point.x + point.y
            return total

        def escape(x: int, y: int) -> Any:
            return Point(x, y)

        print(hot())
        boxed = escape(3, 4)
        alias = boxed
        other = escape(3, 4)
        print(boxed is alias)
        print(boxed is other)
        print(id(boxed) == id(alias))
        try:
            weakref.ref(boxed)
        except TypeError:
            print("weakref-rejected")
        setattr(boxed, "extra", 1)
        print(boxed.extra)
        print(hasattr(boxed, "__dict__"))
        """)
    ir_text = _generate_ir(source)
    hot_start = ir_text.index("@user_value_mod_hot")
    hot_end = ir_text.index("\n}", hot_start)
    hot_ir = ir_text[hot_start:hot_end]
    escape_start = ir_text.index("@user_value_mod_escape")
    escape_end = ir_text.index("\n}", escape_start)
    escape_ir = ir_text[escape_start:escape_end]

    assert "@py_instance_new" not in hot_ir, hot_ir
    assert "@py_valuebox_new" not in hot_ir, hot_ir
    assert "insertvalue" in hot_ir or "getelementptr inbounds { i64, i64 }" in hot_ir
    assert "extractvalue { i64, i64 }" in hot_ir
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_new\b", escape_ir), escape_ir
    assert "@py_instance_new" not in escape_ir, escape_ir

    class OraclePoint:
        def __init__(self, x: int, y: int) -> None:
            self.x = x
            self.y = y

    expected = 0
    for index in range(1000):
        point = OraclePoint(index, index + 1)
        expected = expected + point.x * point.x + point.y

    def compile_and_run(loop_count: int):
        loop_source = source.replace("range(1000)", f"range({loop_count})")
        stem = f"valueclass_zero_alloc_hot_loop_{loop_count}"
        src = tmp_path / f"{stem}.py"
        exe = tmp_path / stem
        log_path = tmp_path / f"{stem}.jsonl"
        src.write_text(loop_source, encoding="utf-8")
        compile_python(
            str(src),
            str(exe),
            libpython_mode="off",
            ir_scaffold_mode="on",
            backend="self",
        )
        env = os.environ.copy()
        env.update(
            {
                "PCC_LOG": "alloc",
                "PCC_LOG_FORMAT": "json",
                "PCC_LOG_FILE": str(log_path),
            }
        )
        proc = subprocess.run(
            [str(exe)],
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
            env=env,
        )
        events = [
            json.loads(line)
            for line in log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        allocations = [
            event
            for event in events
            if event.get("category") == "alloc" and event.get("event") == "alloc_object"
        ]
        instance_allocations = [
            event for event in allocations if int(event.get("value1", 0)) >= 100
        ]
        assert len(instance_allocations) == 2, instance_allocations
        return proc.stdout.strip().splitlines(), allocations

    baseline_output, baseline_allocations = compile_and_run(0)
    hot_output, hot_allocations = compile_and_run(1000)
    expected_tail = [
        "True",
        "False",
        "True",
        "weakref-rejected",
        "1",
        "True",
    ]
    assert baseline_output == ["0", *expected_tail]
    assert hot_output == [str(expected), *expected_tail]
    assert len(hot_allocations) == len(baseline_allocations), (
        len(baseline_allocations),
        len(hot_allocations),
    )
    assert sorted(int(event["value1"]) for event in hot_allocations) == sorted(
        int(event["value1"]) for event in baseline_allocations
    )


def test_valueclass_direct_function_arg_uses_payload_abi():
    ir_text = _generate_ir(textwrap.dedent("""
            import pcc

            @pcc.valueclass
            class Point:
                x: int
                y: int

            def norm2(p: Point) -> int:
                return p.x * p.x + p.y * p.y

            p = Point(3, 4)
            print(norm2(p))
            """))

    assert re.search(
        r"define external ptr @user_value_mod_norm2\(\{ i64, i64 \} %p\)",
        ir_text,
    )
    assert not re.search(r"\bcall\b[^\n]*@py_instance_new\b", ir_text)
    assert "extractvalue" in ir_text


def test_valueclass_wide_payload_uses_aggregate_abi_self_backend(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    source = textwrap.dedent("""
        import pcc

        @pcc.valueclass
        class Wide:
            a: int
            b: int
            c: int
            d: int
            e: int

        def make_wide() -> Wide:
            return Wide(1, 2, 3, 4, 5)

        def total(value: Wide) -> int:
            return value.a + value.b + value.c + value.d + value.e

        print(total(make_wide()))
        """)
    ir_text = _generate_ir(source)
    payload = r"\{ i64, i64, i64, i64, i64 \}"

    assert re.search(
        rf"define external {payload} @user_value_mod_make_wide",
        ir_text,
    ), ir_text
    assert re.search(
        rf"define external ptr @user_value_mod_total\({payload} %value\)",
        ir_text,
    ), ir_text
    for name in ("make_wide", "total"):
        body_start = ir_text.index(f"@user_value_mod_{name}")
        body_end = ir_text.index("\n}", body_start)
        body = ir_text[body_start:body_end]
        assert "@py_instance_new" not in body, body
        assert "@py_valuebox_new" not in body, body

    src = tmp_path / "valueclass_wide_payload.py"
    exe = tmp_path / "valueclass_wide_payload"
    src.write_text(source, encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
    )
    proc = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    assert proc.stdout == "15\n"


def test_valueclass_wide_payload_covers_scaffold_arities_six_and_seven():
    for field_count in (6, 7):
        field_names = [chr(ord("a") + index) for index in range(field_count)]
        fields = "\n".join(f"    {name}: int" for name in field_names)
        source = (
            "import pcc\n\n"
            "@pcc.valueclass\n"
            "class Wide:\n" + fields + "\n\n"
            "def identity(value: Wide) -> Wide:\n"
            "    return value\n"
        )
        ir_text = _generate_ir(source)
        payload = r"\{ " + ", ".join("i64" for _ in field_names) + r" \}"

        assert re.search(
            rf"define external {payload} @user_value_mod_identity"
            rf"\({payload} %value\)",
            ir_text,
        ), ir_text


def test_valueclass_nested_payload_uses_payload_abi_in_direct_calls(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    source = textwrap.dedent("""
        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        def make_segment(a: int, b: int, c: int, d: int) -> Segment:
            return Segment(Point(a, b), Point(c, d))

        def total(s: Segment) -> int:
            return s.start.x + s.start.y + s.end.x + s.end.y

        seg = Segment(Point(1, 2), Point(3, 4))
        made = make_segment(5, 6, 7, 8)
        print(total(seg))
        print(total(made))
        """)
    ir_text = _generate_ir(source)
    main_start = ir_text.index("define i32 @main")
    main_end = ir_text.index("\n}", main_start)
    main_ir = ir_text[main_start:main_end]

    assert re.search(
        r"define external ptr @user_value_mod_total"
        r"\(\{ \{ i64, i64 \}, \{ i64, i64 \} \} %s\)",
        ir_text,
    )
    assert re.search(
        r"define external \{ \{ i64, i64 \}, \{ i64, i64 \} \} "
        r"@user_value_mod_make_segment",
        ir_text,
    )
    assert re.search(
        r"\bcall\b[^\n]*\(\{ \{ i64, i64 \}, \{ i64, i64 \} \}\) "
        r"@user_value_mod_total",
        main_ir,
    )
    assert not re.search(r"\bcall\b[^\n]*@py_instance_new\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@py_valuebox_new\b", main_ir)

    src = tmp_path / "valueclass_nested_payload.py"
    exe = tmp_path / "valueclass_nested_payload"
    src.write_text(source, encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
    )
    proc = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    assert proc.stdout.strip().splitlines() == ["10", "26"]


def test_valueclass_return_constructor_uses_payload_abi():
    ir_text = _generate_ir(textwrap.dedent("""
            import pcc

            @pcc.valueclass
            class Point:
                x: int
                y: int

            def make_point(x: int, y: int) -> Point:
                return Point(x, y)

            p = make_point(3, 4)
            print(p.x + p.y)
            """))

    assert re.search(
        r"define external \{ i64, i64 \} @user_value_mod_make_point",
        ir_text,
    )
    assert re.search(r"\bret \{ i64, i64 \}", ir_text)
    assert not re.search(r"\bcall\b[^\n]*@py_instance_new\b", ir_text)


def test_valueclass_return_payload_compiles_with_default_ir_passes(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "valueclass_return.py"
    exe = tmp_path / "valueclass_return"
    src.write_text(
        textwrap.dedent("""
            import pcc

            @pcc.valueclass
            class Point:
                x: int
                y: int

            def make_point(x: int, y: int) -> Point:
                return Point(x, y)

            p = make_point(3, 4)
            print(p.x + p.y)
            """),
        encoding="utf-8",
    )

    compile_python(str(src), str(exe), libpython_mode="off", ir_scaffold_mode="on")
    proc = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    assert proc.stdout.strip() == "7"


def test_valueclass_direct_method_receiver_uses_payload_abi():
    ir_text = _generate_ir(textwrap.dedent("""
            import pcc

            @pcc.valueclass
            class Point:
                x: int
                y: int

                def norm2(self) -> int:
                    return self.x * self.x + self.y * self.y

            p = Point(3, 4)
            print(p.norm2())
            """))

    assert re.search(
        r"define external ptr @user_value_mod_Point_norm2" r"\(\{ i64, i64 \} %self\)",
        ir_text,
    )
    assert re.search(
        r"\bcall ptr \(\{ i64, i64 \}\) @user_value_mod_Point_norm2",
        ir_text,
    )
    assert not re.search(r"\bextractvalue ptr\b", ir_text)
    assert not re.search(r"\bcall\b[^\n]*@py_instance_new\b", ir_text)


def test_valueclass_payload_equality_uses_fieldwise_compare():
    ir_text = _generate_ir(textwrap.dedent("""
            import pcc

            @pcc.valueclass
            class Point:
                x: int
                y: int

            p = Point(1, 2)
            q = Point(1, 2)
            r = Point(1, 3)
            if p == q:
                print(1)
            if p != r:
                print(2)
            """))

    main_ir = ir_text[ir_text.index("define i32 @main") :]
    assert "value.eq.icmp" in ir_text
    assert "value.eq.and" in ir_text
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Point___eq__\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@py_obj_eq\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@py_instance_new\b", main_ir)


def test_valueclass_nested_payload_equality_uses_recursive_fieldwise_compare(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    source = textwrap.dedent("""
        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        def same(left: Segment, right: Segment) -> bool:
            return left == right

        def different(left: Segment, right: Segment) -> bool:
            return left != right

        a = Segment(Point(1, 2), Point(3, 4))
        b = Segment(Point(1, 2), Point(3, 4))
        c = Segment(Point(1, 2), Point(3, 5))
        print(same(a, b))
        print(same(a, c))
        print(different(a, c))
        """)
    ir_text = _generate_ir(source)
    same_start = ir_text.index("define external i1 @user_value_mod_same")
    different_start = ir_text.index("define external i1 @user_value_mod_different")
    same_ir = ir_text[same_start:different_start]
    different_ir = ir_text[different_start : ir_text.index("define i32 @main")]
    eq_ir = same_ir + different_ir

    assert re.search(
        r"define external i1 @user_value_mod_same"
        r"\(\{ \{ i64, i64 \}, \{ i64, i64 \} \} %left, "
        r"\{ \{ i64, i64 \}, \{ i64, i64 \} \} %right\)",
        ir_text,
    )
    assert not re.search(r"\bicmp eq \{", eq_ir)
    assert eq_ir.count("value.eq.icmp") >= 4
    assert re.search(r"\bextractvalue \{ i64, i64 \}", eq_ir)
    assert not re.search(r"\bcall\b[^\n]*@py_obj_eq\b", eq_ir)
    assert not re.search(r"\bcall\b[^\n]*@py_instance_new\b", eq_ir)

    src = tmp_path / "valueclass_nested_eq.py"
    exe = tmp_path / "valueclass_nested_eq"
    src.write_text(source, encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
    )
    proc = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    assert proc.stdout.strip().splitlines() == ["True", "False", "True"]


def test_valueclass_payload_equality_compiles_with_default_ir_passes(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "valueclass_eq.py"
    exe = tmp_path / "valueclass_eq"
    src.write_text(
        textwrap.dedent("""
            import pcc

            @pcc.valueclass
            class Point:
                x: int
                y: int

            p = Point(1, 2)
            q = Point(1, 2)
            r = Point(1, 3)
            if p == q:
                print(1)
            else:
                print(0)
            if p != r:
                print(2)
            else:
                print(0)
            """),
        encoding="utf-8",
    )

    compile_python(str(src), str(exe), libpython_mode="off", ir_scaffold_mode="on")
    proc = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    assert proc.stdout.strip().splitlines() == ["1", "2"]


def test_valueclass_payload_boxes_at_dyn_function_boundary(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    source = textwrap.dedent("""
        import pcc
        from typing import Any

        @pcc.valueclass
        class Point:
            x: int
            y: int

        def ident(x: Any) -> Any:
            return x

        p = Point(1, 2)
        d = ident(p)
        print(d.x)
        print(d.y)
        """)
    ir_text = _generate_ir(source)
    main_ir = ir_text[ir_text.index("define i32 @main") :]

    assert re.search(r"\bcall\b[^\n]*@py_valuebox_new\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_set_field\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Point___init__\b", main_ir)

    src = tmp_path / "valueclass_box.py"
    exe = tmp_path / "valueclass_box"
    src.write_text(source, encoding="utf-8")
    compile_python(str(src), str(exe), libpython_mode="off", ir_scaffold_mode="on")
    proc = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    assert proc.stdout.strip().splitlines() == ["1", "2"]


def test_valueclass_constructor_returning_dyn_boxes_valuebox_in_return_body():
    source = textwrap.dedent("""
        from typing import Any

        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        def make_dyn(a: int, b: int, c: int, d: int) -> Any:
            return Segment(Point(a, b), Point(c, d))

        value = make_dyn(1, 2, 3, 4)
        print(value)
        """)
    ir_text = _generate_ir(source)
    fn_start = ir_text.index("define external ptr @user_value_mod_make_dyn")
    # End at make_dyn's own close (next function define), NOT at @main: the
    # span up to @main also contains the Segment.__init__ method-native adapter
    # (which legitimately calls @user_value_mod_Segment___init__); scoping to
    # make_dyn's body checks that the RETURN projection uses valuebox_*, not a
    # Segment.__init__ call.
    fn_end = ir_text.index("\ndefine ", fn_start + 1)
    make_dyn_ir = ir_text[fn_start:fn_end]

    assert re.search(r"\bcall\b[^\n]*@py_valuebox_new\b", make_dyn_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_set_field\b", make_dyn_ir)
    assert not re.search(r"\bcall\b[^\n]*@py_instance_new\b", make_dyn_ir)
    assert not re.search(
        r"\bcall\b[^\n]*@user_value_mod_Segment___init__\b", make_dyn_ir
    )


def test_valueclass_constructor_local_and_container_dyn_projection_boxes_valuebox():
    source = textwrap.dedent("""
        from typing import Any

        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        local: Any = Segment(Point(1, 2), Point(3, 4))
        same: Any = Segment(Point(1, 2), Point(3, 4))
        slot = (Segment(Point(1, 2), Point(3, 4)),)
        items = [Segment(Point(5, 6), Point(7, 8))]
        mapping = {"seg": Segment(Point(9, 10), Point(11, 12))}
        print(local == same)
        print(slot[0])
        print(items[0])
        print(mapping["seg"])
        """)
    ir_text = _generate_ir(source)
    main_ir = ir_text[ir_text.index("define i32 @main") :]

    assert re.search(r"\bcall\b[^\n]*@py_valuebox_new\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_set_field\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@py_instance_new\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Segment___init__\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Point___init__\b", main_ir)
    assert not re.search(r"\bextractvalue ptr\b", main_ir)


def test_valueclass_constructor_module_global_dyn_projection_boxes_valuebox():
    source = textwrap.dedent("""
        from typing import Any

        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        global_left: Any = Segment(Point(1, 2), Point(3, 4))
        global_same: Any = Segment(Point(1, 2), Point(3, 4))
        global_table = {global_left: 42}

        def total(s: Segment) -> int:
            return s.start.x + s.start.y + s.end.x + s.end.y

        print(global_left == global_same)
        print(global_table[global_same])
        print(total(global_left))
        """)
    ir_text = _generate_ir(source)
    main_ir = ir_text[ir_text.index("define i32 @main") :]

    assert re.search(r"\bcall\b[^\n]*@py_valuebox_new\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_set_field\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@py_instance_new\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Segment___init__\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Point___init__\b", main_ir)
    assert not re.search(r"\bextractvalue ptr\b", main_ir)


def test_valueclass_constructor_mutation_store_projection_boxes_valuebox():
    source = textwrap.dedent("""
        from typing import Any

        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        items = []
        items.append(Segment(Point(1, 2), Point(3, 4)))
        items.append(Segment(Point(1, 2), Point(3, 5)))
        items[1] = Segment(Point(1, 2), Point(3, 4))
        mapping = {}
        mapping["seg"] = Segment(Point(5, 6), Point(7, 8))
        key: Any = Segment(Point(9, 10), Point(11, 12))
        same_key: Any = Segment(Point(9, 10), Point(11, 12))
        table = {}
        table[key] = 99

        def total(s: Segment) -> int:
            return s.start.x + s.start.y + s.end.x + s.end.y

        print(items[0] == items[1])
        print(table[same_key])
        print(type(items[0]).__name__)
        print(total(items[0]))
        print(total(mapping["seg"]))
        """)
    ir_text = _generate_ir(source)
    main_ir = ir_text[ir_text.index("define i32 @main") :]

    assert re.search(r"\bcall\b[^\n]*@py_valuebox_new\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_set_field\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@py_instance_new\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Segment___init__\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Point___init__\b", main_ir)
    assert not re.search(r"\bextractvalue ptr\b", main_ir)


def test_valueclass_constructor_comprehension_projection_boxes_valuebox():
    source = textwrap.dedent("""
        from typing import Any

        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        items = [Segment(Point(1, 2), Point(3, 4)) for _ in range(2)]
        mapping = {"seg": Segment(Point(5, 6), Point(7, 8)) for _ in range(1)}
        table = {Segment(Point(9, 10), Point(11, 12)): 99 for _ in range(1)}
        same_key: Any = Segment(Point(9, 10), Point(11, 12))

        def total(s: Segment) -> int:
            return s.start.x + s.start.y + s.end.x + s.end.y

        print(items[0] == items[1])
        print(table[same_key])
        print(type(items[0]).__name__)
        print(total(items[0]))
        print(total(mapping["seg"]))
        """)
    ir_text = _generate_ir(source)
    main_ir = ir_text[ir_text.index("define i32 @main") :]

    assert re.search(r"\bcall\b[^\n]*@py_valuebox_new\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_set_field\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@py_instance_new\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Segment___init__\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Point___init__\b", main_ir)
    assert not re.search(r"\bextractvalue ptr\b", main_ir)


def test_valueclass_constructor_call_arg_projection_boxes_valuebox():
    source = textwrap.dedent("""
        from typing import Any

        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        def keep(value: Any) -> Any:
            return value

        left = keep(Segment(Point(1, 2), Point(3, 4)))
        same = keep(Segment(Point(1, 2), Point(3, 4)))
        table = {left: 42}

        def total(s: Segment) -> int:
            return s.start.x + s.start.y + s.end.x + s.end.y

        print(left == same)
        print(table[same])
        print(type(left).__name__)
        print(total(left))
        """)
    ir_text = _generate_ir(source)
    main_ir = ir_text[ir_text.index("define i32 @main") :]

    assert re.search(r"\bcall\b[^\n]*@py_valuebox_new\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_set_field\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@py_instance_new\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Segment___init__\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Point___init__\b", main_ir)
    assert not re.search(r"\bextractvalue ptr\b", main_ir)


def test_valueclass_constructor_dynamic_callable_arg_projection_boxes_valuebox():
    source = textwrap.dedent("""
        from typing import Any

        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        class Keeper:
            def __call__(self, value: Any) -> Any:
                return value

        left = Keeper()(Segment(Point(1, 2), Point(3, 4)))
        same = Keeper()(Segment(Point(1, 2), Point(3, 4)))
        table = {left: 42}

        def total(s: Segment) -> int:
            return s.start.x + s.start.y + s.end.x + s.end.y

        print(left == same)
        print(table[same])
        print(type(left).__name__)
        print(total(left))
        """)
    ir_text = _generate_ir(source)
    main_ir = ir_text[ir_text.index("define i32 @main") :]

    assert "py_obj_call" in main_ir
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_new\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_set_field\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Segment___init__\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Point___init__\b", main_ir)
    assert not re.search(r"\bextractvalue ptr\b", main_ir)


def test_valueclass_constructor_attribute_store_projection_boxes_valuebox():
    source = textwrap.dedent("""
        from typing import Any

        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        class Holder:
            def __init__(self):
                pass

        holder = Holder()
        holder.left = Segment(Point(1, 2), Point(3, 4))
        holder.same = Segment(Point(1, 2), Point(3, 4))
        setattr(holder, "changed", Segment(Point(1, 2), Point(3, 5)))
        table = {holder.left: 42}

        def total(s: Segment) -> int:
            return s.start.x + s.start.y + s.end.x + s.end.y

        print(holder.left == holder.same)
        print(holder.left == holder.changed)
        print(table[holder.same])
        print(type(holder.left).__name__)
        print(total(holder.left))
        """)
    ir_text = _generate_ir(source)
    main_ir = ir_text[ir_text.index("define i32 @main") :]

    assert "py_obj_setattr" in main_ir
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_new\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_set_field\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Segment___init__\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Point___init__\b", main_ir)
    assert not re.search(r"\bextractvalue ptr\b", main_ir)


def test_valueclass_constructor_conditional_expr_projection_boxes_valuebox():
    source = textwrap.dedent("""
        from typing import Any

        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        flag = True
        left: Any = Segment(Point(1, 2), Point(3, 4)) if flag else Segment(Point(0, 0), Point(0, 0))
        same: Any = Segment(Point(1, 2), Point(3, 4)) if flag else Segment(Point(0, 0), Point(0, 0))
        changed: Any = Segment(Point(1, 2), Point(3, 5)) if flag else Segment(Point(0, 0), Point(0, 0))
        table = {left: 42}

        def total(s: Segment) -> int:
            return s.start.x + s.start.y + s.end.x + s.end.y

        print(left == same)
        print(left == changed)
        print(table[same])
        print(type(left).__name__)
        print(total(left))
        """)
    ir_text = _generate_ir(source)
    main_ir = ir_text[ir_text.index("define i32 @main") :]

    assert "ternary_obj_true" in main_ir
    assert "ternary_obj_false" in main_ir
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_new\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_set_field\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@py_instance_new\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Segment___init__\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Point___init__\b", main_ir)
    assert not re.search(r"\bextractvalue ptr\b", main_ir)


def test_valueclass_constructor_short_circuit_projection_boxes_valuebox():
    source = textwrap.dedent("""
        from typing import Any

        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        flag = False
        left: Any = flag or Segment(Point(1, 2), Point(3, 4))
        same: Any = flag or Segment(Point(1, 2), Point(3, 4))
        changed: Any = flag or Segment(Point(1, 2), Point(3, 5))
        tail: Any = left and Segment(Point(5, 6), Point(7, 8))
        table = {left: 42, tail: 99}

        def total(s: Segment) -> int:
            return s.start.x + s.start.y + s.end.x + s.end.y

        print(left == same)
        print(left == changed)
        print(table[same])
        print(type(left).__name__)
        print(total(left))
        print(total(tail))
        """)
    ir_text = _generate_ir(source)
    main_ir = ir_text[ir_text.index("define i32 @main") :]

    assert "bool.obj.rhs" in main_ir
    assert "bool.obj.end" in main_ir
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_new\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_set_field\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@py_instance_new\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Segment___init__\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Point___init__\b", main_ir)
    assert not re.search(r"\bextractvalue ptr\b", main_ir)


def test_valueclass_constructor_dict_builtin_keyword_projection_boxes_valuebox():
    source = textwrap.dedent("""
        from typing import Any

        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        mapping = dict(
            left=Segment(Point(1, 2), Point(3, 4)),
            same=Segment(Point(1, 2), Point(3, 4)),
            changed=Segment(Point(1, 2), Point(3, 5)),
        )
        base = (("seed", 1),)
        merged = dict(
            base,
            tail=Segment(Point(5, 6), Point(7, 8)),
            tail_same=Segment(Point(5, 6), Point(7, 8)),
        )
        table = {mapping["left"]: 42, merged["tail"]: 99}

        def total(s: Segment) -> int:
            return s.start.x + s.start.y + s.end.x + s.end.y

        print(mapping["left"] == mapping["same"])
        print(mapping["left"] == mapping["changed"])
        print(table[mapping["same"]])
        print(type(mapping["left"]).__name__)
        print(total(mapping["left"]))
        print(table[merged["tail_same"]])
        print(total(merged["tail"]))
        """)
    ir_text = _generate_ir(source)
    main_ir = ir_text[ir_text.index("define i32 @main") :]

    assert "dict.new" in main_ir
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_new\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_set_field\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@py_instance_new\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Segment___init__\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Point___init__\b", main_ir)
    assert not re.search(r"\bextractvalue ptr\b", main_ir)


def test_valueclass_constructor_dict_update_keyword_projection_boxes_valuebox():
    source = textwrap.dedent("""
        from typing import Any

        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        mapping: dict[str, Any] = {}
        mapping.update(
            left=Segment(Point(1, 2), Point(3, 4)),
            same=Segment(Point(1, 2), Point(3, 4)),
            changed=Segment(Point(1, 2), Point(3, 5)),
        )
        merged: dict[str, Any] = {"seed": 1}
        merged.update(
            {"seed": 1},
            tail=Segment(Point(5, 6), Point(7, 8)),
            tail_same=Segment(Point(5, 6), Point(7, 8)),
        )
        table = {mapping["left"]: 42, merged["tail"]: 99}

        def total(s: Segment) -> int:
            return s.start.x + s.start.y + s.end.x + s.end.y

        print(mapping["left"] == mapping["same"])
        print(mapping["left"] == mapping["changed"])
        print(table[mapping["same"]])
        print(type(mapping["left"]).__name__)
        print(total(mapping["left"]))
        print(table[merged["tail_same"]])
        print(total(merged["tail"]))
        """)
    ir_text = _generate_ir(source)
    main_ir = ir_text[ir_text.index("define i32 @main") :]

    assert re.search(r"\bcall\b[^\n]*@py_dict_set\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_new\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_set_field\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@py_instance_new\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Segment___init__\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Point___init__\b", main_ir)
    assert not re.search(r"\bextractvalue ptr\b", main_ir)


def test_valueclass_constructor_set_builtin_literal_source_projection_boxes_valuebox():
    source = textwrap.dedent("""
        from typing import Any

        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        left = set([
            Segment(Point(1, 2), Point(3, 4)),
            Segment(Point(1, 2), Point(3, 4)),
            Segment(Point(1, 2), Point(3, 5)),
        ])
        right = set((
            Segment(Point(5, 6), Point(7, 8)),
            Segment(Point(5, 6), Point(7, 8)),
            Segment(Point(5, 6), Point(7, 9)),
        ))
        same: Any = Segment(Point(1, 2), Point(3, 4))
        tail_same: Any = Segment(Point(5, 6), Point(7, 8))

        changed: Any = Segment(Point(1, 2), Point(3, 5))
        missing: Any = Segment(Point(9, 9), Point(9, 9))
        tail_changed: Any = Segment(Point(5, 6), Point(7, 9))

        print(len(left))
        print(same in left)
        print(changed in left)
        print(missing in left)
        print(len(right))
        print(tail_same in right)
        print(tail_changed in right)
        """)
    ir_text = _generate_ir(source)
    main_ir = ir_text[ir_text.index("define i32 @main") :]

    assert re.search(r"\bcall\b[^\n]*@py_set_add\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_new\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_set_field\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@py_instance_new\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Segment___init__\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Point___init__\b", main_ir)
    assert not re.search(r"\bextractvalue ptr\b", main_ir)


def test_valueclass_constructor_set_method_element_projection_boxes_valuebox():
    source = textwrap.dedent("""
        from typing import Any

        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        items = set()
        items.add(Segment(Point(1, 2), Point(3, 4)))
        items.add(Segment(Point(1, 2), Point(3, 4)))
        items.add(Segment(Point(1, 2), Point(3, 5)))
        items.discard(Segment(Point(9, 9), Point(9, 9)))

        same: Any = Segment(Point(1, 2), Point(3, 4))
        changed: Any = Segment(Point(1, 2), Point(3, 5))
        missing: Any = Segment(Point(9, 9), Point(9, 9))

        print(len(items))
        print(same in items)
        print(changed in items)
        print(missing in items)

        items.remove(Segment(Point(1, 2), Point(3, 4)))
        items.discard(Segment(Point(1, 2), Point(3, 5)))

        print(len(items))
        print(same in items)
        print(changed in items)
        """)
    ir_text = _generate_ir(source)
    main_ir = ir_text[ir_text.index("define i32 @main") :]

    assert re.search(r"\bcall\b[^\n]*@py_set_add\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_set_remove\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_new\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_set_field\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@py_instance_new\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Segment___init__\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Point___init__\b", main_ir)
    assert not re.search(r"\bextractvalue ptr\b", main_ir)


def test_valueclass_constructor_sequence_builtin_literal_source_boxes_valuebox():
    source = textwrap.dedent("""
        from typing import Any

        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        left = list([
            Segment(Point(1, 2), Point(3, 4)),
            Segment(Point(1, 2), Point(3, 5)),
        ])
        right = list((
            Segment(Point(5, 6), Point(7, 8)),
            Segment(Point(5, 6), Point(7, 9)),
        ))
        tup = tuple([
            Segment(Point(1, 2), Point(3, 4)),
            Segment(Point(1, 2), Point(3, 5)),
        ])
        tail_tup = tuple((
            Segment(Point(5, 6), Point(7, 8)),
            Segment(Point(5, 6), Point(7, 9)),
        ))

        same: Any = Segment(Point(1, 2), Point(3, 4))
        changed: Any = Segment(Point(1, 2), Point(3, 5))
        tail_same: Any = Segment(Point(5, 6), Point(7, 8))
        tail_changed: Any = Segment(Point(5, 6), Point(7, 9))

        table = {
            same: 10,
            changed: 11,
            tail_same: 20,
            tail_changed: 21,
        }

        print(table[left[0]])
        print(table[left[1]])
        print(table[right[0]])
        print(table[right[1]])
        print(table[tup[0]])
        print(table[tup[1]])
        print(table[tail_tup[0]])
        print(table[tail_tup[1]])
        """)
    ir_text = _generate_ir(source)
    main_ir = ir_text[ir_text.index("define i32 @main") :]

    assert re.search(r"\bcall\b[^\n]*@py_list_append\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_tuple_set_item\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_new\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_set_field\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@py_instance_new\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Segment___init__\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Point___init__\b", main_ir)
    assert not re.search(r"\bextractvalue ptr\b", main_ir)


def test_valueclass_constructor_user_method_argument_projection_boxes_valuebox():
    source = textwrap.dedent("""
        from typing import Any

        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        class Keeper:
            def pick(self, value):
                return value

            @staticmethod
            def pick_static(value):
                return value

        keeper = Keeper()
        left = keeper.pick(Segment(Point(1, 2), Point(3, 4)))
        right = Keeper.pick_static(Segment(Point(5, 6), Point(7, 8)))
        same: Any = Segment(Point(1, 2), Point(3, 4))
        tail_same: Any = Segment(Point(5, 6), Point(7, 8))

        table = {
            left: 10,
            right: 20,
        }

        print(table[same])
        print(table[tail_same])
        """)
    ir_text = _generate_ir(source)
    main_ir = ir_text[ir_text.index("define i32 @main") :]

    assert re.search(r"\bcall\b[^\n]*@user_value_mod_Keeper_pick\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@user_value_mod_Keeper_pick_static\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_new\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_set_field\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Segment___init__\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Point___init__\b", main_ir)
    assert not re.search(r"\bextractvalue ptr\b", main_ir)


def test_valueclass_constructor_super_method_argument_projection_boxes_valuebox():
    source = textwrap.dedent("""
        from typing import Any

        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        class Base:
            def pick(self, value):
                return value

            @staticmethod
            def pick_static(value):
                return value

        class Child(Base):
            def via_super(self):
                return super().pick(Segment(Point(1, 2), Point(3, 4)))

            def via_super_static(self):
                return super().pick_static(Segment(Point(5, 6), Point(7, 8)))

        child = Child()
        left = child.via_super()
        right = child.via_super_static()
        same: Any = Segment(Point(1, 2), Point(3, 4))
        tail_same: Any = Segment(Point(5, 6), Point(7, 8))

        table = {
            left: 10,
            right: 20,
        }

        print(table[same])
        print(table[tail_same])
        """)
    ir_text = _generate_ir(source)

    assert re.search(r"\bcall\b[^\n]*@user_value_mod_Child_via_super\b", ir_text)
    assert re.search(r"\bcall\b[^\n]*@user_value_mod_Child_via_super_static\b", ir_text)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_new\b", ir_text)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_set_field\b", ir_text)
    # Scope the "no constructor call" checks to the function bodies under test.
    # The whole module also emits the Segment/Point __init__ method-native
    # adapters, which legitimately contain a call to those __init__ symbols;
    # what matters is that via_super / via_super_static project the argument via
    # valuebox_* rather than calling the constructor.
    vs = ir_text.index("define external ptr @user_value_mod_Child_via_super(")
    vs_body = ir_text[vs : ir_text.index("\ndefine ", vs + 1)]
    vss = ir_text.index("define external ptr @user_value_mod_Child_via_super_static(")
    vss_body = ir_text[vss : ir_text.index("\ndefine ", vss + 1)]
    scoped = vs_body + vss_body
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Segment___init__\b", scoped)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Point___init__\b", scoped)
    assert not re.search(r"\bextractvalue ptr\b", scoped)


def test_valueclass_constructor_dataclasses_replace_keyword_boxes_valuebox():
    source = textwrap.dedent("""
        from dataclasses import dataclass, replace
        from typing import Any

        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        @dataclass
        class Holder:
            item: Any
            label: str

        base = Holder(None, "base")
        updated = replace(base, item=Segment(Point(1, 2), Point(3, 4)))
        same: Any = Segment(Point(1, 2), Point(3, 4))

        table = {
            updated.item: 10,
        }

        print(table[same])
        print(updated.label)
        """)
    ir_text = _generate_ir(source)
    main_ir = ir_text[ir_text.index("define i32 @main") :]

    assert re.search(r"\bcall\b[^\n]*@py_dataclass_replace\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_new\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_set_field\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Segment___init__\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Point___init__\b", main_ir)
    assert not re.search(r"\bextractvalue ptr\b", main_ir)


def test_valueclass_constructor_membership_needle_projection_boxes_valuebox():
    source = textwrap.dedent("""
        from typing import Any

        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        same: Any = Segment(Point(1, 2), Point(3, 4))
        items = [same]
        table = {same: 10}
        tuple_items = (same,)

        print(Segment(Point(1, 2), Point(3, 4)) in items)
        print(Segment(Point(1, 2), Point(3, 5)) not in items)
        print(Segment(Point(1, 2), Point(3, 4)) in table)
        print(Segment(Point(1, 2), Point(3, 4)) in tuple_items)
        """)
    ir_text = _generate_ir(source)
    main_ir = ir_text[ir_text.index("define i32 @main") :]

    assert re.search(r"\bcall\b[^\n]*@py_list_contains\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_dict_contains\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_obj_contains\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_new\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_set_field\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Segment___init__\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Point___init__\b", main_ir)
    assert not re.search(r"\bextractvalue ptr\b", main_ir)


def test_valueclass_constructor_builtin_object_boundary_boxes_valuebox():
    source = textwrap.dedent("""
        from typing import Any

        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        same: Any = Segment(Point(1, 2), Point(3, 4))
        table = {same: 10}

        print(hash(Segment(Point(1, 2), Point(3, 4))) == hash(same))
        print(table[Segment(Point(1, 2), Point(3, 4))])
        print(repr(Segment(Point(1, 2), Point(3, 4))))
        print(str(Segment(Point(1, 2), Point(3, 4))))
        print(format(Segment(Point(1, 2), Point(3, 4)), ""))
        print(type(Segment(Point(1, 2), Point(3, 4))).__name__)
        """)
    ir_text = _generate_ir(source)
    main_ir = ir_text[ir_text.index("define i32 @main") :]

    assert re.search(r"\bcall\b[^\n]*@py_obj_hash\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_obj_repr\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_obj_str\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_obj_format\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_new\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_set_field\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Segment___init__\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Point___init__\b", main_ir)
    assert not re.search(r"\bextractvalue ptr\b", main_ir)


def test_valueclass_constructor_subscript_store_key_projection_boxes_valuebox():
    source = textwrap.dedent("""
        from typing import Any

        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        def put(d: Any) -> int:
            d[Segment(Point(5, 6), Point(7, 8))] = 20
            return 0

        table = {}
        table[Segment(Point(1, 2), Point(3, 4))] = 10
        put(table)

        print(table[Segment(Point(1, 2), Point(3, 4))])
        print(table[Segment(Point(5, 6), Point(7, 8))])
        """)
    ir_text = _generate_ir(source)
    main_ir = ir_text[ir_text.index("define i32 @main") :]
    put_ir = next(
        chunk
        for chunk in ir_text.split("\ndefine ")
        if chunk.startswith("i64 @user_value_mod_put")
        or "@user_value_mod_put(" in chunk.split("\n", 1)[0]
    )

    assert re.search(r"\bcall\b[^\n]*@py_dict_set\b", main_ir)
    # A dynamic receiver stores through py_obj_assign_subscript, which
    # wraps py_obj_setitem with the unsupported-assignment TypeError.
    assert re.search(
        r"\bcall\b[^\n]*@py_obj_(?:setitem|assign_subscript)\b", put_ir
    )
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_new\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_set_field\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_new\b", put_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Segment___init__\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Point___init__\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Segment___init__\b", put_ir)
    assert not re.search(r"\bextractvalue ptr\b", main_ir)


def test_valueclass_constructor_unpack_subscript_store_key_projection_boxes_valuebox():
    source = textwrap.dedent("""
        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        table = {}
        table[Segment(Point(1, 2), Point(3, 4))], extra = 30, 1

        print(table[Segment(Point(1, 2), Point(3, 4))])
        print(extra)
        """)
    ir_text = _generate_ir(source)
    main_ir = ir_text[ir_text.index("define i32 @main") :]

    assert re.search(r"\bcall\b[^\n]*@py_obj_setitem\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_new\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_set_field\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Segment___init__\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Point___init__\b", main_ir)
    assert not re.search(r"\bextractvalue ptr\b", main_ir)


def test_valueclass_constructor_aug_subscript_key_projection_boxes_valuebox():
    source = textwrap.dedent("""
        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        table = {}
        table[Segment(Point(1, 2), Point(3, 4))] = 10
        table[Segment(Point(1, 2), Point(3, 4))] += 5

        print(table[Segment(Point(1, 2), Point(3, 4))])
        """)
    ir_text = _generate_ir(source)
    main_ir = ir_text[ir_text.index("define i32 @main") :]

    assert re.search(r"\bcall\b[^\n]*@py_obj_getitem\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_obj_setitem\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_new\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_set_field\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Segment___init__\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Point___init__\b", main_ir)
    assert not re.search(r"\bextractvalue ptr\b", main_ir)


def test_valueclass_constructor_dict_literal_key_projection_boxes_valuebox():
    # Coverage lock: this path was already green when probed on 2026-06-10
    # (dict-literal keys project through _maybe_emit_valueclass_constructor_payload
    # in _emit_dict_literal); the guard pins it against regression.
    source = textwrap.dedent("""
        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        table = {
            Segment(Point(1, 2), Point(3, 4)): 10,
            Segment(Point(5, 6), Point(7, 8)): 20,
        }

        print(table[Segment(Point(1, 2), Point(3, 4))])
        print(table[Segment(Point(5, 6), Point(7, 8))])
        print(len(table))
        """)
    ir_text = _generate_ir(source)
    main_ir = ir_text[ir_text.index("define i32 @main") :]

    assert re.search(r"\bcall\b[^\n]*@py_dict_set\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_new\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_set_field\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Segment___init__\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Point___init__\b", main_ir)
    assert not re.search(r"\bextractvalue ptr\b", main_ir)


def test_valueclass_constructor_compare_operand_projection_boxes_valuebox():
    source = textwrap.dedent("""
        from typing import Any

        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        other: Any = 1

        print(Segment(Point(1, 2), Point(3, 4)) == other)
        print(Segment(Point(1, 2), Point(3, 4)) != other)
        """)
    ir_text = _generate_ir(source)
    main_ir = ir_text[ir_text.index("define i32 @main") :]

    assert re.search(r"\bcall\b[^\n]*@py_obj_eq(?:_value)?\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_new\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_set_field\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Segment___init__\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Point___init__\b", main_ir)
    assert not re.search(r"\bextractvalue ptr\b", main_ir)


def test_valueclass_constructor_exception_arg_projection_boxes_valuebox():
    source = textwrap.dedent("""
        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        try:
            raise ValueError(Segment(Point(1, 2), Point(3, 4)))
        except ValueError as e:
            print(e)
        """)
    ir_text = _generate_ir(source)
    main_ir = ir_text[ir_text.index("define i32 @main") :]

    assert re.search(r"\bcall\b[^\n]*@py_exc_new(?:_with_value)?\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_new\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Segment___init__\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Point___init__\b", main_ir)


def test_valueclass_constructor_percent_format_operand_projection_boxes_valuebox():
    source = textwrap.dedent("""
        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        msg = "v=%s" % Segment(Point(1, 2), Point(3, 4))
        print(msg)
        """)
    ir_text = _generate_ir(source)
    main_ir = ir_text[ir_text.index("define i32 @main") :]

    # the %-format lowering may use py_str_mod or a format-specific helper;
    # the essential contract is projection (valuebox, no identity ctor)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_new\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Segment___init__\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Point___init__\b", main_ir)


def test_valueclass_constructor_walrus_target_uses_payload():
    source = textwrap.dedent("""
        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        if (s := Segment(Point(1, 2), Point(3, 4))):
            print(s.start.x)
        """)
    ir_text = _generate_ir(source)
    main_ir = ir_text[ir_text.index("define i32 @main") :]

    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Segment___init__\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Point___init__\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@py_instance_new\b", main_ir)


def test_valueclass_payload_unboxes_from_dyn_function_boundary(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    source = textwrap.dedent("""
        import pcc
        from typing import Any

        @pcc.valueclass
        class Point:
            x: int
            y: int

        def ident(x: Any) -> Any:
            return x

        def total(p: Point) -> int:
            return p.x + p.y

        p = Point(3, 4)
        d = ident(p)
        print(total(d))
        """)
    ir_text = _generate_ir(source)
    main_ir = ir_text[ir_text.index("define i32 @main") :]

    assert re.search(r"\bcall\b[^\n]*@py_valuebox_get_field\b", main_ir)
    assert re.search(r"\bcall ptr \(\{ i64, i64 \}\) @user_value_mod_total", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Point___init__\b", main_ir)

    src = tmp_path / "valueclass_unbox.py"
    exe = tmp_path / "valueclass_unbox"
    src.write_text(source, encoding="utf-8")
    compile_python(str(src), str(exe), libpython_mode="off", ir_scaffold_mode="on")
    proc = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    assert proc.stdout.strip() == "7"


def test_valueclass_payload_unbox_rejects_wrong_dyn_type(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    source = textwrap.dedent("""
        import pcc
        from typing import Any

        @pcc.valueclass
        class Point:
            x: int
            y: int

        def ident(x: Any) -> Any:
            return x

        def total(p: Point) -> int:
            return p.x + p.y

        d = ident(123)
        print(total(d))
        """)
    ir_text = _generate_ir(source)
    assert "py_obj_isinstance" in ir_text
    assert "value.Point.unbox.typeerror" in ir_text

    src = tmp_path / "valueclass_unbox_wrong_type.py"
    exe = tmp_path / "valueclass_unbox_wrong_type"
    src.write_text(source, encoding="utf-8")
    compile_python(str(src), str(exe), libpython_mode="off", ir_scaffold_mode="on")
    proc = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert proc.returncode != 0
    assert "TypeError" in (proc.stdout + proc.stderr)


def test_valueclass_pointer_payload_crosses_dyn_boundary(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    source = textwrap.dedent("""
        import gc
        import pcc
        from typing import Any

        @pcc.valueclass
        class Bag:
            items: list
            count: int

        def ident(x: Any) -> Any:
            return x

        def total(b: Bag) -> int:
            return len(b.items) + b.count

        bag = Bag([1, 2, 3], 4)
        d = ident(bag)
        gc.collect()
        print(total(d))
        print(len(d.items))
        """)
    ir_text = _generate_ir(source)
    main_ir = ir_text[ir_text.index("define i32 @main") :]

    assert re.search(r"\bcall\b[^\n]*@py_valuebox_new\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_set_field\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_get_field\b", main_ir)
    assert re.search(r"\bcall ptr \(\{ ptr, i64 \}\) @user_value_mod_total", main_ir)

    src = tmp_path / "valueclass_pointer_payload.py"
    exe = tmp_path / "valueclass_pointer_payload"
    src.write_text(source, encoding="utf-8")
    compile_python(str(src), str(exe), libpython_mode="off", ir_scaffold_mode="on")
    proc = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    assert proc.stdout.strip().splitlines() == ["7", "3"]


def test_valueclass_constructor_condition_position_uses_payload():
    # `if Seg(...):` / ternary-cond direct constructors project to
    # payloads (boxed once for py_obj_truthy) instead of allocating an
    # identity instance. The degenerate always-truthy form stays
    # semantically CPython-equal (user __bool__/__len__ would dispatch
    # through the boxed object).
    source = textwrap.dedent("""
        import pcc

        @pcc.valueclass
        class Seg:
            a: int
            b: int

        if Seg(1, 2):
            print("truthy")
        v = "yes" if Seg(3, 4) else "no"
        print(v)
        while Seg(5, 6):
            print("once")
            break
        """)
    ir_text = _generate_ir(source)
    main_ir = ir_text[ir_text.index("define i32 @main") :]

    assert not re.search(r"\bcall\b[^\n]*@user_value_mod_Seg___init__\b", main_ir)
    assert not re.search(r"\bcall\b[^\n]*@py_instance_new\b", main_ir)
    assert re.search(r"\bcall\b[^\n]*@py_valuebox_new\b", main_ir)


def test_valueclass_weakref_rejected_at_compile_time():
    # Valhalla projection rule: value projections are identity-free and
    # weak references observe identity lifetime (CPython analogue:
    # weakref.ref(3) raises TypeError). A box created at the call
    # boundary would have an unpredictable lifetime, so statically-known
    # valueclass arguments to weakref.ref/proxy are rejected at compile
    # time like the `is` identity-escape diagnostic.
    import pytest

    source = textwrap.dedent("""
        import weakref

        import pcc

        @pcc.valueclass
        class Pt:
            x: int
            y: int

        p = Pt(1, 2)
        r = weakref.ref(p)
        """)
    with pytest.raises(Exception, match="weak reference to a valueclass"):
        _generate_ir(source)


def test_valueclass_weakref_from_import_rejected_at_compile_time():
    # The weakref identity-escape diagnostic must also cover known
    # `from weakref import ref/proxy` bindings without making every
    # user function named `ref` special.
    import pytest

    source = textwrap.dedent("""
        from weakref import ref as wref, proxy as wproxy

        import pcc

        @pcc.valueclass
        class Pt:
            x: int
            y: int

        p = Pt(1, 2)
        r = wref(p)
        q = wproxy(p)
        """)
    with pytest.raises(Exception, match="weak reference to a valueclass"):
        _generate_ir(source)

    user_ref_source = textwrap.dedent("""
        import pcc

        @pcc.valueclass
        class Pt:
            x: int
            y: int

        def ref(x):
            return x

        p = Pt(1, 2)
        r = ref(p)
        """)
    _generate_ir(user_ref_source)
