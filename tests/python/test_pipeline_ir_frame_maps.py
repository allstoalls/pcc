"""Object-shard map pruning preserves every local data dependency."""

from __future__ import annotations

import platform
import subprocess
import sys

import pytest

from pcc.frontends.python import pipeline_ir_split as split


MAP_A = "@.pcc.gc.frame.map.1 = internal constant i32 1"
MAP_B = "@.pcc.gc.frame.map.10 = private constant i32 -1"
MAP_C = "@.pcc.vthread.frame.map.3 = internal constant i32 3"
MAP_UNUSED = "@.pcc.gc.frame.map.unused = internal constant i32 9"
MAPS = [MAP_A, MAP_B, MAP_C, MAP_UNUSED]


def _function(name, body="  ret ptr null"):
    return f"define ptr @{name}() {{\nentry:\n{body}\n}}"


def _source(functions, prefix=(), maps=MAPS):
    return "\n\n".join([*prefix, *maps, *functions]) + "\n"


def _split(source, budget=1):
    return split.split_self_backend_ir_module_for_object_shards(
        source, export_prefix="__test_", shard_bytes=budget,
    )


def _maps(shard):
    return [line for line in shard.splitlines()
            if split.self_backend_local_frame_map_line(line)]


def test_each_shard_keeps_only_its_exact_referenced_map():
    source = _source([
        _function("first", "  ret ptr @.pcc.gc.frame.map.1"),
        _function("second", "  ret ptr @.pcc.gc.frame.map.10"),
        _function("third", "  ret ptr @.pcc.vthread.frame.map.3"),
        _function("empty"),
    ])
    assert [_maps(shard) for shard in _split(source)] == [
        [MAP_A], [MAP_B], [MAP_C], [],
    ]


@pytest.mark.parametrize("maps,budget", [
    ([], 1), ([MAP_A], 1), ([MAP_A, MAP_B], 1),
    ([MAP_A, MAP_B, MAP_C], 1), (MAPS, 10000),
])
def test_no_benefit_paths_skip_descriptor_and_reference_analysis(maps, budget, monkeypatch):
    source = _source([_function("first"), _function("second")], maps=maps)

    def unexpected_analysis(*_args):
        raise AssertionError("no-benefit sharding path scanned frame maps")

    monkeypatch.setattr(split, "_literal_local_frame_map_name", unexpected_analysis)
    monkeypatch.setattr(split, "_local_frame_map_references", unexpected_analysis)
    shards = _split(source, budget)
    if budget == 1:
        assert [_maps(shard) for shard in shards] == [maps, maps]
    else:
        assert shards == [source]


def test_shared_uses_of_all_maps_skip_function_reference_analysis(monkeypatch):
    shared = ("!0 = !{ptr @.pcc.gc.frame.map.1, ptr @.pcc.gc.frame.map.10, "
              "ptr @.pcc.vthread.frame.map.3, ptr @.pcc.gc.frame.map.unused}")
    source = _source([_function("first"), _function("second")],
                     [shared])
    original = split._local_frame_map_references
    scanned = []

    def record_scan(text, names):
        scanned.append(text)
        return original(text, names)

    monkeypatch.setattr(split, "_local_frame_map_references", record_scan)
    assert [_maps(shard) for shard in _split(source)] == [MAPS] * 2
    assert scanned == [shared]


def test_reference_scan_does_not_walk_unrelated_global_names(monkeypatch):
    original = split.llvm_global_name_char
    visited = []

    def record_character(char):
        visited.append(char)
        return original(char)

    monkeypatch.setattr(split, "llvm_global_name_char", record_character)
    references = split._local_frame_map_references(
        "@unrelated" + "x" * 10000 + " @.pcc.gc.frame.map.10 "
        "@.pcc.gc.frame.map.1.extra @.pcc.gc.frame.map.1",
        {".pcc.gc.frame.map.1"},
    )
    assert references == {".pcc.gc.frame.map.1"}
    assert len(visited) < 20


def test_repeated_uses_stop_scanning_after_all_descriptors_are_live(monkeypatch):
    original = split.llvm_global_name_char
    visited = []

    def record_character(char):
        visited.append(char)
        return original(char)

    monkeypatch.setattr(split, "llvm_global_name_char", record_character)
    names = {".pcc.gc.frame.map.1", ".pcc.gc.frame.map.10"}
    text = "@.pcc.gc.frame.map.1 @.pcc.gc.frame.map.10 " * 1000
    assert split._local_frame_map_references(text, names) == names
    assert len(visited) < 10
    # The early stop must not bypass the full-text unknown-shape guard.
    assert split._local_frame_map_references(text + ' asm "unknown"', names) is None


@pytest.mark.parametrize("body", [
    "  %p = bitcast ptr @.pcc.gc.frame.map.1 to ptr\n  ret ptr %p",
    "  %p = getelementptr i32, ptr @.pcc.gc.frame.map.1, i64 0\n  ret ptr %p",
    "  %slot = alloca ptr\n  store ptr @.pcc.gc.frame.map.1, ptr %slot\n"
    "  %p = load ptr, ptr %slot\n  ret ptr %p",
    "  %p = select i1 true, ptr @.pcc.gc.frame.map.1, ptr null\n  ret ptr %p",
    "  br label %next\nnext:\n"
    "  %p = phi ptr [ @.pcc.gc.frame.map.1, %entry ]\n  ret ptr %p",
    "  %p = call ptr @py_continuation_new(ptr @.pcc.gc.frame.map.1, "
    "ptr null, ptr null)\n  ret ptr %p",
    "  ; @.pcc.gc.frame.map.1 in a comment is conservatively live\n  ret ptr null",
])
def test_non_direct_call_and_indirect_uses_keep_the_descriptor(body):
    shards = _split(_source([_function("first", body), _function("second")]))
    assert [_maps(shard) for shard in shards] == [[MAP_A], []]


@pytest.mark.parametrize("shared", [
    "@alias = internal alias i32, ptr @.pcc.gc.frame.map.1",
    "!0 = !{ptr @.pcc.gc.frame.map.1}",
    "!1 = !{!0}\n!0 = !{ptr @.pcc.gc.frame.map.1}",
    "@alias2 = alias ptr, ptr @alias\n"
    "@alias = alias i32, ptr @.pcc.gc.frame.map.1",
])
def test_shared_alias_and_metadata_dependencies_keep_maps_in_all_shards(shared):
    shards = _split(_source([_function("first"), _function("second")], [shared]))
    assert [_maps(shard) for shard in shards] == [[MAP_A], [MAP_A]]
    assert all(shared in shard for shard in shards)


def test_global_initializer_dependencies_are_retained_in_the_global_shard():
    global_line = "@holder = private global ptr @.pcc.gc.frame.map.1"
    shards = _split(_source([
        _function("first", "  ret ptr @holder"),
        _function("second", "  ret ptr @.pcc.gc.frame.map.10"),
    ], [global_line]))
    assert [_maps(shard) for shard in shards] == [[MAP_A], [], [MAP_B]]
    assert "@__test_holder = global ptr @.pcc.gc.frame.map.1" in shards[0]
    assert "ret ptr @__test_holder" in shards[1]
    assert sum("@__test_holder = " in shard for shard in shards) == 1


@pytest.mark.parametrize("opaque", [
    '  ret ptr @".pcc.gc.frame.map.1"',
    r'  ret ptr @"\2Epcc.gc.frame.map.1"',
    '  call void asm "read .pcc.gc.frame.map.1", ""()\n  ret ptr null',
    '  call void asm\t"read .pcc.gc.frame.map.1", ""()\n  ret ptr null',
    r'  ; unknown escaped text \2E' + "\n  ret ptr null",
])
def test_opaque_body_references_keep_all_maps_in_that_shard(opaque):
    shards = _split(_source([_function("first", opaque), _function("second")]))
    assert [_maps(shard) for shard in shards] == [MAPS, []]


@pytest.mark.parametrize("shared", [
    'module asm "read .pcc.gc.frame.map.1"',
    '@alias = alias i32, ptr @".pcc.gc.frame.map.1"',
    r'!0 = !{ptr @"\2Epcc.gc.frame.map.1"}',
])
def test_opaque_shared_references_keep_all_maps_everywhere(shared):
    shards = _split(_source([_function("first"), _function("second")], [shared]))
    assert [_maps(shard) for shard in shards] == [MAPS, MAPS]


@pytest.mark.parametrize("unknown", [
    '@".pcc.gc.frame.map.quoted" = internal constant i32 1',
    "@.pcc.gc.frame.map.expr = internal constant i32 ptrtoint "
    "(ptr @.pcc.gc.frame.map.1 to i32)",
    "@.pcc.gc.frame.map.aligned = internal constant i32 1, align 4",
    "@.pcc.gc.frame.map.out_of_range = internal constant i32 2147483648",
    "@.pcc.gc.frame.map.no_value = internal constant i32 undef",
    MAP_A,
])
def test_unknown_or_duplicate_map_definitions_keep_the_old_copy_all_path(unknown):
    maps = [*MAPS, unknown]
    shards = _split(_source([_function("first"), _function("second")], maps=maps))
    assert [_maps(shard) for shard in shards] == [maps, maps]


def test_reserved_mutable_global_is_never_pruned_or_duplicated():
    global_line = "@.pcc.gc.frame.map.mutable = internal global i32 7"
    shards = _split(_source([_function("first"), _function("second")], [global_line]))
    assert "@__test_.pcc.gc.frame.map.mutable = global i32 7" in shards[0]
    assert all(not _maps(shard) for shard in shards)
    assert sum("= global i32 7" in shard for shard in shards) == 1


def test_missing_referenced_descriptor_still_fails_closed():
    from pcc.backend import BackendUnavailable
    from pcc.backend.self_backend_x86_64_linux import emit_x86_64_linux_asm

    source = _source([
        "define void @first() {\nentry:\n  %slot = alloca ptr\n"
        "  call void @pcc_gc_frame_enter(ptr @.pcc.gc.frame.map.missing, ptr %slot)\n"
        "  call void @pcc_gc_frame_leave(ptr %slot)\n  ret void\n}",
        _function("second"),
    ], [
        'target triple = "x86_64-unknown-linux-gnu"',
        "declare void @pcc_gc_frame_enter(ptr, ptr)",
        "declare void @pcc_gc_frame_leave(ptr)",
    ])
    with pytest.raises(BackendUnavailable, match="frame map global .* is unavailable"):
        emit_x86_64_linux_asm(_split(source)[0])


@pytest.mark.parametrize("budget", [1, 100, 512, 10_000])
def test_pruning_preserves_partition_order_and_only_reduces_text(monkeypatch, budget):
    source = _source([
        _function(f"f{index}", "  ret ptr @.pcc.gc.frame.map.1")
        for index in range(16)
    ], ["; " + "x" * 1024, "@holder = global ptr @.pcc.gc.frame.map.10"])
    after = _split(source, budget)
    with monkeypatch.context() as context:
        context.setattr(split, "_literal_local_frame_map_name", lambda _line: "")
        before = _split(source, budget)
    assert len(after) == len(before)
    for old, new in zip(before, after):
        old_nonmaps = [line for line in old.splitlines() if line and
                       not split.self_backend_local_frame_map_line(line)]
        new_nonmaps = [line for line in new.splitlines() if line and
                       not split.self_backend_local_frame_map_line(line)]
        assert old_nonmaps == new_nonmaps
        assert len(new.encode("utf-8")) <= len(old.encode("utf-8"))


@pytest.mark.usefixtures("precise_stackmap_tables")
@pytest.mark.pcc_gate(probe=lambda: sys.platform.startswith("linux")
                      and platform.machine() in ("x86_64", "amd64"))
def test_pruned_shards_execute_with_cross_object_maps_and_root_plans(tmp_path, monkeypatch):
    """Owned host backend -> ELF -> native execution, without a GC runtime."""
    from pcc.backend.elf_x86_64 import link_static_executable
    from pcc.backend.precise_stackmap import LOCATION_OWNED, decode_stack_map
    from pcc.backend.self_backend_x86_64_linux import emit_x86_64_linux_asm
    from pcc.backend.x86_64_asm_driver import assemble_file

    source = _source([
        "define internal i64 @first() {\nentry:\n"
        "  %slot = alloca ptr\n  store ptr null, ptr %slot\n"
        "  call void @pcc_gc_frame_enter(ptr @.pcc.gc.frame.map.1, ptr %slot)\n"
        "  call void @pcc_thread_safepoint()\n"
        "  %n = load i32, ptr @.pcc.gc.frame.map.1\n"
        "  call void @pcc_gc_frame_leave(ptr %slot)\n"
        "  %wide = sext i32 %n to i64\n  ret i64 %wide\n}",
        "define internal i64 @second() {\nentry:\n"
        "  %slot = alloca ptr\n  store ptr null, ptr %slot\n"
        "  call void @pcc_gc_frame_enter(ptr @.pcc.gc.frame.map.10, ptr %slot)\n"
        "  call void @pcc_thread_safepoint()\n"
        "  %n = load i32, ptr @.pcc.gc.frame.map.10\n"
        "  call void @pcc_gc_frame_leave(ptr %slot)\n"
        "  %wide = sext i32 %n to i64\n  ret i64 %wide\n}",
        "define i64 @main() {\nentry:\n"
        "  %a = call i64 @first()\n  %b = call i64 @second()\n"
        "  %p = load ptr, ptr @holder\n  %c = load i32, ptr %p\n"
        "  %wide = sext i32 %c to i64\n  %sum = add i64 %a, %b\n"
        "  %with_global = add i64 %sum, %wide\n"
        "  %result = add i64 %with_global, 41\n  ret i64 %result\n}",
    ], [
        'target triple = "x86_64-unknown-linux-gnu"',
        "declare void @pcc_gc_frame_enter(ptr, ptr)",
        "declare void @pcc_gc_frame_leave(ptr)",
        "declare void @pcc_thread_safepoint()",
        "@holder = private global ptr @.pcc.gc.frame.map.1",
    ])
    shards = _split(source)
    assert [_maps(shard) for shard in shards] == [[MAP_A], [MAP_A], [MAP_B], []]
    with monkeypatch.context() as context:
        context.setattr(split, "_literal_local_frame_map_name", lambda _line: "")
        unpruned = _split(source)
    objects = []
    for index, shard in enumerate(shards):
        (tmp_path / f"shard-{index}.ll").write_text(shard)
        assembly = emit_x86_64_linux_asm(shard)
        (tmp_path / f"shard-{index}.s").write_text(assembly)
        if index in (1, 2):
            assert ".pcc_stackmaps" in assembly
        obj = assemble_file(assembly)
        original = assemble_file(emit_x86_64_linux_asm(unpruned[index]))
        sections = [section for section in obj.sections if section.name == ".pcc_stackmaps"]
        old_sections = [section for section in original.sections if section.name == ".pcc_stackmaps"]
        def fingerprint(section, owner):
            return (section.name, section.type, section.flags, section.align,
                    section.data, section.mem_size,
                    [(rel.offset, owner.symbols[rel.symbol_index].name,
                      rel.type, rel.addend) for rel in section.relocations])

        assert [fingerprint(section, obj) for section in sections] == [
            fingerprint(section, original) for section in old_sections
        ]
        if index in (1, 2):
            decoded = decode_stack_map(sections[0].data)
            roots = [location for function in decoded.functions
                     for record in function.records for location in record.locations]
            assert roots
            assert all(bool(root.flags & LOCATION_OWNED) == (index == 1) for root in roots)
        objects.append(obj)
    startup = '''.intel_syntax noprefix
.text
.globl _start
_start:
  call main
  mov rdi, rax
  mov eax, 60
  syscall
.globl pcc_gc_frame_enter
pcc_gc_frame_enter:
  ret
.globl pcc_gc_frame_leave
pcc_gc_frame_leave:
  ret
.globl pcc_thread_safepoint
pcc_thread_safepoint:
  ret
'''
    objects.append(assemble_file(startup))
    executable = tmp_path / "pruned-shards"
    executable.write_bytes(link_static_executable(objects))
    executable.chmod(0o755)
    result = subprocess.run([str(executable)], capture_output=True, timeout=10)
    assert result.returncode == 42, result.stderr
    assert result.stdout == b""
    assert result.stderr == b""
