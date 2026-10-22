from __future__ import annotations

import subprocess
from pathlib import Path
from textwrap import dedent

from pcc.frontends.python.pipeline import compile_python


def _write_and_build_probe(tmp_path: Path, source: str, *, backend: str | None = None) -> Path:
    src = tmp_path / "value_class_runtime_probe.py"
    exe = tmp_path / "value_class_runtime_probe"
    src.write_text(dedent(source), encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend=backend,
    )
    return exe


def _run_probe(exe: Path) -> list[str]:
    proc = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def test_valueclass_runtime_smoke_direct_paths(tmp_path):
    exe = _write_and_build_probe(
        tmp_path,
        """
            import pcc

            @pcc.valueclass
            class Point:
                x: int
                y: int

            def area(p: Point) -> int:
                return p.x * p.y

            if __name__ == "__main__":
                p = Point(3, 4)
                q = Point(1, 7)
                print(area(p))
                print(q.x + q.y)
                print(p.x + p.y + q.x + q.y)
        """,
    )
    out = _run_probe(exe)
    assert out == ["12", "8", "15"]


def test_valueclass_runtime_field_read_consistent_with_payload_assignment(tmp_path):
    exe = _write_and_build_probe(
        tmp_path,
        """
        from typing import Any

        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int
            z: bool

        def checksum(p: Point) -> int:
            return p.x + (p.y * 2) + (1 if p.z else 0)

        if __name__ == "__main__":
            p = Point(2, 3, True)
            print(checksum(p))
            print(Point(0, 0, False).x)
            print(p.z)
        """,
    )
    out = _run_probe(exe)
    assert out == ["9", "0", "True"]


def test_valueclass_runtime_constructor_receiver_method_uses_payload(tmp_path):
    exe = _write_and_build_probe(
        tmp_path,
        """
        import pcc

        @pcc.valueclass
        class Vec:
            x: int
            y: int

            def norm2(self) -> int:
                return self.x * self.x + self.y * self.y

        if __name__ == "__main__":
            print(Vec(3, 4).norm2())
        """,
        backend="self",
    )
    out = _run_probe(exe)
    assert out == ["25"]


def test_valueclass_runtime_genexpr_list_element_fields_use_payload(tmp_path):
    exe = _write_and_build_probe(
        tmp_path,
        """
        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        if __name__ == "__main__":
            points = [Point(1, 2), Point(3, 4), Point(5, 6)]
            print(sum(p.x for p in points))
        """,
        backend="self",
    )
    out = _run_probe(exe)
    assert out == ["9"]


def test_valueclass_runtime_valuebox_survives_gc_collect(tmp_path):
    exe = _write_and_build_probe(
        tmp_path,
        """
        from typing import Any

        import gc
        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        def to_dyn(v: Any) -> Any:
            return v

        def read_point(v: Any) -> int:
            if isinstance(v, Point):
                return v.x + v.y
            return -1

        if __name__ == "__main__":
            slot = [to_dyn(Point(7, 8))]
            gc.collect()
            print(read_point(slot[0]))
            saved = slot[0]
            slot[0] = None
            gc.collect()
            print(read_point(saved))
        """,
    )
    out = _run_probe(exe)
    assert out == ["15", "15"]


def test_valueclass_runtime_type_error_survives_gc_collect(tmp_path):
    exe = _write_and_build_probe(
        tmp_path,
        """
        import gc
        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        def total(p: Point) -> int:
            return p.x + p.y

        def to_dyn(v: object) -> object:
            return v

        if __name__ == "__main__":
            bad = to_dyn(123)
            gc.collect()
            try:
                print(total(bad))
            except Exception as e:
                print(type(e).__name__)
        """,
    )
    out = _run_probe(exe)
    assert out == ["TypeError"]


def test_valueclass_runtime_pointer_payload_survives_dyn_gc_collect(tmp_path):
    exe = _write_and_build_probe(
        tmp_path,
        """
        from typing import Any

        import gc
        import pcc

        @pcc.valueclass
        class Bag:
            items: list
            count: int

        def to_dyn(v: Any) -> Any:
            return v

        def total(b: Bag) -> int:
            return len(b.items) + b.count

        if __name__ == "__main__":
            bag = Bag([1, 2, 3], 4)
            dyn = to_dyn(bag)
            gc.collect()
            print(total(dyn))
            print(len(dyn.items))
        """,
    )
    out = _run_probe(exe)
    assert out == ["7", "3"]


def test_valueclass_runtime_str_payload_survives_dyn_gc_collect(tmp_path):
    exe = _write_and_build_probe(
        tmp_path,
        """
        from typing import Any

        import gc
        import pcc

        @pcc.valueclass
        class Label:
            name: str
            score: int

        def to_dyn(v: Any) -> Any:
            return v

        def total(label: Label) -> int:
            return len(label.name) + label.score

        if __name__ == "__main__":
            dyn = to_dyn(Label("alpha", 6))
            gc.collect()
            print(total(dyn))
            print(dyn.name)
        """,
    )
    out = _run_probe(exe)
    assert out == ["11", "alpha"]


def test_valueclass_runtime_pointer_payload_preserves_mutable_identity(tmp_path):
    exe = _write_and_build_probe(
        tmp_path,
        """
        from typing import Any

        import gc
        import pcc

        @pcc.valueclass
        class Bag:
            items: list
            count: int

        def to_dyn(v: Any) -> Any:
            return v

        def total(bag: Bag) -> int:
            return len(bag.items) + bag.count

        if __name__ == "__main__":
            items = [1, 2]
            dyn = to_dyn(Bag(items, 5))
            gc.collect()
            dyn.items.append(3)
            gc.collect()
            print(len(items))
            print(total(dyn))
        """,
    )
    out = _run_probe(exe)
    assert out == ["3", "8"]


def test_valueclass_runtime_pointer_payload_equality_uses_object_equality(tmp_path):
    exe = _write_and_build_probe(
        tmp_path,
        """
        import gc
        import pcc

        @pcc.valueclass
        class Bag:
            items: list
            count: int

        def same(left: Bag, right: Bag) -> bool:
            return left == right

        def different(left: Bag, right: Bag) -> bool:
            return left != right

        if __name__ == "__main__":
            first = Bag([1, 2], 3)
            same_contents = Bag([1, 2], 3)
            changed_items = Bag([1, 4], 3)
            changed_count = Bag([1, 2], 4)
            gc.collect()
            print(same(first, same_contents))
            print(same(first, changed_items))
            print(different(first, changed_items))
            print(different(first, changed_count))
        """,
    )
    out = _run_probe(exe)
    assert out == ["True", "False", "True", "True"]


def test_valueclass_runtime_boxed_valueclass_equality_uses_payload_fields(tmp_path):
    exe = _write_and_build_probe(
        tmp_path,
        """
        from typing import Any

        import gc
        import pcc

        @pcc.valueclass
        class Bag:
            items: list
            count: int

        @pcc.valueclass
        class Sack:
            items: list
            count: int

        def to_dyn(v: Any) -> Any:
            return v

        if __name__ == "__main__":
            left = to_dyn(Bag([1, 2], 3))
            same = to_dyn(Bag([1, 2], 3))
            different_items = to_dyn(Bag([1, 4], 3))
            different_class = to_dyn(Sack([1, 2], 3))
            gc.collect()
            print(left == same)
            print(left == different_items)
            print(left != different_items)
            print(left == different_class)
        """,
    )
    out = _run_probe(exe)
    assert out == ["True", "False", "True", "False"]


def test_valueclass_runtime_boxed_valueclass_hash_matches_payload_equality(tmp_path):
    exe = _write_and_build_probe(
        tmp_path,
        """
        from typing import Any

        import gc
        import pcc

        @pcc.valueclass
        class Label:
            name: str
            score: int

        def to_dyn(v: Any) -> Any:
            return v

        if __name__ == "__main__":
            left = to_dyn(Label("alpha", 6))
            same = to_dyn(Label("alpha", 6))
            changed = to_dyn(Label("alpha", 7))
            table = {left: 42}
            gc.collect()
            print(table[same])
            print(changed in table)
        """,
    )
    out = _run_probe(exe)
    assert out == ["42", "False"]


def test_valueclass_runtime_boxed_type_name_preserves_valueclass_metadata(tmp_path):
    exe = _write_and_build_probe(
        tmp_path,
        """
        from typing import Any

        import gc
        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        def to_dyn(v: Any) -> Any:
            return v

        if __name__ == "__main__":
            direct = Point(1, 2)
            boxed = to_dyn(direct)
            gc.collect()
            print(type(direct).__name__)
            print(type(boxed).__name__)
        """,
    )
    out = _run_probe(exe)
    assert out == ["Point", "Point"]


def test_valueclass_runtime_boxed_print_tuple_and_dyn_return_boundary(tmp_path):
    exe = _write_and_build_probe(
        tmp_path,
        """
        from typing import Any

        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        def to_dyn(v: Any) -> Any:
            return v

        def make_dyn() -> Any:
            return Point(5, 6)

        if __name__ == "__main__":
            boxed = to_dyn(Point(1, 2))
            slot = (boxed,)
            print(boxed)
            print(str(boxed))
            print(repr(boxed))
            print(slot[0].x)
            returned = make_dyn()
            print(returned.y)
        """,
    )
    out = _run_probe(exe)
    assert out == [
        "Point(x=1, y=2)",
        "Point(x=1, y=2)",
        "Point(x=1, y=2)",
        "1",
        "6",
    ]


def test_valueclass_runtime_boxed_container_subscript_self_backend_to_typed_payload(tmp_path):
    exe = _write_and_build_probe(
        tmp_path,
        """
        from typing import Any

        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        def to_dyn(v: Any) -> Any:
            return v

        def total(p: Point) -> int:
            return p.x + p.y

        if __name__ == "__main__":
            boxed = to_dyn(Point(7, 8))
            tup = (boxed,)
            lst = [boxed]
            print(total(tup[0]))
            print(total(lst[0]))
        """,
        backend="self",
    )
    out = _run_probe(exe)
    assert out == ["15", "15"]


def test_valueclass_runtime_boxed_identity_observes_box_identity_self_backend(tmp_path):
    exe = _write_and_build_probe(
        tmp_path,
        """
        from typing import Any

        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        def to_dyn(v: Any) -> Any:
            return v

        if __name__ == "__main__":
            a = to_dyn(Point(1, 2))
            b = to_dyn(Point(1, 2))
            c = a
            print(a is c)
            print(a is b)
            print(a is not b)
            print(id(a) == id(c))
            print(id(a) == id(b))
        """,
        backend="self",
    )
    out = _run_probe(exe)
    assert out == ["True", "False", "True", "True", "False"]


def test_valueclass_runtime_nested_valuebox_roundtrip_self_backend(tmp_path):
    exe = _write_and_build_probe(
        tmp_path,
        """
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

        def to_dyn(v: Any) -> Any:
            return v

        def total(s: Segment) -> int:
            return s.start.x + s.start.y + s.end.x + s.end.y

        if __name__ == "__main__":
            seg = Segment(Point(1, 2), Point(3, 4))
            dyn = to_dyn(seg)
            print(dyn.start.x)
            print(dyn.end.y)
            print(total(dyn))
        """,
        backend="self",
    )
    out = _run_probe(exe)
    assert out == ["1", "4", "10"]


def test_valueclass_runtime_nested_valuebox_dynamic_equality_hash_and_type_self_backend(
    tmp_path,
):
    exe = _write_and_build_probe(
        tmp_path,
        """
        from typing import Any

        import gc
        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        def to_dyn(v: Any) -> Any:
            return v

        def make_dyn(a: int, b: int, c: int, d: int) -> Any:
            return Segment(Point(a, b), Point(c, d))

        def total(s: Segment) -> int:
            return s.start.x + s.start.y + s.end.x + s.end.y

        if __name__ == "__main__":
            left = to_dyn(Segment(Point(1, 2), Point(3, 4)))
            same = make_dyn(1, 2, 3, 4)
            changed = to_dyn(Segment(Point(1, 2), Point(3, 5)))
            slot = (left,)
            table = {left: 42}
            gc.collect()
            print(left == same)
            print(left == changed)
            print(left != changed)
            print(table[same])
            print(changed in table)
            print(type(slot[0]).__name__)
            print(slot[0])
            returned = make_dyn(5, 6, 7, 8)
            print(total(returned))
            print(slot[0].start.x + slot[0].end.y)
        """,
        backend="self",
    )
    out = _run_probe(exe)
    assert out == [
        "True",
        "False",
        "True",
        "42",
        "False",
        "Segment",
        "Segment(start=Point(x=1, y=2), end=Point(x=3, y=4))",
        "26",
        "5",
    ]


def test_valueclass_runtime_nested_valuebox_local_and_container_projection_self_backend(
    tmp_path,
):
    exe = _write_and_build_probe(
        tmp_path,
        """
        from typing import Any

        import gc
        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        def total(s: Segment) -> int:
            return s.start.x + s.start.y + s.end.x + s.end.y

        if __name__ == "__main__":
            local: Any = Segment(Point(1, 2), Point(3, 4))
            same: Any = Segment(Point(1, 2), Point(3, 4))
            changed: Any = Segment(Point(1, 2), Point(3, 5))
            slot = (Segment(Point(1, 2), Point(3, 4)),)
            items = [Segment(Point(5, 6), Point(7, 8))]
            mapping = {"seg": Segment(Point(9, 10), Point(11, 12))}
            table = {local: 42}
            gc.collect()
            print(local == same)
            print(local == changed)
            print(local != changed)
            print(table[same])
            print(changed in table)
            print(type(slot[0]).__name__)
            print(slot[0])
            print(total(slot[0]))
            print(total(items[0]))
            print(total(mapping["seg"]))
        """,
        backend="self",
    )
    out = _run_probe(exe)
    assert out == [
        "True",
        "False",
        "True",
        "42",
        "False",
        "Segment",
        "Segment(start=Point(x=1, y=2), end=Point(x=3, y=4))",
        "10",
        "26",
        "42",
    ]


def test_valueclass_runtime_nested_valuebox_module_global_projection_self_backend(
    tmp_path,
):
    exe = _write_and_build_probe(
        tmp_path,
        """
        from typing import Any

        import gc
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
        global_changed: Any = Segment(Point(1, 2), Point(3, 5))
        global_table = {global_left: 42}
        global_segments = {"seg": global_left}

        def total(s: Segment) -> int:
            return s.start.x + s.start.y + s.end.x + s.end.y

        def total_global() -> int:
            return total(global_left)

        def total_table() -> int:
            return total(global_segments["seg"])

        if __name__ == "__main__":
            gc.collect()
            print(global_left == global_same)
            print(global_left == global_changed)
            print(global_left != global_changed)
            print(global_table[global_same])
            print(global_changed in global_table)
            print(type(global_left).__name__)
            print(global_left)
            print(total_global())
            print(total_table())
        """,
        backend="self",
    )
    out = _run_probe(exe)
    assert out == [
        "True",
        "False",
        "True",
        "42",
        "False",
        "Segment",
        "Segment(start=Point(x=1, y=2), end=Point(x=3, y=4))",
        "10",
        "10",
    ]


def test_valueclass_runtime_nested_valuebox_mutation_store_projection_self_backend(
    tmp_path,
):
    exe = _write_and_build_probe(
        tmp_path,
        """
        from typing import Any

        import gc
        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        def total(s: Segment) -> int:
            return s.start.x + s.start.y + s.end.x + s.end.y

        if __name__ == "__main__":
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
            gc.collect()
            print(items[0] == items[1])
            print(table[same_key])
            print(type(items[0]).__name__)
            print(items[0])
            print(total(items[0]))
            print(total(mapping["seg"]))
        """,
        backend="self",
    )
    out = _run_probe(exe)
    assert out == [
        "True",
        "99",
        "Segment",
        "Segment(start=Point(x=1, y=2), end=Point(x=3, y=4))",
        "10",
        "26",
    ]


def test_valueclass_runtime_nested_valuebox_comprehension_projection_self_backend(
    tmp_path,
):
    exe = _write_and_build_probe(
        tmp_path,
        """
        from typing import Any

        import gc
        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        def total(s: Segment) -> int:
            return s.start.x + s.start.y + s.end.x + s.end.y

        if __name__ == "__main__":
            items = [Segment(Point(1, 2), Point(3, 4)) for _ in range(2)]
            mapping = {"seg": Segment(Point(5, 6), Point(7, 8)) for _ in range(1)}
            table = {Segment(Point(9, 10), Point(11, 12)): 99 for _ in range(1)}
            same_key: Any = Segment(Point(9, 10), Point(11, 12))
            gc.collect()
            print(items[0] == items[1])
            print(table[same_key])
            print(type(items[0]).__name__)
            print(items[0])
            print(total(items[0]))
            print(total(mapping["seg"]))
        """,
        backend="self",
    )
    out = _run_probe(exe)
    assert out == [
        "True",
        "99",
        "Segment",
        "Segment(start=Point(x=1, y=2), end=Point(x=3, y=4))",
        "10",
        "26",
    ]


def test_valueclass_runtime_nested_valuebox_call_arg_projection_self_backend(
    tmp_path,
):
    exe = _write_and_build_probe(
        tmp_path,
        """
        from typing import Any

        import gc
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

        def total(s: Segment) -> int:
            return s.start.x + s.start.y + s.end.x + s.end.y

        if __name__ == "__main__":
            left = keep(Segment(Point(1, 2), Point(3, 4)))
            same = keep(Segment(Point(1, 2), Point(3, 4)))
            changed = keep(Segment(Point(1, 2), Point(3, 5)))
            table = {left: 42}
            gc.collect()
            print(left == same)
            print(left == changed)
            print(left != changed)
            print(table[same])
            print(changed in table)
            print(type(left).__name__)
            print(left)
            print(total(left))
        """,
        backend="self",
    )
    out = _run_probe(exe)
    assert out == [
        "True",
        "False",
        "True",
        "42",
        "False",
        "Segment",
        "Segment(start=Point(x=1, y=2), end=Point(x=3, y=4))",
        "10",
    ]


def test_valueclass_runtime_nested_valuebox_dynamic_callable_arg_projection_self_backend(
    tmp_path,
):
    exe = _write_and_build_probe(
        tmp_path,
        """
        from typing import Any

        import gc
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

        def total(s: Segment) -> int:
            return s.start.x + s.start.y + s.end.x + s.end.y

        if __name__ == "__main__":
            left = Keeper()(Segment(Point(1, 2), Point(3, 4)))
            same = Keeper()(Segment(Point(1, 2), Point(3, 4)))
            changed = Keeper()(Segment(Point(1, 2), Point(3, 5)))
            table = {left: 42}
            gc.collect()
            print(left == same)
            print(left == changed)
            print(left != changed)
            print(table[same])
            print(changed in table)
            print(type(left).__name__)
            print(left)
            print(total(left))
        """,
        backend="self",
    )
    out = _run_probe(exe)
    assert out == [
        "True",
        "False",
        "True",
        "42",
        "False",
        "Segment",
        "Segment(start=Point(x=1, y=2), end=Point(x=3, y=4))",
        "10",
    ]


def test_valueclass_runtime_nested_valuebox_attribute_store_projection_self_backend(
    tmp_path,
):
    exe = _write_and_build_probe(
        tmp_path,
        """
        from typing import Any

        import gc
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

        def total(s: Segment) -> int:
            return s.start.x + s.start.y + s.end.x + s.end.y

        if __name__ == "__main__":
            holder = Holder()
            holder.left = Segment(Point(1, 2), Point(3, 4))
            holder.same = Segment(Point(1, 2), Point(3, 4))
            setattr(holder, "changed", Segment(Point(1, 2), Point(3, 5)))
            table = {holder.left: 42}
            gc.collect()
            print(holder.left == holder.same)
            print(holder.left == holder.changed)
            print(holder.left != holder.changed)
            print(table[holder.same])
            print(holder.changed in table)
            print(type(holder.left).__name__)
            print(holder.left)
            print(total(holder.left))
            print(total(holder.changed))
        """,
        backend="self",
    )
    out = _run_probe(exe)
    assert out == [
        "True",
        "False",
        "True",
        "42",
        "False",
        "Segment",
        "Segment(start=Point(x=1, y=2), end=Point(x=3, y=4))",
        "10",
        "11",
    ]


def test_valueclass_runtime_nested_valuebox_short_circuit_projection_self_backend(
    tmp_path,
):
    exe = _write_and_build_probe(
        tmp_path,
        """
        from typing import Any

        import gc
        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        def total(s: Segment) -> int:
            return s.start.x + s.start.y + s.end.x + s.end.y

        if __name__ == "__main__":
            flag = False
            left: Any = flag or Segment(Point(1, 2), Point(3, 4))
            same: Any = flag or Segment(Point(1, 2), Point(3, 4))
            changed: Any = flag or Segment(Point(1, 2), Point(3, 5))
            tail: Any = left and Segment(Point(5, 6), Point(7, 8))
            tail_same: Any = same and Segment(Point(5, 6), Point(7, 8))
            tail_changed: Any = same and Segment(Point(5, 6), Point(7, 9))
            table = {left: 42, tail: 99}
            gc.collect()
            print(left == same)
            print(left == changed)
            print(left != changed)
            print(table[same])
            print(changed in table)
            print(tail == tail_same)
            print(tail == tail_changed)
            print(table[tail_same])
            print(tail_changed in table)
            print(type(left).__name__)
            print(type(tail).__name__)
            print(left)
            print(tail)
            print(total(left))
            print(total(tail))
        """,
        backend="self",
    )
    out = _run_probe(exe)
    assert out == [
        "True",
        "False",
        "True",
        "42",
        "False",
        "True",
        "False",
        "99",
        "False",
        "Segment",
        "Segment",
        "Segment(start=Point(x=1, y=2), end=Point(x=3, y=4))",
        "Segment(start=Point(x=5, y=6), end=Point(x=7, y=8))",
        "10",
        "26",
    ]


def test_valueclass_runtime_nested_valuebox_dict_builtin_keyword_projection_self_backend(
    tmp_path,
):
    exe = _write_and_build_probe(
        tmp_path,
        """
        from typing import Any

        import gc
        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        def total(s: Segment) -> int:
            return s.start.x + s.start.y + s.end.x + s.end.y

        if __name__ == "__main__":
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
                tail_changed=Segment(Point(5, 6), Point(7, 9)),
            )
            table = {mapping["left"]: 42, merged["tail"]: 99}
            gc.collect()
            print(mapping["left"] == mapping["same"])
            print(mapping["left"] == mapping["changed"])
            print(mapping["left"] != mapping["changed"])
            print(table[mapping["same"]])
            print(mapping["changed"] in table)
            print(merged["tail"] == merged["tail_same"])
            print(merged["tail"] == merged["tail_changed"])
            print(table[merged["tail_same"]])
            print(merged["tail_changed"] in table)
            print(merged["seed"])
            print(type(mapping["left"]).__name__)
            print(type(merged["tail"]).__name__)
            print(mapping["left"])
            print(merged["tail"])
            print(total(mapping["left"]))
            print(total(merged["tail"]))
        """,
        backend="self",
    )
    out = _run_probe(exe)
    assert out == [
        "True",
        "False",
        "True",
        "42",
        "False",
        "True",
        "False",
        "99",
        "False",
        "1",
        "Segment",
        "Segment",
        "Segment(start=Point(x=1, y=2), end=Point(x=3, y=4))",
        "Segment(start=Point(x=5, y=6), end=Point(x=7, y=8))",
        "10",
        "26",
    ]


def test_valueclass_runtime_nested_valuebox_dict_update_keyword_projection_self_backend(
    tmp_path,
):
    exe = _write_and_build_probe(
        tmp_path,
        """
        from typing import Any

        import gc
        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        def total(s: Segment) -> int:
            return s.start.x + s.start.y + s.end.x + s.end.y

        if __name__ == "__main__":
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
                tail_changed=Segment(Point(5, 6), Point(7, 9)),
            )
            table = {mapping["left"]: 42, merged["tail"]: 99}
            gc.collect()
            print(mapping["left"] == mapping["same"])
            print(mapping["left"] == mapping["changed"])
            print(mapping["left"] != mapping["changed"])
            print(table[mapping["same"]])
            print(mapping["changed"] in table)
            print(merged["tail"] == merged["tail_same"])
            print(merged["tail"] == merged["tail_changed"])
            print(table[merged["tail_same"]])
            print(merged["tail_changed"] in table)
            print(merged["seed"])
            print(type(mapping["left"]).__name__)
            print(type(merged["tail"]).__name__)
            print(mapping["left"])
            print(merged["tail"])
            print(total(mapping["left"]))
            print(total(merged["tail"]))
        """,
        backend="self",
    )
    out = _run_probe(exe)
    assert out == [
        "True",
        "False",
        "True",
        "42",
        "False",
        "True",
        "False",
        "99",
        "False",
        "1",
        "Segment",
        "Segment",
        "Segment(start=Point(x=1, y=2), end=Point(x=3, y=4))",
        "Segment(start=Point(x=5, y=6), end=Point(x=7, y=8))",
        "10",
        "26",
    ]


def test_valueclass_runtime_nested_valuebox_set_builtin_literal_source_self_backend(
    tmp_path,
):
    exe = _write_and_build_probe(
        tmp_path,
        """
        from typing import Any

        import gc
        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        def total(s: Segment) -> int:
            return s.start.x + s.start.y + s.end.x + s.end.y

        if __name__ == "__main__":
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
            changed: Any = Segment(Point(1, 2), Point(3, 5))
            missing: Any = Segment(Point(9, 9), Point(9, 9))
            tail_same: Any = Segment(Point(5, 6), Point(7, 8))
            tail_changed: Any = Segment(Point(5, 6), Point(7, 9))
            gc.collect()

            print(len(left))
            print(same in left)
            print(changed in left)
            print(missing in left)
            print(len(right))
            print(tail_same in right)
            print(tail_changed in right)
        """,
        backend="self",
    )
    out = _run_probe(exe)
    assert out == [
        "2",
        "True",
        "True",
        "False",
        "2",
        "True",
        "True",
    ]


def test_valueclass_runtime_nested_valuebox_set_method_element_self_backend(
    tmp_path,
):
    exe = _write_and_build_probe(
        tmp_path,
        """
        from typing import Any

        import gc
        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        if __name__ == "__main__":
            items = set()
            items.add(Segment(Point(1, 2), Point(3, 4)))
            items.add(Segment(Point(1, 2), Point(3, 4)))
            items.add(Segment(Point(1, 2), Point(3, 5)))
            items.discard(Segment(Point(9, 9), Point(9, 9)))

            same: Any = Segment(Point(1, 2), Point(3, 4))
            changed: Any = Segment(Point(1, 2), Point(3, 5))
            missing: Any = Segment(Point(9, 9), Point(9, 9))
            gc.collect()

            print(len(items))
            print(same in items)
            print(changed in items)
            print(missing in items)

            items.remove(Segment(Point(1, 2), Point(3, 4)))
            print(len(items))
            print(same in items)
            print(changed in items)

            items.discard(Segment(Point(1, 2), Point(3, 5)))
            print(len(items))
            print(changed in items)
        """,
        backend="self",
    )
    out = _run_probe(exe)
    assert out == [
        "2",
        "True",
        "True",
        "False",
        "1",
        "False",
        "True",
        "0",
        "False",
    ]


def test_valueclass_runtime_nested_valuebox_sequence_builtin_literal_source_self_backend(
    tmp_path,
):
    exe = _write_and_build_probe(
        tmp_path,
        """
        from typing import Any

        import gc
        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        if __name__ == "__main__":
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
            gc.collect()

            print(len(left))
            print(table[left[0]])
            print(table[left[1]])
            print(len(right))
            print(table[right[0]])
            print(table[right[1]])
            print(len(tup))
            print(table[tup[0]])
            print(table[tup[1]])
            print(len(tail_tup))
            print(table[tail_tup[0]])
            print(table[tail_tup[1]])
        """,
        backend="self",
    )
    out = _run_probe(exe)
    assert out == [
        "2",
        "10",
        "11",
        "2",
        "20",
        "21",
        "2",
        "10",
        "11",
        "2",
        "20",
        "21",
    ]


def test_valueclass_runtime_nested_valuebox_user_method_argument_self_backend(
    tmp_path,
):
    exe = _write_and_build_probe(
        tmp_path,
        """
        from typing import Any

        import gc
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

        if __name__ == "__main__":
            keeper = Keeper()
            left = keeper.pick(Segment(Point(1, 2), Point(3, 4)))
            right = Keeper.pick_static(Segment(Point(5, 6), Point(7, 8)))
            same: Any = Segment(Point(1, 2), Point(3, 4))
            tail_same: Any = Segment(Point(5, 6), Point(7, 8))
            table = {
                left: 10,
                right: 20,
            }
            gc.collect()

            print(table[same])
            print(table[tail_same])
            print(type(left).__name__)
            print(type(right).__name__)
        """,
        backend="self",
    )
    out = _run_probe(exe)
    assert out == [
        "10",
        "20",
        "Segment",
        "Segment",
    ]


def test_valueclass_runtime_nested_valuebox_super_method_argument_self_backend(
    tmp_path,
):
    exe = _write_and_build_probe(
        tmp_path,
        """
        from typing import Any

        import gc
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

        if __name__ == "__main__":
            child = Child()
            left = child.via_super()
            right = child.via_super_static()
            same: Any = Segment(Point(1, 2), Point(3, 4))
            tail_same: Any = Segment(Point(5, 6), Point(7, 8))
            table = {
                left: 10,
                right: 20,
            }
            gc.collect()

            print(table[same])
            print(table[tail_same])
            print(type(left).__name__)
            print(type(right).__name__)
        """,
        backend="self",
    )
    out = _run_probe(exe)
    assert out == [
        "10",
        "20",
        "Segment",
        "Segment",
    ]


def test_valueclass_runtime_nested_valuebox_dataclasses_replace_keyword_self_backend(
    tmp_path,
):
    exe = _write_and_build_probe(
        tmp_path,
        """
        from dataclasses import dataclass, replace
        from typing import Any

        import gc
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

        if __name__ == "__main__":
            base = Holder(None, "base")
            updated = replace(base, item=Segment(Point(1, 2), Point(3, 4)))
            same: Any = Segment(Point(1, 2), Point(3, 4))
            table = {
                updated.item: 10,
            }
            gc.collect()

            print(table[same])
            print(type(updated.item).__name__)
            print(updated.label)
        """,
        backend="self",
    )
    out = _run_probe(exe)
    assert out == [
        "10",
        "Segment",
        "base",
    ]


def test_valueclass_runtime_nested_valuebox_membership_needle_self_backend(
    tmp_path,
):
    exe = _write_and_build_probe(
        tmp_path,
        """
        from typing import Any

        import gc
        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        if __name__ == "__main__":
            same: Any = Segment(Point(1, 2), Point(3, 4))
            items = [same]
            table = {same: 10}
            tuple_items = (same,)
            gc.collect()

            print(Segment(Point(1, 2), Point(3, 4)) in items)
            print(Segment(Point(1, 2), Point(3, 5)) not in items)
            print(Segment(Point(1, 2), Point(3, 4)) in table)
            print(Segment(Point(1, 2), Point(3, 4)) in tuple_items)
            print(table[Segment(Point(1, 2), Point(3, 4))])
            print(type(same).__name__)
        """,
        backend="self",
    )
    out = _run_probe(exe)
    assert out == [
        "True",
        "True",
        "True",
        "True",
        "10",
        "Segment",
    ]


def test_valueclass_runtime_nested_valuebox_builtin_object_boundary_self_backend(
    tmp_path,
):
    exe = _write_and_build_probe(
        tmp_path,
        """
        from typing import Any

        import gc
        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        if __name__ == "__main__":
            same: Any = Segment(Point(1, 2), Point(3, 4))
            table = {same: 10}
            gc.collect()

            print(hash(Segment(Point(1, 2), Point(3, 4))) == hash(same))
            print(table[Segment(Point(1, 2), Point(3, 4))])
            print(repr(Segment(Point(1, 2), Point(3, 4))))
            print(str(Segment(Point(1, 2), Point(3, 4))))
            print(format(Segment(Point(1, 2), Point(3, 4)), ""))
            print(type(Segment(Point(1, 2), Point(3, 4))).__name__)
        """,
        backend="self",
    )
    out = _run_probe(exe)
    assert out == [
        "True",
        "10",
        "Segment(start=Point(x=1, y=2), end=Point(x=3, y=4))",
        "Segment(start=Point(x=1, y=2), end=Point(x=3, y=4))",
        "Segment(start=Point(x=1, y=2), end=Point(x=3, y=4))",
        "Segment",
    ]


def test_valueclass_runtime_nested_valuebox_subscript_store_key_projection_self_backend(
    tmp_path,
):
    exe = _write_and_build_probe(
        tmp_path,
        """
        from typing import Any

        import gc
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

        if __name__ == "__main__":
            table = {}
            table[Segment(Point(1, 2), Point(3, 4))] = 10
            put(table)
            gc.collect()

            print(table[Segment(Point(1, 2), Point(3, 4))])
            print(table[Segment(Point(5, 6), Point(7, 8))])
            print(Segment(Point(1, 2), Point(3, 4)) in table)
            print(len(table))
            same: Any = Segment(Point(1, 2), Point(3, 4))
            table[same] = 11
            print(table[Segment(Point(1, 2), Point(3, 4))])
            print(len(table))
        """,
        backend="self",
    )
    out = _run_probe(exe)
    assert out == [
        "10",
        "20",
        "True",
        "2",
        "11",
        "2",
    ]


def test_valueclass_runtime_nested_valuebox_unpack_subscript_store_key_projection_self_backend(
    tmp_path,
):
    exe = _write_and_build_probe(
        tmp_path,
        """
        import gc
        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        if __name__ == "__main__":
            table = {}
            table[Segment(Point(1, 2), Point(3, 4))], extra = 30, 1
            gc.collect()

            print(table[Segment(Point(1, 2), Point(3, 4))])
            print(extra)
            print(Segment(Point(1, 2), Point(3, 4)) in table)
            print(len(table))
        """,
        backend="self",
    )
    out = _run_probe(exe)
    assert out == [
        "30",
        "1",
        "True",
        "1",
    ]


def test_valueclass_runtime_nested_valuebox_aug_subscript_key_projection_self_backend(
    tmp_path,
):
    exe = _write_and_build_probe(
        tmp_path,
        """
        import gc
        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        if __name__ == "__main__":
            table = {}
            table[Segment(Point(1, 2), Point(3, 4))] = 10
            table[Segment(Point(1, 2), Point(3, 4))] += 5
            gc.collect()

            print(table[Segment(Point(1, 2), Point(3, 4))])
            print(Segment(Point(1, 2), Point(3, 4)) in table)
            print(len(table))
        """,
        backend="self",
    )
    out = _run_probe(exe)
    assert out == [
        "15",
        "True",
        "1",
    ]


def test_valueclass_runtime_nested_valuebox_dict_literal_key_projection_self_backend(
    tmp_path,
):
    # Coverage lock for the already-green dict-literal constructor-key path.
    exe = _write_and_build_probe(
        tmp_path,
        """
        import gc
        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        if __name__ == "__main__":
            table = {
                Segment(Point(1, 2), Point(3, 4)): 10,
                Segment(Point(5, 6), Point(7, 8)): 20,
            }
            gc.collect()

            print(table[Segment(Point(1, 2), Point(3, 4))])
            print(table[Segment(Point(5, 6), Point(7, 8))])
            print(Segment(Point(1, 2), Point(3, 4)) in table)
            print(len(table))
        """,
        backend="self",
    )
    out = _run_probe(exe)
    assert out == [
        "10",
        "20",
        "True",
        "2",
    ]


def test_valueclass_runtime_nested_valuebox_compare_operand_projection_self_backend(
    tmp_path,
):
    exe = _write_and_build_probe(
        tmp_path,
        """
        from typing import Any

        import gc
        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        if __name__ == "__main__":
            other: Any = 1
            same: Any = Segment(Point(1, 2), Point(3, 4))
            gc.collect()

            print(Segment(Point(1, 2), Point(3, 4)) == other)
            print(Segment(Point(1, 2), Point(3, 4)) != other)
            print(Segment(Point(1, 2), Point(3, 4)) == same)
            print(same != Segment(Point(1, 2), Point(3, 4)))
            print(Segment(Point(1, 2), Point(3, 5)) == same)
        """,
        backend="self",
    )
    out = _run_probe(exe)
    assert out == [
        "False",
        "True",
        "True",
        "False",
        "False",
    ]


def test_valueclass_runtime_nested_valuebox_exception_arg_projection_self_backend(
    tmp_path,
):
    exe = _write_and_build_probe(
        tmp_path,
        """
        import gc
        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        if __name__ == "__main__":
            gc.collect()
            try:
                raise ValueError(Segment(Point(1, 2), Point(3, 4)))
            except ValueError as e:
                print(e)
                print("caught")
        """,
        backend="self",
    )
    out = _run_probe(exe)
    assert out == [
        "Segment(start=Point(x=1, y=2), end=Point(x=3, y=4))",
        "caught",
    ]


def test_valueclass_runtime_constructor_condition_truthiness_self_backend(
    tmp_path,
):
    # Regression: ``if Segment(...):`` crashed codegen with
    # "Layer 1 cannot compute truthiness of ClassType".
    exe = _write_and_build_probe(
        tmp_path,
        """
        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        if __name__ == "__main__":
            if Segment(Point(1, 2), Point(3, 4)):
                print("truthy")
            s = Segment(Point(5, 6), Point(7, 8))
            if s:
                print("var-truthy")
        """,
        backend="self",
    )
    out = _run_probe(exe)
    assert out == ["truthy", "var-truthy"]


def test_valueclass_runtime_percent_format_operand_projection_self_backend(
    tmp_path,
):
    exe = _write_and_build_probe(
        tmp_path,
        """
        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        if __name__ == "__main__":
            msg = "v=%s" % Segment(Point(1, 2), Point(3, 4))
            print(msg)
            print("v=%s" % Segment(Point(5, 6), Point(7, 8)))
        """,
        backend="self",
    )
    out = _run_probe(exe)
    assert out == [
        "v=Segment(start=Point(x=1, y=2), end=Point(x=3, y=4))",
        "v=Segment(start=Point(x=5, y=6), end=Point(x=7, y=8))",
    ]


def test_valueclass_runtime_walrus_target_payload_self_backend(
    tmp_path,
):
    exe = _write_and_build_probe(
        tmp_path,
        """
        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        if __name__ == "__main__":
            if (s := Segment(Point(1, 2), Point(3, 4))):
                print(s.start.x + s.end.y)
                print(type(s).__name__)
        """,
        backend="self",
    )
    out = _run_probe(exe)
    assert out == ["5", "Segment"]


def test_valueclass_runtime_nested_valuebox_conditional_expr_projection_self_backend(
    tmp_path,
):
    exe = _write_and_build_probe(
        tmp_path,
        """
        from typing import Any

        import gc
        import pcc

        @pcc.valueclass
        class Point:
            x: int
            y: int

        @pcc.valueclass
        class Segment:
            start: Point
            end: Point

        def total(s: Segment) -> int:
            return s.start.x + s.start.y + s.end.x + s.end.y

        if __name__ == "__main__":
            flag = True
            left: Any = Segment(Point(1, 2), Point(3, 4)) if flag else Segment(Point(0, 0), Point(0, 0))
            same: Any = Segment(Point(1, 2), Point(3, 4)) if flag else Segment(Point(0, 0), Point(0, 0))
            changed: Any = Segment(Point(1, 2), Point(3, 5)) if flag else Segment(Point(0, 0), Point(0, 0))
            table = {left: 42}
            gc.collect()
            print(left == same)
            print(left == changed)
            print(left != changed)
            print(table[same])
            print(changed in table)
            print(type(left).__name__)
            print(left)
            print(total(left))
            print(total(changed))
        """,
        backend="self",
    )
    out = _run_probe(exe)
    assert out == [
        "True",
        "False",
        "True",
        "42",
        "False",
        "Segment",
        "Segment(start=Point(x=1, y=2), end=Point(x=3, y=4))",
        "10",
        "11",
    ]
