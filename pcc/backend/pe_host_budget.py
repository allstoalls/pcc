"""CPython-only PE admission after parsing, under the existing Windows Job.

The supported launch is platform_process_watchdog's direct root, assigned
while suspended. This is not discovery of arbitrary wrappers or ancestor Jobs.
The reviewed host worker paths neither reassign that root nor use breakaway.
The OS still enforces stricter ancestor limits; an estimate never guarantees
allocation success, system RAM availability, or a native-pcc allocation model.
"""

import json
import os
import struct
import sys
import time

from .precise_stackmap import CompactStackMapBudget, PreciseStackMapError


GUARD_ENV = "PCC_WINDOWS_DIRECT_ROOT_GUARD"
GUARD_SCHEMA = "pcc.windows-direct-root-guard.v1"
HEADROOM_BYTES = 134217728
MAX_JOB_PROCESSES = 4096
MAX_OBSERVATION_SECONDS = 2.0


class _WindowsObservation:
    """Read-only queries; no Job handle, permission or limit changes."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes as w

        self.c = ctypes
        self.w = w
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", w.DWORD), ("min_ws", ctypes.c_size_t),
                        ("max_ws", ctypes.c_size_t), ("active", w.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", w.DWORD),
                        ("scheduling", w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes",
            )]
        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t),
                        ("peak_job", ctypes.c_size_t)]
        class Memory(ctypes.Structure):
            _fields_ = [("size", w.DWORD), ("faults", w.DWORD)] + [
                (name, ctypes.c_size_t) for name in (
                    "peak_ws", "working_set", "peak_paged", "paged",
                    "peak_nonpaged", "nonpaged", "pagefile", "peak_pagefile",
                )
            ]
        self.Extended = Extended
        self.Memory = Memory
        self.kernel.QueryInformationJobObject.argtypes = [
            w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD),
        ]
        self.kernel.QueryInformationJobObject.restype = w.BOOL
        self.kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.kernel.OpenProcess.restype = w.HANDLE
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.kernel.CloseHandle.restype = w.BOOL
        self.kernel.GetProcessTimes.argtypes = [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4
        self.kernel.GetProcessTimes.restype = w.BOOL
        self.kernel.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
        self.kernel.GetExitCodeProcess.restype = w.BOOL
        self.kernel.K32GetProcessMemoryInfo.argtypes = [w.HANDLE, ctypes.c_void_p, w.DWORD]
        self.kernel.K32GetProcessMemoryInfo.restype = w.BOOL

    def limits(self):
        value = self.Extended()
        if not self.kernel.QueryInformationJobObject(
            None, 9, self.c.byref(value), self.c.sizeof(value), None,
        ):
            raise OSError("current Windows Job limits unavailable")
        return int(value.basic.flags), int(value.job_memory), int(value.peak_job)

    def pids(self):
        size = 8 + MAX_JOB_PROCESSES * self.c.sizeof(self.c.c_size_t)
        value = self.c.create_string_buffer(size)
        if not self.kernel.QueryInformationJobObject(None, 3, value, size, None):
            raise OSError("complete Windows Job membership unavailable")
        assigned = self.c.c_uint32.from_buffer(value, 0).value
        count = self.c.c_uint32.from_buffer(value, 4).value
        if assigned != count or not 0 < count <= MAX_JOB_PROCESSES:
            raise OSError("incomplete Windows Job membership")
        return tuple(int(self.c.c_size_t.from_buffer(
            value, 8 + index * self.c.sizeof(self.c.c_size_t),
        ).value) for index in range(count))

    def process(self, pid):
        # Same query/read access as the existing watchdog; never request a
        # Job handle or privilege elevation. Every failure is unavailable.
        handle = self.kernel.OpenProcess(0x410, False, pid)
        if not handle:
            raise OSError("Windows guard process unavailable")
        try:
            times = [self.w.FILETIME() for _ in range(4)]
            if not self.kernel.GetProcessTimes(handle, *(self.c.byref(row) for row in times)):
                raise OSError("Windows guard process identity unavailable")
            status = self.w.DWORD()
            if not self.kernel.GetExitCodeProcess(handle, self.c.byref(status)):
                raise OSError("Windows guard process state unavailable")
            memory = self.Memory()
            memory.size = self.c.sizeof(memory)
            if not self.kernel.K32GetProcessMemoryInfo(handle, self.c.byref(memory), memory.size):
                raise OSError("Windows guard working set unavailable")
            created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            return created, int(memory.working_set), status.value == 259
        finally:
            if not self.kernel.CloseHandle(handle):
                raise OSError("Windows guard query handle did not close")


def _verified_allowance(receipt, expected_cap, owner_pid, parent_pid, api, clock=time.monotonic):
    """Validate a direct-root receipt against a bounded current OS observation."""
    if not isinstance(receipt, dict) or receipt.get("schema") != GUARD_SCHEMA:
        return None
    for key in ("root_pid", "root_creation_time", "supervisor_pid",
                "supervisor_creation_time", "cap_bytes"):
        value = receipt.get(key)
        if type(value) is not int or not 0 < value <= 0x7FFFFFFFFFFFFFFF:
            return None
    deadline = receipt.get("deadline")
    if type(deadline) not in (int, float):
        return None
    started = clock()
    if not started < deadline < float("inf"):
        return None
    if (receipt["root_pid"] != owner_pid or receipt["supervisor_pid"] != parent_pid
            or parent_pid == owner_pid or receipt["cap_bytes"] != expected_cap):
        return None
    first_limits = api.limits()
    # Exactly the configuration this project publishes. Unknown additional
    # flags, either breakaway bit, or a different immediate Job keep the cap.
    if first_limits[:2] != (0x2200, expected_cap) or first_limits[2] <= 0:
        return None
    first = api.pids()
    if (not first or len(first) > MAX_JOB_PROCESSES or len(set(first)) != len(first)
            or any(type(pid) is not int or pid <= 0 for pid in first) or owner_pid not in first):
        return None
    supervisor = api.process(parent_pid)
    if not supervisor[2] or supervisor[0] != receipt["supervisor_creation_time"]:
        return None
    before = {pid: api.process(pid) for pid in first}
    second = api.pids()
    if len(second) != len(first) or set(second) != set(first):
        return None
    after = {pid: api.process(pid) for pid in second}
    final_limits = api.limits()
    # One more inventory after the second process read rejects a member
    # starting/exiting during either pass, rather than counting it as zero.
    final = api.pids()
    if len(final) != len(first) or set(final) != set(first):
        return None
    if final_limits[:2] != first_limits[:2] or final_limits[2] < first_limits[2]:
        return None
    final_supervisor = api.process(parent_pid)
    if final_supervisor[0] != supervisor[0] or not final_supervisor[2]:
        return None
    rss = 0
    for pid in first:
        left, right = before[pid], after[pid]
        if (left[0] <= 0 or left[0] != right[0] or left[1] <= 0 or right[1] <= 0
                or not left[2] or not right[2]):
            return None
        rss += max(left[1], right[1])
    if before[owner_pid][0] != receipt["root_creation_time"]:
        return None
    finished = clock()
    if not 0 <= finished - started <= MAX_OBSERVATION_SECONDS or finished >= deadline:
        return None
    # PeakJobMemoryUsed is kernel-maintained committed memory, not RSS or
    # available RAM. Taking the larger baseline conservatively retains prior
    # commit peaks, including the COFF/archive graph parsed before admission.
    allowance = expected_cap - max(rss, final_limits[2]) - HEADROOM_BYTES
    return allowance if allowance > 0 else None


def _windows_allowance():
    raw = os.environ.get("PCC_WORKER_TREE_BUDGET_BYTES", "")
    path = os.environ.get(GUARD_ENV, "")
    if (not path or not 0 < len(raw) <= 19
            or any(character < "0" or character > "9" for character in raw)):
        return None
    cap = int(raw)
    if not 0 < cap <= 0x7FFFFFFFFFFFFFFF:
        return None
    try:
        with open(path, "r", encoding="utf-8") as stream:
            text = stream.read(4097)
        if len(text) > 4096:
            return None
        receipt = json.loads(text)
        return _verified_allowance(receipt, cap, os.getpid(), os.getppid(), _WindowsObservation())
    except (OSError, ValueError, TypeError, AttributeError, ImportError, OverflowError, RecursionError):
        return None


def _host_profile():
    # Same supported host and insertion-only set model as stackmap_host_budget.
    if (sys.implementation.name != "cpython" or struct.calcsize("P") != 8
            or sys.version_info[:2] not in ((3, 13), (3, 14), (3, 15))
            or hasattr(sys, "gettotalrefcount")
            or os.environ.get("PYTHONMALLOC", "") == "debug"
            or os.environ.get("PYTHONMALLOC", "").endswith("_debug")):
        return None
    if hasattr(sys, "_is_gil_enabled") and not sys._is_gil_enabled():
        return None
    import sysconfig
    if sysconfig.get_config_var("Py_GIL_DISABLED") != 0:
        return None
    word = struct.calcsize("P")
    quantum = 2 * word
    def rounded(size):
        return ((size + quantum - 1) // quantum) * quantum
    integer = rounded(max(sys.getsizeof((1 << 64) - 1), sys.getsizeof(-(1 << 63))))
    # Existing reviewed bound: eight slots/entry, two tables, four words/slot.
    # PE additionally owns four-field rows, short names, and per-group lists.
    # One entry below covers that whole shape; multiple simultaneously live
    # containers each receive their own entry count in prepare().
    metadata = (64 * word + rounded(sys.getsizeof((0, 0, 0, 0))) + 4 * integer
                + rounded(sys.getsizeof(b"")) + rounded(sys.getsizeof(""))
                + rounded(sys.getsizeof([])))
    return dict(set_header_bytes=rounded(sys.getsizeof(set())), set_entry_bytes=2 * word,
                integer_bytes=integer, metadata_entry_bytes=metadata, allocation_quantum=quantum)


class PEHostBudget(CompactStackMapBudget):
    """Future-owner ledger, supplementing the immutable sampled baseline.

    Retained buffers use actual CPython allocated sizes. Before resize we also
    reserve the possible new allocation while the old one is still live.
    This remains an allocation estimate: fragmentation and peer growth are
    enforced by the original watchdog/Job, never by this ledger alone.
    """

    def __init__(self, limit_bytes, record_visit_limit, **profile):
        super().__init__(limit_bytes, record_visit_limit, **profile)
        self._buffers = {}
        self.metadata_owners = {}
        self.metadata_bytes = {}
        self.metadata_counts = {}
        self._initial_retained = None
        self.bytearray_header = sys.getsizeof(bytearray())
        self.bytes_header = sys.getsizeof(b"")
        self.validation_set_header = sys.getsizeof(set())
        self.validation_key_bytes = self._rounded(sys.getsizeof((0, 0, 0)))

    def prepare(self, objects, imports):
        if self._initial_retained is not None:
            raise PreciseStackMapError("PE budget already owns an operation")
        self._initial_retained = self.retained_bytes
        try:
            self._reserve_metadata(objects, imports)
        except BaseException:
            # This call acquired the operation; partial preparation owns its
            # own rollback. An already-active caller never reaches this block.
            self.finish()
            raise

    def _reserve_metadata(self, objects, imports):
        from .coff_x86_64 import CoffError

        sections = maps = functions = fixups = pdata = names = 0
        section_bytes = pdata_bytes = 0
        self.check_peak(0, entries=3, label="PE section group scan")
        group_names = {".text", ".idata", ".rdata"}
        for obj in objects:
            for symbol in obj.symbols:
                # scoped_stable_id joins a Unicode prefix and encodes UTF-8;
                # reserve both worst-case four-byte representations.
                names += 8 * (len(symbol.name) + len("function\0"))
            for section in obj.sections:
                if section.name == ".pcc_stackmaps":
                    maps += 1
                    if len(section.data) >= 24:
                        functions += struct.unpack_from("<I", section.data, 12)[0]
                    continue
                if section.name in (".note.GNU-stack", ".comment"):
                    continue
                # Match the output grouping in pe_x86_64, but reserve the
                # temporary split/encoding and growing set before allocating.
                self.check_peak(8 * len(section.name), entries=len(group_names) + 1,
                                label="PE section group scan")
                base = section.name.split("$", 1)[0]
                if base.startswith(".rodata"):
                    base = ".rdata"
                if len(base.encode()) > 8:
                    raise CoffError("PE section name exceeds eight bytes: " + base)
                group_names.add(base)
                if section.align > 4096:
                    raise CoffError("PE section alignment above page size is unsupported")
                sections += 1
                names += 4 * len(section.name)  # split head/tail combined
                # COFF parsing has already validated positive power-of-two
                # alignment. This also bounds every accepted PE contribution.
                padding = max(0, section.align - 1)
                section_bytes += len(section.data) + padding
                fixups += sum(rel.kind == 1 for rel in section.relocations)
                if section.name == ".pdata" or section.name.startswith(".pdata$"):
                    pdata_bytes += len(section.data) + padding
        pdata = (pdata_bytes + 11) // 12
        fixups += functions + 3  # merged addresses and optional TLS directory
        groups = len(group_names)
        outputs = groups + 2  # optional TLS and relocation output sections
        import_count = len(imports)
        # Encoding plus prefix/suffix concatenation during import emission;
        # system_dll has already admitted only the finite named ABI surface.
        names += sum(3 * (len(name) + 3) for name in imports)
        names += 2 * len("KERNEL32.dll") * import_count
        # Ordinary contributions occupy one interval per output group. The
        # .rdata interval can begin mid-page after the merged map; one extra
        # page per group covers interval boundaries. Keep one possible page
        # per map function: interning overlapping input location slices can
        # expand the merged root table, so input map bytes are NOT a bound on
        # that output's extent. TLS adds at most one fixup-bearing page.
        pages = min(fixups, (section_bytes + 4095) // 4096 + groups + functions + 1)
        self.metadata_counts = dict(sections=sections, groups=groups, functions=functions,
                                    fixups=fixups, pages=pages, pdata_records=pdata)
        self.metadata_owners = {
            "map payload list and tuple": 2 * maps,
            "map targets, merged offsets and returned target tuples": 3 * functions,
            "input scan, relocation dictionary and two offset sets": 4 * functions,
            "section groups and rows": groups + sections + 1,
            "section sorted references and key tuples": 2 * groups + 2 * (sections + 1),
            "placement key and value tuples": 2 * sections,
            "output rows and buffer ledger": 2 * outputs,
            "import group dictionaries and grouped name rows": 2 * import_count,
            "import lookup, IAT and slot dictionaries": 3 * import_count,
            "import name sort and three DLL sorts": 4 * import_count,
            "text thunk dictionary and name sort": 2 * import_count,
            "import directory DLL set": import_count,
            "page dictionaries, keys and item-sort rows": 3 * pages,
            "pdata record rows, sort keys and merge scratch": 2 * pdata + (pdata + 1) // 2,
            "boundary symbols and directory rows": 4 + 5,
        }
        # Raw .pdata records are separate from their object/container headers.
        raw = names + 12 * pdata
        # These are sequential arrays of references/integers, not four
        # independent dictionaries with a full metadata_entry_bytes charge per
        # fixup. Keep all owners simultaneous, including old/new list and set
        # tables, sort scratch, new page-word ints, and per-page odd padding.
        self.metadata_bytes = {
            "names and pdata records": raw,
            "fixup and sorted reference lists": 2 * self._list_bytes(fixups),
            # All address integers predate set(fixups). _set_bytes models
            # insertion-time ownership, so retain the complete list's ints
            # separately even while the set is only partially populated.
            "fixup list integer owners": fixups * self.integer_bytes,
            "fixup dedup set": self._set_bytes(fixups),
            "page entry integer owners": (fixups + pages) * self.integer_bytes,
            "page entry lists": self._list_bytes(fixups + pages, lists=pages),
        }
        raw = sum(self.metadata_bytes.values())
        entries = sum(self.metadata_owners.values())
        try:
            self.check_peak(raw, entries=entries, label="PE future metadata")
        except PreciseStackMapError as exc:
            raise PreciseStackMapError(
                str(exc) + "; allowance_bytes=" + str(self.limit_bytes)
                + "; retained_bytes=" + str(self.retained_bytes)
                + "; future_metadata_bytes=" + str(raw + entries * self.metadata_entry_bytes)
                + "; counts=" + str(self.metadata_counts)
            ) from exc
        self.retained_bytes += raw + entries * self.metadata_entry_bytes

    def _list_bytes(self, entries, *, lists=1):
        # CPython's n+n/8+6 growth is below 2*n+8 slots per list. Doubling
        # that bound includes old/new allocations during resize, or the final
        # array plus n/2 references of unkeyed-sort scratch. Round each list's
        # allocation separately through a full extra quantum per owner.
        word = struct.calcsize("P")
        header = self._rounded(sys.getsizeof([]))
        slots = 2 * entries + 8 * lists
        return 2 * (lists * (header + self.allocation_quantum) + slots * word)

    def _validation_cache_bytes(self, count, allocated):
        # This cache is an insertion-only CPython set of three-integer tuples,
        # not the several dictionary/row shapes covered by metadata entries.
        # Its current allocation exposes the table capacity in O(1), leaving
        # the separate all-record ID-set estimate cached across slice misses.
        slot = 2 * struct.calcsize("P")
        capacity = max(8, (allocated - self.validation_set_header) // slot)
        tables = self._rounded(allocated)
        if count * 5 >= (capacity - 1) * 3:
            target = count * (4 if count <= 50000 else 2)
            following = 1 << target.bit_length()
            tables += self._rounded(following * slot)
        # A cache hit still constructs a temporary equal tuple. Two spare
        # keys cover old/new temporaries during replacement on repeated hits.
        keys = (count + 2) * (self.validation_key_bytes + 3 * self.integer_bytes)
        return tables + keys

    def check_validation_cache(self, payload, records, functions, cache, locations):
        count = len(cache) + 1
        cache_bytes = self._validation_cache_bytes(count, sys.getsizeof(cache))
        # The flag list is discarded after this miss. Include its integer
        # owners and old/new list arrays before any locations are visited.
        scratch_bytes = self._list_bytes(locations) + locations * self.integer_bytes
        try:
            self.check_peak(
                self.unretained_payload_bytes(payload) + cache_bytes + scratch_bytes,
                records=records, entries=functions, label="validation cache",
            )
        except PreciseStackMapError as exc:
            raise PreciseStackMapError(
                str(exc) + "; allowance_bytes=" + str(self.limit_bytes)
                + "; retained_bytes=" + str(self.retained_bytes)
                + "; payload_bytes=" + str(len(payload))
                + "; safepoint_records=" + str(records)
                + "; function_count=" + str(functions)
                + "; validation_cache_entries=" + str(count)
                + "; validation_cache_bytes=" + str(cache_bytes)
                + "; validation_scratch_bytes=" + str(scratch_bytes)
            ) from exc

    def finish(self):
        if self._initial_retained is not None:
            self.retained_bytes = self._initial_retained
        self._initial_retained = None
        self._buffers.clear()

    def retain(self, value, label="PE retained buffer"):
        key = id(value)
        size = sys.getsizeof(value)
        retained = self._buffers.get(key)
        old = retained[1] if retained is not None else 0
        if size > old:
            self.check_peak(size - old, label=label)
        self.retained_bytes += size - old
        # Keep the object itself: an ID alone could be reused after the last
        # external reference is dropped. The metadata reservation covers this
        # dictionary entry and its short owner/size tuple.
        self._buffers[key] = (value, size)

    def release(self, value):
        retained = self._buffers.pop(id(value), None)
        if retained is not None:
            self.retained_bytes -= retained[1]

    def unretained_payload_bytes(self, payload):
        retained = self._buffers.get(id(payload))
        # Validation reads immutable bytes without copying them. Their full
        # allocated size, including the header, is already in retained_bytes.
        # Equal contents/lengths and mutable buffers cannot establish that.
        if type(payload) is bytes and retained is not None and retained[0] is payload:
            return 0
        return len(payload)

    def check_growth(self, value, following_size, *, temporary_bytes=0, label="PE buffer growth"):
        self.retain(value)
        # CPython 3.13--3.15 bytearray_resize: moderate growth uses n+n/8+6;
        # large growth uses n+1. Round up and include both old/new allocations.
        # https://github.com/python/cpython/blob/v3.13.0/Objects/bytearrayobject.c
        allocation = self._rounded(max(following_size + 1, following_size + following_size // 8 + 6))
        self.check_peak(self.bytearray_header + allocation + temporary_bytes, label=label)

    def extend(self, value, data):
        self.check_growth(value, len(value) + len(data))
        value.extend(data)
        self.retain(value)

    def zeros(self, value, count):
        self.check_growth(value, len(value) + count,
                          temporary_bytes=count + self.bytes_header, label="PE padding")
        value.extend(b"\0" * count)
        self.retain(value)

    def check_copy(self, size, copies=1, label="PE buffer copy"):
        self.check_peak(copies * (size + max(self.bytearray_header, self.bytes_header) + 1), label=label)

    def check_image_join(self, size, section_count, padding_bytes):
        """Admit all final-join owners before making the chunk list or pads."""
        if (any(type(value) is not int or value < 0
                for value in (size, section_count, padding_bytes))
                or padding_bytes > 511 * section_count or padding_bytes > size):
            raise PreciseStackMapError("invalid PE image join sizes")
        chunks = 1 + 2 * section_count  # header, then payload/padding pairs
        # CPython 3.13--3.15 bytes.join reuses an exact list, exports its
        # bytes/bytearray items without copying, and allocates one result.
        # Its ten stack Py_buffers coexist with a heap array above ten items.
        # Py_buffer has a stable ABI since 3.11; this is its native layout.
        # https://github.com/python/cpython/blob/v3.13.0/Objects/stringlib/join.h
        # https://github.com/python/cpython/blob/v3.13.0/Include/pybuffer.h
        buffer_size = struct.calcsize("PPnniiPPPPP")
        join_buffers = self._rounded((10 + chunks) * buffer_size)
        # Reserve every pad independently even when empty/one-byte bytes are
        # shared. The extra quantum per pad bounds independent rounding.
        pads = padding_bytes + section_count * (self.bytes_header + self.allocation_quantum)
        result = self._rounded(size + self.bytes_header)
        # Keep all existing section/map/header owners charged. The list bound
        # includes old/new growth, and spare integers cover sizing counters.
        self.check_peak(result + pads + self._list_bytes(chunks) + join_buffers
                        + 8 * self.integer_bytes, label="PE final image join")


def host_pe_budget():
    """Return a host-only operation budget, or preserve the legacy PE caps."""
    if sys.platform != "win32":
        return None
    profile = _host_profile()
    if profile is None:
        return None
    allowance = _windows_allowance()
    if allowance is None or allowance < 32:
        return None
    # Keep the existing work allowance. Suffix planning charges its additional
    # N visits alongside input validation 2N, merge N, post-merge 2N and final
    # address validation 2N; it does not increase this operational limit.
    return PEHostBudget(allowance, 7 * (allowance // 32), **profile)
