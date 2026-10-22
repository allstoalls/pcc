"""Receipt-selected pcc1/pcc2 execute generic cross-module ABI shapes."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest


def _compiler_receipt(compiler: Path):
    stage1 = compiler.parent
    if (stage1 / "build-receipt.json").is_file():
        receipt = json.loads((stage1 / "build-receipt.json").read_text())
        assert receipt["status"] == "SUCCEEDED"
        assert receipt["compiler_sha256"] == hashlib.sha256(
            compiler.read_bytes()
        ).hexdigest()
        return stage1, receipt["environment"]

    # A Stage2 binary must be linked to its successful build receipt; do not
    # manufacture a Stage1 manifest beside pcc2 to satisfy the source check.
    root = compiler.parent.parent
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["schema"] == "pcc.stage2-from-stage1-receipt.v1"
    assert manifest["status"] == "COMPLETE"
    record = json.loads((root / manifest["stage2_record"]).read_text())
    assert Path(record["compiler"]).resolve() == compiler
    assert record["compiler_sha256"] == hashlib.sha256(
        compiler.read_bytes()
    ).hexdigest()
    assert record["process"]["status"] == "COMPLETE"
    assert record["process"]["returncode"] == 0
    return Path(manifest["stage1_dir"]), record["process"]["environment"]


@pytest.mark.integration
@pytest.mark.pcc_gate(env="PCC_ABI_TEST_COMPILER")
def test_pcc1_value_arguments_and_nested_fields_execute(tmp_path):
    compiler = Path(os.environ["PCC_ABI_TEST_COMPILER"]).resolve(strict=True)
    repo = Path(__file__).resolve().parents[2]
    stage1, receipt_environment = _compiler_receipt(compiler)
    manifest = json.loads((stage1 / "source-manifest.json").read_text())
    for relative in (
        "pcc/frontends/python/codegen/method_call_lowering.py",
        "pcc/frontends/python/pipeline_context.py",
        "pcc/frontends/python/pipeline_exports.py",
        "pcc/frontends/python/type_infer.py",
        "pcc/frontends/python/codegen/class_gen.py",
    ):
        assert manifest["files"][relative] == hashlib.sha256(
            (repo / relative).read_bytes()
        ).hexdigest(), "compiler is stale for " + relative

    package = tmp_path / "abi_canary"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "records.py").write_text("""
def valueclass(cls):
    return cls

@valueclass
class PairRecord:
    first: int
    second: int
""".lstrip())
    provider = package / "provider.py"
    provider.write_text("""
from dataclasses import dataclass
from .records import PairRecord as Handle

def consume_handle(value: Handle) -> int:
    return value.first + value.second

def valueclass(cls):
    return cls

@valueclass
class Pair:
    left: int
    right: int

class Counter:
    def __init__(self):
        self.before = 1
        try:
            self.middle = 2
        except Exception:
            raise
        self.flag = False
        self.count = 7

    def total(self, pair: Pair) -> int:
        return pair.left + pair.right

@dataclass
class Record:
    value: int
    def prepare(self, text: str):
        self.cache = text

@dataclass
class Child(Record):
    other: str
""".lstrip())
    stress = "\nclass ManyFields:\n    def close(self, replacement: tuple[int]):\n"
    for index in range(32):
        stress += "        self.field_" + str(index) + " = replacement\n"
    stress += "    def __init__(self):\n"
    for index in range(32):
        stress += "        self.field_" + str(index) + ": list[int] = [" + str(index) + "]\n"
    provider.write_text(provider.read_text() + stress)
    entry = package / "__main__.py"
    entry.write_text("""
from .provider import Counter, Pair, ManyFields, Record, Child, Handle, consume_handle

def run() -> None:
    counter = Counter()
    pair = Pair(2, 3)
    handle = Handle(4, 6)
    assert consume_handle(handle) == 10
    assert consume_handle(value=handle) == 10
    print(counter.total(pair))
    print(counter.count)
    record = Record(7)
    record.prepare("cache")
    assert record.value == 7
    assert record.cache == "cache"
    child = Child(8, "child")
    child.prepare("inherited")
    assert child.value == 8
    assert child.other == "child"
    assert child.cache == "inherited"

run()
""".lstrip())
    checks = "\nmany = ManyFields()\n"
    for index in range(32):
        checks += "assert many.field_" + str(index) + "[0] == " + str(index) + "\n"
    entry.write_text(entry.read_text() + checks)
    env = os.environ.copy()
    env.update(receipt_environment)
    env.pop("LC_ALL", None)
    executable = tmp_path / "abi_canary.out"
    result = subprocess.run(
        [str(compiler), "--backend", "self", "--python-libpython=off",
         "--ir-scaffold=on", str(entry), "-o", str(executable)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60,
    )
    (tmp_path / "compile.stdout").write_text(result.stdout)
    (tmp_path / "compile.stderr").write_text(result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    result = subprocess.run(
        [str(executable)], env=env, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "5\n7\n"
