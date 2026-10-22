"""Bounded builtin OSError-family constructor and ownership regressions.

The runtime model executes actual constructor/payload bodies, including forced
relocation. Native execution remains a separate explicit-runtime integration
gate. BlockingIOError.characters_written and Windows winerror are not qualified.
"""
from pathlib import Path
import json
import re

import pytest

from pcc.runtime.py import py_abi_constants as abi
from tests.python.test_exception_constructor_roots import _Slots
from tests.python.test_os_error_attribute_owner import ErrorMemory, bodies
from tests.python.owned_regression_support import (
    assert_owned_program,
    assert_reference_program,
    explicit_owned_runtime,
)


ROOT = Path(__file__).resolve().parents[2]
PHASES = ("", "allocation", "frame_enter", "frame_leave", "load_root",
          "field_store", "publish", "graph_unlock", "tuple_set", "tuple_get")
ARGUMENTS = (
    (), ("",), (None,), (84,), (84, "msg"), (2, "missing"),
    (13, "denied", "/path"), (84, "msg", b"path"),
    (2, "missing", None), (2, "missing", None, 100),
    (2, "missing", None, None, "ignored-second"),
    (2, "missing", "first", None, "second"),
    (2, "missing", "first", 100, None),
    (None, None), ("2", "text errno"), (2 ** 100, "large errno"),
    (2, "missing", "first", None, "second", "extra"),
)
OS_ERROR_CLASSES = (
    (14, OSError), (34, FileNotFoundError), (35, FileExistsError),
    (36, PermissionError), (37, IsADirectoryError), (38, NotADirectoryError),
    (39, ProcessLookupError), (40, ChildProcessError), (41, TimeoutError),
    (42, InterruptedError), (43, BlockingIOError), (44, ConnectionError),
    (45, BrokenPipeError), (46, ConnectionAbortedError),
    (47, ConnectionRefusedError), (48, ConnectionResetError),
)


@pytest.mark.parametrize("tag,cls", OS_ERROR_CLASSES)
def test_os_error_indexable_third_argument_reference(tag, cls):
    # CPython 3.15 oserror_init uses the third argument as a written count
    # only for exact BlockingIOError. It does not index the errno object.
    events = []

    class Errno:
        def __int__(self):
            raise AssertionError("errno was converted")

        def __index__(self):
            raise AssertionError("errno was indexed")

    class Written:
        def __index__(self):
            events.append("third")
            return 3

    number, written, second = Errno(), Written(), object()
    error = cls(number, "blocked", written, None, second)
    assert type(error) is cls and error.errno is number
    assert error.strerror == "blocked"
    if cls is BlockingIOError:
        assert events == ["third"]
        assert error.characters_written == 3
        assert error.filename is None and error.filename2 is None
        assert error.args == (number, "blocked", written, None, second)
    else:
        assert events == []
        assert error.filename is written and error.filename2 is second
        assert error.args == (number, "blocked")


def test_blocking_indexable_third_argument_error_reference():
    events = []
    problem = ValueError("third argument index failed")

    class Written:
        def __index__(self):
            events.append("third")
            raise problem

    with pytest.raises(ValueError) as caught:
        BlockingIOError(11, "blocked", Written())
    assert caught.value is problem
    assert events == ["third"]


def constructor_memory(phase):
    memory = ErrorMemory(phase)
    memory.namespace["i64"] = int
    memory.namespace["target_sys_platform"] = lambda: "linux"
    bodies(ROOT / "pcc/runtime/py/freestanding_errno.py", memory.namespace,
           names={"pcc_errno_exception_kind"})
    return memory


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("tag,cls", OS_ERROR_CLASSES)
@pytest.mark.parametrize("arguments", ARGUMENTS)
def test_os_error_constructor_payload_matches_cpython(phase, tag, cls, arguments):
    memory = constructor_memory(phase)
    caller = _Slots(8)
    caller.fields[0] = memory.argument_tuple(arguments)
    memory.frames["caller"] = caller
    # Exercise the actual class-call routing, not just the payload helper.
    error = memory.dispatch["_builtin_exception_call"](
        memory.class_cache.fields[tag * 8], caller.fields[0], len(arguments),
    )
    assert error is not None and error.references == 1
    memory.handled.fields[0] = error  # transfer the NEW owner before another call
    memory.store_root(caller, None)
    del memory.frames["caller"]
    oracle = cls(*arguments)
    assert error.fields[16].fields[abi.PYCLASSOBJECT_NAME_OFFSET] == type(oracle).__name__
    for name in ("args", "errno", "strerror", "filename", "filename2"):
        value = memory.dispatch["py_obj_getattr"](memory.handled.fields[0], name)
        assert memory.python(value) == getattr(oracle, name), (name, arguments)
        memory.decref(value)
    assert memory.render() == str(oracle)
    assert memory.render(1) == repr(oracle)
    assert not memory.pin_metric and not memory.graph_depth
    assert set(memory.frames) == {"class_cache", "handled", "pending"}
    assert memory.pending.fields[0] is None


def test_os_error_tag_range_covers_exactly_the_builtin_family():
    from pcc.frontends.python.codegen.builtin_exceptions import BUILTIN_EXC_TAG
    from tests.python.test_builtin_exception_tags_source import _runtime_exception_names

    names = _runtime_exception_names()
    for name, tag in BUILTIN_EXC_TAG.items():
        cls = getattr(__import__("builtins"), name)
        assert (tag == 14 or 34 <= tag <= 48) == issubclass(cls, OSError)
    assert {tag for tag, _cls in OS_ERROR_CLASSES} == {14, *range(34, 49)}
    suffix = "() takes no keyword arguments"
    assert all(len(names[tag]) + len(suffix) + 1 <= 96 for tag, _cls in OS_ERROR_CLASSES)


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("tag,cls", OS_ERROR_CLASSES)
def test_os_error_family_class_call_rejects_keywords(phase, tag, cls):
    memory = constructor_memory(phase)
    names = _Slots(65 * 8)
    for index, builtin in OS_ERROR_CLASSES:
        names.fields[index * 8] = builtin.__name__
    memory.maps["PY_EXC_BUILTIN_NAMES"] = names
    memory.dispatch.update(
        pcc_capi_type_object_is_callable=lambda _cls: 0,
        py_obj_call_context_is_deferred=lambda: 0,
        py_dict_len=lambda _kwargs: 1,
    )
    caller = _Slots(16)
    caller.fields[0] = memory.argument_tuple(())
    caller.fields[8] = memory.make(abi.PY_TYPE_DICT)
    memory.frames["caller"] = caller
    result = memory.dispatch["_py_obj_call_body"](
        memory.class_cache.fields[tag * 8], caller.fields[0], caller.fields[8], 0,
    )
    assert result is None
    error = memory.pending.fields[0]
    assert error.fields[16].fields["exception_tag"] == 3
    with pytest.raises(TypeError) as caught:
        cls(message="invalid")
    assert memory.python(error.fields[24]) == str(caught.value)
    assert not memory.pin_metric and not memory.graph_depth
    assert set(memory.frames) == {"class_cache", "handled", "pending", "caller"}


def test_os_error_null_argument_tuple_means_no_arguments():
    memory = constructor_memory("graph_unlock")
    error = memory.namespace["py_os_error_new"](14, None)
    memory.handled.fields[0] = error
    args = memory.dispatch["py_obj_getattr"](memory.handled.fields[0], "args")
    assert memory.python(args) == ()
    memory.decref(args)
    assert memory.render() == ""
    assert not memory.pin_metric and not memory.graph_depth


@pytest.mark.xfail(strict=True, reason="BlockingIOError.characters_written is outside this constructor slice")
def test_blocking_numeric_third_argument_known_unqualified_field():
    memory = constructor_memory("")
    cls = memory.make(abi.PY_TYPE_CLASS, 65)
    cls.fields["exception_tag"] = 43
    cls.fields[abi.PYCLASSOBJECT_NAME_OFFSET] = "BlockingIOError"
    mro = _Slots(16)
    mro.fields[0] = cls
    mro.fields[8] = memory.class_cache.fields[14 * 8]
    cls.fields[abi.PYCLASSOBJECT_MRO_OFFSET] = mro
    cls.fields[abi.PYCLASSOBJECT_N_MRO_OFFSET] = 2
    memory.class_cache.fields[43 * 8] = cls
    args = memory.argument_tuple((11, "blocked", 3))
    error = memory.namespace["py_os_error_new"](14, args)
    memory.handled.fields[0] = error
    filename = memory.dispatch["py_obj_getattr"](error, "filename")
    assert memory.python(filename) is None
    memory.decref(filename)
    written = memory.dispatch["py_obj_getattr"](error, "characters_written")
    assert memory.python(written) == 3


@pytest.mark.parametrize("failure", [1, 2])
def test_os_error_constructor_allocation_error_does_not_leak_arguments(failure):
    memory = constructor_memory("graph_unlock")
    caller = _Slots(8)
    caller.fields[0] = memory.argument_tuple((2, "missing", "path"))
    memory.frames["caller"] = caller
    original = memory.namespace["py_tuple_new"]
    calls = 0

    def allocate(count):
        nonlocal calls
        calls += 1
        if calls == failure:
            memory.namespace["py_raise_owned"](
                memory.namespace["py_exc_new"](19, "constructor allocation"))
            return None
        return original(count)

    memory.namespace["py_tuple_new"] = allocate
    assert memory.namespace["py_os_error_new"](14, caller.fields[0]) is None
    assert memory.pending.fields[0].fields[16].fields["exception_tag"] == 19
    assert caller.fields[0].references == 1
    assert not memory.pin_metric and not memory.graph_depth
    assert set(memory.frames) == {"class_cache", "handled", "pending", "caller"}


@pytest.mark.parametrize("expression", [
    "OSError(84, 'msg')", "FileNotFoundError(13, 'msg', 'path')",
    "IOError(2, 'missing')", "EnvironmentError(2, 'missing')",
    "OSError(*(2, 'missing', 'path'))", "OSError(message='invalid')",
    "PermissionError(2, 'msg', 'first', None, 'second', 'extra')",
    "FileExistsError(2, 'msg', 'first', None, 'second')",
] + [cls.__name__ + "(2, 'msg', 'path')" for _tag, cls in OS_ERROR_CLASSES]
  + [cls.__name__ + "(message='invalid')" for _tag, cls in OS_ERROR_CLASSES])
@pytest.mark.parametrize("statement", ["return", "raise"])
def test_os_error_direct_constructor_uses_authoritative_call_slots(expression, statement):
    from tests.python.test_shared_call_binding import _emit

    text = _emit("def probe():\n    " + statement + " " + expression + "\n")
    body = re.search(r"(?ms)^define[^\n]*@user_binding_probe\([^\n]*\).*?^}", text).group(0)
    assert "@py_obj_call_slots(" in body
    assert "@py_obj_str(" not in body
    assert not re.search(r"@py_exc_new\(i64 (?:14|3[4-9]|4[0-8]),", body)
    assert "@py_cpy_" not in body


PROGRAM = r'''
import gc

EVENTS = []

def operand(name, value):
    EVENTS.append(name)
    gc.collect()
    return value

class Unformatted:
    def __int__(self):
        raise AssertionError('constructor coerced errno')
    def __index__(self):
        raise AssertionError('constructor indexed errno')
    def __str__(self):
        raise AssertionError('constructor formatted an argument')

class UnformattedPath:
    def __str__(self):
        raise AssertionError('constructor formatted a filename')

def direct():
    return OSError(84, 'msg')

def verify(error, cls, args, number, message, filename=None, filename2=None):
    gc.collect()
    assert type(error) is cls
    assert isinstance(error, OSError) and isinstance(error, Exception)
    assert error.args == args
    assert error.errno == number and error.strerror == message
    assert error.filename == filename and error.filename2 == filename2

def direct_subclasses():
    # Static calls exercise exception lowering; using class values alone
    # would miss its independent message-only shortcut.
    verify(FileNotFoundError(13, 'msg', 'path'), FileNotFoundError,
           (13, 'msg'), 13, 'msg', 'path')
    verify(FileExistsError(17, 'msg', 'path'), FileExistsError,
           (17, 'msg'), 17, 'msg', 'path')
    verify(PermissionError(13, 'msg'), PermissionError,
           (13, 'msg'), 13, 'msg')
    verify(IsADirectoryError(21, 'msg', 'path'), IsADirectoryError,
           (21, 'msg'), 21, 'msg', 'path')
    verify(NotADirectoryError(20, 'msg', 'path'), NotADirectoryError,
           (20, 'msg'), 20, 'msg', 'path')
    verify(ProcessLookupError(2, 'msg', 'path'), ProcessLookupError,
           (2, 'msg'), 2, 'msg', 'path')
    verify(ChildProcessError(2, 'msg', 'path'), ChildProcessError,
           (2, 'msg'), 2, 'msg', 'path')
    verify(TimeoutError(2, 'msg', 'path'), TimeoutError,
           (2, 'msg'), 2, 'msg', 'path')
    verify(InterruptedError(2, 'msg', 'path'), InterruptedError,
           (2, 'msg'), 2, 'msg', 'path')
    verify(BlockingIOError(2, 'msg', 'path'), BlockingIOError,
           (2, 'msg'), 2, 'msg', 'path')
    verify(ConnectionError(2, 'msg', 'path'), ConnectionError,
           (2, 'msg'), 2, 'msg', 'path')
    verify(BrokenPipeError(2, 'msg', 'path'), BrokenPipeError,
           (2, 'msg'), 2, 'msg', 'path')
    verify(ConnectionAbortedError(2, 'msg', 'path'), ConnectionAbortedError,
           (2, 'msg'), 2, 'msg', 'path')
    verify(ConnectionRefusedError(2, 'msg', 'path'), ConnectionRefusedError,
           (2, 'msg'), 2, 'msg', 'path')
    verify(ConnectionResetError(2, 'msg', 'path'), ConnectionResetError,
           (2, 'msg'), 2, 'msg', 'path')

def subclass_edges():
    marker = Unformatted()
    path = UnformattedPath()
    for cls in (FileNotFoundError, FileExistsError, PermissionError,
                IsADirectoryError, NotADirectoryError, ProcessLookupError,
                ChildProcessError, TimeoutError, InterruptedError,
                BlockingIOError, ConnectionError, BrokenPipeError,
                ConnectionAbortedError, ConnectionRefusedError, ConnectionResetError):
        for args in ((), ('message',), (None,)):
            verify(cls(*args), cls, args, None, None)
        verify(cls(2, 'msg'), cls, (2, 'msg'), 2, 'msg')
        verify(cls(2, 'msg', None), cls, (2, 'msg', None), 2, 'msg')
        verify(cls(2, 'msg', b'path'), cls, (2, 'msg'), 2, 'msg', b'path')
        verify(cls(2, 'msg', 'first', None, 'second'), cls,
               (2, 'msg'), 2, 'msg', 'first', 'second')
        verify(cls(2, 'msg', None, None, 'ignored'), cls,
               (2, 'msg', None, None, 'ignored'), 2, 'msg')
        args = (2, 'msg', 'first', None, 'second', 'extra')
        verify(cls(*args), cls, args, None, None)
        for number in (None, '2', b'2', 2.5, 2 ** 100, True):
            verify(cls(number, 'msg', 'path'), cls,
                   (number, 'msg'), number, 'msg', 'path')
        # A numeric third argument has different meaning for BlockingIOError.
        # Keep errno conversion probes separate from ordinary filename probes.
        untouched = cls(marker, marker, path, None, path)
        gc.collect()
        assert type(untouched) is cls
        assert untouched.errno is marker and untouched.strerror is marker
        assert untouched.filename is path and untouched.filename2 is path
        assert untouched.args[0] is marker and untouched.args[1] is marker
        if cls is not BlockingIOError:
            # Indexable filenames remain unconverted for every other subclass.
            indexed_path = cls(marker, marker, marker, None, marker)
            gc.collect()
            assert indexed_path.filename is marker and indexed_path.filename2 is marker
            assert indexed_path.errno is marker and indexed_path.strerror is marker
            assert indexed_path.args[0] is marker and indexed_path.args[1] is marker
        try:
            cls(message='invalid')
        except TypeError as error:
            assert str(error) == cls.__name__ + '() takes no keyword arguments'
        else:
            raise AssertionError('subclass constructor accepted keywords')
    try:
        PermissionError(13, strerror='invalid')
    except TypeError as error:
        assert str(error) == 'PermissionError() takes no keyword arguments'
    else:
        raise AssertionError('direct subclass constructor accepted keywords')
    try:
        raise PermissionError(2, 'msg', 'first', None, 'second')
    except FileNotFoundError:
        raise AssertionError('explicit subclass was remapped from errno')
    except PermissionError as error:
        verify(error, PermissionError, (2, 'msg'), 2, 'msg', 'first', 'second')
        assert str(error) == "[Errno 2] msg: 'first' -> 'second'"
        assert repr(error) == "PermissionError(2, 'msg')"
    try:
        raise ConnectionRefusedError(2, 'msg', 'path')
    except ConnectionResetError:
        raise AssertionError('connection sibling handler matched')
    except ConnectionError as error:
        verify(error, ConnectionRefusedError, (2, 'msg'), 2, 'msg', 'path')

def main():
    direct_subclasses()
    subclass_edges()
    verify(direct(), OSError, (84, 'msg'), 84, 'msg')
    verify(OSError(2, 'missing', 'path'), FileNotFoundError,
           (2, 'missing'), 2, 'missing', 'path')
    verify(FileNotFoundError(13, 'denied'), FileNotFoundError,
           (13, 'denied'), 13, 'denied')
    evaluated = OSError(operand('errno', 84), operand('message', 'msg'),
                        operand('filename', 'path'))
    assert EVENTS == ['errno', 'message', 'filename']
    verify(evaluated, OSError, (84, 'msg'), 84, 'msg', 'path')
    marker = Unformatted()
    untouched = OSError(marker, marker)
    assert untouched.errno is marker and untouched.strerror is marker
    assert untouched.args[0] is marker and untouched.args[1] is marker
    constructor = OSError
    verify(constructor(84, 'msg'), OSError, (84, 'msg'), 84, 'msg')
    for cls in (OSError, IOError, EnvironmentError):
        verify(cls(*(2, 'missing', b'path')), FileNotFoundError,
               (2, 'missing'), 2, 'missing', b'path')
    for args in ((), ('',), (None,), (84,)):
        verify(constructor(*args), OSError, args, None, None)
    verify(constructor(2, 'missing', None), FileNotFoundError,
           (2, 'missing', None), 2, 'missing')
    verify(constructor(2, 'missing', 'first', None, 'second'), FileNotFoundError,
           (2, 'missing'), 2, 'missing', 'first', 'second')
    verify(constructor(2, 'missing', None, None, 'ignored'), FileNotFoundError,
           (2, 'missing', None, None, 'ignored'), 2, 'missing')
    verify(constructor(2 ** 100, 'large'), OSError,
           (2 ** 100, 'large'), 2 ** 100, 'large')
    verify(constructor('2', 'text'), OSError, ('2', 'text'), '2', 'text')
    args = (2, 'missing', 'first', None, 'second', 'extra')
    verify(constructor(*args), OSError, args, None, None)
    try:
        raise OSError(2, 'missing', 'path')
    except PermissionError:
        raise AssertionError('sibling handler matched')
    except FileNotFoundError as error:
        assert error.args == (2, 'missing') and error.filename == 'path'
        assert str(error) == "[Errno 2] missing: 'path'"
        assert repr(error) == "FileNotFoundError(2, 'missing')"
    except OSError:
        raise AssertionError('errno subclass was lost')
    for cls in (OSError, FileNotFoundError):
        try:
            cls(message='invalid')
        except TypeError:
            pass
        else:
            raise AssertionError('constructor accepted keywords')
    try:
        OSError(84, strerror='invalid')
    except TypeError:
        pass
    else:
        raise AssertionError('direct constructor accepted keywords')
    value = constructor(84, 'original', 'path')
    value.args = ('changed',)
    gc.collect()
    assert value.errno == 84 and value.strerror == 'original'
    assert value.filename == 'path' and value.args == ('changed',)
    print('OS_ERROR_CONSTRUCTORS_OK')

main()
'''


def test_os_error_constructor_reference(tmp_path):
    assert_reference_program(PROGRAM, "OS_ERROR_CONSTRUCTORS_OK\n", tmp_path)


@pytest.mark.integration
@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_os_error_constructor_native_five_gc(python_program_compiler, request,
                                          explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params["python_program_compiler"]
    assert_owned_program(PROGRAM, "OS_ERROR_CONSTRUCTORS_OK\n", tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime,
                         capfd, provenance_probe="2")


_BLOCKING_INDEX_PREFIX = r'''
import gc
EVENTS = []
PROBLEM = ValueError('third argument index failed')

class Written:
    def __index__(self):
        EVENTS.append('third')
        return 3

class FailedWritten:
    def __index__(self):
        EVENTS.append('third')
        raise PROBLEM

def main():
'''
BLOCKING_INDEX_PROGRAMS = {
    "value": _BLOCKING_INDEX_PREFIX + r'''
    written = Written()
    error = BlockingIOError(11, 'blocked', written, None, 'ignored')
    gc.collect()
    if EVENTS == []:
        assert type(error) is BlockingIOError
        assert error.errno == 11 and error.strerror == 'blocked'
        assert error.args == (11, 'blocked')
        assert error.filename is written and error.filename2 == 'ignored'
        print('BLOCKING_INDEX_NOT_IMPLEMENTED')
        return
    assert EVENTS == ['third']
    assert type(error) is BlockingIOError
    assert error.characters_written == 3
    assert error.errno == 11 and error.strerror == 'blocked'
    assert error.args == (11, 'blocked', written, None, 'ignored')
    assert error.filename is None and error.filename2 is None
    print('BLOCKING_INDEX_OK')

main()
''',
    "error": _BLOCKING_INDEX_PREFIX + r'''
    written = FailedWritten()
    try:
        result = BlockingIOError(11, 'blocked', written)
    except ValueError as error:
        assert error is PROBLEM
        assert EVENTS == ['third']
    else:
        if EVENTS == []:
            gc.collect()
            assert type(result) is BlockingIOError
            assert result.errno == 11 and result.strerror == 'blocked'
            assert result.args == (11, 'blocked')
            assert result.filename is written and result.filename2 is None
            print('BLOCKING_INDEX_NOT_IMPLEMENTED')
            return
        raise AssertionError('third argument index failure was swallowed')
    print('BLOCKING_INDEX_OK')

main()
''',
}


class _BlockingIndexNotImplemented(AssertionError):
    pass


@pytest.mark.integration
@pytest.mark.xfail(strict=True, raises=_BlockingIndexNotImplemented,
                  reason="BlockingIOError indexable-third/characters_written is unimplemented")
@pytest.mark.parametrize("case", ("value", "error"))
@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_blocking_indexable_third_argument_native_five_gc(
        case, python_program_compiler, request, explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params["python_program_compiler"]
    try:
        assert_owned_program(BLOCKING_INDEX_PROGRAMS[case], "BLOCKING_INDEX_OK\n", tmp_path,
                             python_program_compiler, mode, explicit_owned_runtime,
                             capfd, provenance_probe="2")
    except AssertionError:
        # Only the emitted program's precise absent-callback sentinel is a
        # known failure. Reference, compile, crash or unrelated assertion
        # failures must remain real failures rather than being masked by XFAIL.
        receipt_path = tmp_path / "ownership-regression.json"
        if receipt_path.is_file():
            receipt = json.loads(receipt_path.read_text())
            executions = receipt.get("executions", [])
            if receipt.get("status") == "NATIVE_EXECUTION_FAILED" and executions:
                last = executions[-1]
                if (last["returncode"], last["stdout"], last["stderr"]) == (
                        0, "BLOCKING_INDEX_NOT_IMPLEMENTED\n", ""):
                    raise _BlockingIndexNotImplemented(case) from None
        raise
