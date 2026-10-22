"""Execute the current emitted selector with an isolated raw-ABI C harness.

This regression needs no pcc1, runtime archive, cached object, or historical
source. It does not exercise managed GC or certify a full-runtime bootstrap.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

import pytest

from tests.python import test_freestanding_gc_relocation_selector as contract

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).with_name("fixtures")
ORACLE = json.loads((FIXTURES / "gc4_page_ranking_expected.json").read_text())
pytestmark = [
    pytest.mark.xdist_group("gc4-selector-native-rank"),
    pytest.mark.pcc_gate(
        probe=lambda: sys.platform.startswith("linux")
        and platform.machine().lower() in ("x86_64", "amd64")
        and shutil.which("cc") is not None
        and shutil.which("nm") is not None,
    ),
]

# Use the production single-module frontend settings and runtime pass list.
# Never call build_runtime_archive/ensure_target_runtime: no runtime is linked.
EMIT_MODULE = r"""
from pathlib import Path
import hashlib, json, sys
from pcc.frontends.python.owned_runtime_build import _compile_runtime_module, runtime_ir_passes
from pcc.ir.optimization.driver import optimize_ir
from pcc.backend.owned_object_emit import emit_owned_object
source, output = map(Path, sys.argv[1:])
triple = 'x86_64-unknown-linux-gnu'
ir = output.with_suffix('.ll')
_compile_runtime_module('freestanding_gc_relocation_selector', str(source), str(ir), triple)
passes = runtime_ir_passes(str(source.parent.parent))
text = optimize_ir(ir.read_text(), passes)
ir.write_text(text)
output.write_bytes(emit_owned_object(text, triple))
print(json.dumps({'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                  'object_sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
                  'runtime_passes': passes, 'target': triple}))
"""


@pytest.fixture(scope="module")
def emitted_selector(tmp_path_factory):
    build = tmp_path_factory.mktemp("gc4-page-ranking-native")
    obj = build / "selector.o"
    env = dict(os.environ)
    env.pop("LC_ALL", None)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PCC_TEST_NO_NATIVE_PROVISIONING"] = "1"
    compile_result = subprocess.run(
        [sys.executable, "-c", EMIT_MODULE, str(contract.STRICT_SOURCE), str(obj)],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=30,
    )
    (build / "compile.log").write_text(compile_result.stdout + compile_result.stderr)
    assert compile_result.returncode == 0, compile_result.stdout + compile_result.stderr
    (build / "harness_globals.h").write_text(
        "\n".join("uint64_t " + name + ";" for name in sorted(contract.RAW_GLOBAL_IMPORTS))
        + "\n"
    )
    executable = build / "selector-harness"
    link = subprocess.run(
        ["cc", "-O2", "-I", str(build),
         str(FIXTURES / "gc4_page_ranking_native.c"), str(obj),
         "-Wl,--wrap=malloc", "-Wl,--wrap=free", "-o", str(executable)],
        capture_output=True, text=True, timeout=15,
    )
    (build / "link.log").write_text(link.stdout + link.stderr)
    assert link.returncode == 0, link.stdout + link.stderr
    return build, obj, executable


@pytest.mark.parametrize("case", ORACLE["cases"], ids=lambda c: "-".join(map(str, c["argv"])))
def test_current_native_selector_preserves_page_semantics(emitted_selector, case):
    build, _obj, executable = emitted_selector
    argv = case["argv"]
    result = subprocess.run(
        [str(executable), *map(str, argv)], capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    observed = json.loads(result.stdout)
    with (build / "results.jsonl").open("a") as stream:
        stream.write(json.dumps({"case": argv, "observed": observed}) + "\n")
    expected = dict(zip(ORACLE["fields"], case["expected"], strict=True))
    assert {name: observed[name] for name in ORACLE["fields"]} == expected
    assert observed["rank_live"] == observed["lock_errors"] == 0
    if argv[4] or argv[5] or argv[3] <= 1 or argv[0].endswith("busy"):
        assert observed["pin_checks"] == case["original_pin_checks"]
    if argv[0] == "scale":
        # Measured calls from native scoring/add, not a Python algorithm model.
        assert observed["pin_checks"] <= 4 * argv[1] * argv[2]
    if argv[0] == "residual" and argv[4] == 0:
        # 39 fresh owners: ten full preflights + page commit + residual scan.
        # All residual scores are -1 (the score function never returns zero),
        # so no winner may be rescored and increment this a second time.
        assert observed["fresh"] == 39 * (10 + 1 + 1)
        assert observed["selected"] == 114


def test_current_owned_object_has_exact_strict_closure(emitted_selector):
    _build, obj, _executable = emitted_selector
    undefined = subprocess.check_output(["nm", "-u", str(obj)], text=True, timeout=10)
    assert {line.split()[-1] for line in undefined.splitlines()} == (
        contract.RAW_FUNCTION_IMPORTS | contract.RAW_GLOBAL_IMPORTS
    )
    defined = subprocess.check_output(
        ["nm", "-g", "--defined-only", str(obj)], text=True, timeout=10,
    )
    assert {line.split()[-1] for line in defined.splitlines()} == (
        contract.OWNED_SYMBOLS | contract.OWNED_GLOBALS
    )
