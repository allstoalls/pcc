"""Execute stack-map validation reuse through host and native pcc.

The production module is compiled unchanged; the probe validates a relocatable
table, then the linked table (reusing that verdict), then a linked table with
an unresolved address, and prints each outcome with whether the verdict was
remembered. Native output under every collector must equal CPython's.
"""

import os
from pathlib import Path
import struct
import subprocess
import sys

from pcc.backend import precise_stackmap as wire


def test_native_stackmap_validation_reuse(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    (tmp_path / "wiremap.py").write_bytes(Path(wire.__file__).read_bytes())
    base = wire.StackMapLocation(
        kind=wire.LOCATION_STACK_INDIRECT,
        flags=wire.LOCATION_MANAGED | wire.LOCATION_OWNED,
        register=29, base_index=wire.NO_BASE, offset=-8,
    )
    functions = tuple(sorted((
        wire.FunctionStackMap(wire.function_id(symbol), 0, 32, 32, (
            wire.SafepointRecord(
                wire.safepoint_id(symbol, 0, wire.SAFEPOINT_ENTRY), 4,
                wire.SAFEPOINT_ENTRY, (base,),
            ),
            wire.SafepointRecord(
                wire.safepoint_id(symbol, 1, wire.SAFEPOINT_CALL), 16,
                wire.SAFEPOINT_CALL, (base,),
            ),
        ))
        for symbol in ("_native_reuse_a", "_native_reuse_b")
    ), key=lambda function: function.function_id))
    relocatable = wire.encode_stack_map(wire.PreciseStackMap(wire.ARCH_AARCH64, functions))
    linked = bytearray(relocatable)
    offsets = wire.function_address_offsets(relocatable)
    for index, offset in enumerate(offsets):
        struct.pack_into("<Q", linked, offset, 0x401000 + 0x40 * index)
    unresolved = bytearray(linked)
    struct.pack_into("<Q", unresolved, offsets[-1], 0)
    corrupted = bytearray(linked)
    corrupted[len(corrupted) - 16] = 0x7F  # last location's kind
    for name, payload in (("relocatable", relocatable), ("linked", linked),
                          ("unresolved", unresolved), ("corrupted", corrupted)):
        (tmp_path / (name + ".bin")).write_bytes(bytes(payload))
    source = tmp_path / "probe.py"
    source.write_text(f'''
from wiremap import (validate_stack_map_payload, PreciseStackMapError, ARCH_AARCH64,
                     _VALIDATED_ADDRESS_FREE_TABLES, _address_free_table_key)

def load(name):
    with open({str(tmp_path)!r} + "/" + name + ".bin", "rb") as stream:
        return stream.read()

def check(label, payload, final_image):
    try:
        validate_stack_map_payload(payload, expected_arch=ARCH_AARCH64, final_image=final_image)
        print(label, "ok")
    except PreciseStackMapError as error:
        print(label, "error", str(error))

def main():
    relocatable = load("relocatable")
    linked = load("linked")
    unresolved = load("unresolved")
    corrupted = load("corrupted")
    print("remembered before", _address_free_table_key(linked, ARCH_AARCH64) in _VALIDATED_ADDRESS_FREE_TABLES)
    check("relocatable", relocatable, False)
    print("remembered after", _address_free_table_key(linked, ARCH_AARCH64) in _VALIDATED_ADDRESS_FREE_TABLES)
    print("tables", len(_VALIDATED_ADDRESS_FREE_TABLES))
    check("linked final", linked, True)
    check("linked relocatable", linked, False)
    print("unresolved remembered", _address_free_table_key(unresolved, ARCH_AARCH64) in _VALIDATED_ADDRESS_FREE_TABLES)
    check("unresolved final", unresolved, True)
    print("corrupted remembered", _address_free_table_key(corrupted, ARCH_AARCH64) in _VALIDATED_ADDRESS_FREE_TABLES)
    check("corrupted final", corrupted, True)
    print("tables", len(_VALIDATED_ADDRESS_FREE_TABLES))

main()
''', encoding="utf-8")
    expected = subprocess.run(
        [sys.executable, str(source)], capture_output=True, text=True, timeout=10,
    )
    assert expected.returncode == 0, expected.stderr
    assert expected.stdout.splitlines() == [
        "remembered before False",
        "relocatable ok",
        "remembered after True",
        "tables 1",
        "linked final ok",
        "linked relocatable ok",
        "unresolved remembered True",
        "unresolved final error final-image function address is unresolved",
        "corrupted remembered False",
        "corrupted final error unknown location kind 127",
        "tables 1",
    ]
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
