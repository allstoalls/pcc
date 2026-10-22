"""Production shlex discovery, complete caller bodies and native execution."""
import hashlib
import json
import os
from pathlib import Path
import re
import sys

import pytest

from tests.python.owned_regression_support import explicit_owned_runtime
from tests.python.process_timeout import run_process_group_timeout


ROOT = Path(__file__).resolve().parents[2]
CONTROL = ROOT / "tests/fixtures/native/owned_shlex_provider.py"
RESOURCE_CONTROL = ROOT / "tests/fixtures/native/worker_resource_admission.py"


def _production_closure(program):
    from pcc.frontends.python import pipeline
    module = pipeline._module_name_from_src(str(program))
    sources, modules, recursive = pipeline._prepare_single_source_compile_closure(
        str(program), module, emit_llvm_only=False, libpython_mode="off",
        ir_scaffold_mode="on", recursive_stdlib=False, python_library=False,
    )
    sources, modules = pipeline._prepare_multi_source_compile_closure(
        sources, modules, recursive_stdlib=recursive, ir_scaffold_mode="on",
    )
    return module, sources, modules


def _body(text, function):
    found = re.search(
        r"^define[^\n]*@user_[^(\n]*_" + re.escape(function)
        + r"\([^\n]*\{\n(.*?)^\}", text, re.MULTILINE | re.DOTALL,
    )
    assert found is not None, function
    return found.group(1)


@pytest.mark.parametrize("statement", [
    "import shlex", "import shlex as words", "from shlex import quote as quote_word",
    "from shlex import join, split",
])
def test_shlex_provider_is_admitted_by_production_discovery(tmp_path, statement):
    program = tmp_path / "shlex_user.py"
    program.write_text(statement + "\n", encoding="utf-8")
    _, sources, modules = _production_closure(program)
    assert modules.count("shlex") == 1
    assert Path(sources[modules.index("shlex")]).resolve() == (ROOT / "pcc/stdlib/shlex.py").resolve()


def test_shlex_functions_and_aliases_have_owned_complete_bodies(tmp_path):
    from pcc.frontends.python.pipeline_context import compile_contextual_per_module_fallback_counts
    module, sources, modules = _production_closure(CONTROL)
    counts = compile_contextual_per_module_fallback_counts(
        sources, modules, [module, "shlex"], ir_scaffold_mode="on",
        strict_no_libpython=True, emit_ir_dir=str(tmp_path), entry_module=module,
    )
    assert counts == {module: 0, "shlex": 0}
    for owner, functions in ((module, ("check_words", "main")), ("shlex", ("quote", "join", "split"))):
        text = (tmp_path / (owner.replace(".", "_") + ".ll")).read_text()
        for function in functions:
            body = _body(text, function)
            assert "strict.nolib.stub" not in body, (owner, function)
            assert "@py_cpy_" not in body, (owner, function)


def test_original_resource_command_bodies_use_the_owned_shlex_provider(tmp_path):
    from pcc.frontends.python.pipeline_context import compile_contextual_per_module_fallback_counts
    module, sources, modules = _production_closure(RESOURCE_CONTROL)
    assert "shlex" in modules
    counts = compile_contextual_per_module_fallback_counts(
        sources, modules, [module], ir_scaffold_mode="on", strict_no_libpython=True,
        emit_ir_dir=str(tmp_path), entry_module=module,
    )
    assert counts == {module: 0}
    text = (tmp_path / (module.replace(".", "_") + ".ll")).read_text()
    for function in ("child_command", "spawn_failure"):
        body = _body(text, function)
        assert "strict.nolib.stub" not in body, function
        assert "@py_cpy_" not in body, function
    # This IR proof does not qualify the other native resource-driver calls.
    # Its unchanged full native execution test remains the acceptance owner.


def test_shlex_control_matches_cpython(tmp_path):
    result = run_process_group_timeout(
        [sys.executable, "-B", str(CONTROL)], timeout=10,
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, "OWNED_SHLEX_REFERENCE_OK\n", "")


@pytest.mark.integration
def test_shlex_provider_native_five_collectors(
    tmp_path, explicit_owned_runtime, python_program_compiler, request, capfd,
):
    binary = tmp_path / "owned-shlex.out"
    receipt = {
        "status": "COMPILING", "source_sha256": hashlib.sha256(CONTROL.read_bytes()).hexdigest(),
        "compiler": request.node.callspec.params["python_program_compiler"],
        "runtime_sha256": hashlib.sha256(explicit_owned_runtime.read_bytes()).hexdigest(),
        "backend": "self", "libpython": "off", "executions": [],
    }
    path = tmp_path / "shlex-native.json"
    def save():
        path.write_text(json.dumps(receipt, indent=2) + "\n")
    save()
    try:
        python_program_compiler(
            str(CONTROL), str(binary), backend="self", libpython_mode="off",
            ir_scaffold_mode="on", runtime_archive=str(explicit_owned_runtime),
        )
    except BaseException as error:
        receipt.update(status="COMPILE_FAILED", error=type(error).__name__ + ": " + str(error))
        save()
        raise
    finally:
        captured = capfd.readouterr()
        (tmp_path / "compile.stdout").write_text(captured.out)
        (tmp_path / "compile.stderr").write_text(captured.err)
    receipt["binary_sha256"] = hashlib.sha256(binary.read_bytes()).hexdigest()
    for collector in range(5):
        environment = dict(os.environ, PCC_GC_BACKEND=str(collector), PATH="",
                           PCC_HOST_PYTHON="/nonexistent/host-python")
        result = run_process_group_timeout([str(binary)], env=environment, timeout=10)
        (tmp_path / ("gc" + str(collector) + ".stdout")).write_text(result.stdout)
        (tmp_path / ("gc" + str(collector) + ".stderr")).write_text(result.stderr)
        passed = (result.returncode, result.stdout, result.stderr) == (
            0, "OWNED_SHLEX_OK " + str(collector) + "\n", "",
        )
        receipt["executions"].append({"requested_collector": collector,
            "observed_collector": collector if passed else None,
            "returncode": result.returncode, "passed": passed})
        save()
    receipt["status"] = "PASS" if all(row["passed"] for row in receipt["executions"]) else "NATIVE_EXECUTION_FAILED"
    save()
    assert receipt["status"] == "PASS", receipt
