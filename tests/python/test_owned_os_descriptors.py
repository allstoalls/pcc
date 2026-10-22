"""Owned public OS descriptor providers and their original fdopen shape."""
import os
import re

import pytest

from tests.python.test_owned_fdopen_provider import FileRuntime
from tests.python.test_owned_tempfile_provider import Obj
from tests.python.test_shared_call_binding import _emit

# The model runtime targets Linux and translates Linux open flags to the
# host's; pass Linux values, not the host's os.O_* constants.
_LINUX_WRONLY_CREAT_EXCL = 0o1 | 0o100 | 0o200


def test_os_open_close_create_permissions_inheritance_and_errno(tmp_path):
    runtime=FileRuntime();path=tmp_path/'lease'
    args=Obj('tuple',[runtime.string(str(path)),_LINUX_WRONLY_CREAT_EXCL,0o600,runtime.none])
    fd=runtime.ns['_os_open_entry'](None,args)
    assert fd>=0 and not os.get_inheritable(fd) and path.stat().st_mode&0o777==0o600
    assert runtime.ns['_os_close_entry'](None,Obj('tuple',[fd])) is runtime.none
    with pytest.raises(OSError):os.fstat(fd)
    assert runtime.ns['_os_open_entry'](None,args) is None
    assert runtime.error.value[0]==35 and runtime.error.attrs['errno']==17
    assert not runtime.frames and not runtime.leases


def test_os_open_dir_fd_and_bytes(tmp_path):
    runtime=FileRuntime();directory=os.open(tmp_path,os.O_RDONLY)
    try:
        args=Obj('tuple',[Obj('bytes',b'child'),_LINUX_WRONLY_CREAT_EXCL,0o600,directory])
        fd=runtime.ns['_os_open_entry'](None,args)
        assert fd>=0 and (tmp_path/'child').exists()
        runtime.ns['_os_close_entry'](None,Obj('tuple',[fd]))
    finally:os.close(directory)
    assert not runtime.frames and not runtime.leases


@pytest.mark.parametrize('aliases',(False,True))
def test_os_open_fdopen_original_owner_shape_lowers(aliases,tmp_path):
    imports='import os\n'
    create='os.open';flag='os.O_WRONLY | os.O_CREAT | os.O_EXCL';adopt='os.fdopen'
    if aliases:
        imports+='from os import open as create, fdopen as adopt, O_WRONLY, O_CREAT, O_EXCL\n'
        create='create';adopt='adopt';flag='O_WRONLY | O_CREAT | O_EXCL'
    text=_emit(imports+'def consume(*, stream, **extras):\n    return stream\n'
        +'def probe(path):\n    fd = '+create+'(path, '+flag+', 0o600)\n'
        +'    with '+adopt+'(fd, "w", encoding="utf-8") as stream:\n'
        +'        consume(stream=stream)\n        stream.write("value")\n')
    (tmp_path/'original-owner-shape.ll').write_text(text)
    assert not re.search(r'\bcall [^\n]*@py_cpy_',text)
    assert 'strict.nolib.stub' not in text
    assert re.search(r'\bcall [^\n]*@py_os_open_function\(',text)
    assert re.search(r'\bcall [^\n]*@py_file_fdopen_function\(',text)
