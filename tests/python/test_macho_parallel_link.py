"""Focused deterministic-concurrency contracts for the owned Mach-O linker."""

from __future__ import annotations

from pathlib import Path

import pytest

from pcc.backend import macho_spec as spec
from pcc.backend.macho_archive import read_archive
from pcc.backend.macho_exec import link_executable
from pcc.backend.macho_link import LinkError
from pcc.backend.macho_obj import Relocation, Section, TextSymbol, TEXT_SECTION_FLAGS
from pcc.backend.macho_parallel import (
    PARALLEL_JOBS_ENV,
    OutputRegion,
    ParallelLinkError,
    ShardedSymbolDefinitions,
    SymbolDefinition,
    materialize_output,
    ordered_parallel_map,
    resolve_link_jobs,
    write_mmap_output,
)
from pcc.backend.native_object import NativeObject


_RET = b"\xc0\x03\x5f\xd6"
_BL_PLACEHOLDER = b"\x00\x00\x00\x94"


def _link_inputs() -> list[NativeObject]:
    caller = NativeObject.from_sections(
        [Section(
            sectname="__text",
            segname="__TEXT",
            data=_BL_PLACEHOLDER + _RET,
            align_log2=2,
            flags=TEXT_SECTION_FLAGS,
            symbols=(TextSymbol("_main", 0),),
            relocations=(Relocation(
                offset=0,
                symbol="_helper",
                type=spec.ARM64_RELOC_BRANCH26,
                pcrel=True,
            ),),
        )],
        undefined=["_helper"],
    )
    helper = NativeObject.from_sections([Section(
        sectname="__text",
        segname="__TEXT",
        data=_RET,
        align_log2=2,
        flags=TEXT_SECTION_FLAGS,
        symbols=(TextSymbol("_helper", 0),),
    )])
    return [caller, helper]


def _archive_bytes(members: list[tuple[str, bytes]]) -> bytes:
    archive = bytearray(b"!<arch>\n")
    for name, payload in members:
        encoded_name = (name + "/").encode("ascii")
        assert len(encoded_name) <= 16
        archive += encoded_name.ljust(16, b" ")
        archive += b"0".ljust(12, b" ")  # timestamp
        archive += b"0".ljust(6, b" ")   # uid
        archive += b"0".ljust(6, b" ")   # gid
        archive += b"100644".ljust(8, b" ")
        archive += str(len(payload)).encode("ascii").ljust(10, b" ")
        archive += b"`\n"
        archive += payload
        if len(payload) % 2:
            archive += b"\n"
    return bytes(archive)


def test_disjoint_output_is_independent_of_region_and_worker_order() -> None:
    regions = [
        OutputRegion(16, b"tail", "tail"),
        OutputRegion(0, b"head", "head"),
        OutputRegion(8, b"middle", "middle"),
    ]
    expected = b"head\0\0\0\0middle\0\0tail"

    assert materialize_output(20, regions, jobs=1) == expected
    for jobs in (2, 3, 8):
        assert materialize_output(20, list(reversed(regions)), jobs=jobs) == expected


def test_parallel_archive_inspection_preserves_file_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller, helper = _link_inputs()
    archive = _archive_bytes([
        ("caller.o", caller.to_macho()),
        ("helper.o", helper.to_macho()),
    ])
    monkeypatch.setenv(PARALLEL_JOBS_ENV, "4")

    first = read_archive(archive)
    second = read_archive(archive)

    assert [member.name for member in first] == ["caller.o", "helper.o"]
    assert first == second
    assert first[0].undefined == frozenset({"_helper"})
    assert first[1].defines == frozenset({"_helper"})


def test_disjoint_regions_patch_a_file_backed_mapping(
    tmp_path: Path,
) -> None:
    output = tmp_path / "linked-image"
    output.write_bytes(b"-" * 20)
    regions = [
        OutputRegion(16, b"tail", "tail"),
        OutputRegion(0, b"head", "head"),
        OutputRegion(8, b"middle", "middle"),
    ]

    with output.open("r+b") as file:
        write_mmap_output(file, 20, regions, jobs=4)

    assert output.read_bytes() == b"head----middle--tail"


def test_parallel_map_reports_lowest_input_failure_not_first_scheduled() -> None:
    def inspect(index: int) -> int:
        if index in (1, 3):
            raise ValueError(f"bad input {index}")
        return index * 2

    with pytest.raises(ValueError, match="bad input 1"):
        ordered_parallel_map(
            [0, 1, 2, 3],
            inspect,
            total_bytes=1024 * 1024,
            jobs=4,
        )


def test_sharded_symbol_owner_is_independent_of_parallel_insertion_order() -> None:
    definitions = ShardedSymbolDefinitions()
    candidates = [
        SymbolDefinition(7, 3),
        SymbolDefinition(1, 9),
        SymbolDefinition(4, 2),
        SymbolDefinition(1, 5),
    ]

    ordered_parallel_map(
        list(reversed(candidates)),
        lambda definition: definitions.add("_shared", definition),
        total_bytes=1024 * 1024,
        jobs=4,
    )
    with pytest.raises(ParallelLinkError, match="frozen before lookup"):
        definitions.owner("_shared")
    definitions.freeze()

    assert definitions.definitions("_shared") == tuple(sorted(candidates))
    assert definitions.owner("_shared") == SymbolDefinition(1, 5)
    with pytest.raises(ParallelLinkError, match="frozen"):
        definitions.add("_shared", SymbolDefinition(0, 0))


def test_shard_index_keeps_low_six_bits_of_fnv1a() -> None:
    """Shard selection must equal full-width FNV-1a modulo 64.

    The production accumulator tracks only the low six bits; this pins it to
    the reference 64-bit hash for symbol-shaped ASCII, high code points and
    length boundaries, so shard ownership stays part of the proof.
    """

    def reference(name: str) -> int:
        value = 0xCBF29CE484222325
        for character in name:
            value = ((value ^ ord(character)) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
        return value % ShardedSymbolDefinitions._SHARD_COUNT

    names = ["", " ", "_main", "_py_sha256_state_update", ".pystr.obj.17"]
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.$"
    for index in range(400):
        length = index % 24
        start = (index * 7) % len(alphabet)
        names.append("".join(
            alphabet[(start + position * 3) % len(alphabet)]
            for position in range(length)
        ))
    names.extend(chr(code) for code in (0x7F, 0x80, 0x10FFFF, 0x1F600))
    for name in names:
        assert ShardedSymbolDefinitions._shard_index(name) == reference(name)


def test_parallel_output_rejects_overlap_and_out_of_bounds() -> None:
    with pytest.raises(ParallelLinkError, match="overlaps"):
        materialize_output(8, [
            OutputRegion(0, b"12345", "first"),
            OutputRegion(4, b"5678", "second"),
        ], jobs=4)

    with pytest.raises(ParallelLinkError, match="past the image size"):
        materialize_output(8, [
            OutputRegion(7, b"too long", "overflow"),
        ], jobs=4)


def test_mmap_layout_failure_does_not_resize_or_patch_the_file(
    tmp_path: Path,
) -> None:
    output = tmp_path / "must-survive"
    original = b"unchanged"
    output.write_bytes(original)

    with output.open("r+b") as file:
        with pytest.raises(ParallelLinkError, match="overlaps"):
            write_mmap_output(file, 4, [
                OutputRegion(0, b"abc", "first"),
                OutputRegion(2, b"xy", "second"),
            ], jobs=2)

    assert output.read_bytes() == original


def test_parallel_job_configuration_is_bounded_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(PARALLEL_JOBS_ENV, raising=False)
    monkeypatch.setenv("PCC_OUTER_PARALLELISM", "2")
    monkeypatch.setattr("pcc.backend.macho_parallel.os.cpu_count", lambda: 12)
    assert resolve_link_jobs(1000, 1024) == 1
    assert resolve_link_jobs(1000, 1024 * 1024) == 6

    monkeypatch.setenv(PARALLEL_JOBS_ENV, "9999")
    assert resolve_link_jobs(1000, 1024 * 1024) == 32

    monkeypatch.setenv(PARALLEL_JOBS_ENV, "off")
    assert resolve_link_jobs(1000, 1024 * 1024) == 1

    monkeypatch.setenv(PARALLEL_JOBS_ENV, "four")
    with pytest.raises(ParallelLinkError, match="positive integer"):
        resolve_link_jobs(1000, 1024 * 1024)


def test_parallel_and_serial_executable_links_are_byte_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _link_inputs()
    monkeypatch.setenv(PARALLEL_JOBS_ENV, "1")
    serial = link_executable(inputs)

    for jobs in (2, 4, 8):
        monkeypatch.setenv(PARALLEL_JOBS_ENV, str(jobs))
        assert link_executable(inputs) == serial
        assert link_executable(inputs) == serial


def test_duplicate_definition_diagnostic_is_worker_count_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    duplicate = NativeObject.from_sections([Section(
        sectname="__text",
        segname="__TEXT",
        data=_RET,
        align_log2=2,
        flags=TEXT_SECTION_FLAGS,
        symbols=(TextSymbol("_main", 0),),
    )])

    for jobs in (1, 2, 8):
        monkeypatch.setenv(PARALLEL_JOBS_ENV, str(jobs))
        with pytest.raises(LinkError, match="duplicate definition of '_main'"):
            link_executable([duplicate, duplicate])


@pytest.mark.parametrize("jobs", [1, 4])
def test_positional_writer_preserves_seed_gaps_cursor_and_descriptor(tmp_path, jobs):
    import os
    from pcc.backend.macho_parallel import write_file_output

    path = tmp_path / "positioned"
    with path.open("w+b") as stream:
        descriptor = stream.fileno()
        # Keep this seed buffered: flushing after shrinking would re-add its tail.
        stream.write(b"abcdefghijklmnop")
        position = stream.tell()
        write_file_output(stream, 8, [OutputRegion(2, b"XY")], jobs=jobs)
        assert stream.tell() == position
        assert stream.fileno() == descriptor and not stream.closed
        stream.seek(0)
        assert stream.read() == b"abXYefgh"
        stream.seek(3)
        write_file_output(stream, 14, [OutputRegion(11, b"end")], jobs=jobs)
        assert stream.tell() == 3
        stream.seek(0)
        assert stream.read() == b"abXYefgh\0\0\0end"
        assert os.fstat(descriptor).st_size == 14
        write_file_output(stream, 0, [], jobs=jobs)
        assert not stream.closed and os.fstat(descriptor).st_size == 0


def test_positional_writer_rejects_actual_append_flag_before_mutation(tmp_path):
    import os
    from pcc.backend.macho_parallel import write_file_output

    path = tmp_path / "append"
    path.write_bytes(b"original")
    descriptor = os.open(path, os.O_RDWR | os.O_APPEND)
    try:
        # The wrapper mode does not describe the descriptor's O_APPEND flag.
        with os.fdopen(descriptor, "r+b", closefd=False) as stream:
            assert "a" not in stream.mode
            with pytest.raises(ParallelLinkError, match="O_APPEND"):
                write_file_output(stream, 2, [OutputRegion(0, b"XX")], jobs=2)
            assert stream.tell() == 0 and not stream.closed
        assert os.fstat(descriptor).st_size == 8
    finally:
        os.close(descriptor)
    assert path.read_bytes() == b"original"


def test_positional_writer_invalid_configuration_precedes_io(tmp_path, monkeypatch):
    from pcc.backend.macho_parallel import write_file_output

    path = tmp_path / "unchanged"
    path.write_bytes(b"original")
    with path.open("r+b") as stream:
        monkeypatch.setenv(PARALLEL_JOBS_ENV, "bad")
        with pytest.raises(ParallelLinkError, match="positive integer"):
            write_file_output(stream, 2, [OutputRegion(0, b"XX")])
        assert stream.tell() == 0 and not stream.closed
    assert path.read_bytes() == b"original"


def test_positional_writer_retries_short_writes_and_bounds_payload(tmp_path, monkeypatch):
    import os
    from pcc.backend import macho_parallel

    original = os.pwrite
    writes = []

    def short_write(fd, payload, offset):
        writes.append((offset, len(payload)))
        return original(fd, payload[:3], offset)

    monkeypatch.setattr(macho_parallel.os, "pwrite", short_write)
    path = tmp_path / "short"
    with path.open("w+b") as stream:
        stream.write(b"-" * 16)
        macho_parallel.write_file_output(
            stream, 16, [OutputRegion(2, b"abcdefgh")], jobs=2,
        )
        assert stream.tell() == 16 and not stream.closed
    assert path.read_bytes() == b"--abcdefgh------"
    assert writes == [(2, 8), (5, 5), (8, 2)]


@pytest.mark.parametrize("count", [0, -1, 100])
def test_positional_writer_rejects_invalid_write_progress(tmp_path, monkeypatch, count):
    from pcc.backend import macho_parallel

    monkeypatch.setattr(macho_parallel.os, "pwrite", lambda *args: count)
    path = tmp_path / "no-progress"
    with path.open("w+b") as stream:
        with pytest.raises(ParallelLinkError, match="invalid progress"):
            macho_parallel.write_file_output(stream, 3, [OutputRegion(0, b"abc")])
        assert not stream.closed and stream.tell() == 0


def test_positional_writer_has_real_bounded_parallel_workers(tmp_path, monkeypatch):
    import os
    import threading
    from pcc.backend import macho_parallel

    chunk = macho_parallel._OUTPUT_CHUNK_BYTES
    barrier = threading.Barrier(4, timeout=3)
    lock = threading.Lock()
    participants = set()
    calls = []
    original = os.pwrite

    def simultaneous_write(fd, payload, offset):
        with lock:
            participants.add(threading.get_ident())
            calls.append((offset, len(payload)))
        barrier.wait()
        return original(fd, payload, offset)

    monkeypatch.setattr(macho_parallel.os, "pwrite", simultaneous_write)
    path = tmp_path / "parallel"
    data = b"A" * chunk + b"B" * chunk + b"C" * chunk + b"D" * chunk
    with path.open("w+b") as stream:
        macho_parallel.write_file_output(stream, len(data), [OutputRegion(0, data)], jobs=4)
        assert stream.tell() == 0 and not stream.closed
    assert path.read_bytes() == data
    assert len(participants) == 4
    assert sorted(calls) == [(index * chunk, chunk) for index in range(4)]


def test_positional_writer_reports_lowest_failed_region_after_join(tmp_path, monkeypatch):
    import threading
    from pcc.backend import macho_parallel

    barrier = threading.Barrier(3, timeout=3)
    finished = []
    lock = threading.Lock()

    def failed_write(fd, payload, offset):
        barrier.wait()
        with lock:
            finished.append(offset)
        raise OSError("region " + str(offset))

    monkeypatch.setattr(macho_parallel.os, "pwrite", failed_write)
    path = tmp_path / "failures"
    with path.open("w+b") as stream:
        with pytest.raises(ParallelLinkError, match="could not write") as caught:
            macho_parallel.write_file_output(stream, 9, [
                OutputRegion(6, b"ghi"), OutputRegion(0, b"abc"), OutputRegion(3, b"def"),
            ], jobs=3)
        assert str(caught.value.__cause__) == "region 0"
        assert sorted(finished) == [0, 3, 6]
        assert stream.tell() == 0 and not stream.closed


def test_positional_writer_does_not_read_or_materialize_existing_image(tmp_path):
    from pcc.backend.macho_parallel import write_file_output

    class WriteOnlyWrapper:
        def __init__(self, stream):
            self.stream = stream
        def fileno(self):
            return self.stream.fileno()
        def flush(self):
            return self.stream.flush()
        def read(self, *args):
            raise AssertionError("writer must not read the image")
        def seek(self, *args):
            raise AssertionError("writer must not move the cursor")
        def write(self, *args):
            raise AssertionError("writer must use positional IO")
        def close(self):
            raise AssertionError("writer must not close caller's file")

    path = tmp_path / "sparse"
    with path.open("w+b") as stream:
        write_file_output(WriteOnlyWrapper(stream), 8 * 1024 * 1024,
                          [OutputRegion(7 * 1024 * 1024, b"end")], jobs=4)
        assert stream.tell() == 0 and not stream.closed
        stream.seek(7 * 1024 * 1024 - 2)
        assert stream.read(7) == b"\0\0end\0\0"
