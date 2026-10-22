"""G-P3-LONGRUN slice 1: pause telemetry (count / sum / histogram).

`pcc_gc_record_pause` previously tracked only the max; the long-running
benchmark plan (docs/plans/gc-longrun-benchmark-plan.md) needs pause
COUNT, SUM, and a fixed histogram per backend. This C-harness gate
exercises an explicit-collect workload on each tracing backend and
asserts the new counters are consistent (backend 0's explicit
cycle collect is timed via pcc_gc_record_explicit_pause) (count > 0, sum >= max,
histogram buckets sum to count, reset zeroes everything).
"""
from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from tests.runtime_build_cache import cached_pcc_python_runtime

REPO_ROOT = Path(__file__).absolute().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"


def _cc() -> str:
    return os.environ.get("CC", "cc")


def _build_runtime(tmp_path: Path) -> Path:
    del tmp_path
    return cached_pcc_python_runtime()


_PROBE = """
#include "py_runtime.h"
#include <stdint.h>
#include <stdio.h>

int main(void) {
    if (pcc_gc_set_backend(%(backend)d) != 0) return 3;
    pcc_gc_telemetry_reset();

    for (int round = 0; round < 5; round++) {
        for (int i = 0; i < 2000; i++) {
            PyObject *o = pcc_gc_alloc(64, PY_TYPE_LIST, 0);
            if (o == 0) return 5;
            pcc_gc_release(o);
        }
        pcc_gc_collect(0);
    }

    int64_t count = pcc_gc_telemetry(PCC_GC_COUNTER_PAUSE_COUNT);
    int64_t sum_us = pcc_gc_telemetry(PCC_GC_COUNTER_PAUSE_SUM_US);
    int64_t max_us = pcc_gc_telemetry(PCC_GC_COUNTER_MAX_PAUSE_US);
    int64_t hist = 0;
    for (int m = PCC_GC_COUNTER_PAUSE_HIST_LT_100US;
         m <= PCC_GC_COUNTER_PAUSE_HIST_GE_10MS; m++) {
        hist += pcc_gc_telemetry(m);
    }
    printf("%%d\\n", count > 0);
    printf("%%d\\n", sum_us >= max_us);
    printf("%%d\\n", hist == count);

    pcc_gc_telemetry_reset();
    printf("%%lld\\n", (long long)(
        pcc_gc_telemetry(PCC_GC_COUNTER_PAUSE_COUNT)
        + pcc_gc_telemetry(PCC_GC_COUNTER_PAUSE_SUM_US)
        + pcc_gc_telemetry(PCC_GC_COUNTER_PAUSE_HIST_LT_100US)
    ));
    return 0;
}
"""


@pytest.mark.parametrize("backend", [0, 1, 2, 3, 4])
def test_pause_telemetry_counters_consistent(tmp_path, backend):
    work_runtime = _build_runtime(tmp_path)
    src = tmp_path / "pause_probe.c"
    exe = tmp_path / "pause_probe.out"
    src.write_text(textwrap.dedent(_PROBE % {"backend": backend}).lstrip(), encoding="utf-8")
    build = subprocess.run(
        [
            _cc(),
            "-std=c11",
            f"-I{work_runtime / 'include'}",
            str(src),
            str(work_runtime / "libpy_runtime_pcc_py.a"),
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stderr
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == ["1", "1", "1", "0"]
