"""Execute opaque local annotations on an imported aggregate return."""
from __future__ import annotations

import os
import subprocess


MODULES = ["records", "provider", "other", "consumer"]
RECORDS = """def valueclass(cls):
    return cls

@valueclass
class Pair:
    first: int
    second: int
"""
PROVIDER = """from records import Pair

def make() -> Pair:
    return Pair(17, 25)
"""
OTHER = """class Pair:
    first: int
    second: int
"""
CONSUMER = """from provider import make

def read() -> int:
    value: Pair = make()
    opaque: UnknownLocalType = make()
    return value.first + opaque.second

def main() -> None:
    total = 0
    for index in range(64):
        total += read()
    assert total == 2688
    print("VALUE_ANNOTATION_OK", total)

main()
"""
EXPECTED = "VALUE_ANNOTATION_OK 2688\n"


def package_sources(directory):
    paths = []
    for name, source in zip(MODULES, (RECORDS, PROVIDER, OTHER, CONSUMER)):
        path = directory / (name + ".py")
        path.write_text(source)
        paths.append(str(path))
    return paths


def test_unresolved_local_value_annotation_executes_natively(tmp_path, pcc_runtime_archive):
    from pcc.frontends.python.pipeline import compile_python_multi

    executable = tmp_path / "unresolved_value_annotation"
    compile_python_multi(
        package_sources(tmp_path), str(executable), module_names=MODULES,
        entry_module="consumer", backend="self", libpython_mode="off",
        ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        result = subprocess.run(
            [str(executable)], cwd=tmp_path, capture_output=True, text=True,
            timeout=20, env=dict(os.environ, PCC_GC_BACKEND=str(backend), PATH=""),
        )
        (tmp_path / ("gc" + str(backend) + ".stdout")).write_text(result.stdout)
        (tmp_path / ("gc" + str(backend) + ".stderr")).write_text(result.stderr)
        assert result.returncode == 0, f"GC{backend}: {result.stdout}{result.stderr}"
        assert result.stderr == "", f"GC{backend}: {result.stderr}"
        assert result.stdout == EXPECTED, f"GC{backend}: {result.stdout}"
