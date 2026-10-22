from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).absolute().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"


def _cc() -> str:
    return os.environ.get("CC", "cc")


def _run_strategy_probe(
    tmp_path: Path, archive: Path, *, strategy: int, with_threads: int
) -> subprocess.CompletedProcess[str]:
    """Link a C probe of the refcount ABI against one runtime archive.

    The single-thread kernel is the non-atomic strategy (0); the pthread
    kernel of a ``PCC_WITH_THREADS=1`` archive is the atomic one (1).
    """
    src = tmp_path / f"strategy_{strategy}.c"
    exe = tmp_path / f"strategy_{strategy}.out"
    src.write_text(textwrap.dedent(f"""
        #include "py_internal.h"
        #include <stdint.h>
        #include <stdio.h>

        int main(void) {{
            int64_t slot = 1;
            if (pcc_refcount_strategy() != {strategy}) return 10;
            if (pcc_threads_enabled() != {with_threads}) return 11;
            if (pcc_refcount_incref(&slot) != 2) return 12;
            if (pcc_refcount_decref(&slot) != 1) return 13;
            if (pcc_refcount_decref(&slot) != 0) return 14;
            if (pcc_refcount_incref(NULL) != 0) return 15;
            if (pcc_stop_the_world() != 0) return 16;
            if (pcc_resume_world() != 0) return 17;
            printf("ok\\n");
            return 0;
        }}
        """).lstrip(), encoding="utf-8")
    build = subprocess.run(
        [
            _cc(),
            "-std=c11",
            "-pthread",
            f"-I{RUNTIME_DIR / 'include'}",
            f"-I{RUNTIME_DIR / 'src'}",
            str(src),
            str(archive),
            "-lm",
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stderr
    return subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)


def test_nonatomic_refcount_strategy_smoke(tmp_path, pcc_runtime_archive):
    result = _run_strategy_probe(
        tmp_path, pcc_runtime_archive, strategy=0, with_threads=1
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_atomic_refcount_strategy_smoke(tmp_path, threaded_pcc_runtime_archive):
    result = _run_strategy_probe(
        tmp_path, threaded_pcc_runtime_archive, strategy=1, with_threads=1
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
