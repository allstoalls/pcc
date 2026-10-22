"""Precise stack-map tables follow the target's publication policy.

Darwin publishes tables by default; ELF and PE retain registered slots by
default. ``PCC_PRECISE_STACKMAP_TABLES`` 0/1 overrides either default.
Safepoint planning runs either way, so table emission preserves code.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from pcc.backend import macho_spec
from pcc.backend.precise_stackmap import (
    MAGIC,
    PRECISE_STACKMAP_TABLE_SYMBOL,
    PRECISE_STACKMAP_TABLES_ENV,
    PreciseStackMapError,
    precise_stackmap_tables_enabled,
)
from pcc.backend.self_backend_dispatch import emit_self_asm
from pcc.frontends.python import pipeline
from pcc.frontends.python import pipeline_targets
from pcc.frontends.python.pipeline_self_backend_cache import (
    _effective_backend_configuration,
)


_FORMAT_ENV = "PCC_PRECISE_STACKMAP_FORMAT"
_TARGET_POLICIES = (
    ("arm64-apple-darwin23.6.0", True),
    ("x86_64-apple-darwin", True),
    ("aarch64-unknown-linux-gnu", False),
    ("x86_64-unknown-linux-gnu", False),
    ("x86_64-pc-windows-msvc", False),
)


def _assert_linked_table_format(output, table_format):
    # The final link writes version 2, or the compact version 3 runtime table
    # when PCC_PRECISE_STACKMAP_FORMAT=3 selects it.
    image = output.read_bytes()
    for section in macho_spec.parse_object(image).sections():
        if (section["segname_str"], section["sectname_str"]) == ("__DATA", "__pcc_stackmaps"):
            table = image[section["offset"]:section["offset"] + section["size"]]
            assert table[:8] == MAGIC
            assert int.from_bytes(table[8:10], "little") == int(table_format)
            return
    raise AssertionError("executable has no stack-map table")


_PROGRAM = '''
class Box:
    def __init__(self, value):
        self.value = value


def build(count):
    boxes = []
    for index in range(count):
        boxes.append(Box([index, str(index)]))
    return boxes


def main() -> None:
    boxes = build(2000)
    total = 0
    for box in boxes:
        total += box.value[0]
    print(total, boxes[-1].value[1], len(boxes))


main()
'''


@pytest.mark.parametrize("triple,default_enabled", _TARGET_POLICIES)
def test_policy_accepts_only_explicit_values(monkeypatch, triple, default_enabled):
    monkeypatch.delenv(PRECISE_STACKMAP_TABLES_ENV, raising=False)
    assert precise_stackmap_tables_enabled(triple) is default_enabled
    monkeypatch.setenv(PRECISE_STACKMAP_TABLES_ENV, "0")
    assert precise_stackmap_tables_enabled(triple) is False
    monkeypatch.setenv(PRECISE_STACKMAP_TABLES_ENV, "1")
    assert precise_stackmap_tables_enabled(triple) is True
    monkeypatch.setenv(PRECISE_STACKMAP_TABLES_ENV, "yes")
    with pytest.raises(PreciseStackMapError):
        precise_stackmap_tables_enabled(triple)


@pytest.mark.parametrize("triple,default_enabled", _TARGET_POLICIES)
@pytest.mark.parametrize("target", [None, "", "unknown"])
def test_unspecified_target_uses_the_host_policy(
    monkeypatch, triple, default_enabled, target,
):
    monkeypatch.setattr(pipeline_targets, "host_target_triple", lambda: triple)
    monkeypatch.delenv(PRECISE_STACKMAP_TABLES_ENV, raising=False)
    assert precise_stackmap_tables_enabled(target) is default_enabled


@pytest.mark.parametrize("triple,default_enabled", _TARGET_POLICIES)
def test_object_cache_identity_binds_the_policy(monkeypatch, triple, default_enabled):
    monkeypatch.setattr(pipeline_targets, "host_target_triple", lambda: triple)
    monkeypatch.delenv(PRECISE_STACKMAP_TABLES_ENV, raising=False)
    default = _effective_backend_configuration()
    monkeypatch.setenv(PRECISE_STACKMAP_TABLES_ENV, "1")
    enabled = _effective_backend_configuration()
    monkeypatch.setenv(PRECISE_STACKMAP_TABLES_ENV, "0")
    disabled = _effective_backend_configuration()
    assert enabled != disabled
    assert default == (enabled if default_enabled else disabled)


@pytest.mark.parametrize("triple,default_enabled", _TARGET_POLICIES)
def test_runtime_test_cache_binds_the_policy(monkeypatch, triple, default_enabled):
    # The default shares a cache entry only with the matching explicit policy.
    from pathlib import Path
    from tests.runtime_build_cache import _pcc_runtime_cache_key

    pcc_bin = Path(sys.executable)
    monkeypatch.setattr(pipeline_targets, "host_target_triple", lambda: triple)
    monkeypatch.delenv(PRECISE_STACKMAP_TABLES_ENV, raising=False)
    default = _pcc_runtime_cache_key(pcc_bin, variant="pcc-py")
    monkeypatch.setenv(PRECISE_STACKMAP_TABLES_ENV, "1")
    enabled = _pcc_runtime_cache_key(pcc_bin, variant="pcc-py")
    monkeypatch.setenv(PRECISE_STACKMAP_TABLES_ENV, "0")
    disabled = _pcc_runtime_cache_key(pcc_bin, variant="pcc-py")
    assert enabled != disabled
    assert default == (enabled if default_enabled else disabled)


@pytest.mark.parametrize("triple,directive,default_enabled", [
    ("aarch64-apple-darwin", "__pcc_stackmaps", True),
    ("aarch64-unknown-linux-gnu", "__pcc_stackmaps", False),
    ("x86_64-unknown-linux-gnu", ".pcc_stackmaps", False),
])
def test_emitted_assembly_carries_the_table_according_to_target_policy(
    tmp_path, monkeypatch, triple, directive, default_enabled,
):
    source = tmp_path / "boxes.py"
    source.write_text(_PROGRAM, encoding="utf-8")
    ir = tmp_path / "boxes.ll"
    pipeline.compile_python(
        str(source), str(ir), emit_llvm_only=True, libpython_mode="off",
        target_triple=triple,
    )
    text = ir.read_text(encoding="utf-8")
    monkeypatch.delenv(PRECISE_STACKMAP_TABLES_ENV, raising=False)
    default = emit_self_asm(text, triple)
    monkeypatch.setenv(PRECISE_STACKMAP_TABLES_ENV, "1")
    requested = emit_self_asm(text, triple)
    monkeypatch.setenv(PRECISE_STACKMAP_TABLES_ENV, "0")
    disabled = emit_self_asm(text, triple)
    assert (directive in default) is default_enabled
    assert directive in requested
    assert directive not in disabled
    # Only the metadata section differs: the code is identical.
    assert _without_section(requested, directive) == disabled
    assert default == (requested if default_enabled else disabled)


_TABLE_DATA_DIRECTIVES = (".byte", ".short", ".long", ".quad", ".p2align")


def _without_section(text, directive):
    kept = []
    skipping = False
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith(".section"):
            skipping = directive in line
        elif skipping and stripped.startswith(".") and not stripped.startswith(_TABLE_DATA_DIRECTIVES):
            skipping = False
        if not skipping:
            kept.append(line)
    return "\n".join(kept)


def test_default_executable_has_no_table_and_matches_cpython_on_five_collectors(
    tmp_path, monkeypatch, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "boxes.py"
    source.write_text(_PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    outputs = {}
    for setting in ("", "1"):
        if setting:
            monkeypatch.setenv(PRECISE_STACKMAP_TABLES_ENV, setting)
        else:
            monkeypatch.delenv(PRECISE_STACKMAP_TABLES_ENV, raising=False)
        output = tmp_path / ("boxes" + setting)
        python_program_compiler(
            str(source), str(output), backend="self", libpython_mode="off",
            runtime_archive=str(pcc_runtime_archive),
        )
        outputs[setting] = output
        for gc in range(5):
            ran = subprocess.run(
                [str(output)], capture_output=True, text=True, timeout=60,
                env=dict(os.environ, PCC_GC_BACKEND=str(gc)),
            )
            assert ran.returncode == 0, f"{setting!r} GC{gc}: {ran.stdout}{ran.stderr}"
            assert ran.stdout == expected.stdout, f"{setting!r} GC{gc}: {ran.stdout}"
    default_bytes = outputs[""].read_bytes()
    requested_bytes = outputs["1"].read_bytes()
    assert MAGIC not in default_bytes
    assert MAGIC in requested_bytes
    assert len(default_bytes) < len(requested_bytes)


@pytest.mark.pcc_gate(probe=lambda: sys.platform == "darwin")
def test_macho_executable_names_its_table_for_the_runtime(
    tmp_path, monkeypatch, pcc_runtime_archive, python_program_compiler,
):
    # The GC runtime reaches the linked table through this one symbol; only
    # the final executable link defines it, at the merged section's start.
    source = tmp_path / "boxes.py"
    source.write_text(_PROGRAM, encoding="utf-8")
    found = {}
    for setting in ("", "1"):
        if setting:
            monkeypatch.setenv(PRECISE_STACKMAP_TABLES_ENV, setting)
        else:
            monkeypatch.delenv(PRECISE_STACKMAP_TABLES_ENV, raising=False)
        output = tmp_path / ("boxes" + setting)
        python_program_compiler(
            str(source), str(output), backend="self", libpython_mode="off",
            runtime_archive=str(pcc_runtime_archive),
        )
        image = macho_spec.parse_object(output.read_bytes())
        sections = {
            (section["segname_str"], section["sectname_str"]): section
            for section in image.sections()
        }
        symbols = {symbol["name"]: symbol for symbol in image.symbols()}
        found[setting] = (
            sections.get(("__DATA", "__pcc_stackmaps")),
            symbols.get("_" + PRECISE_STACKMAP_TABLE_SYMBOL),
        )
    assert found[""] == (None, None)
    table, symbol = found["1"]
    assert table is not None and symbol is not None
    assert symbol["n_type"] & macho_spec.N_TYPE == macho_spec.N_SECT
    assert symbol["n_value"] == table["addr"]


_TABLE_PROBE = '''
from pcc.unsafe import global_load_ptr, load_i64, ptr_is_null


def main() -> None:
    table = global_load_ptr("pcc_gc_precise_stackmap_table")
    if ptr_is_null(table) != 0:
        print("no table")
    else:
        print("table", load_i64(table, 0))


main()
'''


@pytest.mark.pcc_gate(probe=lambda: sys.platform == "darwin")
def test_table_enabled_main_publishes_the_table_to_the_runtime(
    tmp_path, monkeypatch, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "probe.py"
    source.write_text(_TABLE_PROBE, encoding="utf-8")
    expected = {"": "no table\n", "1": f"table {int.from_bytes(MAGIC, 'little')}\n"}
    for setting in ("", "1"):
        if setting:
            monkeypatch.setenv(PRECISE_STACKMAP_TABLES_ENV, setting)
        else:
            monkeypatch.delenv(PRECISE_STACKMAP_TABLES_ENV, raising=False)
        output = tmp_path / ("probe" + setting)
        python_program_compiler(
            str(source), str(output), backend="self", libpython_mode="off",
            runtime_archive=str(pcc_runtime_archive),
        )
        for gc in range(5):
            ran = subprocess.run(
                [str(output)], capture_output=True, text=True, timeout=60,
                env=dict(os.environ, PCC_GC_BACKEND=str(gc)),
            )
            assert ran.returncode == 0, f"{setting!r} GC{gc}: {ran.stdout}{ran.stderr}"
            assert ran.stdout == expected[setting], f"{setting!r} GC{gc}: {ran.stdout}"


_CALL_LINES = ("bl ", "blr ", "call ")
_RESULT_MOVE_LINES = ("mov ", "movz ", "fmov ", "str ", "stur ", "stp ", "movsxd ", "movzx ")


def _late_call_safepoint_labels(asm):
    """Labels that follow a call only after the call's result moves."""
    late = []
    lines = [line.strip() for line in asm.splitlines()]
    for index, line in enumerate(lines):
        if not (line.startswith(("L_pcc_smap_", ".Lpcc_smap_")) and line.endswith(":")):
            continue
        if "smap_end" in line:
            continue
        cursor = index - 1
        moved = False
        while cursor >= 0 and lines[cursor].startswith(_RESULT_MOVE_LINES):
            moved = True
            cursor -= 1
        if moved and cursor >= 0 and lines[cursor].startswith(_CALL_LINES):
            late.append(line)
    return late


@pytest.mark.parametrize("triple", [
    "aarch64-apple-darwin",
    "aarch64-unknown-linux-gnu",
    "x86_64-unknown-linux-gnu",
])
def test_call_safepoints_are_recorded_at_the_return_address(tmp_path, monkeypatch, triple):
    # A stack walk finds a frame's record by the return address it holds, so
    # a call safepoint's label must follow the call itself, not the result
    # moves after it.
    source = tmp_path / "boxes.py"
    source.write_text(_PROGRAM, encoding="utf-8")
    ir = tmp_path / "boxes.ll"
    pipeline.compile_python(
        str(source), str(ir), emit_llvm_only=True, libpython_mode="off",
        target_triple=triple,
    )
    monkeypatch.setenv(PRECISE_STACKMAP_TABLES_ENV, "1")
    asm = emit_self_asm(ir.read_text(encoding="utf-8"), triple)
    calls_with_labels = sum(
        1 for previous, line in zip(asm.splitlines(), asm.splitlines()[1:])
        if previous.strip().startswith(_CALL_LINES) and "smap_" in line and line.strip().endswith(":")
    )
    assert calls_with_labels > 0
    assert _late_call_safepoint_labels(asm) == []


_DEEP_COLLECT = '''
import gc


class Box:
    def __init__(self, value):
        self.value = value


def inner(depth, keep):
    box = Box([depth])
    if depth == 0:
        gc.collect()
        return len(keep) + box.value[0]
    return inner(depth - 1, keep + [box])


def main():
    print(inner(3, []))


main()
'''


@pytest.mark.pcc_gate(probe=lambda: sys.platform == "darwin")
@pytest.mark.parametrize("table_format", ["2", "3"])
def test_stack_walk_mapped_and_registered_roots_cover_each_other(
    tmp_path, monkeypatch, pcc_stackmap_runtime_archive, python_program_compiler,
    table_format,
):
    # Step 3 differential: at an explicit GC3/GC4 collection the runtime walks
    # the stopped stack through saved frame pointers, finds each frame's call
    # record by its return address, and checks that every managed object the
    # map names is also held by a registered root slot, and that every
    # registered root in the walked stack is named by the map.
    import re

    source = tmp_path / "deep.py"
    source.write_text(_DEEP_COLLECT, encoding="utf-8")
    monkeypatch.setenv(PRECISE_STACKMAP_TABLES_ENV, "1")
    monkeypatch.setenv(_FORMAT_ENV, table_format)
    output = tmp_path / "deep"
    python_program_compiler(
        str(source), str(output), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_stackmap_runtime_archive),
    )
    _assert_linked_table_format(output, table_format)
    report = re.compile(
        r"pcc-stackmap-diff frames=(\d+) mapped=(\d+) mismatches=(\d+) errors=(\d+)"
        r" registered=(\d+) unmapped=(\d+)"
    )
    for gc, requested in ((3, True), (4, True), (0, True), (3, False)):
        env = dict(os.environ, PCC_GC_BACKEND=str(gc))
        if requested:
            env["PCC_GC_STACKMAP_DIFF"] = "1"
        else:
            env.pop("PCC_GC_STACKMAP_DIFF", None)
        ran = subprocess.run([str(output)], capture_output=True, text=True, timeout=120, env=env)
        assert ran.returncode == 0, f"GC{gc}: {ran.stdout}{ran.stderr}"
        assert ran.stdout == "3\n", f"GC{gc}: {ran.stdout}"
        found = report.findall(ran.stderr)
        if gc in (3, 4) and requested:
            assert len(found) == 1, ran.stderr
            frames, mapped, mismatches, errors, registered, unmapped = (
                int(value) for value in found[0]
            )
            # Frames whose call site holds no root have no record (step 2).
            assert mapped >= 1 and frames >= mapped, ran.stderr
            assert registered > 0, ran.stderr
            assert (mismatches, errors, unmapped) == (0, 0, 0), ran.stderr
        else:
            assert found == [], ran.stderr


# Root shapes the registered-slot protocol handles specially: call-argument
# temporaries live across a collecting call, an exception bound by except,
# try/finally, generators, closures, with, comprehensions, *args/**kwargs and
# keyword-only arguments.
_ROOT_SHAPES = '''
import gc


class Box:
    def __init__(self, value):
        self.value = value

    def scaled(self, factor, *, offset=0):
        gc.collect()
        return self.value * factor + offset


def collect_and(value):
    gc.collect()
    return value


def pair(left, right):
    return left.value + right


def temporaries():
    return pair(Box(5), collect_and(7))


def in_except():
    try:
        raise ValueError(Box(11))
    except ValueError as error:
        gc.collect()
        return error.args[0].value


def in_finally():
    box = Box(13)
    try:
        return box.value
    finally:
        gc.collect()


def generator(count):
    held = Box(count)
    for index in range(count):
        gc.collect()
        yield held.value + index


def closure():
    held = Box(17)

    def inner():
        gc.collect()
        return held.value

    return inner()


class Manager:
    def __init__(self):
        self.box = Box(19)

    def __enter__(self):
        return self.box

    def __exit__(self, *args):
        gc.collect()
        return False


def with_statement():
    with Manager() as box:
        gc.collect()
        return box.value


def comprehension():
    return [collect_and(Box(index)).value for index in range(3)]


def varargs(*values, **named):
    gc.collect()
    return sum(values) + named["extra"].value


def main():
    print(temporaries())
    print(in_except())
    print(in_finally())
    print(list(generator(3)))
    print(closure())
    print(with_statement())
    print(comprehension())
    print(varargs(1, 2, extra=Box(3)))
    print(Box(4).scaled(2, offset=Box(1).value))


main()
'''


@pytest.mark.pcc_gate(probe=lambda: sys.platform == "darwin")
@pytest.mark.parametrize("table_format", ["2", "3"])
def test_stack_walk_agrees_with_registered_roots_across_root_shapes(
    tmp_path, monkeypatch, pcc_stackmap_runtime_archive, python_program_compiler,
    table_format,
):
    import re

    source = tmp_path / "shapes.py"
    source.write_text(_ROOT_SHAPES, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    monkeypatch.setenv(PRECISE_STACKMAP_TABLES_ENV, "1")
    monkeypatch.setenv(_FORMAT_ENV, table_format)
    output = tmp_path / "shapes"
    python_program_compiler(
        str(source), str(output), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_stackmap_runtime_archive),
    )
    _assert_linked_table_format(output, table_format)
    report = re.compile(
        r"pcc-stackmap-diff frames=\d+ mapped=\d+ mismatches=(\d+) errors=(\d+)"
        r" registered=(\d+) unmapped=(\d+)"
    )
    for gc in (3, 4):
        ran = subprocess.run(
            [str(output)], capture_output=True, text=True, timeout=600,
            env=dict(os.environ, PCC_GC_BACKEND=str(gc), PCC_GC_STACKMAP_DIFF="1"),
        )
        assert ran.returncode == 0, f"GC{gc}: {ran.stdout}{ran.stderr}"
        assert ran.stdout == expected.stdout, f"GC{gc}: {ran.stdout}"
        found = report.findall(ran.stderr)
        assert len(found) >= 10, ran.stderr
        assert sum(int(registered) for _m, _e, registered, _u in found) > 0
        assert all((m, e, u) == ("0", "0", "0") for m, e, _r, u in found), ran.stderr


_ALLOCATION_CHURN = '''
class Node:
    def __init__(self, value, children):
        self.value = value
        self.children = children


def build(depth, width):
    if depth == 0:
        return Node(depth, [])
    return Node(depth, [build(depth - 1, width) for _ in range(width)])


def total(node):
    result = node.value
    for child in node.children:
        result += total(child)
    return result


def churn(rounds):
    kept = []
    for round_index in range(rounds):
        tree = build(4, 3)
        words = {str(index): [index, str(index * 2)] for index in range(50)}
        kept.append((total(tree), len(words), words["7"][1]))
        if len(kept) > 5:
            kept.pop(0)
    return kept


def main():
    print(churn(400)[-1])


main()
'''


@pytest.mark.pcc_gate(probe=lambda: sys.platform == "darwin")
def test_stack_walk_agrees_with_registered_roots_at_automatic_collections(
    tmp_path, monkeypatch, pcc_stackmap_runtime_archive, python_program_compiler,
):
    # GC4 steps when allocation debt crosses its threshold, from whatever
    # call made the allocation; a low threshold makes that happen often.
    # (GC3's young blocks grow without stepping, so only explicit GC3
    # collections run the walk.)
    import re

    source = tmp_path / "churn.py"
    source.write_text(_ALLOCATION_CHURN, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    monkeypatch.setenv(PRECISE_STACKMAP_TABLES_ENV, "1")
    output = tmp_path / "churn"
    python_program_compiler(
        str(source), str(output), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_stackmap_runtime_archive),
    )
    ran = subprocess.run(
        [str(output)], capture_output=True, text=True, timeout=600,
        env=dict(
            os.environ, PCC_GC_BACKEND="4", PCC_GC_STACKMAP_DIFF="1",
            PCC_GC_DEBT_THRESHOLD="4096",
        ),
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr[-2000:]
    assert ran.stdout == expected.stdout
    report = re.compile(
        r"pcc-stackmap-diff frames=\d+ mapped=\d+ mismatches=(\d+) errors=(\d+)"
        r" registered=(\d+) unmapped=(\d+)"
    )
    found = report.findall(ran.stderr)
    assert len(found) >= 100, len(found)
    assert sum(int(registered) for _m, _e, registered, _u in found) > 0
    bad = [line for line in found if (line[0], line[1], line[3]) != ("0", "0", "0")]
    assert bad == [], bad[:5]


@pytest.mark.pcc_gate(probe=lambda: sys.platform == "darwin")
@pytest.mark.parametrize("table_format", ["2", "3"])
@pytest.mark.parametrize("program", ["deep", "shapes", "churn"])
def test_stack_maps_alone_root_the_stack_frames_on_gc3_and_gc4(
    tmp_path, monkeypatch, pcc_stackmap_runtime_archive, python_program_compiler, program,
    table_format,
):
    # Step 3 production consumer: with PCC_GC_STACKMAP_ROOTS the collector
    # marks (and GC4 rewrites after moving) this thread's frame roots through
    # the stack walk and skips the registered slots inside the walked stack.
    # The programs must behave exactly as under CPython.
    import re

    text = {"deep": _DEEP_COLLECT, "shapes": _ROOT_SHAPES, "churn": _ALLOCATION_CHURN}[program]
    source = tmp_path / (program + ".py")
    source.write_text(text, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    monkeypatch.setenv(PRECISE_STACKMAP_TABLES_ENV, "1")
    monkeypatch.setenv(_FORMAT_ENV, table_format)
    output = tmp_path / program
    python_program_compiler(
        str(source), str(output), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_stackmap_runtime_archive),
    )
    _assert_linked_table_format(output, table_format)
    report = re.compile(r"pcc-stackmap-roots mode=\d+ mapped=(\d+) skipped=(\d+)")
    for gc in (3, 4):
        ran = subprocess.run(
            [str(output)], capture_output=True, text=True, timeout=600,
            env=dict(
                os.environ, PCC_GC_BACKEND=str(gc), PCC_GC_STACKMAP_ROOTS="report",
                PCC_GC_DEBT_THRESHOLD="4096",
            ),
        )
        assert ran.returncode == 0, f"GC{gc}: {ran.stdout}{ran.stderr[-2000:]}"
        assert ran.stdout == expected.stdout, f"GC{gc}: {ran.stdout}"
        found = report.findall(ran.stderr)
        if program == "churn" and gc == 3:
            assert found == []  # GC3 young blocks grow without collecting here
            continue
        assert found, ran.stderr[-2000:]
        assert sum(int(mapped) for mapped, _skipped in found) > 0
        assert sum(int(skipped) for _mapped, skipped in found) > 0


def _rootless_and_total_records(payload):
    """Call and loop records with no location, flag or exception edge."""
    import struct

    function_count = struct.unpack_from("<I", payload, 12)[0]
    cursor = 24
    rootless = 0
    total = 0
    for _function in range(function_count):
        record_count = struct.unpack_from("<I", payload, cursor + 24)[0]
        cursor += 32
        for _record in range(record_count):
            location_count = struct.unpack_from("<H", payload, cursor + 20)[0]
            kind, flags = struct.unpack_from("<BB", payload, cursor + 24)
            total += 1
            if kind in (2, 3) and flags == 0 and location_count == 0:
                rootless += 1
            cursor += 32
    return rootless, total


@pytest.mark.parametrize("route", ["text", "structured-lines", "native-sink"])
def test_aarch64_tables_omit_rootless_safepoints(monkeypatch, route):
    # Step 2: a frame without managed locations needs no record; a walk that
    # finds none at its return address skips the frame either way.
    from pcc.backend import self_backend_aarch64_darwin as aarch64
    from pcc.backend.arm64_asm_driver import assemble_file
    from tests.python.test_aarch64_stackmap_plan_ownership import _emit, _source

    monkeypatch.setenv(PRECISE_STACKMAP_TABLES_ENV, "1")
    source = _source("arm64-apple-darwin23.6.0")
    if route == "text":
        sections, _undefined = assemble_file(aarch64.emit_aarch64_darwin_asm(source, optimize=False))
    else:
        sections, _undefined = _emit(source, route)
    table = next(section for section in sections if section.sectname == "__pcc_stackmaps")
    rootless, total = _rootless_and_total_records(bytes(table.data))
    assert total > 0
    assert rootless == 0


def test_x86_64_tables_omit_rootless_safepoints(tmp_path, monkeypatch):
    from pcc.backend import elf_x86_64 as elf
    from pcc.backend import self_backend_x86_64_linux as x86
    from pcc.backend.target_objects import encode_assembly_object

    source = tmp_path / "boxes.py"
    source.write_text(_PROGRAM, encoding="utf-8")
    ir = tmp_path / "boxes.ll"
    pipeline.compile_python(
        str(source), str(ir), emit_llvm_only=True, libpython_mode="off",
        target_triple="x86_64-unknown-linux-gnu",
    )
    monkeypatch.setenv(PRECISE_STACKMAP_TABLES_ENV, "1")
    obj = elf.parse_relocatable(encode_assembly_object(
        x86.emit_x86_64_linux_asm(ir.read_text(encoding="utf-8")), "x86_64-unknown-linux-gnu",
    ))
    table = next(section for section in obj.sections if section.name == ".pcc_stackmaps")
    rootless, total = _rootless_and_total_records(bytes(table.data))
    assert total > 0
    assert rootless == 0
