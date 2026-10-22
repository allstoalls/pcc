"""A module's vars result keeps its actual namespace and owned lifetime."""
from __future__ import annotations

import hashlib
import json
import os
import sys

import pytest

from tests.python.owned_regression_support import (
    _record_execution,
    explicit_owned_runtime,
)
from tests.python.test_native_namespace_projection_behaviors import (
    _error_output,
    _native_format,
    _observed_collectors,
)


PROVIDER = "value: object = None\n"

PROGRAM = '''import gc
import provider_vars_owner as owner
import provider_vars_owner as alias

events = []

class Token:
    def __init__(self, label):
        self.label = label

    def __del__(self):
        events.append(self.label)

class Plain:
    def __init__(self):
        self.a = 1
        self.b = 'two'

def exercise():
    namespace = vars(owner)
    assert namespace is owner.__dict__
    assert vars(alias) is namespace
    for index in range(16):
        assert vars(owner) is namespace
        gc.collect()
    token = Token('owned')
    namespace['value'] = token
    del token
    gc.collect()
    assert events == []
    assert owner.value is alias.value
    assert owner.value.label == 'owned'
    namespace['value'] = None
    assert owner.value is None
    replacement = object()
    owner.value = replacement
    assert namespace['value'] is replacement
    alias.extra = replacement
    assert namespace['extra'] is replacement
    del namespace['extra']
    try:
        owner.extra
    except AttributeError:
        pass
    else:
        raise AssertionError('module namespace deletion was detached')
    namespace['extra'] = replacement
    delattr(alias, 'extra')
    assert 'extra' not in namespace
    del namespace['value']
    try:
        alias.value
    except AttributeError:
        pass
    else:
        raise AssertionError('module value deletion was detached')
    namespace['value'] = None
    plain = Plain()
    setattr(plain, 'z', 3)
    fields = vars(plain)
    assert fields['a'] == 1
    assert fields['b'] == 'two'
    assert fields['z'] == 3
    for invalid in (None, 123, 'text'):
        try:
            vars(invalid)
        except TypeError:
            pass
        else:
            raise AssertionError('vars accepted an object without a namespace')

def main():
    exercise()
    gc.collect()
    assert events == ['owned']
    assert vars(owner) is owner.__dict__
    assert alias.value is None
    print('NATIVE_MODULE_VARS_NAMESPACE_OK')

main()
'''

EXPECTED = "NATIVE_MODULE_VARS_NAMESPACE_OK\n"


def _write_provider(directory):
    (directory / "provider_vars_owner.py").write_text(PROVIDER)


def _reference_program(directory):
    program = directory / "program.py"
    program.write_text(PROGRAM)
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    environment.pop("LC_ALL", None)
    result = _record_execution(
        directory, "reference", [sys.executable, "-B", str(program)], environment,
    )
    assert (result["returncode"], result["stdout"], result["stderr"]) == (0, EXPECTED, "")
    return program, result


def test_module_vars_namespace_reference(tmp_path):
    _write_provider(tmp_path)
    _reference_program(tmp_path)


@pytest.mark.integration
@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_module_vars_namespace_native_five_gc(
    tmp_path, request, python_program_compiler, explicit_owned_runtime, capfd,
):
    _write_provider(tmp_path)
    program, reference = _reference_program(tmp_path)
    binary = tmp_path / "module-vars.out"
    receipt = {
        "status": "RUNNING",
        "compiler_fixture_parameter": request.node.callspec.params["python_program_compiler"],
        "compiler_mode": (
            "pcc1-routed" if python_program_compiler.__module__ == "tests.pcc1_route"
            else request.node.callspec.params["python_program_compiler"]
        ),
        "backend": "self", "libpython": "off",
        "runtime_archive": str(explicit_owned_runtime),
        "runtime_sha256": hashlib.sha256(explicit_owned_runtime.read_bytes()).hexdigest(),
        "sources": {
            name: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
            for name in ("program.py", "provider_vars_owner.py")
        },
        "reference": reference, "executions": [],
    }
    receipt_path = tmp_path / "module-vars-namespace.json"
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    try:
        python_program_compiler(
            str(program), str(binary), backend="self", libpython_mode="off",
            ir_scaffold_mode="on", runtime_archive=str(explicit_owned_runtime),
        )
    except Exception as error:
        receipt.update(status="COMPILE_FAILED", error=type(error).__name__ + ": " + str(error))
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
        raise
    finally:
        captured = capfd.readouterr()
        (tmp_path / "compiler-wrapper.stdout").write_text(captured.out)
        (tmp_path / "compiler-wrapper.stderr").write_text(captured.err)
    assert binary.is_file(), "Compiler did not emit an executable"
    receipt["binary_format"] = _native_format(binary)
    assert receipt["binary_format"] is not None
    receipt["binary_sha256"] = hashlib.sha256(binary.read_bytes()).hexdigest()
    for collector in range(5):
        log = tmp_path / ("gc" + str(collector) + ".gc.jsonl")
        environment = dict(
            os.environ, PCC_GC_BACKEND=str(collector), PCC_LOG="gc",
            PCC_LOG_FORMAT="json", PCC_LOG_FILE=str(log), PATH="",
            PCC_GC_REFCOUNT_PROVENANCE_PROBE="2",
            PCC_HOST_PYTHON="/nonexistent/host-python", PCC_HOST_PCC="/nonexistent/host-pcc",
        )
        environment.pop("LC_ALL", None)
        row = {"requested_gc": collector, "observed_gc": [], "status": "FAIL", "errors": []}
        try:
            row.update(_record_execution(tmp_path, "gc" + str(collector), [str(binary)], environment))
            assert (row["returncode"], row["stdout"], row["stderr"]) == (0, EXPECTED, "")
        except Exception as error:
            row["errors"].append(type(error).__name__ + ": " + str(error))
            if "returncode" not in row:
                row.update(command=[str(binary)], returncode=None)
                for label in ("stdout", "stderr"):
                    row[label] = _error_output(getattr(error, label, None))
                    (tmp_path / ("gc" + str(collector) + "." + label)).write_text(row[label])
        try:
            row["observed_gc"] = _observed_collectors(log)
            row["gc_log_sha256"] = hashlib.sha256(log.read_bytes()).hexdigest()
            assert row["observed_gc"] == [collector]
        except Exception as error:
            row["errors"].append(type(error).__name__ + ": " + str(error))
        row["status"] = "FAIL" if row["errors"] else "PASS"
        receipt["executions"].append(row)
        (tmp_path / ("gc" + str(collector) + ".json")).write_text(json.dumps(row, indent=2) + "\n")
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    receipt["status"] = (
        "PASS" if all(row["status"] == "PASS" for row in receipt["executions"])
        else "NATIVE_EXECUTION_FAILED"
    )
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    assert receipt["status"] == "PASS", receipt
