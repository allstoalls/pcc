"""Path text helpers preserve the standard newline default and explicit values."""

import inspect
import os
from pathlib import Path
import subprocess
import sys

import pytest

from pcc.stdlib.pathlib import Path as OwnedPath
from pcc.frontends.python.pipeline import compile_python


@pytest.mark.parametrize('method', ('read_text', 'write_text'))
def test_path_text_newline_default_matches_host(method):
    expected = inspect.signature(getattr(Path, method)).parameters['newline'].default
    actual = inspect.signature(getattr(OwnedPath, method)).parameters['newline'].default
    assert expected is None and actual is expected


@pytest.mark.parametrize('newline', (None, '', '\n', '\r', '\r\n'))
def test_path_text_explicit_newline_is_forwarded(tmp_path, newline):
    source = tmp_path / 'mixed'
    source.write_bytes(b'a\r\nb\rc\n')
    assert OwnedPath(source).read_text(encoding='utf-8', newline=newline) == source.read_text(
        encoding='utf-8', newline=newline,
    )
    reference = tmp_path / 'reference'
    actual = tmp_path / 'actual'
    assert OwnedPath(actual).write_text('a\nb\n', encoding='utf-8', newline=newline) == reference.write_text(
        'a\nb\n', encoding='utf-8', newline=newline,
    )
    assert actual.read_bytes() == reference.read_bytes()


def test_path_text_default_reads_universal_newlines(tmp_path):
    source = tmp_path / 'mixed'
    source.write_bytes(b'a\r\nb\rc\n')
    assert OwnedPath(source).read_text(encoding='utf-8') == 'a\nb\nc\n'


def test_path_text_invalid_newline_is_not_normalized(tmp_path):
    source = tmp_path / 'mixed'
    source.write_text('text')
    with pytest.raises(ValueError):
        OwnedPath(source).read_text(newline='bad')
    with pytest.raises(ValueError):
        OwnedPath(source).write_text('text', newline='bad')


NATIVE = '''from pathlib import Path
import sys
read = getattr(Path, 'read_text')
write = getattr(Path, 'write_text')
def main():
    source = Path(sys.argv[1])
    output = Path(sys.argv[2])
    assert read(source, encoding='utf-8') == 'a\\nb\\nc\\n'
    assert read(source, encoding='utf-8', newline=None) == 'a\\nb\\nc\\n'
    assert write(output, 'a\\nb\\n', encoding='utf-8') == 4
    assert output.read_bytes() == b'a\\nb\\n'
    assert write(output, 'a\\nb\\n', encoding='utf-8', newline=None) == 4
    assert output.read_bytes() == b'a\\nb\\n'
    print('PATH_TEXT_DEFAULT_OK')
main()
'''


def test_path_text_defaults_execute_real_native_provider(tmp_path, pcc_runtime_archive):
    source = tmp_path / 'main.py'
    source.write_text(NATIVE)
    data = tmp_path / 'input'
    data.write_bytes(b'a\r\nb\rc\n')
    output = tmp_path / 'program'
    compile_python(str(source), str(output), backend='self', libpython_mode='off',
                   runtime_archive=str(pcc_runtime_archive))
    result = subprocess.run([str(output), str(data), str(tmp_path / 'output')],
                            capture_output=True, text=True, timeout=30,
                            env=dict(os.environ, PCC_GC_REFCOUNT_PROVENANCE_PROBE='2'))
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'PATH_TEXT_DEFAULT_OK\n' and result.stderr == ''
