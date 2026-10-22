"""Execute builtin tuple base publication on a strictly admitted runtime."""
import os
import subprocess

import pytest

pytestmark = pytest.mark.integration

SOURCES = [
    ('''class Record(tuple):
    __slots__ = ()
    __pcc_struct_sequence__ = True
class Ordinary(tuple):
    __slots__ = ()
class Derived(Ordinary):
    __slots__ = ()
def main():
    assert Record.__bases__ == (tuple,)
    assert Record.__mro__ == (Record, tuple, object)
    assert Ordinary.__mro__ == (Ordinary, tuple, object)
    assert Derived.__mro__ == (Derived, Ordinary, tuple, object)
    assert issubclass(Record, tuple)
    assert issubclass(Derived, tuple)
    print("tuple-base-ok")
main()
''', 'tuple-base-ok\n', ''),
    ('''class tuple:
    pass
class Child(tuple):
    pass
def main():
    assert Child.__bases__ == (tuple,)
    assert Child.__mro__ == (Child, tuple, object)
    print("shadow-base-ok")
main()
''', 'shadow-base-ok\n', ''),
    ('''class Invalid:
    __slots__ = ()
    __pcc_struct_sequence__ = True
def main():
    print("unreachable")
main()
''', '', 'struct sequence requires a tuple subtype'),
    ('''class Invalid(tuple):
    __slots__ = ("value",)
    __pcc_struct_sequence__ = True
def main():
    print("unreachable")
main()
''', '', 'struct sequence requires an empty slots-only layout'),
    ('''import time
def main():
    assert time.struct_time.__bases__ == (tuple,)
    assert issubclass(time.struct_time, tuple)
    print("time-base-ok")
main()
''', 'time-base-ok\n', ''),
]


@pytest.mark.parametrize('source,stdout,error', SOURCES,
                         ids=['tuple-mro', 'shadowed-tuple', 'non-tuple-marker', 'nonempty-slots-marker', 'time-import'])
def test_builtin_tuple_base_native(tmp_path, source, stdout, error):
    from tests.native_provisioning import native_test_runtime_options
    from pcc.frontends.python.pipeline import compile_python

    options = native_test_runtime_options()
    path = tmp_path / 'tuple_base_native.py'
    binary = tmp_path / 'tuple_base_native'
    path.write_text(source)
    compile_python(str(path), str(binary), backend='self', libpython_mode='off',
                   ir_scaffold_mode='on', **options)
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PATH='/nonexistent', PCC_GC_BACKEND=str(backend)))
        assert result.stdout == stdout, (backend, result.stdout, result.stderr)
        if error:
            assert result.returncode != 0, (backend, result.stdout, result.stderr)
            assert error in result.stderr
        else:
            assert result.returncode == 0, (backend, result.stdout, result.stderr)
            assert result.stderr == ''
