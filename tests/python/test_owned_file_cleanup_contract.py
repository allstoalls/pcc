"""Additional ordinary NamedTemporaryFile cleanup reference comparisons."""
import os
import tempfile

import pytest

from tests.python.test_owned_fdopen_provider import FileRuntime
from tests.python.test_owned_namedtempfile_provider import create,invoke


@pytest.mark.parametrize('on_close',(True,False))
def test_external_unlink_is_already_cleaned_at_close_or_exit(tmp_path,on_close):
    with tempfile.NamedTemporaryFile(dir=tmp_path,delete_on_close=on_close) as oracle:
        os.unlink(oracle.name)
        oracle.close()
    runtime=FileRuntime();manager=create(runtime,tmp_path,on_close=on_close)
    os.unlink(manager.attrs['name'].value)
    invoke(runtime,manager,'close')
    assert runtime.error is None
    invoke(runtime,manager,'__exit__',runtime.none,runtime.none,runtime.none)
    assert runtime.error is None and not runtime.frames and not runtime.leases


def test_missing_path_preserves_a_real_close_error(tmp_path):
    from tests.python.test_owned_tempfile_provider import Obj
    runtime=FileRuntime();manager=create(runtime,tmp_path)
    os.unlink(manager.attrs['name'].value)
    original=Obj('exception',(14,'flush failed'))
    close=runtime.ns['py_file_close_checked']
    def fail_after_close(file):close(file);runtime.error=original
    runtime.ns['py_file_close_checked']=fail_after_close
    assert invoke(runtime,manager,'close') is None
    assert runtime.error is original and not runtime.frames and not runtime.leases


def test_cleanup_keeps_other_unlink_errors_visible(tmp_path):
    runtime=FileRuntime();manager=create(runtime,tmp_path)
    name=manager.attrs['name'].value
    runtime.ns['unlinkat']=lambda path,flags:-13
    assert invoke(runtime,manager,'close') is None
    assert runtime.error.attrs['errno']==13 and os.path.exists(name)
    assert not runtime.frames and not runtime.leases
    os.unlink(name)
