"""OS/path NEW results remain authoritative across calls and cleanup."""
from __future__ import annotations

from pathlib import Path
import re
import textwrap

import pytest

from tests.python.owned_regression_support import (
    assert_owned_program,
    assert_reference_program,
    explicit_owned_runtime,
)
from tests.python.test_foreign_address_leases import _functions
from tests.python.test_shared_call_binding import _emit, _function
from tests.python.test_slot_call_subscript_producers import _assert_immediate_publication


PRODUCERS = (
    ("os.getpid()", "py_os_getpid"),
    ("os.getcwd()", "py_os_getcwd_str"),
    ("os.cpu_count()", "py_os_cpu_count"),
    ("os.uname()", "py_os_uname"),
    ("platform.machine()", "py_platform_machine_str"),
    ("platform.release()", "py_platform_release_str"),
    ("os.path.basename(path)", "py_os_path_basename"),
    ("os.path.dirname(path)", "py_os_path_dirname"),
    ("os.path.split(path)", "py_os_path_split"),
    ("os.path.join(path, 'child')", "py_os_path_join"),
    ("os.path.getmtime(path)", "py_os_path_getmtime"),
    ("os.path.getsize(path)", "py_os_path_getsize"),
    ("os.path.commonpath([path, path])", "py_os_path_commonpath"),
    ("os.path.commonprefix([path, path])", "py_os_path_commonprefix"),
    ("os.path.splitext(path)", "py_os_path_splitext"),
    ("os.path.normcase(path)", "py_os_path_normcase"),
    ("os.path.splitdrive(path)", "py_os_path_splitdrive"),
    ("os.path.expanduser(path)", "py_os_path_expanduser"),
    ("os.path.expandvars(path)", "py_os_path_expandvars"),
    ("os.path.abspath(path)", "py_os_path_abspath"),
    ("os.path.normpath(path)", "py_os_path_normpath"),
    ("os.path.realpath(path)", "py_os_path_realpath"),
    ("os.path.relpath(path)", "py_os_path_relpath"),
    ("os.path.relpath(path, start)", "py_os_path_relpath"),
)


@pytest.mark.parametrize("producer,runtime", PRODUCERS)
@pytest.mark.parametrize("site", ("return", "argument", "default", "later-error"))
def test_os_new_results_publish_before_parking(producer, runtime, site):
    bodies = {
        "return": "    return " + producer + "\n",
        "argument": "    return take(value=" + producer + ")\n",
        "default": "    def selected(value=" + producer + "):\n        return value\n    return selected()\n",
        "later-error": "    return take(value=" + producer + ", later=fail())\n",
    }
    source = (
        "import os\nimport platform\n"
        "def take(*, value, later=None, **extras):\n    return value\n"
        "def fail():\n    raise ValueError('later')\n"
        "def probe(path: str, start: str):\n" + bodies[site]
    )
    text = _emit(source)
    _assert_immediate_publication(text, runtime)
    assert "strict.nolib.stub" not in text
    assert "@py_cpy_" not in _function(text)


@pytest.mark.parametrize("field", ("sysname", "nodename", "release", "version", "machine"))
@pytest.mark.parametrize("site", ("return", "conversion", "argument", "default"))
def test_uname_field_publishes_tuple_and_retained_field(field, site):
    producer = "system.uname()." + field
    body = {
        "return": "    return " + producer + "\n",
        "conversion": "    return str(" + producer + ")\n",
        "argument": "    return take(value=" + producer + ")\n",
        "default": "    def selected(value=" + producer + "):\n        return value\n    return selected()\n",
    }[site]
    text = _emit(
        "import os as system\ndef take(*, value, **extras):\n    return value\n"
        "def probe():\n" + body
    )
    _assert_immediate_publication(text, "py_os_uname")
    _assert_immediate_publication(text, "py_tuple_get")
    assert "@py_obj_getattr(" not in _function(text)


def test_uname_shadowed_receiver_uses_ordinary_attribute_lookup():
    text = _function(_emit("def probe(system):\n    return str(system.uname().machine)\n"))
    assert "@py_os_uname(" not in text
    assert "@py_obj_getattr(" in text


def test_join_roots_list_before_argument_factories_and_reloads_after_cleanup():
    text = _function(_emit('''import os

def first():
    return 'root'
def later():
    return 'child'
def probe():
    return os.path.join(first(), later())
'''))
    _assert_immediate_publication(text, "py_list_new")
    _assert_immediate_publication(text, "py_os_path_join")
    first = re.search(r"(%[^ ]+) = call [^\n]*@user_binding_first\([^\n]*\)\n", text)
    later = re.search(r"(%[^ ]+) = call [^\n]*@user_binding_later\([^\n]*\)\n", text)
    assert first and later and first.start() < later.start()
    assert text[first.end():].lstrip().startswith("store ptr " + first.group(1) + ",")
    assert text[later.end():].lstrip().startswith("store ptr " + later.group(1) + ",")
    producer = re.search(r"(%[^ ]+) = call [^\n]*@py_os_path_join\([^\n]*\)\n  store ptr \1, ptr (%[^,\n]+)", text)
    assert producer
    result_slot = producer.group(2)
    after = text[producer.end():]
    assert re.search(r"@pcc_gc_store_root\([^\n]*null\)", after)
    # The result slot is used directly, or through identity pointer aliases.
    aliases = [result_slot] + re.findall(
        r"(%[^ ]+) = bitcast ptr " + re.escape(result_slot) + r" to ptr", after,
    )
    assert any("@pcc_gc_load_ptr(ptr null, ptr " + alias + ")" in after for alias in aliases)
    assert any("@pcc_gc_take_pinned_slot(ptr " + alias + "," in after for alias in aliases)


@pytest.mark.parametrize("path", ("''", "path"))
def test_relpath_default_keeps_original_path_and_dot_start(path):
    text = _function(_emit("import os\ndef probe(path: str):\n    return os.path.relpath(" + path + ")\n"))
    _assert_immediate_publication(text, "py_os_path_relpath")
    assert "@py_os_path_abspath(" not in text
    call = re.search(r"@py_os_path_relpath\(ptr (%[^,]+), ptr (%[^)]+)\)", text)
    assert call and call.group(1) != call.group(2)


class _OwnedObject:
    def __init__(self, value):
        self.value = value
        self.refs = 1


class _UnameModel:
    """Execute the production uname body with observable owner balances."""
    def __init__(self, fail_at=None, platform_status=0):
        self.fields = ("system", "node", "release", "version", "machine")
        self.fail_at = fail_at
        self.objects = []
        self.error = None
        self.ns = {
            "stack_alloc": lambda size: object(),
            "pcc_platform_uname": lambda buffer: platform_status,
            "pcc_platform_uname_field": lambda buffer, index: self.fields[index],
            "py_tuple_new": self.new_tuple,
            "py_tuple_set_item": self.set_item,
            "py_str_new": self.new_string,
            "py_decref": self.release,
            "py_exc_new": lambda kind, message: (kind, message),
            "py_raise_owned": self.raise_error,
            "null": lambda: None,
            "ptr_is_null": lambda value: value is None,
            "cstr": lambda value: value,
            "strlen": len,
        }
        path = Path(__file__).resolve().parents[2] / "pcc/runtime/py/py_os_system.py"
        _functions(path, {"py_os_uname"}, self.ns)

    def new_tuple(self, size):
        if self.fail_at == "tuple":
            self.error = "allocation"
            return None
        result = _OwnedObject([None] * size)
        self.objects.append(result)
        return result

    def new_string(self, value, size):
        if self.fail_at == self.fields.index(value):
            self.error = "allocation"
            return None
        result = _OwnedObject(value[:size])
        self.objects.append(result)
        return result

    def set_item(self, sequence, index, value):
        value.refs += 1
        sequence.value[index] = value

    def release(self, value):
        if value is None:
            return
        value.refs -= 1
        assert value.refs >= 0
        if value.refs == 0 and isinstance(value.value, list):
            for item in value.value:
                self.release(item)

    def raise_error(self, error):
        self.error = error


def test_uname_runtime_result_is_one_owner_of_all_five_fields():
    model = _UnameModel()
    result = model.ns["py_os_uname"]()
    assert tuple(field.value for field in result.value) == model.fields
    assert all(value.refs == 1 for value in model.objects)
    model.release(result)
    assert all(value.refs == 0 for value in model.objects)


@pytest.mark.parametrize("failure", ("tuple", 0, 1, 2, 3, 4))
def test_uname_runtime_failure_releases_partial_result(failure):
    model = _UnameModel(fail_at=failure)
    assert model.ns["py_os_uname"]() is None
    assert model.error == "allocation"
    assert all(value.refs == 0 for value in model.objects)


def test_uname_runtime_platform_error_has_no_partial_result():
    model = _UnameModel(platform_status=-1)
    assert model.ns["py_os_uname"]() is None
    assert model.error == (14, "os.uname() failed")
    assert model.objects == []


PROGRAM = textwrap.dedent('''\
    import gc
    import os
    import platform
    events = []
    class Path:
        def __init__(self, value):
            self.value = value
        def __fspath__(self):
            gc.collect()
            return self.value
        def __del__(self):
            events.append('drop:' + self.value)
    def take(*, value, later=None, **extras):
        gc.collect()
        return value
    def part(value):
        events.append('make:' + value)
        gc.collect()
        return Path(value)
    def fail():
        gc.collect()
        raise ValueError('later')
    def main():
        value = take(value=os.path.join(part('root'), part('child')))
        assert value == 'root/child'
        gc.collect()
        assert events[:2] == ['make:root', 'make:child']
        assert events.count('drop:root') == 1
        assert events.count('drop:child') == 1
        assert take(value=os.path.basename('root/child')) == 'child'
        assert take(value=os.path.dirname('root/child')) == 'root'
        assert take(value=os.path.join('root', *['leaf'], 'file')) == 'root/leaf/file'
        assert take(value=os.path.normpath('root/../leaf')) == 'leaf'
        assert take(value=os.path.relpath('/root/leaf', '/root')) == 'leaf'
        assert os.path.isabs(take(value=os.path.abspath('.')))
        assert os.path.isabs(take(value=os.getcwd()))
        assert take(value=os.getpid()) > 0
        assert len(take(value=os.uname())) == 5
        assert take(value=os.uname().machine) == platform.machine()
        def selected(value=os.path.basename('root/default')):
            gc.collect()
            return value
        assert selected() == 'default'
        try:
            take(value=os.path.join(part('cleanup'), part('error')), later=fail())
        except ValueError:
            pass
        else:
            raise AssertionError('later error lost')
        gc.collect()
        assert events.count('drop:cleanup') == 1
        assert events.count('drop:error') == 1
        try:
            os.path.relpath('')
        except ValueError:
            pass
        else:
            raise AssertionError('empty relpath accepted')
        try:
            os.path.join('root', 7)
        except TypeError:
            pass
        else:
            raise AssertionError('invalid path accepted')
        try:
            os.path.getsize('/pcc-os-owner-no-such-file-194856372')
        except OSError:
            pass
        else:
            raise AssertionError('missing path accepted')
        print('OS_RESULT_OWNERSHIP_OK')
    main()
''')


def test_os_result_native_program_reference(tmp_path):
    assert_reference_program(PROGRAM, "OS_RESULT_OWNERSHIP_OK\n", tmp_path)


@pytest.mark.integration
@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_os_result_native_five_gc(python_program_compiler, request,
                                 explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params["python_program_compiler"]
    assert_owned_program(PROGRAM, "OS_RESULT_OWNERSHIP_OK\n", tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime, capfd)
