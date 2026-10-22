"""The Darwin in-process sampler attributes CPU work to the sampled binary."""

import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

import pytest

from scripts.pcc_inprocess_profile import report


# diagnostic sampler requires macOS ARM64 and host clang
@pytest.mark.pcc_gate(probe=lambda: not (sys.platform != "darwin" or platform.machine() != "arm64" or shutil.which("clang") is None))
def test_inprocess_sampler_attributes_busy_loop_to_main(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    dylib = tmp_path / "sampler.dylib"
    built = subprocess.run(
        ["clang", "-dynamiclib", "-O2", "-Wall", "-Wextra", "-Werror",
         str(root / "scripts/pcc_inprocess_sampler.c"), "-o", str(dylib)],
        capture_output=True, text=True, timeout=30,
    )
    assert built.returncode == 0, built.stderr
    source = tmp_path / "busy.c"
    source.write_text(
        '#include <stdint.h>\n'
        'static volatile uint64_t sink;\n'
        'int main(void) {\n'
        '    for (uint64_t i = 0; i < UINT64_C(4000000000); i++) sink += i;\n'
        '    return sink == UINT64_C(7999999998000000000) ? 0 : 1;\n'
        '}\n'
    )
    binary = tmp_path / "busy"
    built = subprocess.run(
        ["clang", "-O2", str(source), "-o", str(binary)],
        capture_output=True, text=True, timeout=30,
    )
    assert built.returncode == 0, built.stderr
    raw = tmp_path / "busy.raw"
    env = dict(os.environ)
    env.pop("LC_ALL", None)
    env.update(DYLD_INSERT_LIBRARIES=str(dylib),
               PCC_THREAD_SAMPLE_FILE=str(raw), PCC_THREAD_SAMPLE_US="2000")
    ran = subprocess.run([str(binary)], env=env, capture_output=True,
                         text=True, timeout=20)
    assert ran.returncode == 0, ran.stderr
    result = report(binary, raw, dylib)
    assert result["main_samples"] >= 100
    assert result["outside_main_samples"] == 0
    assert result["symbols"][0]["name"] == "_main"
    assert result["symbols"][0]["samples"] == result["main_samples"]
    assert result["cpu_delta_us"][0] > 100000
