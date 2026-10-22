"""Deterministic PE32+ executable linking over owned AMD64 COFF objects.

System imports are named DLL ABI entries. Import libraries, CRT startup,
external linkers and host interpreters do not participate in this boundary.
"""

import struct

from .coff_x86_64 import (
    CoffError, CoffObject, CoffSection, CoffSymbol, ADDR64, ADDR32, ADDR32NB,
    REL32, SECREL, SECTION, parse_object,
)
from .elf_x86_64 import read_archive_payloads

_KERNEL32 = frozenset("""
FormatMessageW ExitProcess GetCurrentProcess GetCurrentProcessId GetCurrentThreadId GetProcessHandleCount GetHandleInformation
VirtualAlloc VirtualFree VirtualProtect GetSystemInfo GetNativeSystemInfo
GetProcessHeap HeapAlloc HeapReAlloc HeapFree GetLastError SetLastError
GetStdHandle SetStdHandle ReadFile WriteFile CloseHandle CreateFileW LockFileEx UnlockFileEx
DeleteFileW MoveFileExW CreateDirectoryW RemoveDirectoryW GetFileAttributesW
SetFileAttributesW SetFileTime GetFileInformationByHandle GetFileInformationByHandleEx SetFileInformationByHandle GetFileSizeEx SetFilePointerEx
SetEndOfFile FlushFileBuffers GetCurrentDirectoryW SetCurrentDirectoryW
GetFullPathNameW GetFinalPathNameByHandleW GetTempPathW GetTempFileNameW
FindFirstFileW FindNextFileW FindClose GetModuleFileNameW GetModuleHandleW
LoadLibraryW FreeLibrary GetProcAddress GetCommandLineW LocalFree
GetEnvironmentStringsW FreeEnvironmentStringsW GetEnvironmentVariableW
SetEnvironmentVariableW MultiByteToWideChar WideCharToMultiByte
CompareStringOrdinal
CreateProcessW CreatePipe SetHandleInformation DuplicateHandle
InitializeProcThreadAttributeList UpdateProcThreadAttribute DeleteProcThreadAttributeList
WaitForSingleObject WaitForMultipleObjects GetExitCodeProcess TerminateProcess
CreateJobObjectW AssignProcessToJobObject SetInformationJobObject
TerminateJobObject CreateThread ResumeThread GetExitCodeThread Sleep
TlsAlloc TlsFree TlsGetValue TlsSetValue InitializeCriticalSection
DeleteCriticalSection EnterCriticalSection LeaveCriticalSection
InitializeSRWLock AcquireSRWLockExclusive TryAcquireSRWLockExclusive ReleaseSRWLockExclusive
AcquireSRWLockShared ReleaseSRWLockShared InitializeConditionVariable
SleepConditionVariableSRW WakeConditionVariable WakeAllConditionVariable
WaitOnAddress WakeByAddressSingle WakeByAddressAll
QueryPerformanceCounter QueryPerformanceFrequency GetSystemTimeAsFileTime
GetSystemTimePreciseAsFileTime GetTickCount64 GetProcessTimes GetThreadTimes
GetConsoleMode SetConsoleMode GetConsoleScreenBufferInfo WriteConsoleW SetConsoleCtrlHandler OpenProcess
SetUnhandledExceptionFilter GetCurrentThreadStackLimits
GetActiveProcessorCount GetProcessAffinityMask GetLogicalProcessorInformationEx
CreateEventW SetEvent ResetEvent CreateSemaphoreW ReleaseSemaphore
GetDynamicTimeZoneInformation SystemTimeToTzSpecificLocalTimeEx GetFileType GetFileAttributesExW GetTimeZoneInformation DeviceIoControl GetComputerNameW PeekNamedPipe
""".split())
_WS2 = frozenset("""WSAStartup WSACleanup WSAGetLastError socket closesocket
connect bind listen accept shutdown send recv sendto recvfrom getsockopt
setsockopt getsockname getpeername ioctlsocket WSAPoll getaddrinfo freeaddrinfo
""".split())


def system_dll(symbol: str) -> str:
    if symbol in _KERNEL32:
        return "KERNEL32.dll"
    if symbol in _WS2:
        return "WS2_32.dll"
    if symbol in ("RtlGetVersion", "NtCreateFile", "NtSetInformationFile", "RtlNtStatusToDosError"):
        return "NTDLL.dll"
    if symbol == "ConvertStringSecurityDescriptorToSecurityDescriptorW":
        return "ADVAPI32.dll"
    if symbol == "CommandLineToArgvW":
        return "SHELL32.dll"
    if symbol in ("GetProcessMemoryInfo", "K32GetProcessMemoryInfo"):
        return "PSAPI.dll" if symbol == "GetProcessMemoryInfo" else "KERNEL32.dll"
    if symbol in ("BCryptGenRandom",):
        return "BCRYPT.dll"
    raise CoffError("undefined Windows symbol has no named system DLL ABI: " + symbol)


def _align(value, alignment):
    return (value + alignment - 1) & -alignment


def _stack_map_size_counts(payload):
    """Read only the header of an already validated merged v2 stack map.

    Fixed-width function and location-table entries partition its byte length;
    the remainder is exactly the safepoint records. No body scan, slice or
    decoded map is needed. Impossible metadata leaves the diagnostic unknown
    rather than replacing the image-size guard with another exception.
    """
    from .precise_stackmap import (
        ARCH_X86_64, FUNCTION_SIZE, HEADER_SIZE, LOCATION_SIZE, MAGIC,
        MAX_FUNCTIONS, MAX_LOCATIONS, POINTER_SIZE, RECORD_SIZE, _HEADER,
    )

    if not isinstance(payload, bytes) or len(payload) < HEADER_SIZE:
        return ()
    magic, version, arch, pointer_size, functions, locations, reserved = (
        _HEADER.unpack_from(payload, 0)
    )
    if (magic != MAGIC or version != 2 or arch != ARCH_X86_64
            or pointer_size != POINTER_SIZE or reserved
            or functions > MAX_FUNCTIONS or locations > MAX_LOCATIONS):
        return ()
    record_bytes = (len(payload) - HEADER_SIZE - functions * FUNCTION_SIZE
                    - locations * LOCATION_SIZE)
    if record_bytes < 0 or record_bytes % RECORD_SIZE:
        return ()
    records = record_bytes // RECORD_SIZE
    # Each validated function carries one uint32 record count. Do not impose
    # MAX_RECORDS here: a budgeted link can validly exceed that legacy cap.
    if records > functions * 0xFFFFFFFF:
        return ()
    return functions, records, locations


def _check_image_size(size, sections=(), *, stack_map_bytes=0,
                      stack_map_counts=()):
    # Microsoft documents a 2 GiB PE32+ image maximum without specifying
    # equality behavior. Reject sizes above it; passing this range check
    # is not a claim that a Windows loader has qualified the image.
    # https://learn.microsoft.com/windows/win32/debug/pe-format
    if size <= 0x80000000:
        return
    valid_counts = isinstance(stack_map_counts, tuple) and len(stack_map_counts) == 3
    if valid_counts:
        for count in stack_map_counts:
            if type(count) is not int or count < 0:
                valid_counts = False
                break
    partition = "unavailable"
    if stack_map_bytes == 0:
        partition = "absent"
    elif valid_counts:
        from .precise_stackmap import (
            FUNCTION_SIZE, HEADER_SIZE, LOCATION_SIZE, MAX_FUNCTIONS,
            MAX_LOCATIONS, RECORD_SIZE,
        )

        functions, records, locations = stack_map_counts
        function_bytes = functions * FUNCTION_SIZE
        record_bytes = records * RECORD_SIZE
        location_bytes = locations * LOCATION_SIZE
        if (functions <= MAX_FUNCTIONS and locations <= MAX_LOCATIONS
                and records <= functions * 0xFFFFFFFF
                and HEADER_SIZE + function_bytes + record_bytes + location_bytes
                == stack_map_bytes):
            partition = (
                "functions=" + str(functions) + " records=" + str(records)
                + " location_entries=" + str(locations)
                + " header_bytes=" + str(HEADER_SIZE)
                + " function_bytes=" + str(function_bytes)
                + " record_bytes=" + str(record_bytes)
                + " location_table_bytes=" + str(location_bytes)
            )
    layout = []
    for name, address, payload, _flags in sections:
        layout.append(name + "@" + hex(address) + " bytes=" + str(len(payload)))
    raise CoffError(
        "PE32+ image size " + str(size) + " bytes (" + hex(size)
        + ") exceeds documented 2 GiB limit; merged_stack_map_bytes="
        + str(stack_map_bytes) + "; merged_stack_map_partition=" + partition
        + "; sections " + ", ".join(layout)
    )


def _add_symbols(obj, oi, defined, needed):
    """Register one selected object's symbols in input order."""
    for si, symbol in enumerate(obj.symbols):
        if not symbol.external:
            continue
        if symbol.section:
            if symbol.name in defined:
                raise CoffError("duplicate COFF symbol: " + symbol.name)
            defined[symbol.name] = (oi, si)
        else:
            needed.add(symbol.name)


def _symbols(objects):
    defined = {}
    needed = set()
    for oi, obj in enumerate(objects):
        _add_symbols(obj, oi, defined, needed)
    return defined, needed - set(defined)


def _append(payload: bytearray, data, compact_budget) -> bytearray:
    # Native growth replaces the buffer. Keep the result in an owned local,
    # and let each caller rebind it before publishing the completed payload.
    result = payload
    if compact_budget is None:
        if type(data) is bytes or type(data) is bytearray:
            if not data:
                return result
        result.extend(data)
    else:
        compact_budget.extend(result, data)
    return result


def _zeros(payload: bytearray, count, compact_budget) -> bytearray:
    result = payload
    if compact_budget is None:
        if type(count) is int and count == 0:
            return result
        result.extend(b"\0" * count)
    else:
        compact_budget.zeros(result, count)
    return result


class _Pieces:
    """Bytes appended in order, then finished into one bytearray.

    pcc's native bytearray copies its whole storage on every extend, so a
    section grown one input at a time took quadratic time in pcc1. Without a
    host budget the pieces are kept and joined once. Under one (CPython on
    Windows) every append still grows the budgeted bytearray, as before.
    """

    def __init__(self, compact_budget):
        self.compact_budget = compact_budget
        self.payload = bytearray()
        self.pieces = [] if compact_budget is None else None
        self.size = 0

    def append(self, data) -> None:
        if self.pieces is None:
            self.payload = _append(self.payload, data, self.compact_budget)
        elif data:
            self.pieces.append(data)
        self.size += len(data)

    def zeros(self, count) -> None:
        if self.pieces is None:
            self.payload = _zeros(self.payload, count, self.compact_budget)
        elif count:
            self.pieces.append(b"\0" * count)
        self.size += count

    def finish(self) -> bytearray:
        if self.pieces is not None and self.pieces:
            self.payload = bytearray(b"".join(self.pieces))
            self.pieces = []
        return self.payload


def _import_data(names, base_rva, compact_budget=None):
    groups = {}
    for name in sorted(names):
        groups.setdefault(system_dll(name), []).append(name)
    payload = bytearray()
    payload = _zeros(payload, 20 * (len(groups) + 1), compact_budget)
    payload = _zeros(payload, (-len(payload)) & 7, compact_budget)
    lookups = {}
    iats = {}
    slots = {}
    for dll in sorted(groups):
        lookups[dll] = len(payload)
        payload = _zeros(payload, (len(groups[dll]) + 1) * 8, compact_budget)
    iat_start = len(payload)
    for dll in sorted(groups):
        iats[dll] = len(payload)
        payload = _zeros(payload, (len(groups[dll]) + 1) * 8, compact_budget)
    iat_end = len(payload)
    for ordinal, dll in enumerate(sorted(groups)):
        dll_offset = len(payload)
        payload = _append(payload, dll.encode("ascii") + b"\0", compact_budget)
        for index, name in enumerate(groups[dll]):
            payload = _zeros(payload, len(payload) & 1, compact_budget)
            hint = len(payload)
            payload = _append(payload, b"\0\0" + name.encode("ascii") + b"\0", compact_budget)
            struct.pack_into("<Q", payload, lookups[dll] + index * 8, base_rva + hint)
            struct.pack_into("<Q", payload, iats[dll] + index * 8, base_rva + hint)
            slots[name] = base_rva + iats[dll] + index * 8
        struct.pack_into("<IIIII", payload, ordinal * 20,
                         base_rva + lookups[dll], 0, 0, base_rva + dll_offset, base_rva + iats[dll])
    directory = (base_rva + iat_start, iat_end - iat_start) if groups else (0, 0)
    return payload, slots, directory


def _rel32_range_error(symbol, source_section, place, target, target_section,
                       addend, value, sections) -> str:
    """Name the reference and the image layout of a REL32 that cannot reach."""
    if target_section is None:
        target_name = "import"
    else:
        target_name = sections[target_section][0]
    layout = []
    for name, address, payload, _flags in sections:
        layout.append(name + "@" + hex(address) + "+" + hex(len(payload)))
    return ("PE rel32 out of range: " + symbol + " from " + source_section
            + " at rva " + hex(place) + " to rva " + hex(target) + " (" + target_name
            + ") addend " + str(addend) + " displacement " + str(value)
            + "; sections " + ", ".join(layout))


def _merge_stack_maps(objects, compact_budget=None):
    """Intern root-set shapes across COFF inputs without renaming functions.

    Ordinary section concatenation repeats every object's location table.
    Keep address targets as original object/symbol indices: local symbols
    belong to their input and must not be resolved by an unqualified name.
    The existing wire merger preserves records and reindexes shared shapes.
    Its existing record/location caps, and the caller's RSS guard, remain.
    """
    from .precise_stackmap import (
        ARCH_X86_64, PreciseStackMapError, _scan_stack_map_payload,
        function_id, merge_stack_map_payloads, validate_stack_map_payload,
    )

    payloads = []
    targets = {}
    for oi, obj in enumerate(objects):
        for si, section in enumerate(obj.sections, 1):
            if section.name != ".pcc_stackmaps":
                continue
            if (section.flags & 0xE00000E0 != 0x40000040
                    or section.align < 8 or section.align > 4096
                    or section.align & (section.align - 1)):
                raise CoffError("PE stack-map section must be aligned read-only data")
            if any(symbol.section == si for symbol in obj.symbols):
                raise CoffError("PE stack-map section must not define symbols")
            try:
                validate_stack_map_payload(section.data, expected_arch=ARCH_X86_64,
                                           compact_budget=compact_budget)
                _count, functions, _start, _locations = _scan_stack_map_payload(
                    section.data, compact_budget=compact_budget,
                )
            except PreciseStackMapError as exc:
                raise CoffError("invalid PE stack-map input: " + str(exc)) from exc
            # A malformed relocation list must not allocate an unbounded
            # dictionary before the function-count reservation can reject it.
            if len(section.relocations) != len(functions):
                raise CoffError("every PE stack-map function needs exactly one relocation")
            relocations = {rel.offset: rel for rel in section.relocations}
            offsets = {start + 8 for _fid, start, _end in functions}
            if (len(relocations) != len(section.relocations)
                    or set(relocations) != offsets):
                raise CoffError("every PE stack-map function needs exactly one relocation")
            for fid, start, _end in functions:
                offset = start + 8
                relocation = relocations[offset]
                if (relocation.kind != ADDR64
                        or section.data[offset:offset + 8] != b"\0" * 8):
                    raise CoffError("PE stack-map address needs a zero-addend ADDR64 relocation")
                if not 0 <= relocation.symbol < len(obj.symbols):
                    raise CoffError("PE stack-map relocation symbol is out of range")
                symbol = obj.symbols[relocation.symbol]
                if not 0 < symbol.section <= len(obj.sections):
                    raise CoffError("PE stack-map function must be section-defined")
                code = obj.sections[symbol.section - 1]
                code_size = struct.unpack_from("<I", section.data, start + 16)[0]
                if (not code.flags & 0x20000000 or symbol.value < 0
                        or symbol.value + code_size > len(code.data)):
                    raise CoffError("PE stack-map function range is outside executable data")
                if fid != function_id(symbol.name):
                    raise CoffError("PE stack-map function id does not match its relocation symbol")
                if fid in targets:
                    raise CoffError("duplicate PE stack-map function id")
                targets[fid] = (oi, relocation.symbol)
            payloads.append(section.data)
    if not payloads:
        return b"", ()
    # Reverse-key planning has a CPython allocation model. Native and
    # unbudgeted callers retain the existing exact-vector merger.
    import sys as execution_runtime
    share_suffixes = False
    if execution_runtime.implementation.name == "cpython":
        share_suffixes = compact_budget is not None
    try:
        payload, offsets = merge_stack_map_payloads(
            tuple(payloads), compact_budget=compact_budget, _share_suffixes=share_suffixes,
        )
        if compact_budget is not None:
            compact_budget.retain(payload, "PE merged stack map")
        validate_stack_map_payload(payload, expected_arch=ARCH_X86_64, compact_budget=compact_budget)
    except PreciseStackMapError as exc:
        raise CoffError("invalid merged PE stack map: " + str(exc)) from exc
    return payload, tuple((offset, *targets[fid]) for fid, offset in offsets)


def link_executable(objects, *, archives=(), entry="pcc_windows_start",
                    image_base=0x140000000, compact_budget=None) -> bytes:
    objects = list(objects)
    pool = []
    for archive in archives:
        pool.extend(parse_object(payload) for _, payload in read_archive_payloads(archive))
    definitions = {}
    needed = set()
    for oi, obj in enumerate(objects):
        _add_symbols(obj, oi, definitions, needed)
    # Select archive members in the order a rescan from the first member
    # would: the lowest-index member defining a name that is needed (or the
    # entry) and still undefined. A member can only start to qualify when a
    # name it defines becomes needed, so only those members are re-checked;
    # rescanning every member's symbols per selection was quadratic.
    pool_definers = {}
    for index, obj in enumerate(pool):
        for sym in obj.symbols:
            if sym.external and sym.section:
                pool_definers.setdefault(sym.name, []).append(index)
    dirty = [False] * len(pool)
    taken = [False] * len(pool)
    for name in needed:
        for index in pool_definers.get(name, ()):
            dirty[index] = True
    for index in pool_definers.get(entry, ()):
        dirty[index] = True
    while True:
        selected = -1
        index = 0
        while selected < 0 and index < len(pool):
            if dirty[index]:
                dirty[index] = False
                if any(sym.external and sym.section and sym.name not in definitions
                       and (sym.name in needed or sym.name == entry)
                       for sym in pool[index].symbols):
                    selected = index
            index += 1
        if selected < 0:
            break
        taken[selected] = True
        member = pool[selected]
        added = [sym.name for sym in member.symbols
                 if sym.external and not sym.section and sym.name not in needed]
        objects.append(member)
        _add_symbols(member, len(objects) - 1, definitions, needed)
        for name in added:
            for index in pool_definers.get(name, ()):
                if not taken[index]:
                    dirty[index] = True
    if entry not in definitions:
        raise CoffError("Windows entry is not defined: " + entry)
    # Reserve loader-written TLS index storage even for images without TLS.
    index_object = CoffObject((CoffSection(".data$tlsidx", b"\0" * 4, 0xC0000040, 4),),
                              (CoffSymbol("_tls_index", 1),))
    if "_tls_index" not in definitions:
        objects.append(index_object)
        _add_symbols(index_object, len(objects) - 1, definitions, needed)
    # Preserve the original undefined-name insertion order and final set
    # subtraction, without rebuilding all selected symbols for each member.
    needed = needed - set(definitions)
    boundary_names = {"__init_array_start", "__init_array_end", "__fini_array_start", "__fini_array_end"}
    if boundary_names & set(definitions):
        raise CoffError("initializer boundary symbols are owned by the linker")
    needed -= boundary_names
    imports = {name[6:] if name.startswith("__imp_") else name for name in needed}
    for name in imports:
        system_dll(name)

    # All original COFF/archive owners, including unselected members, exist
    # before the host baseline is observed. The recognized implementation
    # guard keeps the ctypes/provider module out of the native compiler closure.
    import sys as execution_runtime
    if execution_runtime.implementation.name == "cpython":
        if compact_budget is None:
            from pcc.backend.pe_host_budget import host_pe_budget
            compact_budget = host_pe_budget()
    from .precise_stackmap import PreciseStackMapError
    budget_prepared = False
    try:
        if compact_budget is not None:
            compact_budget.prepare(objects, imports)
            budget_prepared = True
        return _link_selected_objects(
            objects, definitions, imports, boundary_names, entry, image_base, compact_budget,
        )
    except PreciseStackMapError as exc:
        sizes = getattr(compact_budget, "last_rejection", "")
        raise CoffError(
            "PE compact stack-map budget rejected: " + str(exc) + ("; " + sizes if sizes else "")
        ) from exc
    finally:
        if budget_prepared:
            compact_budget.finish()


def _link_selected_objects(objects, definitions, imports, boundary_names,
                           entry, image_base, compact_budget):
    stack_payload, stack_targets = _merge_stack_maps(objects, compact_budget)
    stack_size = len(stack_payload)
    stack_counts = _stack_map_size_counts(stack_payload)
    stack_placement = None
    # Merge sections by PE name, sorting COFF $ suffixes before concatenation.
    groups = {}
    for oi, obj in enumerate(objects):
        for si, sec in enumerate(obj.sections, 1):
            if sec.name in (".note.GNU-stack", ".comment", ".pcc_stackmaps"):
                continue
            base = sec.name.split("$", 1)[0]
            if base.startswith(".rodata"):
                base = ".rdata"
            if len(base.encode()) > 8:
                raise CoffError("PE section name exceeds eight bytes: " + base)
            groups.setdefault(base, []).append((sec.name, oi, si, sec))
    groups.setdefault(".text", [])
    groups.setdefault(".idata", [])
    groups.setdefault(".rdata", [])
    if stack_payload:
        # The negative index is private to this synthetic section; all
        # address relocations retain the original nonnegative input index.
        groups[".rdata"].append((".pcc_stackmaps", -1, 0,
                                CoffSection(".pcc_stackmaps", stack_payload, 0x40000040, 8)))
    sections = []
    placements = {}
    rva = 0x1000
    text_thunks = {}
    tls_start = tls_end = tls_directory = 0
    tls_alignment = 1
    tls_pointers = []
    iat_directory = (0, 0)
    import_directory = (0, 0)
    for name in sorted(groups, key=lambda value: (value != ".text", value)):
        pieces = _Pieces(compact_budget)
        if compact_budget is not None:
            compact_budget.retain(pieces.payload)
        flags = 0x40000040
        if name == ".text":
            flags = 0x60000020
        for _source_name, oi, si, sec in sorted(groups[name], key=lambda item: (item[0], item[1], item[2])):
            if sec.align > 4096:
                raise CoffError("PE section alignment above page size is unsupported")
            if name == ".tls":
                if sec.align <= 0 or sec.align & (sec.align - 1):
                    raise CoffError("invalid PE TLS section alignment")
                tls_alignment = max(tls_alignment, sec.align)
            pieces.zeros((-pieces.size) & (sec.align - 1))
            if oi < 0:
                stack_placement = (len(sections), pieces.size)
            else:
                placements[(oi, si)] = (len(sections), pieces.size)
            pieces.append(sec.data)
            flags |= sec.flags & 0xE00000E0
        if name == ".text":
            for symbol in sorted(imports):
                text_thunks[symbol] = rva + pieces.size
                pieces.append(b"\xff\x25\0\0\0\0")
        payload = pieces.finish()
        if name == ".idata":
            if payload:
                raise CoffError("explicit import sections conflict with owned imports")
            if compact_budget is not None:
                compact_budget.release(payload)
            payload, import_slots, iat_directory = _import_data(imports, rva, compact_budget)
            flags = 0xC0000040
            import_directory = (rva, 20 * (len({system_dll(n) for n in imports}) + 1)) if imports else (0, 0)
        if name == ".tls":
            tls_start, tls_end = rva, rva + len(payload)
        sections.append([name, rva, payload, flags])
        rva = _align(rva + max(1, len(payload)), 4096)

    boundaries = {name: 0 for name in boundary_names}
    for name, address, payload, flags in sections:
        if name in (".pccinit", ".pccfini"):
            family = "init" if name == ".pccinit" else "fini"
            boundaries["__" + family + "_array_start"] = address
            boundaries["__" + family + "_array_end"] = address + len(payload)

    resolved = {}

    def resolve(oi, si):
        # Every relocation of an object against one symbol index resolves
        # the same way; the lookup by name is the expensive part.
        key = oi * 4294967296 + si
        if key in resolved:
            return resolved[key]
        result = _resolve_uncached(oi, si)
        resolved[key] = result
        return result

    def _resolve_uncached(oi, si):
        sym = objects[oi].symbols[si]
        if sym.external and sym.name in definitions:
            oi, si = definitions[sym.name]
            sym = objects[oi].symbols[si]
        if not sym.section:
            if sym.name in boundaries:
                return boundaries[sym.name], None
            if sym.name.startswith("__imp_"):
                return import_slots[sym.name[6:]], None
            return text_thunks[sym.name], None
        if (oi, sym.section) not in placements:
            raise CoffError("symbol belongs to a discarded section: " + sym.name)
        index, offset = placements[(oi, sym.section)]
        return sections[index][1] + offset + sym.value, index

    fixups = []
    for oi, obj in enumerate(objects):
        for si, source in enumerate(obj.sections, 1):
            if (oi, si) not in placements:
                continue
            section_index, base = placements[(oi, si)]
            output = sections[section_index]
            for reloc in source.relocations:
                patch = base + reloc.offset
                target, target_section = resolve(oi, reloc.symbol)
                place = output[1] + patch
                width = 8 if reloc.kind == ADDR64 else 2 if reloc.kind == SECTION else 4
                signed = 4 <= reloc.kind <= 9
                addend = int.from_bytes(output[2][patch:patch + width], "little", signed=signed)
                if reloc.kind == ADDR64:
                    value = image_base + target + addend
                    fixups.append(place)
                elif reloc.kind == ADDR32NB:
                    value = target + addend
                elif reloc.kind == ADDR32:
                    value = image_base + target + addend
                elif 4 <= reloc.kind <= 9:
                    value = target + addend - (place + 4 + reloc.kind - 4)
                elif reloc.kind == SECREL:
                    if target_section is None:
                        raise CoffError("SECREL cannot reference an import")
                    value = target - sections[target_section][1] + addend
                elif reloc.kind == SECTION:
                    if target_section is None:
                        raise CoffError("SECTION cannot reference an import")
                    value = target_section + 1 + addend
                else:
                    raise CoffError("unhandled PE relocation")
                if signed:
                    if value < -(1 << 31) or value >= 1 << 31:
                        raise CoffError(_rel32_range_error(
                            objects[oi].symbols[reloc.symbol].name, source.name, place,
                            target, target_section, addend, value, sections))
                elif value < 0 or value >= 1 << (width * 8):
                    raise CoffError("PE relocation overflow")
                output[2][patch:patch + width] = (value & ((1 << (width * 8)) - 1)).to_bytes(width, "little")
    if stack_placement is not None:
        from .precise_stackmap import ARCH_X86_64, validate_stack_map_payload

        section_index, base = stack_placement
        output = sections[section_index]
        for offset, oi, si in stack_targets:
            target, _target_section = resolve(oi, si)
            value = image_base + target
            if not 0 < value < 1 << 64:
                raise CoffError("PE stack-map function address overflows uint64")
            struct.pack_into("<Q", output[2], base + offset, value)
            fixups.append(output[1] + base + offset)
        # Validate the final address-bound table, excluding ordinary .rodata
        # that follows it. No input table or caller-owned object was mutated.
        # On CPython a memoryview slice is a view, so bytes() makes the one
        # immutable copy the validator needs; a bytearray slice would first
        # copy the table (1.3 GB in a Windows Stage1 link) and bytes() would
        # copy it again. The native runtime's slicing still copies.
        import sys as execution_runtime
        view_slice = execution_runtime.implementation.name == "cpython"
        if compact_budget is not None:
            # Keep every output section and the original merged payload charged.
            compact_budget.check_copy(stack_size, 1 if view_slice else 2, "PE final stack-map snapshot")
        if view_slice:
            with memoryview(output[2]) as output_view:
                final_table = bytes(output_view[base:base + stack_size])
        else:
            final_table = bytes(output[2][base:base + stack_size])
        if compact_budget is not None:
            compact_budget.retain(final_table)
        try:
            validate_stack_map_payload(
                final_table, expected_arch=ARCH_X86_64, final_image=True,
                compact_budget=compact_budget,
            )
        finally:
            if compact_budget is not None:
                compact_budget.release(final_table)
        del final_table
    for name, address in text_thunks.items():
        text = sections[0]
        struct.pack_into("<i", text[2], address - text[1] + 2, import_slots[name] - address - 6)
    if tls_start:
        # A separate section keeps all previous RVAs stable.
        tls_directory = rva
        oi, si = definitions["_tls_index"]
        index_rva = resolve(oi, si)[0]
        if compact_budget is not None:
            compact_budget.check_copy(40, 2, "PE TLS directory")
        # Characteristics bits [23:20] encode log2(alignment) + 1 for the loader.
        payload = bytearray(struct.pack("<QQQQII", image_base + tls_start, image_base + tls_end,
                                        image_base + index_rva, 0, 0, tls_alignment.bit_length() << 20))
        if compact_budget is not None:
            compact_budget.retain(payload)
        sections.append([".tlsdir", rva, payload, 0x40000040])
        fixups.extend((rva, rva + 8, rva + 16))
        rva += 4096
    exception_directory = (0, 0)
    for name, address, payload, flags in sections:
        if name == ".pdata":
            if len(payload) % 12:
                raise CoffError("malformed runtime function table")
            records = [bytes(payload[i:i + 12]) for i in range(0, len(payload), 12)]
            records.sort(key=lambda record: struct.unpack_from("<I", record)[0])
            if compact_budget is not None:
                # Joining bytes and bytearray slice-assignment's conversion
                # overlap. The existing payload stays retained as well.
                compact_budget.check_copy(len(payload), 2, "PE pdata join")
            payload[:] = b"".join(records)
            if compact_budget is not None:
                compact_budget.retain(payload)
            exception_directory = (address, len(payload))
    reloc_pieces = _Pieces(compact_budget)
    pages = {}
    for address in sorted(set(fixups)):
        pages.setdefault(address & -4096, []).append(0xA000 | (address & 4095))
    for page, entries in sorted(pages.items()):
        if len(entries) & 1:
            entries.append(0)
        reloc_pieces.append(struct.pack("<II", page, 8 + len(entries) * 2))
        for word in entries:
            reloc_pieces.append(struct.pack("<H", word))
    reloc_data = reloc_pieces.finish()
    reloc_directory = (rva, len(reloc_data)) if reloc_data else (0, 0)
    if reloc_data:
        sections.append([".reloc", rva, reloc_data, 0x42000040])
        rva = _align(rva + len(reloc_data), 4096)
    _check_image_size(rva, sections, stack_map_bytes=stack_size,
                      stack_map_counts=stack_counts)
    headers = _align(128 + 4 + 20 + 240 + len(sections) * 40, 512)
    if compact_budget is not None:
        compact_budget.check_copy(headers, 2, "PE image header")
    image = bytearray(b"\0" * headers)
    if compact_budget is not None:
        compact_budget.retain(image)
    image[:2] = b"MZ"
    struct.pack_into("<I", image, 60, 128)
    image[128:132] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", image, 132, 0x8664, len(sections), 0, 0, 0, 240, 0x22 if reloc_data else 0x23)
    opt = 152
    entry_rva = resolve(*definitions[entry])[0]
    struct.pack_into("<HBBIIIIIQ", image, opt, 0x20B, 1, 0,
                     sum(_align(len(row[2]), 512) for row in sections if row[3] & 0x20),
                     sum(_align(len(row[2]), 512) for row in sections if row[3] & 0x40),
                     0, entry_rva, sections[0][1], image_base)
    struct.pack_into("<IIHHHHHHIIIIHHQQQQII", image, opt + 32,
                     4096, 512, 6, 0, 0, 0, 6, 0, 0, rva, headers, 0,
                     3, 0x0160 if reloc_data else 0x0100, 8 * 1024 * 1024, 65536, 1024 * 1024, 4096, 0, 16)
    for index, (address, size) in ((1, import_directory), (3, exception_directory),
                                  (5, reloc_directory), (9, (tls_directory, 40 if tls_directory else 0)),
                                  (12, iat_directory)):
        struct.pack_into("<II", image, opt + 112 + index * 8, address, size)
    file_size = headers
    padding_bytes = 0
    for _name, _address, payload, _flags in sections:
        size = _align(len(payload), 512)
        file_size += size
        padding_bytes += size - len(payload)
    if compact_budget is not None:
        compact_budget.check_image_join(file_size, len(sections), padding_bytes)
    # Only the small header is mutable here. All section contents are final:
    # one join avoids growing a full-image bytearray and copying it to bytes.
    # Keep the section buffers alive; the budget charges them through return.
    chunks = [image]
    raw = headers
    for index, (name, address, payload, flags) in enumerate(sections):
        size = _align(len(payload), 512)
        chunks.append(payload)
        chunks.append(b"\0" * (size - len(payload)))
        struct.pack_into("<8sIIIIIIHHI", image, opt + 240 + index * 40,
                         name.encode().ljust(8, b"\0"), len(payload), address, size, raw,
                         0, 0, 0, 0, flags)
        raw += size
    return b"".join(chunks)
