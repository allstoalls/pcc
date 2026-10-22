"""Effect-handler dispatch for pcc virtual threads.

The Handler.Dispatch model of algebraic effects:
a virtual thread can perform an effect operation; a registered handler
decides whether the computation continues (1) or short-circuits (0).
No host Python / Go code is involved — the handlers are pcc-Python or C
functions registered through the runtime ABI.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _compile_and_run(
    tmp_path: Path, archive: Path, source: str, name: str
) -> subprocess.CompletedProcess[str]:
    src = tmp_path / f"{name}.py"
    exe = tmp_path / name
    src.write_text(source, encoding="utf-8")
    build = subprocess.run(
        [
            str(REPO / ".venv" / "bin" / "pcc"),
            "--backend", "self",
            "--python-libpython", "off",
            "--ir-scaffold", "on",
            str(src), "-o", str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    return subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)


PROBE = r"""
import sys


def main() -> int:
    # register a handler for effect kind 7: continue with detail + 10
    set_handler = __import__('pcc.vthread_effect', fromlist=['set_handler'])

    # set handler via ABI
    import ctypes  # not available — use runtime ABI through compiled module
    return 0


main()
"""


@pytest.mark.integration
def test_vthread_effect_handler_abi_symbols_exist(
    pcc_runtime_archive: Path,
) -> None:
    """The kont-style effect handler ABI must be exported by the runtime."""
    import re
    src = (REPO / "pcc" / "runtime" / "py" / "py_virtual_thread_runtime.py").read_text(
        encoding="utf-8"
    )
    for symbol in (
        "py_vthread_effect_set_handler",
        "py_vthread_effect_clear_handler",
        "py_vthread_effect_perform",
        "py_vthread_effect_handled_count",
    ):
        assert f'@c_abi_export("{symbol}")' in src, symbol


@pytest.mark.integration
def test_vthread_effect_handler_dispatch_from_c_probe(
    tmp_path: Path, pcc_runtime_archive: Path
) -> None:
    """A C probe links the archive, registers a handler, performs an effect,
    and observes the handler's continue/short-circuit decision."""
    probe = tmp_path / "effect_probe.c"
    exe = tmp_path / "effect_probe"
    probe.write_text(
        r"""
#include <stdint.h>
typedef struct PyObject PyObject;
extern int64_t py_vthread_effect_set_handler(int64_t kind, void *fn, PyObject *ctx);
extern int64_t py_vthread_effect_clear_handler(int64_t kind);
extern int64_t py_vthread_effect_perform(int64_t kind, int64_t detail);
extern int64_t py_vthread_effect_handled_count(void);

static int64_t handler_continue(int64_t kind, int64_t detail, PyObject *ctx) {
    (void)ctx;
    return detail + 10;  /* non-zero -> continue */
}

static int64_t handler_shortcircuit(int64_t kind, int64_t detail, PyObject *ctx) {
    (void)ctx;
    return 0;  /* zero -> short-circuit */
}

int main(void) {
    if (py_vthread_effect_set_handler(7, (void *)handler_continue, 0) != 0) return 1;
    if (py_vthread_effect_perform(7, 5) != 1) return 2;      /* continue, detail=5 */
    if (py_vthread_effect_perform(7, 100) != 1) return 3;    /* continue */
    if (py_vthread_effect_set_handler(8, (void *)handler_shortcircuit, 0) != 0) return 4;
    if (py_vthread_effect_perform(8, 1) != 0) return 5;      /* short-circuit */
    if (py_vthread_effect_perform(9, 1) != -1) return 6;     /* unhandled -> -1 */
    if (py_vthread_effect_clear_handler(7) != 0) return 7;
    if (py_vthread_effect_perform(7, 1) != -1) return 8;     /* cleared -> unhandled */
    if (py_vthread_effect_handled_count() < 2) return 9;
    return 0;
}
""",
        encoding="utf-8",
    )
    build = subprocess.run(
        [
            "clang", "-std=c11",
            f"-I{REPO / 'pcc' / 'runtime' / 'include'}",
            str(probe), str(pcc_runtime_archive), "-pthread", "-o", str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stdout + run.stderr
