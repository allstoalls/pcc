"""G-P3-LONGRUN slice 2: host-C oracle for process-RSS sampling.

The long-run workloads poll `pcc_os_current_rss_bytes` /
`pcc_os_peak_rss_bytes` for their CSV time series. Assertions stay
loose (the OS owns the numbers): both calls succeed, report > 1 MB for
a live process, peak >= a freshly-sampled current, and current grows
(or at least does not report failure) after a deliberate multi-MB
allocation burst. Production pcc-Python ownership and both target lowering
shapes are gated separately in ``test_freestanding_platform_rss.py``.
"""
from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

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
#include <stdlib.h>
#include <string.h>

int main(void) {
    int64_t cur0 = pcc_os_current_rss_bytes();
    int64_t peak0 = pcc_os_peak_rss_bytes();
    printf("%d\\n", cur0 > 1024 * 1024);
    printf("%d\\n", peak0 >= cur0 && peak0 > 0);

    /* deliberate burst: 32 MB touched so it is resident */
    size_t n = 32u * 1024u * 1024u;
    char *block = (char *)malloc(n);
    if (block == NULL) return 5;
    memset(block, 0xAB, n);

    int64_t cur1 = pcc_os_current_rss_bytes();
    printf("%d\\n", cur1 > 0);
    printf("%d\\n", cur1 >= cur0);
    /* keep block alive so the optimizer cannot drop the memset */
    printf("%d\\n", block[12345] == (char)0xAB);
    free(block);
    return 0;
}
"""


def test_rss_helper_reports_sane_values(tmp_path):
    work_runtime = _build_runtime(tmp_path)
    src = tmp_path / "rss_probe.c"
    exe = tmp_path / "rss_probe.out"
    src.write_text(textwrap.dedent(_PROBE).lstrip(), encoding="utf-8")
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
    assert result.stdout.splitlines() == ["1", "1", "1", "1", "1"]
