"""Compiler version decisions and runtime introspection share one target."""

import os
import subprocess

import pytest

from pcc.driver import python_target
from pcc.frontends.python.codegen.control_flow_lowering import ControlFlowLoweringMixin
from pcc.frontends.python.codegen.native_system import NativeSystemLoweringMixin
from pcc.frontends.python.parser import parse
from pcc.stdlib import platform, sys, sysconfig
from tests.python.python_target_canary import PYTHON_TARGET_SOURCE, PYTHON_TARGET_STDOUT


class VersionEmitter(NativeSystemLoweringMixin):
    def __init__(self):
        self.builder = self
        self.runtime = {
            name: name
            for name in ("py_tuple_new", "py_tuple_set_item", "py_int_from_i64")
        }

    def _fresh(self, name):
        return name

    def call(self, function, args, **kwargs):
        def integer(value):
            return value.value if hasattr(value, "value") else value.constant

        if function == "py_tuple_new":
            return [None] * integer(args[0])
        if function == "py_int_from_i64":
            return integer(args[0])
        assert function == "py_tuple_set_item"
        args[0][integer(args[1])] = args[2]


class VersionFolder(ControlFlowLoweringMixin):
    def _native_builtin_module_for_name(self, name):
        return "sys" if name == "sys" else ""


def test_version_literals_emission_and_provider_agree():
    target = (3, 15, 0)
    assert python_target.PYTHON_TARGET_VERSION_INFO == target
    assert (
        python_target.PYTHON_TARGET_MAJOR,
        python_target.PYTHON_TARGET_MINOR,
        python_target.PYTHON_TARGET_MICRO,
    ) == target
    assert python_target.PYTHON_TARGET_VERSION == "3.15"
    assert python_target.PYTHON_TARGET_FULL_VERSION == "3.15.0"
    assert python_target.PYTHON_TARGET_VERSION_PARTS == ("3", "15", "0")
    emitter = VersionEmitter()
    assert tuple(emitter._emit_sys_version_info_tuple()) == target
    assert (
        tuple(
            emitter._emit_sys_version_info_attr(name)
            for name in ("major", "minor", "micro")
        )
        == target
    )
    assert tuple(sys.version_info) == target
    assert (
        sys.version_info.major,
        sys.version_info.minor,
        sys.version_info.micro,
    ) == target
    assert sys.version == "3.15.0 (pcc self-host)"
    assert platform.python_version() == "3.15.0"
    assert platform.python_version_tuple() == ("3", "15", "0")


@pytest.mark.parametrize(
    "condition, expected",
    [
        ("sys.version_info >= (3, 15)", True),
        ("sys.version_info < (3, 15)", False),
        ("sys.version_info >= (3, 16)", False),
        ("sys.version_info == (3, 15, 0)", True),
        ("sys.version_info != (3, 15, 0)", False),
    ],
)
def test_version_branch_folding_agrees_with_runtime_tuple(condition, expected):
    module = parse(
        "import sys\nif " + condition + ":\n    pass\n", "version_contract.py"
    )
    assert VersionFolder()._static_bool_condition(module.body[1].cond) is expected


@pytest.mark.parametrize("package_target", [None, "3.11"])
def test_runtime_sysconfig_version_is_independent_of_package_selection(
    monkeypatch, package_target
):
    monkeypatch.setattr(sysconfig, "sys", sys)
    monkeypatch.setattr(sysconfig, "_CONFIG_VARS", None)
    monkeypatch.delenv("PCC_PACKAGE_TARGET_PYTHON", raising=False)
    if package_target is not None:
        monkeypatch.setenv("PCC_PACKAGE_TARGET_PYTHON", package_target)
    assert sysconfig.get_python_version() == "3.15"
    assert sysconfig.get_config_var("VERSION") == "3.15"
    assert sysconfig.get_config_var("py_version") == "3.15.0"
    assert tuple(sys.version_info) == (3, 15, 0)


@pytest.mark.parametrize("expansion", ["required", "recursive"])
def test_provider_closure_admits_target_constants_only(
    tmp_path, monkeypatch, expansion
):
    from pcc.frontends.python import pipeline_dependency_closure as closure

    source = tmp_path / "consumer.py"
    source.write_text(
        "from pcc.driver.python_target import PYTHON_TARGET_VERSION_PARTS\n"
        "from pcc.driver.cli_core import cli_main\n"
    )
    sources = [str(source)]
    modules = ["consumer"]
    seen = {"consumer": str(source)}
    monkeypatch.setattr(closure, "_locate_stdlib_module_source", lambda name: None)
    monkeypatch.setattr(closure, "_stdlib_module_compiles", lambda path, name: True)
    if expansion == "required":
        closure._expand_required_native_builtin_providers(sources, modules, seen)
    else:
        closure._expand_recursive_stdlib(sources, modules, seen)
    assert "pcc.driver.python_target" in modules
    assert "pcc.driver.cli_core" not in modules
    assert seen["pcc.driver.python_target"] == python_target.__file__


def test_host_pcc_emits_native_python_target_contract_with_c_runtime(
    tmp_path, monkeypatch
):
    from pcc.frontends.python.pipeline import compile_python

    # This is the cheap host->native semantic gate; fresh pcc1/runtime ownership
    # and bootstrap are separate release gates using the identical program.
    monkeypatch.delenv("PCC_RUNTIME_ARCHIVE", raising=False)
    source = tmp_path / "semantic_target.py"
    output = tmp_path / "semantic_target"
    source.write_text(PYTHON_TARGET_SOURCE)
    compile_python(
        str(source),
        str(output),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
        recursive_stdlib=True,
    )
    environment = os.environ.copy()
    environment["PCC_HOST_PYTHON"] = "/usr/bin/false"
    environment["PCC_PACKAGE_TARGET_PYTHON"] = "3.11"
    result = subprocess.run(
        [str(output)], capture_output=True, text=True, timeout=20, env=environment
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == PYTHON_TARGET_STDOUT
