"""The live sys.modules provider publishes through the ordinary slot ABI."""
from __future__ import annotations

import re

import pytest

from tests.python.test_shared_call_binding import _emit as _emit_native
from tests.python.test_slot_call_operand_roots import _calls


def _emit(source):
    text = _emit_native(source)
    assert "strict.nolib.stub:" not in text
    return text


@pytest.mark.parametrize("expression", ("sys.modules", "sys.modules[name]", "sys.modules['known']"))
def test_live_module_cache_provider_has_immediate_output_owner(expression):
    text = _emit("import sys\ndef take(*, value, **extras):\n    return value\ndef probe(name):\n    return take(value=" + expression + ")\n")
    calls = list(re.finditer(r'^  (%[^ ]+) = call [^\n]*@py_sys_modules\(\)\n', text, re.M))
    assert len(calls) == 1
    call = calls[0]
    assert text[call.end():].splitlines()[0].lstrip().startswith("store ptr " + call.group(1) + ",")
    assert not _calls(text, "py_cpy_getattr")
    assert not _calls(text, "py_compiled_module_import_by_name")


def test_lexer_dynamic_module_bitmask_stays_in_the_native_domain():
    text = _emit('''import sys
import re
def probe(name, pattern):
    module = sys.modules[name]
    return re.compile(pattern, module._lexreflags | re.VERBOSE)
''')
    assert len(_calls(text, "py_sys_modules")) == 1
    assert len(_calls(text, "py_obj_or")) == 1
    assert not _calls(text, "py_cpy_getattr")
    assert not _calls(text, "py_cpy_getitem")


def test_shadowed_sys_parameter_uses_its_actual_attribute():
    text = _emit("import sys\ndef probe(sys, name):\n    return sys.modules[name]\n")
    assert not _calls(text, "py_sys_modules")
    assert _calls(text, "py_obj_getattr")


def test_import_alias_and_local_cache_binding_remain_native():
    text = _emit("import sys as system\ndef probe(name):\n    cache = system.modules\n    return cache[name]\n")
    assert len(_calls(text, "py_sys_modules")) == 1
    assert not _calls(text, "py_cpy_getattr")


@pytest.mark.parametrize("source", (
    "def probe(name):\n    import sys\n    return sys.modules[name]\n",
    "from sys import modules\ndef probe(name):\n    return modules[name]\n",
    "def probe(name):\n    from sys import modules as cache\n    return cache[name]\n",
    "from sys import modules, stdout\ndef probe(name):\n    return modules[name]\n",
    "def bind():\n    global cache\n    from sys import modules as cache, stdout\ndef probe(name):\n    return cache[name]\n",
))
def test_local_and_from_import_cache_bindings_are_real_native_values(source):
    text = _emit(source)
    assert len(_calls(text, "py_sys_modules")) == 1
    assert not _calls(text, "py_cpy_getattr")
    assert not _calls(text, "py_cpy_getitem")


@pytest.mark.parametrize("arguments", ("name", "name, None, None, ()", "name, None, None, (), 0"))
def test_dynamic_import_publishes_new_cache_value_before_cleanup(arguments):
    text = _emit("def take(*, value, later=None, **extras):\n    return value\ndef fail():\n    raise ValueError('later')\ndef probe(name):\n    return take(value=__import__(" + arguments + "), later=fail())\n")
    calls = list(re.finditer(r'^  (%[^ ]+) = call [^\n]*@py_builtin_import\([^\n]*\)\n', text, re.M))
    assert len(calls) == 1
    call = calls[0]
    assert text[call.end():].splitlines()[0].lstrip().startswith("store ptr " + call.group(1) + ",")
