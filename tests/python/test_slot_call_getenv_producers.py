"""Environment lookups publish an owner on both the hit and default edges."""
from __future__ import annotations

from pathlib import Path
import hashlib
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


@pytest.mark.parametrize("lookup", ("os.getenv", "os.environ.get"))
@pytest.mark.parametrize("body", (
    "return LOOKUP(key)",
    "return LOOKUP(key, default)",
    "return take(value=LOOKUP(key, default))",
    "return str(LOOKUP(key, default) or '')",
    "return take(value=LOOKUP(key, default), later=fail())",
))
def test_environment_lookup_publishes_before_cleanup(lookup, body):
    source = (
        "import os\n"
        "def take(*, value, later=None, **extras):\n    return value\n"
        "def fail():\n    raise ValueError('later')\n"
        "def probe(key, default):\n    " + body.replace("LOOKUP", lookup) + "\n"
    )
    text = _emit(source)
    _assert_immediate_publication(text, "py_os_getenv")
    assert "strict.nolib.stub" not in text
    assert "@py_cpy_" not in _function(text)


@pytest.mark.parametrize("lookup", ("os.getenv", "os.environ.get"))
def test_environment_lookup_evaluates_key_before_default(lookup):
    text = _function(_emit(
        "import os\n"
        "def key():\n    return 'PCC_ENV_OWNERSHIP'\n"
        "def default():\n    return 'fallback'\n"
        "def probe():\n    return " + lookup + "(key(), default())\n"
    ))
    key = re.search(r"(%[^ ]+) = call [^\n]*@user_binding_key\([^\n]*\)\n", text)
    default = re.search(r"(%[^ ]+) = call [^\n]*@user_binding_default\([^\n]*\)\n", text)
    lookup_call = re.search(r"(%[^ ]+) = call [^\n]*@py_os_getenv\(", text)
    assert key and default and lookup_call
    assert key.start() < default.start() < lookup_call.start()
    # The first argument is already rooted when the second call can collect.
    assert text[key.end():].lstrip().startswith("store ptr " + key.group(1) + ",")
    _assert_immediate_publication(text, "py_os_getenv")


class _Object:
    def __init__(self, value, tag=4):
        self.value = value
        self.tag = tag
        self.refs = 1


class _EnvironmentModel:
    """Run the production runtime body with observable input/result owners."""
    def __init__(self, values, allocation_fails=False, utf8_fails=False):
        self.values = values
        self.allocation_fails = allocation_fails
        self.utf8_fails = utf8_fails
        self.pending = None
        self.events = []
        self.ns = {
            "PY_TYPE_INT": 1,
            "PY_TYPE_STR": 4,
            "null": lambda: None,
            "ptr_is_null": lambda value: value is None,
            "is_tagged_int": lambda value: False,
            "load_i32": lambda value, offset: value.tag,
            "cstr": lambda value: value,
            "py_str_utf8": self.utf8,
            "getenv": self.getenv,
            "strlen": len,
            "py_str_new": self.new_string,
            "py_incref": self.retain,
            "py_exc_new": lambda kind, message: (kind, message),
            "py_raise_owned": self.raise_owned,
        }
        runtime = Path(__file__).resolve().parents[2] / "pcc/runtime/py/py_os_env.py"
        _functions(runtime, {"_type_of", "py_os_getenv"}, self.ns)

    def utf8(self, key):
        if self.utf8_fails:
            self.pending = "utf8-error"
            return None
        return key.value

    def getenv(self, key):
        self.events.append(("lookup", key))
        return self.values.get(key)

    def new_string(self, value, size):
        if self.allocation_fails:
            self.pending = "allocation-error"
            return None
        return _Object(value[:size])

    def retain(self, value):
        assert value.refs > 0
        value.refs += 1

    def raise_owned(self, error):
        self.pending = error

    def call(self, key, default):
        return self.ns["py_os_getenv"](key, default)


def test_getenv_missing_retains_arbitrary_default_until_result_release():
    model = _EnvironmentModel({})
    key, default = _Object("missing"), _Object("default", tag=99)
    result = model.call(key, default)
    assert result is default and default.refs == 2 and model.pending is None
    key.refs -= 1
    default.refs -= 1  # The caller releases its argument roots after publication.
    assert result.refs == 1
    result.refs -= 1
    assert result.refs == 0


def test_getenv_missing_default_aliases_key_with_independent_owner():
    model = _EnvironmentModel({})
    key = _Object("missing")
    key.refs = 2  # Two independently owned operand slots may alias.
    result = model.call(key, key)
    assert result is key and key.refs == 3
    key.refs -= 2
    assert result.refs == 1


def test_getenv_hit_returns_new_string_without_consuming_default():
    model = _EnvironmentModel({"present": "answer"})
    key, default = _Object("present"), _Object("default", tag=99)
    result = model.call(key, default)
    assert result.value == "answer" and result.refs == 1
    assert result is not key and result is not default
    assert key.refs == default.refs == 1 and model.pending is None


@pytest.mark.parametrize("kind", ("key-type", "null-key", "allocation", "utf8"))
def test_getenv_error_has_no_result_owner_and_preserves_default(kind):
    model = _EnvironmentModel(
        {"present": "answer"}, allocation_fails=kind == "allocation",
        utf8_fails=kind == "utf8",
    )
    key = None if kind == "null-key" else _Object("present", 99 if kind == "key-type" else 4)
    default = _Object("default", tag=99)
    assert model.call(key, default) is None
    assert model.pending is not None
    assert default.refs == 1
    if kind in ("key-type", "null-key"):
        assert model.events == []
        assert model.pending[0] == 3


def test_getenv_borrowed_default_receipt_is_rejected(tmp_path):
    """Unchanged symbol/signature must not admit the former borrowed ABI."""
    from pcc.tools import runtime_archive_provenance as provenance

    runtime = Path(__file__).resolve().parents[2] / "pcc/runtime/py/py_os_env.py"
    owned_source = runtime.read_text()
    retain = "        py_incref(default_value)\n"
    assert owned_source.count(retain) == 1
    borrowed_source = owned_source.replace(retain, "")
    current = tmp_path / "py/py_os_env.py"
    current.parent.mkdir()
    current.write_text(owned_source)
    member_bytes = b"fixture object; no native emission needed for source admission"
    record = {
        "schema": provenance.RECEIPT_SCHEMA,
        "member": "py_os_env.o",
        "object_sha256": hashlib.sha256(member_bytes).hexdigest(),
        "ir_sha256": "a" * 64,
        "source": "pcc/runtime/py/py_os_env.py",
        "source_sha256": hashlib.sha256(owned_source.encode()).hexdigest(),
        "source_kind": provenance._PCC_PYTHON_SOURCE_KIND,
        "producer_kind": provenance._PCC_PYTHON_PRODUCER,
        "object_emitter": provenance._PCC_OBJECT_EMITTER,
        "uses_host_cc": False,
        "target_triple": "x86_64-unknown-linux-gnu",
    }
    # Establish that the only rejected property is the runtime source ABI.
    assert provenance._validate_member_record(
        record, member="py_os_env.o", member_bytes=member_bytes, runtime_root=tmp_path,
    ) == record["target_triple"]
    record["source_sha256"] = hashlib.sha256(borrowed_source.encode()).hexdigest()
    with pytest.raises(provenance.ProvenanceError, match="source does not match its receipt"):
        provenance._validate_member_record(
            record, member="py_os_env.o", member_bytes=member_bytes, runtime_root=tmp_path,
        )


PROGRAM = textwrap.dedent('''\
    import gc
    import os
    events = []
    class Default:
        def __del__(self):
            events.append('drop')
    def key():
        events.append('key')
        return 'PCC_GETENV_OWNER_ABSENT'
    def default():
        events.append('default')
        gc.collect()
        return Default()
    def main():
        if 'PCC_GETENV_OWNER_ABSENT' in os.environ:
            del os.environ['PCC_GETENV_OWNER_ABSENT']
        os.environ['PCC_GETENV_OWNER_PRESENT'] = 'present'
        first = os.getenv(key(), default())
        assert events == ['key', 'default']
        gc.collect()
        assert events == ['key', 'default']
        del first
        gc.collect()
        assert events == ['key', 'default', 'drop']
        second = os.environ.get('PCC_GETENV_OWNER_ABSENT', Default())
        gc.collect()
        assert isinstance(second, Default)
        del second
        gc.collect()
        assert events == ['key', 'default', 'drop', 'drop']
        hit = os.environ.get('PCC_GETENV_OWNER_PRESENT', Default())
        gc.collect()
        assert hit == 'present'
        assert events == ['key', 'default', 'drop', 'drop', 'drop']
        try:
            os.getenv(7, Default())
        except TypeError:
            pass
        else:
            raise AssertionError('invalid key accepted')
        gc.collect()
        assert events == ['key', 'default', 'drop', 'drop', 'drop', 'drop']
        assert os.getenv('PCC_GETENV_OWNER_ABSENT') is None
        assert str(os.environ.get('PCC_GETENV_OWNER_ABSENT', '') or '') == ''
        print('GETENV_OWNERSHIP_OK')
    main()
''')


def test_getenv_native_program_reference(tmp_path):
    assert_reference_program(PROGRAM, "GETENV_OWNERSHIP_OK\n", tmp_path)


@pytest.mark.integration
@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_getenv_native_five_gc(python_program_compiler, request,
                             explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params["python_program_compiler"]
    assert_owned_program(PROGRAM, "GETENV_OWNERSHIP_OK\n", tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime, capfd)
