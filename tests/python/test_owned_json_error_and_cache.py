"""Malformed JSON remains a ValueError and a recoverable C cache miss."""
from pathlib import Path
import os
import subprocess

import pytest


@pytest.mark.parametrize("source", ["{", "{ ", "{1: 2}", '{"a": 1,}'])
def test_owned_json_decode_error_is_a_value_error(source):
    from pcc.py_stdlib import json

    with pytest.raises(ValueError) as caught:
        json.loads(source)
    assert isinstance(caught.value, json.JSONDecodeError)


def test_malformed_compile_cache_is_a_miss(tmp_path):
    from pcc.evaluater.c_evaluator import _compile_cache_path, _load_compiled_artifact

    key = "ab" + "0" * 62
    path = Path(_compile_cache_path(str(tmp_path), key))
    path.parent.mkdir(parents=True)
    path.write_text("{", encoding="utf-8")
    assert _load_compiled_artifact(str(tmp_path), key) is None


def test_owned_json_errors_execute_natively(tmp_path, pcc_py_runtime_archive):
    from pcc.py_frontend.pipeline import compile_python_multi
    from pcc.py_stdlib import json

    provider = tmp_path / "owned_json.py"
    provider.write_text(Path(json.__file__).read_text(encoding="utf-8"), encoding="utf-8")
    source = tmp_path / "json_errors.py"
    source.write_text('''import owned_json
def main():
    for source in ["{", "{ ", "{1: 2}", '{"a": 1,}']:
        try:
            owned_json.loads(source)
        except ValueError as error:
            print(isinstance(error, owned_json.JSONDecodeError), error.pos)
    print(owned_json.loads('{"a": 42}')['a'])
main()
''', encoding="utf-8")
    binary = tmp_path / "json_errors"
    compile_python_multi(
        [str(provider), str(source)], str(binary),
        module_names=["owned_json", "json_errors"], entry_module="json_errors",
        backend="self", libpython_mode="off", runtime_archive=str(pcc_py_runtime_archive),
    )
    for gc in range(5):
        result = subprocess.run(
            [str(binary)], capture_output=True, text=True, timeout=10,
            env=dict(os.environ, PCC_GC_BACKEND=str(gc)),
        )
        assert result.returncode == 0, f"GC{gc}: {result.stderr}; {result.stdout}"
        assert result.stdout == "True 1\nTrue 2\nTrue 1\nTrue 8\n42\n", f"GC{gc}: {result.stdout}"
