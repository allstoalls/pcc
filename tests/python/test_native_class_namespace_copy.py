"""Native controls for actual dict-subclass type namespaces."""
import pytest

from tests.python.owned_regression_support import (
    assert_owned_program,
    assert_reference_program,
    explicit_owned_runtime,
)


PROGRAM = '''events = []
sentinel = object()
class Namespace(dict):
    __slots__ = ()
    def keys(self):
        raise AssertionError('type used mapping keys override')
    def __getitem__(self, key):
        raise AssertionError('type used mapping getitem override')
    def __missing__(self, key):
        raise AssertionError('type used missing callback')
class Parent:
    def inherited(self):
        return sentinel
namespace = Namespace()
namespace['value'] = sentinel
Copied = type('Copied', (Parent,), namespace)
namespace['value'] = object()
Empty = type('Empty', (), Namespace())
class Mapping:
    def keys(self):
        return ['value']
    def __getitem__(self, key):
        return sentinel
def main():
    assert Copied.value is sentinel
    assert Copied().inherited() is sentinel
    assert Empty.__name__ == 'Empty'
    for invalid in (Mapping(), None):
        try:
            type('Invalid', (), invalid)
            raise AssertionError('non-dict accepted')
        except TypeError:
            pass
    print('CLASS_NAMESPACE_STORAGE_OK')
main()
'''
EXPECTED = 'CLASS_NAMESPACE_STORAGE_OK\n'


def test_class_namespace_storage_reference(tmp_path):
    assert_reference_program(PROGRAM, EXPECTED, tmp_path)


@pytest.mark.integration
def test_class_namespace_storage_native_five_gc(tmp_path, explicit_owned_runtime,
        python_program_compiler, request, capfd):
    assert_owned_program(PROGRAM, EXPECTED, tmp_path, python_program_compiler,
        request.node.callspec.params['python_program_compiler'], explicit_owned_runtime,
        capfd, provenance_probe='2')
