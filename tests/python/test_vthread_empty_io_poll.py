"""Ready-only scheduler steps must not call the OS readiness backend."""

from pathlib import Path
import subprocess
import sys

import pytest


# interposes Darwin kevent
@pytest.mark.pcc_gate(probe=lambda: not (sys.platform != "darwin"))
def test_empty_nonblocking_io_poll_avoids_kevent(tmp_path: Path, pcc_runtime_archive):
    archive = pcc_runtime_archive
    root = Path(__file__).resolve().parents[2]
    source = tmp_path / "empty_io.c"
    source.write_text('''#include "py_runtime.h"
#include <sys/types.h>
#include <sys/event.h>
#include <stdio.h>
static int calls = 0;
int kevent(int queue, const struct kevent *changes, int nchanges,
           struct kevent *events, int nevents, const struct timespec *timeout) {
    calls++;
    return 0;
}
int main(void) {
    for (int index = 0; index < 100; index++) {
        if (py_virtual_thread_poll_io(0) != 0) return 1;
    }
    printf("%d\\n", calls);
    return 0;
}
''')
    executable = tmp_path / "empty_io"
    built = subprocess.run([
        "clang", "-std=c11", "-I" + str(root / "pcc/runtime/include"),
        str(source), str(archive), "-pthread", "-o", str(executable),
    ], capture_output=True, text=True, timeout=30)
    assert built.returncode == 0, built.stdout + built.stderr
    ran = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert ran.stdout.strip() == "0"
