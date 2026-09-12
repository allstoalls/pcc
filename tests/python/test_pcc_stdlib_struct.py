"""Issue 11.C.2: ``pcc/stdlib/struct.py`` parity tests.

The port must produce byte-identical results to CPython's ``struct``
for the formats actually used by ``pcc/llvm_capi/ir.py``:

  - ``>d`` (8-byte big-endian double)
  - ``>f`` (4-byte big-endian single)
  - ``>e`` (2-byte big-endian half)
  - ``>Q`` (8-byte big-endian unsigned 64-bit int)

These are the only ``pack``/``unpack`` shapes ir.py needs. Other
format codes are out of scope (the port is intentionally minimal).
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import struct as _cpy_struct
import sys
import textwrap

import pytest

from pcc1_gate import find_current_pcc1, skip_or_fail_no_current_pcc1


REPO_ROOT = Path(__file__).resolve().parents[2]
FLOAT_BITS_SOURCE = REPO_ROOT / "pcc" / "stdlib" / "_float_bits.py"


@pytest.fixture(scope="module")
def pcc_struct():
    """Import the pcc-stdlib port directly (not via the recursive
    walker; that's tested elsewhere)."""
    import importlib
    import pcc.stdlib  # ensures the package is on sys.path
    return importlib.import_module("pcc.stdlib.struct")


@pytest.fixture(scope="module")
def native_struct():
    """Ordinary ``import struct`` provider used by compiled pcc modules."""
    import importlib

    return importlib.import_module("pcc.py_stdlib.struct")


def test_module_functions_reuse_format_plan(native_struct, monkeypatch):
    clear = getattr(native_struct, "_clearcache", None)
    if clear is not None:
        clear()
    parsed = []
    original = native_struct._parse_format

    def record(fmt):
        parsed.append(fmt)
        return original(fmt)

    monkeypatch.setattr(native_struct, "_parse_format", record)
    for _ in range(20):
        payload = native_struct.pack("<iI", -7, 42)
        assert native_struct.calcsize("<iI") == 8
        assert native_struct.unpack_from("<iI", payload) == (-7, 42)
    assert parsed == ["<iI"]


def test_format_plan_cache_is_bounded_and_clearable(native_struct):
    native_struct._clearcache()
    for width in range(1, 202):
        assert native_struct.calcsize("<" + str(width) + "s") == width
    assert len(native_struct._FORMAT_CACHE) <= 100
    native_struct._clearcache()
    assert native_struct._FORMAT_CACHE == {}


def test_native_format_cache_keeps_results_and_bounded_live_memory(
    tmp_path, pcc_py_runtime_archive,
):
    from pcc.py_frontend.pipeline import compile_python

    source = tmp_path / "struct_cache_probe.py"
    source.write_text('''
import struct
from pcc.extern import c_int64, extern
live_bytes = extern("pcc_os_heap_in_use_bytes", (), c_int64)
def scan(count: int) -> int:
    data = b"\\xf9\\xff\\xff\\xff\\x2a\\x00\\x00\\x00"
    total = 0
    index = 0
    while index < count:
        values = struct.unpack_from("<iI", data)
        total += values[0] + values[1] + struct.calcsize("<iI")
        index += 1
    return total
def main():
    scan(16)
    before = live_bytes()
    first = scan(500)
    middle = live_bytes()
    second = scan(500)
    after = live_bytes()
    print(first, second, middle - before, after - middle)
main()
''', encoding="utf-8")
    output = tmp_path / "struct_cache_probe"
    compile_python(
        str(source), str(output), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_py_runtime_archive),
    )
    for backend in range(5):
        result = subprocess.run(
            [str(output)], env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
            capture_output=True, text=True, timeout=20,
        )
        assert result.returncode == 0, f"GC{backend}: {result.stderr}"
        first, second, growth1, growth2 = map(int, result.stdout.split())
        assert first == second == 43 * 500
        if backend == 0:
            assert growth1 < 32768 and growth2 < 32768, (growth1, growth2)


@pytest.mark.parametrize(
    "fmt,values",
    [
        ("<8sHBBI", (b"PCCSMAP1", 1, 2, 8, 7)),
        ("<QQIIII", (1, 2, 3, 4, 5, 6)),
        ("<QIIIHHBBHI", (1, 2, 3, 4, 5, 6, 7, 8, 9, 10)),
        ("<BBHHhiI", (1, 2, 3, 4, -1, -7, 8)),
    ],
)
def test_native_struct_object_matches_precise_stackmap_layouts(
    native_struct,
    fmt,
    values,
):
    shape = native_struct.Struct(fmt)
    expected = _cpy_struct.Struct(fmt)
    assert shape.size == expected.size
    payload = shape.pack(*values)
    assert payload == expected.pack(*values)
    assert shape.unpack(payload) == expected.unpack(payload)
    padded = b"xx" + payload + b"yy"
    assert shape.unpack_from(padded, 2) == values


_UNPACK_PARITY_CASES = [
    ("<QQIIII", (1, 2, 3, 4, 5, 6)),
    ("<QIIIHHBBHI", (2**64 - 1, 2**32 - 1, 0, 7, 65535, 1, 255, 0, 9, 10)),
    ("<qihb?", (-(2**63), -(2**31), -32768, -128, True)),
    ("<qihb?", (2**63 - 1, 2**31 - 1, 32767, 127, False)),
    ("<qQ", (-1, 2**63)),
    ("<8sHBBI", (b"PCCSMAP1", 1, 2, 8, 7)),
    ("<3xIc2s", (5, b"z", b"ab")),
    ("<4H2q", (1, 2, 3, 4, -5, 6)),
    ("<lL", (-1, 2**32 - 1)),
    (">QIHBqihb", (2**64 - 1, 2**32 - 1, 65535, 255, -1, -2, -3, -4)),
    ("=IH", (0x01020304, 0x0506)),
    ("", ()),
]


@pytest.mark.parametrize("fmt,values", _UNPACK_PARITY_CASES)
def test_native_struct_unpack_from_matches_cpython(native_struct, fmt, values):
    """The compiled ``struct`` provider must decode exactly like CPython.

    The self-backend native-object and stack-map validators call
    ``Struct.unpack_from`` on immutable ``bytes`` payloads tens of thousands
    of times per module, so that path is the one exercised at several
    offsets, through both the module-level and ``Struct`` spellings, and on a
    ``bytearray`` copy.  Value identity includes ``bool`` for ``?`` fields.
    """
    payload = _cpy_struct.pack(fmt, *values)
    expected = _cpy_struct.unpack_from(fmt, payload, 0)
    padded = b"\xfe\xff" + payload + b"\x00\x01\x02"
    layout = native_struct.Struct(fmt)
    assert layout.size == _cpy_struct.calcsize(fmt)
    assert native_struct.unpack_from(fmt, payload, 0) == expected
    assert native_struct.unpack(fmt, payload) == expected
    assert layout.unpack_from(padded, 2) == expected
    assert layout.unpack_from(padded, -(len(payload) + 3)) == expected
    assert layout.unpack_from(bytearray(padded), 2) == expected
    assert layout.unpack_from(memoryview(padded), 2) == expected
    assert [type(item) for item in layout.unpack_from(padded, 2)] == [
        type(item) for item in expected
    ]
    if payload:
        with pytest.raises(native_struct.error):
            layout.unpack_from(payload[:-1], 0)
        with pytest.raises(native_struct.error):
            layout.unpack_from(padded, len(padded) - len(payload) + 1)
    with pytest.raises(native_struct.error):
        layout.unpack_from(padded, len(padded) + 1)


def test_native_struct_unpack_from_reads_every_offset_of_a_large_payload(
    native_struct,
):
    """Sweep a multi-record payload the way the stack-map validator does."""
    record = _cpy_struct.Struct("<QIIIHHBBHI")
    rows = [
        (index * 7919, index & 0xFFFFFFFF, 2**32 - 1 - index, 3, index & 0xFFFF,
         0xFFFF - (index & 0xFFFF), index & 0xFF, 0xFF - (index & 0xFF),
         (index * 3) & 0xFFFF, index * 5)
        for index in range(257)
    ]
    payload = b"".join(record.pack(*row) for row in rows)
    layout = native_struct.Struct("<QIIIHHBBHI")
    offset = 0
    for row in rows:
        assert layout.unpack_from(payload, offset) == row
        offset += layout.size
    assert offset == len(payload)


def test_unpack_from_does_not_copy_an_entire_mutable_buffer(native_struct):
    import tracemalloc

    payload = bytearray(2 * 1024 * 1024)
    shape = native_struct.Struct("<I")
    tracemalloc.start()
    try:
        for offset in range(64):
            assert shape.unpack_from(payload, offset) == (0,)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 65536, peak


def test_compiled_mutable_unpack_preserves_bytes_fields(tmp_path, pcc_py_runtime_archive):
    from pcc.py_frontend.pipeline import compile_python

    source = tmp_path / "mutable_struct.py"
    source.write_text('''
import struct
def main():
    data = bytearray(b"\\x01\\x00\\x00\\x00xyZ" + b"\\x00" * 65536)
    layout = struct.Struct("<I2sc")
    for index in range(32):
        values = layout.unpack_from(data, 0)
    print(values[0], values[1], values[2])
    print(isinstance(values[1], bytes), isinstance(values[2], bytes))
    data[4] = 97
    print(layout.unpack_from(data, 0)[1], values[1])
main()
''', encoding="utf-8")
    output = tmp_path / "mutable_struct"
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   runtime_archive=str(pcc_py_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f"GC{gc}: {result.stderr}"
        assert result.stdout == "1 b'xy' b'Z'\nTrue True\nb'ay' b'xy'\n"


def test_native_struct_pack_into_matches_cpython(native_struct):
    fmt = "<IIQ"
    expected = bytearray(b"_" * 24)
    actual = bytearray(expected)
    _cpy_struct.pack_into(fmt, expected, 3, 4, 5, 6)
    native_struct.pack_into(fmt, actual, 3, 4, 5, 6)
    assert actual == expected


def test_float_bits_negative_zero_does_not_depend_on_string_rendering():
    source = FLOAT_BITS_SOURCE.read_text(encoding="utf-8")
    assert "str(f)" not in source
    assert "repr(f)" not in source
    assert 'float("' not in source


# ---------------------------------------------------------------------------
# pack: float / double / half  (round-trip via unpack to verify bytes)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fmt,value", [
    (">d", 0.0),
    (">d", 1.0),
    (">d", -1.0),
    (">d", 3.141592653589793),
    (">d", 11.1),
    (">d", 1.5e308),
    (">f", 0.0),
    (">f", 1.0),
    (">f", -1.0),
    (">f", 11.1),
    (">f", 3.141592653589793),
    (">e", 0.0),
    (">e", 1.0),
    (">e", -1.0),
    (">e", 0.5),
    (">d", 2.0 ** -1074),
    (">d", -(2.0 ** -1074)),
    (">d", float.fromhex("0x0.fffffffffffffp-1022")),
    (">f", 2.0 ** -149),
    (">f", -(2.0 ** -149)),
    (">f", (2.0 ** -126) - (2.0 ** -149)),
    (">f", (2.0 ** -126) - (2.0 ** -150)),
    (">e", 2.0 ** -24),
    (">e", -(2.0 ** -24)),
    (">e", (2.0 ** -14) - (2.0 ** -24)),
    (">e", (2.0 ** -14) - (2.0 ** -25)),
])
def test_pack_matches_cpython(pcc_struct, fmt, value):
    expected = _cpy_struct.pack(fmt, value)
    actual = pcc_struct.pack(fmt, value)
    assert actual == expected, (
        f"pcc_struct.pack({fmt!r}, {value}) → {actual.hex()}, "
        f"cpython → {expected.hex()}"
    )


# ---------------------------------------------------------------------------
# unpack: round-trip + integer formats
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fmt,value", [
    (">d", 1.5707963267948966),
    (">f", 1.5707963267948966),
    (">e", 1.0),
])
def test_pack_then_unpack_round_trips(pcc_struct, fmt, value):
    """Pack via pcc_struct then unpack via pcc_struct gives the same
    rounded value as CPython's round-trip."""
    expected_bytes = _cpy_struct.pack(fmt, value)
    expected_unpacked = _cpy_struct.unpack(fmt, expected_bytes)[0]
    actual_bytes = pcc_struct.pack(fmt, value)
    actual_unpacked = pcc_struct.unpack(fmt, actual_bytes)[0]
    if expected_unpacked != expected_unpacked:  # NaN
        assert actual_unpacked != actual_unpacked
    else:
        assert actual_unpacked == expected_unpacked


@pytest.mark.parametrize(
    "fmt,value",
    [
        (">f", 2.0 ** -150),
        (">f", 3.0 * (2.0 ** -150)),
        (">f", (2.0 ** -150) + (2.0 ** -151)),
        (">e", 2.0 ** -25),
        (">e", 3.0 * (2.0 ** -25)),
        (">e", (2.0 ** -25) + (2.0 ** -26)),
    ],
)
def test_subnormal_halfway_cases_round_to_even(pcc_struct, fmt, value):
    expected = _cpy_struct.pack(fmt, value)
    actual = pcc_struct.pack(fmt, value)
    assert actual == expected
    assert pcc_struct.unpack(fmt, actual) == _cpy_struct.unpack(fmt, expected)


@pytest.mark.parametrize("fmt", [">d", ">f", ">e"])
def test_negative_zero_preserves_its_sign_bit(pcc_struct, fmt):
    assert pcc_struct.pack(fmt, -0.0) == _cpy_struct.pack(fmt, -0.0)


@pytest.mark.parametrize("fmt,value", [(">f", 1e40), (">e", 1e10)])
def test_finite_overflow_matches_cpython(pcc_struct, fmt, value):
    with pytest.raises(OverflowError) as expected:
        _cpy_struct.pack(fmt, value)
    with pytest.raises(OverflowError) as actual:
        pcc_struct.pack(fmt, value)
    assert str(actual.value) == str(expected.value)


@pytest.mark.parametrize("fmt", [">d", ">f", ">e"])
@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_nonfinite_pack_class_matches_cpython(pcc_struct, fmt, value):
    expected = _cpy_struct.pack(fmt, value)
    actual = pcc_struct.pack(fmt, value)
    if value == value:
        assert actual == expected
    else:
        payload_and_exponent = int.from_bytes(actual, "big") & (
            (1 << (8 * len(actual) - 1)) - 1
        )
        assert payload_and_exponent != 0


def test_every_binary16_finite_pattern_round_trips_exactly(pcc_struct):
    for raw_bits in range(1 << 16):
        raw = raw_bits.to_bytes(2, "big")
        expected = _cpy_struct.unpack(">e", raw)[0]
        actual = pcc_struct.unpack(">e", raw)[0]
        if expected != expected:
            assert actual != actual
            continue
        assert pcc_struct.pack(">d", actual) == _cpy_struct.pack(">d", expected)
        assert pcc_struct.pack(">e", expected) == raw


@pytest.mark.parametrize(
    "fmt,raw_bits,width",
    [
        (">d", 0x0000000000000001, 8),
        (">d", 0x000FFFFFFFFFFFFF, 8),
        (">d", 0x8000000000000001, 8),
        (">f", 0x00000001, 4),
        (">f", 0x007FFFFF, 4),
        (">f", 0x80000001, 4),
    ],
)
def test_binary32_and_binary64_subnormal_decode_matches_cpython(
    pcc_struct,
    fmt,
    raw_bits,
    width,
):
    raw = raw_bits.to_bytes(width, "big")
    expected = _cpy_struct.unpack(fmt, raw)[0]
    actual = pcc_struct.unpack(fmt, raw)[0]
    assert pcc_struct.pack(">d", actual) == _cpy_struct.pack(">d", expected)


@pytest.mark.parametrize("value", [0, 1, 42, 0xFF, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF])
def test_pack_unpack_Q_uint64(pcc_struct, value):
    """``>Q`` must produce the exact 8-byte big-endian uint64."""
    expected = _cpy_struct.pack(">Q", value)
    actual = pcc_struct.pack(">Q", value)
    assert actual == expected, f"{value:#x}: {actual.hex()} vs {expected.hex()}"

    unpacked = pcc_struct.unpack(">Q", expected)
    assert unpacked == (value,)


# ---------------------------------------------------------------------------
# Critical compatibility test: ir.py's specific usage pattern
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [
    0.0, 1.0, -1.0, 11.1, 3.141592653589793, 1e-10, 1e10,
])
def test_ir_py_double_bits_pattern(pcc_struct, value):
    """The exact pattern used by ir.py's Constant.__init__:
    ``unpack('>Q', pack('>d', f))[0]`` to extract the 64-bit IEEE 754
    double bit pattern."""
    expected = _cpy_struct.unpack(">Q", _cpy_struct.pack(">d", value))[0]
    actual = pcc_struct.unpack(">Q", pcc_struct.pack(">d", value))[0]
    assert actual == expected, (
        f"value={value}: pcc=0x{actual:016X}, cpython=0x{expected:016X}"
    )


@pytest.mark.parametrize("value", [
    1.0, 11.1, 3.141592653589793,
])
def test_ir_py_float32_round_pattern(pcc_struct, value):
    """ir.py's float32-rounding pattern:
    ``unpack('>f', pack('>f', f))[0]``."""
    expected = _cpy_struct.unpack(">f", _cpy_struct.pack(">f", value))[0]
    actual = pcc_struct.unpack(">f", pcc_struct.pack(">f", value))[0]
    assert actual == expected


_UNPACK_PARITY_PROGRAM = '''
import struct


def show(fmt: str, payload: bytes, offset: int) -> None:
    values = struct.unpack_from(fmt, payload, offset)
    print(fmt, len(values))
    for item in values:
        if isinstance(item, bytes):
            print("bytes", item.hex())
        else:
            print(item)


def main() -> None:
    show("<QQIIII", struct.pack("<QQIIII", 1, 2, 3, 4, 5, 6), 0)
    show("<QQIIII", b"\\x01\\x02" + struct.pack("<QQIIII", 1, 2, 3, 4, 5, 6), 2)
    show("<QIIIHHBBHI", struct.pack("<QIIIHHBBHI", 18446744073709551615, 4294967295, 0, 7, 65535, 1, 255, 0, 9, 10), 0)
    show("<qihb?", struct.pack("<qihb?", -9223372036854775808, -2147483648, -32768, -128, True), 0)
    show("<qihb?", struct.pack("<qihb?", 9223372036854775807, 2147483647, 32767, 127, False), 0)
    show("<qQ", struct.pack("<qQ", -1, 9223372036854775808), 0)
    show("<8sHBBI", struct.pack("<8sHBBI", b"PCCSMAP1", 1, 2, 8, 7), 0)
    show("<3xIc2s", struct.pack("<3xIc2s", 5, b"z", b"ab"), 0)
    show("<4H2q", struct.pack("<4H2q", 1, 2, 3, 4, -5, 6), 0)
    show("<lL", struct.pack("<lL", -1, 4294967295), 0)
    show(">QIHBqihb", struct.pack(">QIHBqihb", 18446744073709551615, 4294967295, 65535, 255, -1, -2, -3, -4), 0)
    show("=IH", struct.pack("=IH", 16909060, 1286), 0)
    layout = struct.Struct("<QIIIHHBBHI")
    payload = b"".join(layout.pack(i * 7919, i, 4294967295 - i, 3, i, 65535 - i, i & 255, 255 - (i & 255), (i * 3) & 65535, i * 5) for i in range(64))
    total = 0
    offset = 0
    while offset < len(payload):
        row = layout.unpack_from(payload, offset)
        total = total + row[0] + row[1] + row[2] + row[4] + row[6] + row[9]
        offset = offset + layout.size
    print("sweep", total)
    try:
        layout.unpack_from(payload, len(payload) - 3)
    except struct.error:
        print("truncated-rejected")


main()
'''.lstrip()


@pytest.mark.integration
def test_host_pcc_and_current_pcc1_unpack_from_matches_cpython_without_libpython(
    tmp_path: Path,
    pcc_py_runtime_archive: Path,
):
    """The compiled provider's in-place payload reads must equal CPython.

    Covers every owned integer width in both signednesses, the unsigned
    64-bit high-bit lift, pad/bytes/char fields, offsets, the big-endian
    generic path, a multi-record sweep like the stack-map validator and the
    truncation error, under host pcc and the current pcc1 with no libpython.
    """
    pcc1 = find_current_pcc1(REPO_ROOT)
    if pcc1 is None:
        skip_or_fail_no_current_pcc1(
            "no current pcc1 for struct unpack_from parity"
        )
        return

    source = tmp_path / "struct_unpack_parity.py"
    source.write_text(_UNPACK_PARITY_PROGRAM, encoding="utf-8")
    expected = subprocess.run(
        [sys.executable, str(source)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    ).stdout
    assert expected.count("\n") == 74
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_RUNTIME_ARCHIVE"] = str(pcc_py_runtime_archive)
    for label, compiler in (
        ("host-pcc", ["uv", "run", "pcc"]),
        ("current-pcc1", [str(pcc1)]),
    ):
        executable = tmp_path / ("struct_unpack_parity_" + label)
        compiled = subprocess.run(
            compiler
            + [
                "--backend=self",
                "--python-libpython=off",
                "--ir-scaffold=on",
                str(source),
                "-o",
                str(executable),
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            timeout=420,
            env=env,
        )
        assert compiled.returncode == 0, (
            label + " compile failed: " + compiled.stdout + compiled.stderr
        )
        actual = subprocess.run(
            [str(executable)],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert actual.returncode == 0, label + ": " + actual.stderr
        assert actual.stdout == expected, label


@pytest.mark.integration
def test_host_pcc_and_current_pcc1_pack_subnormals_without_libpython(
    tmp_path: Path,
    pcc_py_runtime_archive: Path,
):
    pcc1 = find_current_pcc1(REPO_ROOT)
    if pcc1 is None:
        skip_or_fail_no_current_pcc1(
            "no current pcc1 for IEEE subnormal struct parity"
        )
        return

    source = tmp_path / "struct_subnormal.py"
    source.write_text(
        textwrap.dedent(
            """
            import struct

            def power2_negative(count: int) -> float:
                value = 1.0
                index = 0
                while index < count:
                    value = value * 0.5
                    index = index + 1
                return value

            print(struct.pack(">d", power2_negative(1074)))
            print(struct.pack(">d", -0.0))
            print(struct.pack(">f", power2_negative(149)))
            print(struct.pack(">e", power2_negative(24)))
            """
        ).lstrip(),
        encoding="utf-8",
    )
    expected = subprocess.run(
        [sys.executable, str(source)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    ).stdout
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_RUNTIME_ARCHIVE"] = str(pcc_py_runtime_archive)
    for label, compiler in (
        ("host-pcc", ["uv", "run", "pcc"]),
        ("current-pcc1", [str(pcc1)]),
    ):
        executable = tmp_path / ("struct_subnormal_" + label)
        compiled = subprocess.run(
            compiler
            + [
                "--backend=self",
                "--python-libpython=off",
                "--ir-scaffold=on",
                str(source),
                "-o",
                str(executable),
            ],
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=180,
        )
        assert compiled.returncode == 0, (
            label + ": " + compiled.stdout + compiled.stderr
        )
        run = subprocess.run(
            [str(executable)],
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert run.returncode == 0, label + ": " + run.stdout + run.stderr
        assert run.stdout == expected, label
