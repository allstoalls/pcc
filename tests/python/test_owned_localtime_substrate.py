"""Execute owned calendar/TZif/POSIX bodies with an explicit raw-memory model.

The host OS supplies fixture bytes, and CPython is the labeled calendar oracle.
This is component execution; it is not owned native artifact qualification.
"""
from __future__ import annotations
import ast
from contextlib import contextmanager
import os
from pathlib import Path
import struct
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]


class Buffer:
    def __init__(self, size):
        self.bytes = bytearray(size)
        self.pointers = {}
        self.alive = True


class Ptr:
    def __init__(self, owner, offset=0):
        self.owner, self.offset = owner, offset


class Memory:
    def __init__(self, zone, fail_allocation=0):
        self.zone = zone
        self.error = 0
        self.allocations = 0
        self.fail_allocation = fail_allocation
        self.globals = {'pcc_time_zone_names': None}
        self.held = False
        ns = dict(i64=int, c_ptr=object, stack_alloc=self.stack, malloc=self.malloc,
                  free=self.free, null=lambda: None, ptr_is_null=lambda p:p is None,
                  ptr_add=self.add, cstr=self.text, load_i8=lambda p,o:self.load(p,o,1),
                  load_i32=lambda p,o:self.load(p,o,4), load_i64=lambda p,o:self.load(p,o,8),
                  store_i8=lambda p,o,v:self.store(p,o,v,1), store_i32=lambda p,o,v:self.store(p,o,v,4),
                  store_i64=lambda p,o,v:self.store(p,o,v,8), load_ptr=self.load_ptr,
                  store_ptr=self.store_ptr, global_addr=lambda s:s,
                  global_load_ptr=self.globals.get, global_store_ptr=self.globals.__setitem__,
                  atomic_test_and_set=self.lock, atomic_clear=self.unlock,
                  getenv=lambda _name:None if zone is None else self.text(zone),
                  set_errno=self.set_errno, open_readonly=self.open, read=self.read,
                  close=lambda fd:os.close(fd))
        self.calendar = self.module('freestanding_time_calendar.py', ns)
        self.calendar = self.module('freestanding_time_format.py', self.calendar)
        self.posix = self.module('freestanding_posix_timezone.py', dict(ns,
            days_from_civil=self.calendar['days_from_civil'], breakdown=self.calendar['breakdown']))
        self.linux = self.module('freestanding_linux_time.py', dict(ns,
            breakdown=self.calendar['breakdown'], posix_zone=self.posix['posix_zone'],
            intern_zone=self.calendar['intern_zone']))
        # be's explicit i64 return wraps an eight-byte TZif integer. Model that
        # ABI conversion, including negative historical transition timestamps.
        be = self.linux['be']
        self.linux['be'] = lambda *a: ((be(*a) + 2**63) % 2**64) - 2**63

    @staticmethod
    def module(name, ns):
        class Intrinsics(ast.NodeTransformer):
            def visit_ImportFrom(self, node):
                return ast.Pass()
        path = ROOT / 'pcc/runtime/py' / name
        nodes = []
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.FunctionDef):
                node.decorator_list = []
                nodes.append(Intrinsics().visit(node))
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                nodes.append(node)
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(path), 'exec'), ns)
        return ns

    @staticmethod
    def stack(size):
        return Ptr(Buffer(size))

    def malloc(self, size):
        self.allocations += 1
        return None if self.allocations == self.fail_allocation else self.stack(size)

    @staticmethod
    def free(ptr):
        if ptr is not None:
            assert ptr.owner.alive
            ptr.owner.alive = False

    def text(self, value):
        data = value.encode() + b'\0'
        result = self.stack(len(data))
        result.owner.bytes[:] = data
        return result

    @staticmethod
    def add(ptr, offset):
        return Ptr(ptr.owner, ptr.offset + offset)

    @staticmethod
    def load(ptr, offset, size):
        assert ptr.owner.alive
        data = ptr.owner.bytes[ptr.offset+offset:ptr.offset+offset+size]
        assert len(data) == size
        return int.from_bytes(data, 'little', signed=size != 1)

    @staticmethod
    def store(ptr, offset, value, size):
        assert ptr.owner.alive
        data = (value % (1 << (size*8))).to_bytes(size, 'little')
        ptr.owner.bytes[ptr.offset+offset:ptr.offset+offset+size] = data

    @staticmethod
    def load_ptr(ptr, offset):
        assert ptr.owner.alive
        return ptr.owner.pointers.get(ptr.offset+offset)

    @staticmethod
    def store_ptr(ptr, offset, value):
        assert ptr.owner.alive
        ptr.owner.pointers[ptr.offset+offset] = value

    def lock(self, *args):
        previous = self.held
        self.held = True
        return previous

    def unlock(self, *args):
        assert self.held
        self.held = False

    def set_errno(self, number):
        self.error = number

    @staticmethod
    def string(ptr):
        assert ptr.owner.alive
        data = ptr.owner.bytes[ptr.offset:]
        return bytes(data[:data.index(0)]).decode()

    def open(self, path):
        try:
            return os.open(self.string(path), os.O_RDONLY)
        except OSError as error:
            return -error.errno

    @staticmethod
    def read(fd, ptr, capacity):
        try:
            data = os.read(fd, capacity)
        except OSError as error:
            return -error.errno
        ptr.owner.bytes[ptr.offset:ptr.offset+len(data)] = data
        return len(data)

    def convert(self, timestamp):
        clock, output = self.stack(8), self.stack(64)
        self.store(clock, 0, timestamp, 8)
        result = self.linux['localtime_r'](clock, output)
        if result is None:
            return None
        fields = tuple(self.load(output, offset, 4) for offset in (20,16,12,8,4,0,24,28,32))
        fields = (fields[0]+1900, fields[1]+1, *fields[2:6], (fields[6]+6)%7, fields[7]+1, fields[8])
        return fields, self.string(self.load_ptr(output,48)), self.load(output,40,8)


@contextmanager
def timezone(zone):
    previous = os.environ.get('TZ')
    if zone is None:
        os.environ.pop('TZ', None)
    else:
        os.environ['TZ'] = zone
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = previous
        time.tzset()


@pytest.mark.parametrize('zone', ('UTC0', 'GMT0', 'America/New_York', 'Asia/Kolkata',
                                 'Australia/Sydney', 'XST-5:30XDT-6:30,M3.2.0/2,M11.1.0/2'))
@pytest.mark.parametrize('timestamp', (-2208988800, -1, 0, 951782400, 1710053999, 1710054000,
                                      1730613599, 1730613600, 4102444800))
def test_owned_calendar_timezone_fields_match_reference(zone, timestamp):
    memory = Memory(zone)
    with timezone(zone):
        expected = time.localtime(timestamp)
    assert memory.convert(timestamp) == (tuple(expected), expected.tm_zone, expected.tm_gmtoff)
    assert memory.error == 0
    assert not memory.held


@pytest.mark.parametrize('timestamp', (-2**63, 2**63-1))
@pytest.mark.parametrize('zone', ('UTC0', 'America/New_York', 'XST-5:30'))
def test_calendar_overflow_retains_platform_error(zone, timestamp):
    memory = Memory(zone)
    with timezone(zone), pytest.raises(OSError):
        time.localtime(timestamp)
    assert memory.convert(timestamp) is None
    # This models the Linux port, which reports Linux's EOVERFLOW; a Darwin
    # host numbers the same error 84.
    assert memory.error == 75


@pytest.mark.parametrize('allocation', (1,2,3))
def test_time_allocation_failures_report_enomem(allocation):
    memory = Memory('America/New_York', fail_allocation=allocation)
    assert memory.convert(0) is None
    assert memory.error == 12



def test_unowned_timezone_forms_fail_with_explicit_capability_status():
    memory = Memory('/unavailable/owned-time-zone')
    assert memory.convert(0) is None
    assert memory.error == 95
