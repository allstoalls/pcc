"""Host-only PE import-table controls for both qualification receipt readers.

The byte fixtures contain headers and import descriptors, not runnable code.
No compiler, linker, runtime build, target loader, or subprocess is involved.
"""

import struct

import pytest

from scripts import bootstrap_platform as bootstrap
from scripts import ci_native_object_regressions as native
from scripts.verify_nolibpython import linked_libraries


EXPECTED_SYSTEM_DLLS = {
    "advapi32.dll", "bcrypt.dll", "kernel32.dll", "ntdll.dll", "psapi.dll",
    "shell32.dll", "ws2_32.dll",
}
# Exact DLL list in the Windows 2c464414 Stage1 failure receipt. The private
# temporary-directory DACL introduces Advapi32's security-descriptor parser.
STAGE1_DLLS = (
    "ADVAPI32.dll", "BCRYPT.dll", "KERNEL32.dll", "NTDLL.dll",
    "SHELL32.dll", "WS2_32.dll",
)


def _pe_import_table(libraries):
    pe = 128
    optional = pe + 24
    section = optional + 240
    raw = 512
    rva = 4096
    names = [name.encode("ascii") + b"\0" for name in libraries]
    descriptors_size = 20 * (len(names) + 1)
    payload_size = descriptors_size + sum(map(len, names))
    data = bytearray(raw + payload_size)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 60, pe)
    data[pe:pe + 4] = b"PE\0\0"
    struct.pack_into("<HH", data, pe + 4, 0x8664, 1)
    struct.pack_into("<H", data, pe + 20, 240)
    struct.pack_into("<H", data, optional, 0x20B)
    struct.pack_into("<I", data, optional + 108, 16)
    struct.pack_into("<II", data, optional + 120,
                     rva if names else 0, descriptors_size if names else 0)
    data[section:section + 8] = b".idata\0\0"
    struct.pack_into("<IIII", data, section + 8, payload_size, rva, payload_size, raw)
    cursor = descriptors_size
    for index, name in enumerate(names):
        struct.pack_into("<IIIII", data, raw + index * 20, 0, 0, 0, rva + cursor, 0)
        data[raw + cursor:raw + cursor + len(name)] = name
        cursor += len(name)
    return bytes(data)


def _image(tmp_path, libraries):
    path = tmp_path / "import-table-only.exe"
    payload = _pe_import_table(libraries)
    path.write_bytes(payload)
    assert linked_libraries(payload) == [name.lower() for name in libraries]
    return path


@pytest.mark.parametrize("reader", ("bootstrap", "native-object"))
def test_actual_stage1_dll_set_is_admitted(tmp_path, reader):
    path = _image(tmp_path, STAGE1_DLLS)
    expected = sorted(name.lower() for name in STAGE1_DLLS)
    if reader == "bootstrap":
        receipt = bootstrap.dependency_receipt(path)
        assert receipt["system_dlls"] == expected
        assert receipt["imports_only_system_dlls"] is True
        assert "does not identify statically linked source ownership" in receipt["proof_scope"]
    else:
        assert native.verify_native_image(path) == {"format": "pe", "libraries": expected}


@pytest.mark.parametrize("library", sorted(EXPECTED_SYSTEM_DLLS))
def test_each_explicit_system_dll_remains_admitted(tmp_path, library):
    path = _image(tmp_path, (library.upper(),))
    assert bootstrap.dependency_receipt(path)["system_dlls"] == [library]
    assert native.verify_native_image(path) == {"format": "pe", "libraries": [library]}


@pytest.mark.parametrize("library", (
    "python313.dll", "python3.dll", "libpython3.13.dll", "libLLVM.dll",
    "msvcrt.dll", "ucrtbase.dll", "vcruntime140.dll",
    "api-ms-win-crt-runtime-l1-1-0.dll", "custom.dll", "advapi32.dll.evil",
    r"C:\Windows\System32\advapi32.dll", r"..\advapi32.dll",
))
def test_extra_dll_cannot_hide_behind_valid_windows_imports(tmp_path, library):
    path = _image(tmp_path, (*STAGE1_DLLS, library))
    with pytest.raises(RuntimeError, match="non-system compiler dependencies"):
        bootstrap.dependency_receipt(path)
    with pytest.raises(AssertionError):
        native.verify_native_image(path)


def test_no_imports_keeps_existing_reader_contracts(tmp_path):
    path = _image(tmp_path, ())
    assert bootstrap.dependency_receipt(path)["system_dlls"] == []
    with pytest.raises(AssertionError):
        native.verify_native_image(path)


def test_finite_named_abi_policy_does_not_admit_arbitrary_system_symbols():
    from pcc.backend.coff_x86_64 import CoffError
    from pcc.backend.pe_x86_64 import system_dll

    assert native.WINDOWS_SYSTEM_LIBRARIES == EXPECTED_SYSTEM_DLLS
    assert system_dll("ConvertStringSecurityDescriptorToSecurityDescriptorW") == "ADVAPI32.dll"
    assert system_dll("NtCreateFile") == "NTDLL.dll"
    assert system_dll("RtlNtStatusToDosError") == "NTDLL.dll"
    with pytest.raises(CoffError, match="no named system DLL ABI"):
        system_dll("ConvertStringSecurityDescriptorToSecurityDescriptorA")
