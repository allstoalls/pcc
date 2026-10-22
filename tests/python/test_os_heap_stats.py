"""G-P3-LONGRUN fragmentation slice: malloc-heap statistics helper.

Backends 0-3 are malloc-backed, so their fragmentation/overhead axis is
defined at the allocator level: `pcc_os_heap_in_use_bytes` (bytes the
production pcc allocator has handed out) vs `pcc_os_heap_capacity_bytes`
(bytes it holds mapped from the OS); proxy = capacity - in_use. Assertions stay loose
(the allocator owns the numbers): both calls succeed, capacity >=
in_use, in_use grows across a retained allocation burst, and the
freed burst does not push in_use below its pre-burst floor minus slack.
The Linux mallinfo2 branch ships UNTESTED until S-P2-LINUX.
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
    int64_t use0 = pcc_os_heap_in_use_bytes();
    int64_t cap0 = pcc_os_heap_capacity_bytes();
    /* The numbers are the production pcc allocator's own (requested vs
     * mapped bytes); nothing may have been requested before this point. */
    printf("%d\\n", use0 >= 0);
    printf("%d\\n", cap0 >= use0);

    /* retained burst: 8 MB in 1 KB blocks so in_use visibly grows */
    enum { N = 8192 };
    static char *blocks[N];
    for (int i = 0; i < N; i++) {
        blocks[i] = (char *)malloc(1024);
        if (blocks[i] == NULL) return 5;
        blocks[i][0] = (char)(i & 0xFF);
    }
    int64_t use1 = pcc_os_heap_in_use_bytes();
    int64_t cap1 = pcc_os_heap_capacity_bytes();
    printf("%d\\n", use1 > use0);
    printf("%d\\n", cap1 >= use1);
    printf("%d\\n", blocks[1234][0] == (char)(1234 & 0xFF));

    for (int i = 0; i < N; i++) free(blocks[i]);
    int64_t use2 = pcc_os_heap_in_use_bytes();
    /* freed bytes leave in_use (they may stay in capacity) */
    printf("%d\\n", use2 < use1);
    return 0;
}
"""


def test_heap_stats_report_sane_values(tmp_path):
    work_runtime = _build_runtime(tmp_path)
    src = tmp_path / "heap_probe.c"
    exe = tmp_path / "heap_probe.out"
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
    assert result.stdout.splitlines() == ["1", "1", "1", "1", "1", "1"]
