"""pcc's json provider appends and slices strings whole when nothing needs escaping.

``pcc/stdlib/json.py`` is what compiled ``json`` code runs (pcc1's AST and
export wires).  Its encoder emitted every string one character at a time and
its decoder rebuilt every string one character at a time; under pcc1 that was
22% of a Stage2 export worker.  A string whose characters are all in
0x20..0x7E except '"' and '\\' is now appended whole, and a string without a
backslash before its closing quote is now one slice.  Both must keep CPython's
bytes and values exactly, including at the edges of those conditions.
"""

from __future__ import annotations

import importlib.util
import json as host_json
import os
from pathlib import Path
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
PROVIDER = REPO_ROOT / "pcc" / "stdlib" / "json.py"

# Strings at and around the whole-string condition.
_STRINGS = [
    "", "plain", " ", "~", "a b ~ !#$%&'()*+,-./:;<=>?@[]^_`{|}",
    'quote"inside', "back\\slash", '"', "\\", "\\\"",
    "tab\tx", "nl\nx", "cr\rx", "\x00", "\x1f", "\x7f", "del\x7fx",
    "é", "café", "日本語", "🚀", "mixed é\"\\\n\x7f🚀",
    "long ascii " * 50,
]

_OBJECTS = [
    _STRINGS,
    {key: key for key in _STRINGS},
    {"nested": [{"k": value, value: [value, 1, None]} for value in _STRINGS]},
]

_KWARGS = [
    {},
    {"ensure_ascii": False},
    {"separators": (",", ":")},
    {"indent": 2, "sort_keys": True},
    {"indent": 2, "ensure_ascii": False},
]

# Documents whose strings mix the slice path and the escape path.
_DOCUMENTS = [
    '["a", "b\\"c", "d"]',
    '{"k": "v", "k2": "w\\\\", "k3": "x"}',
    '["\\u00e9t\\u00e9", "plain", "\\n", ""]',
    '"no escapes at all"',
    '"escape at the end\\\\"',
    '["\\"", "\\\\", "/", "\\/"]',
]


def _provider():
    spec = importlib.util.spec_from_file_location("pcc_stdlib_json", PROVIDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dumps_matches_cpython_around_the_whole_string_condition():
    provider = _provider()
    for obj in _OBJECTS:
        for kwargs in _KWARGS:
            assert provider.dumps(obj, **kwargs) == host_json.dumps(obj, **kwargs), kwargs


def test_loads_matches_cpython_for_slice_and_escape_strings():
    provider = _provider()
    for obj in _OBJECTS:
        for kwargs in _KWARGS:
            text = host_json.dumps(obj, **kwargs)
            assert provider.loads(text) == host_json.loads(text) == obj
    for text in _DOCUMENTS:
        assert provider.loads(text) == host_json.loads(text), text


@pytest.mark.parametrize("text", ['"abc', '["abc', '"ab\\"', '{"k": "v'])
def test_unterminated_strings_still_raise(text):
    provider = _provider()
    with pytest.raises(ValueError):
        provider.loads(text)
    with pytest.raises(ValueError):
        host_json.loads(text)


_PROBE = '''
import json

STRINGS = {strings!r}
OBJECTS = [
    STRINGS,
    {{key: key for key in STRINGS}},
    {{"nested": [{{"k": value, value: [value, 1, None]}} for value in STRINGS]}},
]
KWARGS = {kwargs!r}
DOCUMENTS = {documents!r}


def main():
    for obj in OBJECTS:
        for kwargs in KWARGS:
            text = json.dumps(obj, **kwargs)
            print(text.encode("utf-8").hex())
            print(json.loads(text) == obj)
    for text in DOCUMENTS:
        print(json.dumps(json.loads(text)))


main()
'''


def test_native_json_whole_string_paths_match_cpython(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "json_probe.py"
    source.write_text(_PROBE.format(strings=_STRINGS, kwargs=_KWARGS, documents=_DOCUMENTS),
                      encoding="utf-8")
    expected = subprocess.run(
        [sys.executable, str(source)], capture_output=True, text=True, timeout=30,
    )
    assert expected.returncode == 0, expected.stderr
    assert "False" not in expected.stdout.split()
    output = tmp_path / "json_probe"
    python_program_compiler(
        str(source), str(output), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for gc in range(5):
        ran = subprocess.run(
            [str(output)], capture_output=True, text=True, timeout=60,
            env=dict(os.environ, PCC_GC_BACKEND=str(gc)),
        )
        assert ran.returncode == 0, f"GC{gc}: {ran.stdout}{ran.stderr}"
        assert ran.stdout == expected.stdout, f"GC{gc}"
