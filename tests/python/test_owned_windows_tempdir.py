"""Unchanged production tempfile/Win32 bodies over a deterministic ABI model.

These are host controls and single-module IR/object emission checks, not
Windows execution or filesystem qualification. No test builds or links a
runtime archive, executes generated code, loads a DLL, or changes permissions.
"""
from __future__ import annotations

import ast
import gc
import ntpath
from pathlib import Path

import pytest

from tests.python.test_owned_tempfile_provider import Obj, Ptr, Runtime, ROOT


PRIVATE_DACL = 'D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;OW)'


class Node:
    def __init__(self, name, parent=None, attributes=16):
        self.name, self.parent, self.attributes = name, parent, attributes
        self.children = {}
        self.deleted = False
        self.pending = False
        self.target = None
        self.dacl = None
        if parent is not None:
            parent.children[name] = self


class WindowsRuntime(Runtime):
    def __init__(self):
        super().__init__()
        self.cwd = r'C:\base'
        self.roots = {r'C:\base': Node('base'), r'C:\outside': Node('outside')}
        self.live = {}
        self.next_handle = 100
        self.last_error = 999
        self.events = []
        self.enumerated = {}
        self.before_open = None
        self.fail_security = False
        self.fail_parent_close = False
        self.fail_local_free = False
        self.fail_remove = False
        self.close_failure_once = False
        self.malformed = None
        self.environment = {}
        self.ns.update({
            'target_sys_platform': lambda: self.cstr('win32'),
            'utf16': lambda p: self.wide(self.raw(p).decode()),
            'windows_full_path': self.full_path,
            'py_os_path_join': lambda parts: self.string(ntpath.join(*(x.value for x in parts.value))),
            'py_os_path_abspath': lambda value: self.string(self.absolute(value.value)),
            'py_os_path_exists': lambda value: int(self.lookup(value.value) is not None),
            'getenv': lambda name: self.cstr(self.environment[self.raw(name).decode()]) if self.raw(name).decode() in self.environment else None,
            'CreateFileW': self.open_parent, 'CloseHandle': self.close_handle,
            'NtCreateFile': self.nt_create,
            'GetLastError': lambda: self.last_error,
            'RtlNtStatusToDosError': self.dos_error,
            'GetFileInformationByHandleEx': self.get_info,
            'SetFileInformationByHandle': self.set_info,
            'ConvertStringSecurityDescriptorToSecurityDescriptorW': self.security,
            'LocalFree': self.local_free,
        })
        tree = ast.parse((ROOT / 'pcc/runtime/py/freestanding_windows.py').read_text())
        functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ('error', 'error_code')]
        for node in functions:
            node.decorator_list = []
        exec(compile(ast.Module(functions, type_ignores=[]), 'windows-errors', 'exec'), self.ns)
        self.load(ROOT / 'pcc/runtime/py/freestanding_windows_directory.py')
        self.ns['windows_tempdir_create'] = self.ns['tempdir_create']
        self.ns['windows_tempdir_remove_owned'] = self.ns['tempdir_remove_owned']
        self.ns['windows_tempdir_close_owned'] = self.ns['tempdir_close_owned']
        # A path-based cleanup is always a bug in the Windows manager path.
        self.ns['remove_tree'] = lambda *a: pytest.fail('Windows pathname cleanup')
        self.ns['readlink'] = lambda *a: pytest.fail('Windows pathname readlink')

    def wide(self, text):
        data = text.encode('utf-16-le') + b'\0\0'
        pointer = self.alloc(len(data))
        pointer.memory.data[:] = data
        return pointer

    def wide_text(self, pointer, length=None):
        if length is None:
            length = 0
            while self.byte(pointer, length) or self.byte(pointer, length + 1):
                length += 2
        return self.raw_count(pointer, length).decode('utf-16-le')

    def absolute(self, path):
        return ntpath.normpath(ntpath.join(self.cwd, path))

    def full_path(self, path, output, capacity):
        data = self.absolute(self.raw(path).decode()).encode() + b'\0'
        assert len(data) <= capacity
        output.memory.data[output.offset:output.offset + len(data)] = data
        return output

    def lookup(self, path):
        path = self.absolute(path).rstrip('\\')
        if path in self.roots:
            return self.roots[path]
        parent, name = ntpath.split(path)
        if parent == path:
            return None
        node = self.lookup(parent) if parent else None
        return node.children.get(name) if node else None

    def handle(self, node, kind='owned'):
        self.next_handle += 1
        self.live[self.next_handle] = (node, kind)
        return self.next_handle

    def open_parent(self, name, access, share, security, disposition, flags, template):
        spelling = self.wide_text(name)
        self.events.append(('parent', spelling, access, share, flags))
        assert access == 128 | 32 and share == 1 and disposition == 3
        assert flags == 35651584 and security is None and template is None
        node = self.lookup(spelling)
        if node is None:
            self.last_error = 3
            return -1
        return self.handle(node, 'parent')

    def security(self, text, revision, output, length):
        assert revision == 1 and length is None
        assert self.wide_text(text) == PRIVATE_DACL
        self.events.append(('security', PRIVATE_DACL))
        if self.fail_security:
            self.last_error = 5
            return 0
        descriptor = self.alloc(20)
        self.write(descriptor, 0, PRIVATE_DACL)
        self.write(output, 0, descriptor)
        return 1

    def local_free(self, descriptor):
        self.events.append(('local_free',))
        if self.fail_local_free:
            return descriptor
        self.free(descriptor)
        return None

    @staticmethod
    def nt_status(value):
        return value - (1 << 32)

    def dos_error(self, status):
        self.events.append(('nt_error', status))
        return {
            self.nt_status(0xC0000035): 183,
            self.nt_status(0xC0000034): 2,
            self.nt_status(0xC0000022): 5,
            self.nt_status(0xC000050B): 4395,
        }.get(status, 31)

    def nt_create(self, output, access, attrs, iosb, allocation, attributes,
                  share, disposition, options, ea, ea_length):
        assert self.read(attrs, 0) == 48
        assert self.read(attrs, 24) == 64 | 4096
        assert access == 65536 | 1048576 | 128 | 1 and share == 1
        assert options == 2097152 | 32 | (1 if disposition == 2 else 0)
        assert allocation is None and ea is None and ea_length == 0
        text = self.read(attrs, 16)
        packed = self.read(text, 0)
        assert packed & 65535 == packed >> 16
        name = self.wide_text(self.read(text, 8), packed & 65535)
        parent = self.live[self.read(attrs, 8)][0]
        self.events.append(('relative', parent.name, name, disposition))
        if self.before_open:
            callback, self.before_open = self.before_open, None
            callback(parent, name, disposition)
        if parent.attributes & 1024:
            return self.nt_status(0xC000050B)
        if disposition == 2:
            descriptor = self.read(attrs, 32)
            assert self.read(descriptor, 0) == PRIVATE_DACL
            if name in parent.children:
                self.last_error = 2  # Deliberately stale; NTSTATUS must win.
                return self.nt_status(0xC0000035)
            node = Node(name, parent)
            node.dacl = PRIVATE_DACL
            self.write(iosb, 8, 2)
        else:
            assert disposition == 1 and self.read(attrs, 32) is None
            node = parent.children.get(name)
            if node is None:
                return self.nt_status(0xC0000034)
            self.write(iosb, 8, 1)
        self.write(output, 0, self.handle(node))
        return 0

    def get_info(self, handle, kind, output, capacity):
        node = self.live[handle][0]
        if kind == 9:
            assert capacity == 8
            self.write(output, 0, node.attributes)
            self.write(output, 4, 0xA000001D if node.attributes & 1024 else 0)
            return 1
        assert kind in (14, 15) and capacity == 65536
        assert node.attributes & 16 and not node.attributes & 1024
        self.events.append(('enumerate', node.name))
        if kind == 15:
            self.enumerated[handle] = iter(['.', '..', *node.children])
        names = []
        for _ in range(3):
            try:
                names.append(next(self.enumerated[handle]))
            except StopIteration:
                break
        if not names:
            self.last_error = 18
            return 0
        self.memset(output, 0, capacity)
        offset = 0
        for index, name in enumerate(names):
            data = name.encode('utf-16-le')
            size = (68 + len(data) + 7) & ~7
            entry = Ptr(output.memory, output.offset + offset)
            self.write(entry, 0, size if index + 1 < len(names) else 0)
            self.write(entry, 60, len(data))
            entry.memory.data[entry.offset + 68:entry.offset + 68 + len(data)] = data
            offset += size
        if self.malformed:
            field, value = self.malformed
            self.write(output, field, value)
        return 1

    def set_info(self, handle, kind, data, capacity):
        assert kind == 4 and capacity == 1 and self.byte(data, 0) == 1
        node = self.live[handle][0]
        self.events.append(('delete', node.name))
        if self.fail_remove:
            self.last_error = 5
            return 0
        if node.children and not node.attributes & 1024:
            self.last_error = 145
            return 0
        node.pending = True
        return 1

    def close_handle(self, handle):
        node, kind = self.live[handle]
        self.events.append(('close', node.name, kind))
        if self.fail_parent_close and kind == 'parent':
            self.fail_parent_close = False
            self.last_error = 6
            return 0
        if self.close_failure_once and kind == 'owned':
            self.close_failure_once = False
            self.last_error = 6
            return 0
        del self.live[handle]
        if node.pending:
            node.deleted = True
            if node.parent is not None:
                assert node.parent.children[node.name] is node
                del node.parent.children[node.name]
        return 1

    def new_manager(self, **kwargs):
        return self.create(self.cwd, **kwargs)


@pytest.fixture
def windows():
    model = WindowsRuntime()
    yield model
    model.error = None
    model.fail_remove = False
    model.ns['_td_shutdown']()
    assert not model.frames and not model.handles and not model.leases
    assert not model.live


def test_windows_manager_owns_private_unique_directory_and_nested_contents(windows):
    first = windows.new_manager(prefix='前缀_', suffix='_尾')
    second = windows.new_manager()
    assert windows.error is None
    assert windows.name(first) != windows.name(second)
    node = windows.lookup(windows.name(first))
    assert node.dacl == PRIVATE_DACL
    child = Node('nested', node)
    Node('file.txt', child, 128)
    outside = windows.roots[r'C:\outside']
    Node('keep', outside, 128)
    link = Node('junction', node, 16 | 1024)
    link.target = outside
    file_link = Node('unknown-tag', node, 1024)
    file_link.target = outside
    assert windows.method(first, 'enter') is first.attrs['name']
    assert windows.method(first, 'cleanup') is windows.none
    assert node.deleted and child.deleted and link.deleted and file_link.deleted
    assert 'keep' in outside.children
    assert not any(event == ('enumerate', 'junction') for event in windows.events)
    assert not any(event == ('enumerate', 'unknown-tag') for event in windows.events)
    assert windows.method(first, 'cleanup') is windows.none
    windows.method(second, 'cleanup')


def test_windows_name_mutation_and_recreated_path_never_gain_ownership(windows):
    manager = windows.new_manager()
    original = windows.lookup(windows.name(manager))
    manager.attrs['name'] = windows.string(r'C:\outside')
    windows.method(manager, 'cleanup')
    assert original.deleted and not windows.roots[r'C:\outside'].deleted
    replacement = Node(original.name, original.parent)
    manager.attrs['name'] = windows.string(ntpath.join(windows.cwd, replacement.name))
    windows.method(manager, 'cleanup')
    assert not replacement.deleted


@pytest.mark.parametrize('failure', ('state', 'attribute'))
def test_windows_constructor_failure_uses_owned_rollback(windows, failure):
    windows.fail_state = failure == 'state'
    windows.fail_setattr = failure == 'attribute'
    windows.new_manager(delete=False)
    assert windows.error.value[0] == 19
    assert not windows.roots[windows.cwd].children
    assert not windows.live and not windows.handles


@pytest.mark.parametrize('boundary', ('pcc_gc_scheduler_root_register_handle', 'py_weakref_new'))
def test_windows_registration_failure_rolls_back_untransferred_owner(windows, boundary):
    original = windows.ns[boundary]
    windows.ns[boundary] = lambda *args: None
    windows.new_manager(delete=False)
    windows.ns[boundary] = original
    assert windows.error.value[0] == 19
    assert not windows.roots[windows.cwd].children
    assert not windows.live and not windows.handles


@pytest.mark.parametrize('failure', ('parent_close', 'local_free'))
def test_windows_release_failure_after_create_reports_error_and_rolls_back_owner(windows, failure):
    windows.fail_parent_close = failure == 'parent_close'
    windows.fail_local_free = failure == 'local_free'
    manager = windows.new_manager()
    assert windows.error.attrs['errno'] == (9 if failure == 'parent_close' else 5)
    assert 'name' not in manager.attrs and not windows.roots[windows.cwd].children
    assert not windows.handles
    # A failed OS release is reported; the model still owns that parent's
    # failed-close handle. Releasing it here is test cleanup, not a guarantee
    # that the production API can recover from every CloseHandle failure.
    for handle, (_, kind) in list(windows.live.items()):
        assert failure == 'parent_close' and kind == 'parent'
        assert windows.close_handle(handle)


@pytest.mark.parametrize('ignore', (False, True))
def test_windows_cleanup_failure_keeps_exact_owner_for_retry(windows, ignore):
    manager = windows.new_manager(ignore=ignore)
    node = windows.lookup(windows.name(manager))
    windows.fail_remove = True
    windows.method(manager, 'cleanup')
    assert (windows.error is None) is ignore
    assert windows.live and windows.handles and not node.deleted
    windows.error = None
    windows.fail_remove = False
    windows.method(manager, 'cleanup')
    assert node.deleted and not windows.live and not windows.handles


def test_windows_weakref_finalizer_and_delete_false_release_handles(windows):
    first = windows.new_manager()
    node = windows.lookup(windows.name(first))
    del first
    gc.collect()
    assert node.deleted and not windows.live
    retained = windows.new_manager(delete=False)
    node = windows.lookup(windows.name(retained))
    del retained
    gc.collect()
    assert not node.deleted and not windows.live and not windows.handles


def test_windows_shutdown_failure_releases_owner_and_terminates(windows):
    manager = windows.new_manager()
    node = windows.lookup(windows.name(manager))
    windows.fail_remove = True
    windows.ns['_td_shutdown']()
    assert not node.deleted and not windows.live and not windows.handles
    assert windows.error is None


def test_windows_context_exit_preserves_body_exception_contract(windows):
    manager = windows.new_manager()
    body_error = Obj('exception', (2, 'body error'))
    assert windows.method(manager, 'exit', windows.string('ValueError'), body_error, windows.none) is windows.none
    assert windows.error is None and body_error.value == (2, 'body error')
    assert windows.lookup(windows.name(manager)) is None


def test_windows_public_mkdtemp_transfers_path_without_finalizer(windows):
    args = Obj('tuple', [windows.none, windows.none, windows.string(windows.cwd)])
    name = windows.ns['_td_mkdtemp_entry'](None, args)
    assert windows.error is None and windows.lookup(name.value) is not None
    assert not windows.live and not windows.handles
    windows.ns['_td_shutdown']()
    assert windows.lookup(name.value) is not None


def test_windows_public_mkdtemp_failed_close_rolls_back_and_reports_error(windows):
    windows.close_failure_once = True
    args = Obj('tuple', [windows.none, windows.none, windows.string(windows.cwd)])
    assert windows.ns['_td_mkdtemp_entry'](None, args) is None
    assert windows.error.attrs['errno'] == 9
    assert not windows.live and not windows.handles
    assert not windows.roots[windows.cwd].children


def test_windows_delete_truth_failure_detaches_without_deleting(windows):
    manager = windows.new_manager()
    node = windows.lookup(windows.name(manager))
    state = manager.attrs['_pcc_finalizer_state']
    original = windows.ns['py_obj_truthy']
    def fail(value):
        windows.error = Obj('exception', (2, 'truth conversion failed'))
        return 0
    windows.ns['py_obj_truthy'] = fail
    windows.ns['_td_finalize_state'](state)
    windows.ns['py_obj_truthy'] = original
    assert windows.error.value == (2, 'truth conversion failed')
    assert not node.deleted and not windows.live and not windows.handles


def test_windows_collision_uses_ntstatus_not_stale_last_error(windows):
    first = windows.new_manager()
    windows.global_store('pcc_platform_mkdtemp_counter', 0)
    second = windows.new_manager()
    assert windows.error is None and windows.name(first) != windows.name(second)
    assert len([e for e in windows.events if e[0] == 'relative']) == 3
    assert any(e[0] == 'nt_error' and e[1] == windows.nt_status(0xC0000035) for e in windows.events)


def test_windows_security_failure_prevents_creation(windows):
    windows.fail_security = True
    manager = windows.new_manager()
    assert windows.error.attrs['errno'] == 13
    assert 'name' not in manager.attrs
    assert not any(e[0] == 'relative' for e in windows.events)
    assert not windows.live and not windows.roots[windows.cwd].children


def test_windows_missing_security_descriptor_never_falls_back_to_inheritance(windows):
    windows.ns['ConvertStringSecurityDescriptorToSecurityDescriptorW'] = lambda *args: 1
    manager = windows.new_manager()
    assert windows.error.attrs['errno'] == 5 and 'name' not in manager.attrs
    assert not any(e[0] == 'relative' for e in windows.events)
    assert not windows.live and not windows.roots[windows.cwd].children


def test_windows_parent_reparse_refused_before_child_creation(windows):
    windows.roots[windows.cwd].attributes |= 1024
    manager = windows.new_manager()
    assert windows.error.attrs['errno'] == 40 and 'name' not in manager.attrs
    assert not any(e[0] == 'relative' for e in windows.events)
    assert not windows.live


def test_windows_injected_nt_reparse_rejection_aborts_creation(windows):
    # The model supplies an NT reparse failure. This does not establish how
    # Windows handles a tag changed after opening the parent HANDLE.
    def replace(parent, name, disposition):
        parent.attributes |= 1024
    windows.before_open = replace
    manager = windows.new_manager()
    assert windows.error.attrs['errno'] == 40 and 'name' not in manager.attrs
    assert not windows.roots[windows.cwd].children and not windows.live


def test_windows_child_replaced_by_reparse_after_enumeration_never_followed(windows):
    manager = windows.new_manager()
    node = windows.lookup(windows.name(manager))
    child = Node('swapped', node)
    outside = windows.roots[r'C:\outside']
    Node('keep', outside, 128)
    replacement = []
    def replace(parent, name, disposition):
        assert parent is node and name == 'swapped' and disposition == 1
        link = Node(name, parent, 16 | 1024)
        link.target = outside
        replacement.append(link)
    windows.before_open = replace
    windows.method(manager, 'cleanup')
    assert windows.error is None and not child.deleted and node.deleted
    assert len(replacement) == 1 and replacement[0].deleted
    assert 'keep' in outside.children
    assert ('enumerate', 'swapped') not in windows.events


@pytest.mark.parametrize('field,value', ((0, 7), (0, 65536), (60, 1), (60, 65534), (60, 0)))
def test_windows_malformed_directory_records_fail_closed(windows, field, value):
    manager = windows.new_manager()
    node = windows.lookup(windows.name(manager))
    Node('keep', node, 128)
    windows.malformed = (field, value)
    windows.method(manager, 'cleanup')
    assert windows.error is not None and not node.deleted and 'keep' in node.children
    windows.malformed = None
    windows.error = None


@pytest.mark.parametrize('name', ('..', '.', r'a\b', 'a/b', 'a:b', 'x\0y'))
def test_windows_relative_names_are_single_components(windows, name):
    output = windows.alloc(8)
    parent = windows.handle(windows.roots[windows.cwd], 'parent')
    status = windows.ns['tempdir_open_relative'](parent, windows.wide(name), len(name.encode('utf-16-le')), 0, output)
    assert status == -22 and windows.read(output, 0) is None
    windows.close_handle(parent)


@pytest.mark.parametrize('prefix,suffix', (
    ('CON.', ''), ('normal', '.'), ('normal', ' '), ('CONIN$.', ''),
    ('前缀', '.bad*'), ('normal', '.bad?'), ('前缀', '<bad'),
))
def test_windows_ambiguous_dos_leaf_is_rejected_before_creation(windows, prefix, suffix):
    manager = windows.new_manager(prefix=prefix, suffix=suffix)
    assert windows.error is not None and 'name' not in manager.attrs
    assert not windows.live and not windows.roots[windows.cwd].children


def test_windows_default_directory_probe_owned_cleanup_and_frozen_absolute_name(windows):
    windows.environment['TEMP'] = windows.cwd
    args = Obj('tuple', [windows.none, windows.string('prefix_'), windows.none])
    name = windows.ns['_td_mkdtemp_entry'](None, args)
    assert windows.error is None and name.value.startswith(windows.cwd + '\\prefix_')
    assert len(windows.roots[windows.cwd].children) == 1
    assert not windows.live
    assert any(e[0] == 'delete' and e[1].startswith('.pcc_probe_') for e in windows.events)


def test_windows_relative_directory_name_is_frozen_before_creation(windows):
    original = windows.cwd
    def chdir(parent, name, disposition):
        windows.cwd = r'C:\outside'
    windows.before_open = chdir
    manager = windows.create('.')
    assert windows.error is None and windows.name(manager).startswith(original + '\\')
    node = windows.lookup(windows.name(manager))
    assert node is not None and node.parent is windows.roots[original]
    windows.method(manager, 'cleanup')
    assert node.deleted and not windows.roots[r'C:\outside'].children


def test_windows_depth_limit_closes_children_and_keeps_owner_retryable(windows):
    manager = windows.new_manager()
    root = windows.lookup(windows.name(manager))
    node = root
    for i in range(4):
        node = Node(str(i), node)
    windows.ns['_TD_MAX_DEPTH'] = 3
    windows.method(manager, 'cleanup')
    assert windows.error.attrs['errno'] == 36 and not root.deleted
    assert len(windows.live) == 1
    windows.ns['_TD_MAX_DEPTH'] = 256
    windows.error = None


def test_windows_named_system_imports_are_exact():
    from pcc.backend.pe_x86_64 import system_dll
    assert system_dll('NtCreateFile') == 'NTDLL.dll'
    assert system_dll('RtlNtStatusToDosError') == 'NTDLL.dll'
    assert system_dll('SetFileInformationByHandle') == 'KERNEL32.dll'
    assert system_dll('ConvertStringSecurityDescriptorToSecurityDescriptorW') == 'ADVAPI32.dll'


@pytest.mark.parametrize('module,target', (
    ('freestanding_windows_directory', 'x86_64-pc-windows-msvc'),
    ('freestanding_windows', 'x86_64-pc-windows-msvc'),
    ('freestanding_platform_fs', 'x86_64-pc-windows-msvc'),
    ('freestanding_platform_fs', 'x86_64-unknown-linux-gnu'),
    ('freestanding_platform_fs', 'aarch64-apple-darwin'),
    ('py_tempfile', 'x86_64-pc-windows-msvc'),
))
def test_tempdir_changed_module_emission_only(tmp_path, module, target):
    """Closed-world validation and owned emission; no link or execution."""
    from pcc.backend.owned_object_emit import emit_owned_object
    from pcc.frontends.python.owned_runtime_build import _compile_runtime_module, runtime_ir_passes
    from pcc.ir.optimization.driver import optimize_ir

    runtime = ROOT / 'pcc/runtime'
    output = tmp_path / (module + '.ll')
    _compile_runtime_module(module, str(runtime / 'py' / (module + '.py')), str(output), target)
    text = optimize_ir(output.read_text(), runtime_ir_passes(str(runtime)))
    payload = emit_owned_object(text, target)
    magic = b'\x7fELF' if 'linux' in target else (b'\xcf\xfa\xed\xfe' if 'darwin' in target else b'\x64\x86')
    assert payload.startswith(magic)
    if module == 'freestanding_platform_fs' and 'windows' not in target:
        import re
        has_windows_calls = bool(re.search(r'\bcall\b[^\n]*@"?pcc_win_tempdir_', text))
        assert not has_windows_calls, 'Non-Windows object must not reference Windows-only leaves'
    if module == 'freestanding_windows_directory':
        from pcc.backend.coff_x86_64 import parse_object
        names = {symbol.name for symbol in parse_object(payload).symbols if symbol.external and not symbol.section}
        assert {'NtCreateFile', 'RtlNtStatusToDosError', 'SetFileInformationByHandle',
                'ConvertStringSecurityDescriptorToSecurityDescriptorW'} <= names
