"""Execute the production stack-map scanners through host and native pcc."""

import os
from pathlib import Path
import subprocess
import sys

from pcc.backend import precise_stackmap as wire


def test_native_stackmap_structural_scan(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    # Compile the actual module in an isolated ordinary package context.
    (tmp_path / "wiremap.py").write_bytes(Path(wire.__file__).read_bytes())
    locations = tuple(wire.StackMapLocation(
        kind=wire.LOCATION_STACK_INDIRECT,
        flags=wire.LOCATION_MANAGED | wire.LOCATION_OWNED,
        register=29, base_index=wire.NO_BASE, offset=-8 * (index + 1),
    ) for index in range(257))
    value = wire.PreciseStackMap(wire.ARCH_AARCH64, (
        wire.FunctionStackMap(7, 0, 16, 4096, (
            wire.SafepointRecord((1 << 64) - 1, 4, wire.SAFEPOINT_ENTRY, locations),
        )),
    ))
    data = tmp_path / "payload.bin"
    data.write_bytes(wire.encode_stack_map(value))
    source = tmp_path / "probe.py"
    source.write_text(f'''
from wiremap import (function_address_offsets, _scan_stack_map_payload,
                     validate_stack_map_payload, PreciseStackMapError,
                     HEADER_SIZE, FUNCTION_SIZE)

def main():
    with open({str(data)!r}, "rb") as stream:
        payload = stream.read()
    print(function_address_offsets(payload))
    print(_scan_stack_map_payload(payload))
    print(function_address_offsets(bytearray(payload)))
    for cut in (0, 1, 31):
        short = payload[:HEADER_SIZE + FUNCTION_SIZE + cut]
        try:
            function_address_offsets(short)
        except PreciseStackMapError as error:
            print(str(error))
        try:
            _scan_stack_map_payload(short)
        except PreciseStackMapError as error:
            print(str(error))
    malformed = bytearray(payload)
    malformed[HEADER_SIZE + FUNCTION_SIZE + 22] = 1
    try:
        validate_stack_map_payload(bytes(malformed))
    except PreciseStackMapError as error:
        print(str(error))

main()
''', encoding="utf-8")
    expected = subprocess.run(
        [sys.executable, str(source)], capture_output=True, text=True, timeout=10,
    )
    assert expected.returncode == 0, expected.stderr
    output = tmp_path / "probe"
    python_program_compiler(
        str(source), str(output), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for gc in range(5):
        ran = subprocess.run(
            [str(output)], capture_output=True, text=True, timeout=10,
            env=dict(os.environ, PCC_GC_BACKEND=str(gc)),
        )
        assert ran.returncode == 0, f"GC{gc}: {ran.stdout}{ran.stderr}"
        assert ran.stdout == expected.stdout, f"GC{gc}: {ran.stdout}"
