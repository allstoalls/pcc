from __future__ import annotations

import os
from pathlib import Path
import subprocess


REPO = Path(__file__).resolve().parents[2]
RUNTIME = REPO / "pcc" / "runtime"


_SOURCE = r"""
#include "py_runtime.h"
#include <stdint.h>
#include <stdio.h>
#include <string.h>

enum { COUNT = 5000, LIMIT = 4096 };
typedef union {
    uint64_t align[2];
    unsigned char bytes[16];
} ObjectStorage;
static ObjectStorage objects[COUNT];

int main(void) {
    memset(objects, 0, sizeof(objects));
    py_gc_init();
    for (int i = 0; i < COUNT; i++) py_gc_track((PyObject *)objects[i].bytes);
    for (int i = 0; i < COUNT; i++) py_gc_untrack((PyObject *)objects[i].bytes);
    int64_t cached = pcc_gc_tracked_node_pool_cached_count();
    if (cached != LIMIT) return 1;
    pcc_gc_tracked_node_pool_drain();
    if (pcc_gc_tracked_node_pool_cached_count() != 0) return 2;
    py_gc_track((PyObject *)objects[0].bytes);
    py_gc_untrack((PyObject *)objects[0].bytes);
    int64_t reused = pcc_gc_tracked_node_pool_cached_count();
    if (reused != 1) return 3;
    printf("pool:%lld,drain:0,reuse:%lld\n",
           (long long)cached, (long long)reused);
    pcc_gc_tracked_node_pool_drain();
    return 0;
}
"""


def _build(tmp_path: Path, name: str, archive: Path) -> Path:
    source = tmp_path / (name + ".c")
    executable = tmp_path / name
    source.write_text(_SOURCE, encoding="utf-8")
    result = subprocess.run(
        [
            "clang",
            "-std=c11",
            "-I" + str(RUNTIME / "include"),
            str(source),
            str(archive),
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return executable


def test_tracking_node_pool_is_bounded_and_reusable(
    tmp_path: Path,
    pcc_runtime_archive: Path,
):
    port = _build(tmp_path, "tracked_pool_port", pcc_runtime_archive)
    expected = "pool:4096,drain:0,reuse:1\n"
    for backend in range(5):
        env = {**os.environ, "PCC_GC_BACKEND": str(backend)}
        port_result = subprocess.run(
            [str(port)], capture_output=True, text=True, timeout=30, env=env
        )
        assert port_result.returncode == 0, port_result.stderr
        assert port_result.stdout == expected
