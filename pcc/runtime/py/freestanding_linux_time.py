"""Linux localtime from TZif files using owned filesystem primitives."""

from pcc import i64
from pcc.extern import c_abi_export, c_ptr, c_int32, c_int64, c_void, extern
from pcc.unsafe import (
    open_readonly, read, close, malloc, free, ptr_is_null, null, ptr_add,
    load_i8, load_i32, load_i64, store_i8, global_addr, global_load_ptr,
    global_store_ptr, define_thread_local_ptr_null, cstr, stack_alloc, load_ptr,
)

__pcc_freestanding__ = True

set_errno = extern("pcc_errno_set", (c_int32,), c_void)
getenv = extern("pcc_platform_getenv", (c_ptr,), c_ptr)
breakdown = extern("pcc_time_breakdown", (c_int64, c_int64, c_int64, c_ptr, c_ptr), c_int64)
posix_zone = extern("pcc_time_posix_zone", (c_ptr, c_int64, c_ptr), c_int64)
intern_zone = extern("pcc_time_intern_zone", (c_ptr, c_int64), c_ptr)


@c_abi_export("pcc_linux_time_length")
def length(text: c_ptr) -> i64:
    size: i64 = 0
    while load_i8(text, size) != 0:
        size = size + 1
    return size


@c_abi_export("pcc_linux_time_same")
def same(a: c_ptr, b: c_ptr) -> i64:
    index: i64 = 0
    while True:
        byte: i64 = load_i8(a, index)
        if byte != load_i8(b, index):
            return 0
        if byte == 0:
            return 1
        index = index + 1
    return 0


@c_abi_export("pcc_linux_time_be")
def be(buffer: c_ptr, offset: i64, width: i64) -> i64:
    value: i64 = 0
    index: i64 = 0
    while index < width:
        value = (value << 8) | (load_i8(buffer, offset + index) & 255)
        index = index + 1
    if width == 4 and value >= 2147483648:
        value = value - 4294967296
    return value


@c_abi_export("pcc_linux_time_zone_path")
def zone_path() -> c_ptr:
    setting = getenv(cstr("TZ"))
    if ptr_is_null(setting):
        setting = cstr("/etc/localtime")
    if load_i8(setting, 0) == 58:
        setting = ptr_add(setting, 1)
    if load_i8(setting, 0) == 0 or same(setting, cstr("UTC")) or same(setting, cstr("UTC0")) or same(setting, cstr("GMT")) or same(setting, cstr("GMT0")):
        return cstr("")
    prefix = cstr("/usr/share/zoneinfo/")
    prefix_size: i64 = length(prefix)
    if load_i8(setting, 0) == 47:
        prefix_size = 0
    size: i64 = length(setting)
    path = malloc(prefix_size + size + 1)
    if ptr_is_null(path):
        set_errno(12)
        return null()
    index: i64 = 0
    while index < prefix_size:
        store_i8(path, index, load_i8(prefix, index))
        index = index + 1
    index = 0
    while index <= size:
        store_i8(path, prefix_size + index, load_i8(setting, index))
        index = index + 1
    return path


@c_abi_export("localtime_r")
def localtime_r(clock: c_ptr, output: c_ptr) -> c_ptr:
    # Unsupported TZif/rule forms have a distinct capability failure. More
    # specific allocation, I/O and calendar failures replace this value.
    set_errno(95)
    timestamp: i64 = load_i64(clock, 0)
    path = zone_path()
    if ptr_is_null(path):
        set_errno(12)
        return null()
    if load_i8(path, 0) == 0:
        abbreviation = cstr("UTC")
        setting = getenv(cstr("TZ"))
        if not ptr_is_null(setting):
            if load_i8(setting, 0) == 58:
                setting = ptr_add(setting, 1)
            if same(setting, cstr("GMT")) or same(setting, cstr("GMT0")):
                abbreviation = cstr("GMT")
        if breakdown(timestamp, 0, 0, abbreviation, output) == 0:
            set_errno(0)
            return output
        set_errno(75)
        return null()
    fd: i64 = open_readonly(path)
    free(path)
    if fd < 0:
        setting = getenv(cstr("TZ"))
        if ptr_is_null(setting) or load_i8(setting, 0) == 58:
            return null()
        zone = stack_alloc(32)
        zone_status: i64 = posix_zone(setting, timestamp, zone)
        if zone_status != 0:
            if zone_status < -1:
                set_errno(0 - zone_status)
            return null()
        return finish(timestamp, load_i64(zone, 0), load_i64(zone, 8),
                      load_ptr(zone, 16), load_i64(zone, 24), output)
    capacity: i64 = 1048576
    data = malloc(capacity + 1)
    if ptr_is_null(data):
        close(fd)
        set_errno(12)
        return null()
    size: i64 = 0
    while size < capacity:
        result: i64 = read(fd, ptr_add(data, size), capacity - size)
        if result == -4:
            continue
        if result < 0:
            set_errno(0 - result)
            size = 0
            break
        if result == 0:
            break
        size = size + result
    close(fd)
    if size < 44 or size == capacity or load_i8(data, 0) != 84 or load_i8(data, 1) != 90 or load_i8(data, 2) != 105 or load_i8(data, 3) != 102:
        free(data)
        return null()
    header: i64 = 0
    width: i64 = 4
    if load_i8(data, 4) == 50 or load_i8(data, 4) == 51 or load_i8(data, 4) == 52:
        times: i64 = be(data, 32, 4)
        types: i64 = be(data, 36, 4)
        chars: i64 = be(data, 40, 4)
        leap: i64 = be(data, 28, 4)
        header = 44 + times * 5 + types * 6 + chars + leap * 8 + be(data, 20, 4) + be(data, 24, 4)
        width = 8
    if header < 0 or header + 44 > size:
        free(data)
        return null()
    times: i64 = be(data, header + 32, 4)
    types: i64 = be(data, header + 36, 4)
    chars: i64 = be(data, header + 40, 4)
    if times < 0 or times > 100000 or types <= 0 or types > 256 or chars < 0:
        free(data)
        return null()
    indices: i64 = header + 44 + times * width
    records: i64 = indices + times
    strings: i64 = records + types * 6
    if strings + chars > size or be(data, header + 28, 4) != 0:
        free(data)
        return null()  # leap-second TZif variants need their own time scale
    store_i8(data, size, 0)
    selected: i64 = 0
    lo: i64 = 0
    hi: i64 = times
    while lo < hi:
        middle: i64 = (lo + hi) // 2
        when: i64 = be(data, header + 44 + middle * width, width)
        if when <= timestamp:
            lo = middle + 1
        else:
            hi = middle
    if lo > 0:
        selected = load_i8(data, indices + lo - 1) & 255
    if selected >= types:
        free(data)
        return null()
    offset: i64 = be(data, records + selected * 6, 4)
    daylight: i64 = load_i8(data, records + selected * 6 + 4) & 255
    label_offset: i64 = load_i8(data, records + selected * 6 + 5) & 255
    if label_offset >= chars:
        free(data)
        return null()
    label_source = ptr_add(data, strings + label_offset)
    label_size: i64 = 0
    while label_offset + label_size < chars and load_i8(data, strings + label_offset + label_size) != 0:
        label_size = label_size + 1
    if label_offset + label_size >= chars:
        free(data)
        return null()
    footer: i64 = strings + chars + be(data, header + 20, 4) + be(data, header + 24, 4)
    if width == 8 and lo == times and footer >= 0 and footer + 1 < size and load_i8(data, footer) == 10:
        footer = footer + 1
        end: i64 = footer
        while end < size and load_i8(data, end) != 10:
            end = end + 1
        if end < size and end > footer:
            store_i8(data, end, 0)
            zone = stack_alloc(32)
            zone_status: i64 = posix_zone(ptr_add(data, footer), timestamp, zone)
            if zone_status != 0:
                free(data)
                if zone_status < -1:
                    set_errno(0 - zone_status)
                return null()
            offset = load_i64(zone, 0)
            daylight = load_i64(zone, 8)
            label_source = load_ptr(zone, 16)
            label_size = load_i64(zone, 24)
    # ``result`` is the i64 read count above; one type per local name.
    converted = finish(timestamp, offset, daylight, label_source, label_size, output)
    free(data)
    return converted


@c_abi_export("pcc_linux_time_finish")
def finish(timestamp: i64, offset: i64, daylight: i64, text: c_ptr, size: i64, output: c_ptr) -> c_ptr:
    label = intern_zone(text, size)
    if ptr_is_null(label):
        set_errno(12)
        return null()
    if breakdown(timestamp, offset, daylight, label, output) == 0:
        set_errno(0)
        return output
    set_errno(75)
    return null()
