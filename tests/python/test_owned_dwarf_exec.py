"""Real native execution/debugger checks for owned linked DWARF.

The scalar IR fixtures exercise backend/linker ownership. These checks are
separate from a current-source PCC1 build and whole Python debuggee admission.
"""
from __future__ import annotations

import platform
import shutil
import subprocess
import sys

import pytest

from pcc.backend import macho_spec as spec
from pcc.backend import native_object as native
from pcc.backend import macho_host_prepare as host
from pcc.backend.macho_exec import link_executable, link_prepared_executable
from pcc.backend.self_backend_aarch64_darwin import emit_aarch64_darwin_indexed_transport
from pcc.backend.self_backend_parse import parse_self_backend_module
from tests.python.test_macho_windowed_parity import _budget, _write_pcos
from tests.python.test_owned_dwarf import _dump
from tests.python.test_owned_source_debug_info import program

pytestmark = pytest.mark.skipif(sys.platform != 'darwin' or platform.machine() != 'arm64',
                                reason='native Darwin arm64 debugger acceptance')


def _units(tmp_path, *, bss=False):
    (tmp_path / 'callee.py').write_text('\n' * 20 + 'def add(a, b):\n    value = a + b\n    return value\n')
    (tmp_path / 'driver.py').write_text('def main():\n    value = add(30, 12)\n    return value\n')
    callee = program().replace('debuggee.py', 'callee.py').replace('/tmp/source', str(tmp_path))
    callee = callee.replace('line: 1,', 'line: 21,').replace('line: 2,', 'line: 22,').replace('line: 3,', 'line: 23,')
    main = program('entry:\n  %value = call i64 @add(i64 30, i64 12), !dbg !3\n  ret i64 %value, !dbg !4\n')
    main = main.replace('define i64 @add(i64 %a, i64 %b)', 'define i64 @main()')
    main = main.replace('name: "add"', 'name: "main"').replace('debuggee.py', 'driver.py').replace('/tmp/source', str(tmp_path))
    main += '\ndeclare i64 @add(i64, i64)\n'
    if bss:
        main += '@scratch = global [32768 x i64] zeroinitializer, align 16\n'
    objects = []
    for source in (main, callee):
        transport = emit_aarch64_darwin_indexed_transport(parse_self_backend_module(source))
        try:
            sections, undefined = transport.assemble_sections()
            objects.append(native.NativeObject.from_sections(sections, undefined=undefined))
        finally:
            if transport.encoded_line_records is not None:
                transport.encoded_line_records.close()
    return objects


def _run_image(tmp_path, image, name='program'):
    path = tmp_path / name
    path.write_bytes(image)
    path.chmod(0o755)
    run = subprocess.run([str(path)], capture_output=True, text=True, timeout=15)
    assert run.returncode == 42, (run.returncode, run.stdout, run.stderr)
    assert run.stdout == run.stderr == ''
    _dump(tmp_path, image)
    obj = spec.parse_object(image)
    segments = [c.body for c in obj.commands if c.cmd == spec.LC_SEGMENT_64]
    dwarf = next(s for s in segments if s['segname_str'] == '__DWARF')
    assert dwarf['initprot'] == dwarf['maxprot'] == spec.VM_PROT_READ
    assert dwarf['vmsize'] > 0 and dwarf['filesize'] > 0
    assert segments[-1]['segname_str'] == '__LINKEDIT'
    assert dwarf['fileoff'] + dwarf['filesize'] <= segments[-1]['fileoff']
    assert dwarf['vmaddr'] + dwarf['vmsize'] <= segments[-1]['vmaddr']
    for section in obj.sections():
        if section['segname_str'] == '__DWARF':
            assert section['nreloc'] == 0
    return path


@pytest.mark.parametrize('bss', [False, True])
def test_owned_debug_executable_runs_with_compact_file_and_bss_layout(tmp_path, bss):
    _run_image(tmp_path, link_executable(_units(tmp_path, bss=bss)))


@pytest.mark.parametrize('staged', [False, True])
def test_windowed_prepared_debug_executable_matches_normal_and_runs(tmp_path, staged):
    objects = _units(tmp_path)
    paths = _write_pcos(tmp_path, objects)
    budget = _budget()
    pending = [host.prepare_executable_from_paths(paths, compact_budget=budget, _staged_rows=staged)]
    try:
        if staged:
            from pcc.backend.macho_host_output import link_owned_executable
            # This public owner consumes staged row/payload stores.
            image = link_owned_executable(pending.pop(), compact_budget=budget)
        else:
            image = link_prepared_executable(pending[0], compact_budget=budget)
        assert image == link_executable(objects)
        _run_image(tmp_path, image)
        assert budget.retained_bytes == 0
    finally:
        if pending:
            host.close_staged_source_rows(pending.pop())


def test_lldb_hits_source_breakpoint_steps_and_unwinds_real_owned_executable(tmp_path):
    debugger = shutil.which('lldb')
    if debugger is None:
        pytest.skip('LLDB unavailable')
    path = _run_image(tmp_path, link_executable(_units(tmp_path)))
    result = subprocess.run([
        debugger, '-b', '-o', 'settings set target.disable-aslr false',
        '-o', 'target create ' + str(path),
        '-o', 'breakpoint set --file callee.py --line 22',
        '-o', 'run', '-o', 'thread backtrace', '-o', 'thread step-over',
        '-o', 'frame info', '-o', 'breakpoint disable', '-o', 'process continue',
    ], capture_output=True, text=True, timeout=30)
    (tmp_path / 'lldb.txt').write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'stop reason = breakpoint 1.1' in result.stdout
    assert 'callee.py:22:4' in result.stdout
    assert 'callee.py:23:4' in result.stdout
    assert 'driver.py:2:4' in result.stdout
    assert 'exited with status = 42' in result.stdout


def test_real_python_source_runs_on_five_collectors_and_hits_native_breakpoint(
    tmp_path, monkeypatch, pcc_runtime_archive,
):
    import os
    from pcc.frontends.python.pipeline import compile_python
    debugger = shutil.which('lldb')
    if debugger is None:
        pytest.skip('LLDB unavailable')
    source = tmp_path / 'debuggee.py'
    text = 'def add(a, b):\n    total = a + b\n    return total\n\nprint(add(3, 4))\n'
    source.write_text(text)
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=15)
    assert expected.returncode == 0
    monkeypatch.setenv('PCC_PY_DEBUG_INFO', '1')
    monkeypatch.setenv('PCC_LINKER', 'pcc')
    output = tmp_path / 'debuggee'
    compile_python(str(source), str(output), backend='self', libpython_mode='off',
                   ir_scaffold_mode='on', runtime_archive=str(pcc_runtime_archive))
    assert source.read_text() == text
    for collector in range(5):
        run = subprocess.run([str(output)], env=dict(os.environ, PCC_GC_BACKEND=str(collector)),
                             capture_output=True, text=True, timeout=20)
        assert (run.returncode, run.stdout, run.stderr) == (expected.returncode, expected.stdout, expected.stderr)
    _dump(tmp_path, output.read_bytes())
    libraries = subprocess.check_output(['otool', '-L', str(output)], text=True, timeout=15)
    assert 'libpython' not in libraries.lower() and 'Python.framework' not in libraries
    result = subprocess.run([
        debugger, '-b', '-o', 'settings set target.disable-aslr false',
        '-o', 'target create ' + str(output),
        '-o', 'breakpoint set --file debuggee.py --line 2', '-o', 'run',
        '-o', 'thread backtrace', '-o', 'thread step-over', '-o', 'frame info',
        '-o', 'breakpoint disable', '-o', 'process continue',
    ], capture_output=True, text=True, timeout=40)
    (tmp_path / 'real-python-lldb.txt').write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'stop reason = breakpoint 1.1' in result.stdout
    assert 'debuggee.py:2:1' in result.stdout and 'debuggee.py:3:1' in result.stdout
    assert 'exited with status = 0' in result.stdout
