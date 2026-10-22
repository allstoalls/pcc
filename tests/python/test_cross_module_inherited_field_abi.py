from __future__ import annotations

import re
import subprocess

import pytest


def test_export_field_order_matches_nested_method_control_flow(tmp_path):
    from pcc.frontends.python.pipeline_context import build_closed_world_context

    provider = tmp_path / "nested_fields.py"
    provider.write_text("""
class Example:
    def setter(self):
        self.before = 0

    def __init__(self, flag: bool):
        self.first = 1
        if flag:
            self.if_field = 2
        else:
            self.else_field = 3
        for item in ():
            self.for_field = 4
        else:
            self.for_else = 5
        while False:
            self.while_field = 6
        else:
            self.while_else = 7
        with None:
            self.with_field = 8
        try:
            self.try_field = 9
        except Exception:
            self.handler_field = 10
        else:
            self.try_else = 11
        finally:
            self.finally_field = 12
        self.last = 13
""".lstrip())
    _, exports, _ = build_closed_world_context([str(provider)], ["nested_fields"])
    assert exports["nested_fields"]["Example"]["field_names"] == (
        "before", "first", "if_field", "else_field", "for_field", "for_else",
        "while_field", "while_else", "with_field", "try_field", "handler_field",
        "try_else", "finally_field", "last",
    )


def test_imported_subclass_field_index_includes_imported_base_fields(
    tmp_path,
) -> None:
    from pcc.frontends.python.pipeline import compile_python_multi

    records = tmp_path / "records.py"
    base = tmp_path / "base.py"
    provider = tmp_path / "provider.py"
    consumer = tmp_path / "consumer.py"
    output = tmp_path / "inherited_field.ll"

    records.write_text(
        """
class Arena:
    __slots__ = ("length",)
    length: int

    def __init__(self) -> None:
        self.length = 0

    def append4(self, a: int, b: int, c: int, d: int) -> None:
        self.length += 4
""".lstrip(),
        encoding="utf-8",
    )
    base.write_text(
        """
class Base:
    __slots__ = ("base0", "base1", "base2", "base3", "base4", "base5", "base6")
    base0: int
    base1: int
    base2: int
    base3: int
    base4: int
    base5: int
    base6: int

    def __init__(self) -> None:
        self.base0 = 0
        self.base1 = 1
        self.base2 = 2
        self.base3 = 3
        self.base4 = 4
        self.base5 = 5
        self.base6 = 6
""".lstrip(),
        encoding="utf-8",
    )
    provider.write_text(
        """
from base import Base
from records import Arena

class Seed(Base):
    __slots__ = (
        "field0", "field1", "field2", "field3", "field4",
        "field5", "field6", "field7", "field8", "field9",
        "field10", "field11", "field12", "field13", "arena",
    )
    field0: int
    field1: int
    field2: int
    field3: int
    field4: int
    field5: int
    field6: int
    field7: int
    field8: int
    field9: int
    field10: int
    field11: int
    field12: int
    field13: int
    arena: Arena

    def __init__(self) -> None:
        Base.__init__(self)
        self.field0 = 0
        self.field1 = 1
        self.field2 = 2
        self.field3 = 3
        self.field4 = 4
        self.field5 = 5
        self.field6 = 6
        self.field7 = 7
        self.field8 = 8
        self.field9 = 9
        self.field10 = 10
        self.field11 = 11
        self.field12 = 12
        self.field13 = 13
        self.arena = Arena()
""".lstrip(),
        encoding="utf-8",
    )
    consumer.write_text(
        """
from provider import Seed

def touch(seed: Seed) -> None:
    seed.arena.append4(1, 2, 3, 4)

def read_base(seed: Seed) -> int:
    return seed.base6
""".lstrip(),
        encoding="utf-8",
    )

    from pcc.frontends.python.pipeline_context import build_closed_world_context

    _parsed, exports, _derived = build_closed_world_context(
        [str(consumer), str(provider), str(base), str(records)],
        ["consumer", "provider", "base", "records"],
    )
    seed_fields = exports["provider"]["Seed"]["field_names"]
    assert seed_fields[:7] == tuple(f"base{index}" for index in range(7))
    assert seed_fields[21] == "arena"

    compile_python_multi(
        [str(consumer), str(provider), str(base), str(records)],
        str(output),
        entry_module="consumer",
        module_names=["consumer", "provider", "base", "records"],
        emit_llvm_only=True,
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
        recursive_stdlib=False,
        target_triple="arm64-apple-darwin23.6.0",
    )
    ir_text = output.read_text(encoding="utf-8")
    start = ir_text.index("define external void @user_consumer_touch")
    end = ir_text.index("\n}", start)
    body = ir_text[start:end]

    # ``arena`` is subclass-local field 14 after seven imported Base fields.
    # The runtime layout is flattened, so the exact index is 7 + 14 = 21.
    assert re.search(
        r"@py_instance_get_field\(ptr %[^,]+, i32 21\)",
        body,
    ), body
    assert "i32 14)" not in body

    base_symbol = ir_text.index("@user_consumer_read_base")
    base_start = ir_text.rfind("define external", 0, base_symbol)
    assert base_start >= 0
    base_end = ir_text.index("\n}", base_start)
    base_body = ir_text[base_start:base_end]
    assert re.search(
        r"@py_instance_get_field\(ptr %[^,]+, i32 6\)",
        base_body,
    ), base_body


@pytest.mark.integration
def test_cross_module_base_annotation_preserves_subclass_override(
    tmp_path,
) -> None:
    from pcc.frontends.python.pipeline import compile_python_multi

    owner = tmp_path / "owner.py"
    consumer = tmp_path / "consumer.py"
    output = tmp_path / "override.out"

    owner.write_text(
        """
class Base:
    def ping(self) -> int:
        return 1

class Child(Base):
    def ping(self) -> int:
        return 2
""".lstrip(),
        encoding="utf-8",
    )
    consumer.write_text(
        """
from owner import Base, Child

def dispatch(value: Base | None) -> int:
    if value is None:
        return 0
    return value.ping()

print(dispatch(Child()))
""".lstrip(),
        encoding="utf-8",
    )

    compile_python_multi(
        [str(consumer), str(owner)],
        str(output),
        entry_module="consumer",
        module_names=["consumer", "owner"],
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
        recursive_stdlib=False,
        target_triple="arm64-apple-darwin23.6.0",
    )
    result = subprocess.run(
        [str(output)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "2\n"


def test_pep604_optional_class_annotation_keeps_object_projection() -> None:
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.py_ast import ClassType, DynType, FuncDef

    module = parse_and_lift(
        """
class Arena:
    pass

class Other:
    pass

def optional_arena(value: Arena | None) -> None:
    pass

def optional_int(value: int | None) -> None:
    pass

def arbitrary_union(value: Arena | Other) -> None:
    pass
""".lstrip(),
        "optional_annotation.py",
        "optional_annotation",
    )
    annotations = {
        stmt.name: stmt.args[0].annotation
        for stmt in module.body
        if isinstance(stmt, FuncDef)
    }
    assert isinstance(annotations["optional_arena"], ClassType)
    assert annotations["optional_arena"].name == "Arena"
    assert isinstance(annotations["optional_int"], DynType)
    assert isinstance(annotations["arbitrary_union"], DynType)


def test_pep604_optional_exact_receiver_uses_direct_method_abi(tmp_path) -> None:
    from pcc.frontends.python.pipeline import compile_python_multi

    owner = tmp_path / "owner.py"
    consumer = tmp_path / "consumer.py"
    output = tmp_path / "optional_receiver.ll"
    owner.write_text(
        """
class Arena:
    __slots__ = ("length",)
    length: int

    def __init__(self) -> None:
        self.length = 0

    def append4(self, a: int, b: int, c: int, d: int) -> None:
        self.length += 4
""".lstrip(),
        encoding="utf-8",
    )
    consumer.write_text(
        """
from owner import Arena

def touch(arena: Arena | None) -> int:
    if arena is None:
        return 0
    arena.append4(1, 2, 3, 4)
    return arena.length

def touch_nested(arena: Arena | None) -> int:
    def append_one() -> None:
        if arena is not None:
            typed_arena: Arena = arena
            typed_arena.append4(1, 2, 3, 4)

    append_one()
    return 0 if arena is None else arena.length
""".lstrip(),
        encoding="utf-8",
    )

    compile_python_multi(
        [str(consumer), str(owner)],
        str(output),
        entry_module="consumer",
        module_names=["consumer", "owner"],
        emit_llvm_only=True,
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
        recursive_stdlib=False,
        target_triple="arm64-apple-darwin23.6.0",
    )
    ir_text = output.read_text(encoding="utf-8")
    start = ir_text.index("@user_consumer_touch")
    end = ir_text.index("\n}", start)
    body = ir_text[start:end]
    assert "@user_owner_Arena_append4" in body, body
    assert "@py_obj_getattr" not in body, body
    nested_start = ir_text.index(
        "define external void @user_consumer___nested_append_one"
    )
    nested_end = ir_text.index("\n}", nested_start)
    nested_body = ir_text[nested_start:nested_end]
    assert "@user_owner_Arena_append4" in nested_body, nested_body
    assert "@py_obj_getattr" not in nested_body, nested_body


def test_transitive_inherited_raw_arena_control(tmp_path) -> None:
    from pcc.frontends.python.pipeline import compile_python_multi

    base = tmp_path / "base.py"
    records = tmp_path / "records.py"
    provider = tmp_path / "provider.py"
    consumer = tmp_path / "consumer.py"
    output = tmp_path / "raw_arena.out"

    base_names = [f"base{index}" for index in range(7)]
    base.write_text(
        "class Base:\n"
        + "    __slots__ = ("
        + ", ".join(repr(name) for name in base_names)
        + ",)\n"
        + "".join(f"    {name}: int\n" for name in base_names)
        + "\n    def __init__(self) -> None:\n"
        + "".join(
            f"        self.{name} = {index}\n"
            for index, name in enumerate(base_names)
        ),
        encoding="utf-8",
    )
    records.write_text(
        """
from pcc.unsafe import int_to_ptr, malloc, ptr_to_int, store_i64

class Arena:
    __slots__ = ("address", "length", "capacity")
    address: int
    length: int
    capacity: int

    def __init__(self) -> None:
        self.address = ptr_to_int(malloc(64))
        self.length = 0
        self.capacity = 8

    def append4(self, a: int, b: int, c: int, d: int) -> None:
        address = int_to_ptr(self.address)
        store_i64(address, self.length * 8, a)
        store_i64(address, (self.length + 1) * 8, b)
        store_i64(address, (self.length + 2) * 8, c)
        store_i64(address, (self.length + 3) * 8, d)
        self.length += 4
""".lstrip(),
        encoding="utf-8",
    )
    local_names = [f"local{index}" for index in range(14)]
    local_names += ["arena"]
    local_names += [f"local{index}" for index in range(15, 20)]
    provider.write_text(
        "from base import Base\nfrom records import Arena\n\n"
        + "class Seed(Base):\n"
        + "    __slots__ = ("
        + ", ".join(repr(name) for name in local_names)
        + ",)\n"
        + "".join(
            f"    {name}: {'Arena' if name == 'arena' else 'int'}\n"
            for name in local_names
        )
        + "\n    def __init__(self) -> None:\n"
        + "        Base.__init__(self)\n"
        + "".join(
            (
                "        self.arena = Arena()\n"
                if name == "arena"
                else f"        self.{name} = {index}\n"
            )
            for index, name in enumerate(local_names)
        ),
        encoding="utf-8",
    )
    consumer.write_text(
        """
from provider import Seed

def touch(seed: Seed) -> int:
    seed.arena.append4(1, 2, 3, 4)
    return seed.arena.length

print(touch(Seed()))
""".lstrip(),
        encoding="utf-8",
    )

    compile_python_multi(
        [str(consumer), str(provider), str(base), str(records)],
        str(output),
        entry_module="consumer",
        module_names=["consumer", "provider", "base", "records"],
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
        recursive_stdlib=False,
        target_triple="arm64-apple-darwin23.6.0",
    )
    result = subprocess.run(
        [str(output)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "4\n"
