"""Exported global defaults must cross the dynamic default-value boundary."""

import os
import subprocess

import pytest

from pcc.py_frontend.pipeline import compile_python_multi


@pytest.mark.parametrize("provider_name", ["defaults_provider", "pcc.defaults_provider"])
def test_cross_module_scalar_global_defaults(tmp_path, pcc_py_runtime_archive, provider_name):
    provider = tmp_path / "provider.py"
    provider.write_text('''
BASE = 131072
FLAGS = BASE | 2
ACTIVE = BASE > 0
RATE = BASE / 2.0
def flags(*, value: int = FLAGS) -> int:
    return value
def active(*, value: bool = ACTIVE) -> bool:
    return value
def rate(*, value: float = RATE) -> float:
    return value
''', encoding="utf-8")
    source = tmp_path / "consumer.py"
    source.write_text(f'''
from {provider_name} import flags, active, rate
def main():
    print(flags(), active(), rate())
main()
''', encoding="utf-8")
    output = tmp_path / "consumer"
    compile_python_multi([str(provider), str(source)], str(output),
                         module_names=[provider_name, "defaults_consumer"],
                         entry_module="defaults_consumer", recursive_stdlib=True,
                         backend="self", libpython_mode="off",
                         runtime_archive=str(pcc_py_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f"GC{gc}: {result.stderr}"
        assert result.stdout == "131074 True 65536.0\n", f"GC{gc}: {result.stdout}"
