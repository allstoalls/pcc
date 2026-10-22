from __future__ import annotations

import re
from pathlib import Path


def test_cross_module_valueclass_method_keeps_direct_aggregate_abi(tmp_path) -> None:
    from pcc.frontends.python.pipeline import compile_python_multi

    provider = tmp_path / "provider.py"
    consumer = tmp_path / "consumer.py"
    output = tmp_path / "pair.ll"
    provider.write_text(
        """
def valueclass(cls):
    return cls

@valueclass
class Quad:
    first: int
    second: int
    third: int
    fourth: int

class Source:
    def read(self) -> Quad:
        return Quad(1, 2, 3, 4)

class Arena:
    def get(self) -> Quad:
        return Quad(5, 6, 7, 8)
""".lstrip(),
        encoding="utf-8",
    )
    consumer.write_text(
        """
from provider import Arena, Quad, Source

class Holder:
    arena: Arena

    def read(self) -> Quad:
        return self.arena.get()

def pick(source: Source) -> int:
    return source.read().fourth
""".lstrip(),
        encoding="utf-8",
    )

    compile_python_multi(
        [str(consumer), str(provider)],
        str(output),
        entry_module="consumer",
        module_names=["consumer", "provider"],
        emit_llvm_only=True,
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
        recursive_stdlib=False,
        target_triple="arm64-apple-darwin23.6.0",
    )
    ir_text = output.read_text(encoding="utf-8")
    aggregate = r"\{ i64, i64, i64, i64 \}"
    assert re.search(
        rf"define external {aggregate} @user_provider_Source_read\(",
        ir_text,
    )
    pick_start = ir_text.index("@user_consumer_pick")
    pick_end = ir_text.index("\n}", pick_start)
    pick_ir = ir_text[pick_start:pick_end]
    assert re.search(
        rf"call {aggregate} \(ptr\) @user_provider_Source_read\(",
        pick_ir,
    ), pick_ir
    assert "@py_obj_call" not in pick_ir
    assert "@py_valuebox_new" not in pick_ir
    holder_match = re.search(
        rf"define external {aggregate} @user_consumer_Holder_read\(",
        ir_text,
    )
    assert holder_match, ir_text
    holder_start = holder_match.start()
    holder_end = ir_text.index("\n}", holder_start)
    holder_ir = ir_text[holder_start:holder_end]
    assert re.search(
        rf"call {aggregate} \(ptr\) @user_provider_Arena_get\(",
        holder_ir,
    ), holder_ir
    assert "@py_obj_call" not in holder_ir
    assert "@py_valuebox_new" not in holder_ir

    from pcc.backend.self_backend_dispatch import emit_self_asm

    assembly = emit_self_asm(ir_text)
    assert "_user_provider_Source_read:" in assembly
    assert "_user_consumer_Holder_read:" in assembly


def test_imported_valueclass_return_annotation_keeps_aggregate_abi(
    tmp_path,
) -> None:
    from pcc.frontends.python.pipeline import compile_python_multi

    records = tmp_path / "records.py"
    provider = tmp_path / "provider.py"
    consumer = tmp_path / "consumer.py"
    output = tmp_path / "triple.ll"
    records.write_text(
        """
def valueclass(cls):
    return cls

@valueclass
class Quad:
    first: int
    second: int
    third: int
    fourth: int
""".lstrip(),
        encoding="utf-8",
    )
    provider.write_text(
        """
from records import Quad

class Source:
    def read(self) -> Quad:
        return Quad(1, 2, 3, 4)
""".lstrip(),
        encoding="utf-8",
    )
    consumer.write_text(
        """
from provider import Source

def pick(source: Source) -> int:
    return source.read().fourth
""".lstrip(),
        encoding="utf-8",
    )

    compile_python_multi(
        [str(consumer), str(provider), str(records)],
        str(output),
        entry_module="consumer",
        module_names=["consumer", "provider", "records"],
        emit_llvm_only=True,
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
        recursive_stdlib=False,
        target_triple="arm64-apple-darwin23.6.0",
    )
    ir_text = output.read_text(encoding="utf-8")
    aggregate = r"\{ i64, i64, i64, i64 \}"
    assert re.search(
        rf"define external {aggregate} @user_provider_Source_read\(",
        ir_text,
    )
    pick_start = ir_text.index("@user_consumer_pick")
    pick_end = ir_text.index("\n}", pick_start)
    pick_ir = ir_text[pick_start:pick_end]
    assert re.search(
        rf"call {aggregate} \(ptr\) @user_provider_Source_read\(",
        pick_ir,
    ), pick_ir
    assert "@py_obj_call" not in pick_ir
    assert "@py_valuebox_new" not in pick_ir


def test_imported_valueclass_parameter_keeps_aggregate_abi(tmp_path) -> None:
    from pcc.frontends.python.pipeline import compile_python_multi

    records = tmp_path / "records.py"
    consumer = tmp_path / "consumer.py"
    output = tmp_path / "parameter.ll"
    records.write_text(
        """
from __future__ import annotations

def valueclass(cls):
    return cls

@valueclass
class Triple:
    first: int
    second: int
    third: int

    def get_unchecked(self, index: int) -> int:
        return self.first + index
""".lstrip(),
        encoding="utf-8",
    )
    consumer.write_text(
        """
from __future__ import annotations

from pcc.backend.records import Triple as Span

def read_span(span: Span, index: int) -> int:
    return span.get_unchecked(index)

def use_span() -> int:
    return read_span(Span(1, 2, 3), 4)
""".lstrip(),
        encoding="utf-8",
    )

    compile_python_multi(
        [str(consumer), str(records)],
        str(output),
        entry_module="pcc.backend.consumer",
        module_names=["pcc.backend.consumer", "pcc.backend.records"],
        emit_llvm_only=True,
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
        recursive_stdlib=False,
        target_triple="arm64-apple-darwin23.6.0",
    )
    ir_text = output.read_text(encoding="utf-8")
    aggregate = r"\{ i64, i64, i64 \}"
    # A compiler-package name is not a range proof for ordinary Python int.
    # Keep the valueclass payload ABI while independently preserving the
    # free function's boxed integer parameter and result.
    assert re.search(
        rf"define external ptr @user_pcc_backend_consumer_read_span\({aggregate} %span, ptr %index\)",
        ir_text,
    ), ir_text
    read_start = ir_text.index("@user_pcc_backend_consumer_read_span")
    read_end = ir_text.index("\n}", read_start)
    read_ir = ir_text[read_start:read_end]
    assert re.search(
        rf"call i64 \({aggregate}, i64\) "
        rf"@user_pcc_backend_records_Triple_get_unchecked\({aggregate}",
        read_ir,
    ), read_ir
    use_start = ir_text.index("@user_pcc_backend_consumer_use_span")
    use_end = ir_text.index("\n}", use_start)
    use_ir = ir_text[use_start:use_end]
    assert re.search(
        rf"call ptr \({aggregate}, ptr\) @user_pcc_backend_consumer_read_span\({aggregate}",
        use_ir,
    ), use_ir
    assert "@py_valuebox_new" not in use_ir
    assert "@py_instance_new" not in use_ir
    assert "@py_valuebox_get_field" not in use_ir

    from pcc.backend.self_backend_dispatch import emit_self_asm

    assembly = emit_self_asm(ir_text)
    assert "_user_pcc_backend_consumer_read_span:" in assembly


def test_relative_imported_valueclass_return_keeps_aggregate_abi(
    tmp_path,
) -> None:
    from pcc.frontends.python.pipeline import compile_python_multi

    package = tmp_path / "pkg"
    package.mkdir()
    records = package / "records.py"
    provider = package / "provider.py"
    consumer = package / "consumer.py"
    output = tmp_path / "relative.ll"
    records.write_text(
        """
def valueclass(cls):
    return cls

@valueclass
class Quad:
    first: int
    second: int
    third: int
    fourth: int
""".lstrip(),
        encoding="utf-8",
    )
    provider.write_text(
        """
from .records import Quad

class Source:
    def read(self) -> Quad:
        return Quad(1, 2, 3, 4)
""".lstrip(),
        encoding="utf-8",
    )
    consumer.write_text(
        """
from .provider import Source

def pick(source: Source) -> int:
    return source.read().fourth
""".lstrip(),
        encoding="utf-8",
    )

    compile_python_multi(
        [str(consumer), str(provider), str(records)],
        str(output),
        entry_module="pkg.consumer",
        module_names=["pkg.consumer", "pkg.provider", "pkg.records"],
        emit_llvm_only=True,
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
        recursive_stdlib=False,
        target_triple="arm64-apple-darwin23.6.0",
    )
    ir_text = output.read_text(encoding="utf-8")
    aggregate = r"\{ i64, i64, i64, i64 \}"
    assert re.search(
        rf"define external {aggregate} @user_pkg_provider_Source_read\(",
        ir_text,
    )
    pick_start = ir_text.index("@user_pkg_consumer_pick")
    pick_end = ir_text.index("\n}", pick_start)
    pick_ir = ir_text[pick_start:pick_end]
    assert re.search(
        rf"call {aggregate} \(ptr\) @user_pkg_provider_Source_read\(",
        pick_ir,
    ), pick_ir
    assert "@py_obj_call" not in pick_ir
    assert "@py_valuebox_new" not in pick_ir


def test_valueclass_attribute_keeps_projection_in_class_constructor_argument(
    tmp_path,
) -> None:
    from pcc.frontends.python.pipeline import compile_python_multi

    package = tmp_path / "pkg"
    package.mkdir()
    consumer = package / "consumer.py"
    output = tmp_path / "constructor_arg.ll"
    repo = Path(__file__).resolve().parents[2]
    value_arena = repo / "pcc/backend/self_backend_value_arena.py"
    unsafe = repo / "pcc/unsafe/__init__.py"
    consumer.write_text(
        """
from __future__ import annotations

from dataclasses import dataclass
from pcc.backend.self_backend_value_arena import (
    CompilerInt4,
    CompilerIntArena,
)

class Holder:
    __slots__ = ("states",)
    states: CompilerIntArena

    def __init__(self, states: CompilerIntArena) -> None:
        self.states = states

@dataclass(frozen=True)
class Reload:
    source_offset: int
    destination_offset: int
    derived_offset: int = 0

def build_reload(holder: Holder, target: str) -> Reload:
    value_id = 0
    while value_id < 2:
        origin: CompilerInt4 = holder.states.get4_unchecked(value_id)
        value_id += 1
        if origin.first == -1:
            raise RuntimeError("ambiguous")
        if origin.first != 4:
            continue
        if target == "x86" and not (
            -(1 << 31) <= origin.third < (1 << 31)
        ):
            raise RuntimeError("offset")
        destination = -8
        return Reload(origin.second, destination, origin.third)
    return Reload(0, 0, 0)
""".lstrip(),
        encoding="utf-8",
    )

    compile_python_multi(
        [str(consumer), str(value_arena), str(unsafe)],
        str(output),
        entry_module="pcc.backend.repro_consumer",
        module_names=[
            "pcc.backend.repro_consumer",
            "pcc.backend.self_backend_value_arena",
            "pcc.unsafe",
        ],
        emit_llvm_only=True,
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
        recursive_stdlib=False,
        target_triple="arm64-apple-darwin23.6.0",
    )
    ir_text = output.read_text(encoding="utf-8")

    from pcc.backend.self_backend_dispatch import emit_self_asm

    assembly = emit_self_asm(ir_text)
    assert "_user_pcc_backend_repro_consumer_build_reload:" in assembly
