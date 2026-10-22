"""Socket ABI constants follow the C compilation target, not the host."""

import platform
from pathlib import Path
import subprocess
import sys

import pytest

from pcc.backend.owned_elf_link import link_inputs
from pcc.backend.owned_object_emit import emit_owned_object
from pcc.driver.project import TranslationUnit
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator


TARGETS = (
    ("x86_64-unknown-linux-gnu", 2048, 111),
    ("aarch64-unknown-linux-gnu", 2048, 111),
    ("arm64-apple-darwin", 4, 61),
)


def _socket_constants_object(target, nonblock, refused):
    source = """
#include <fcntl.h>
#include <errno.h>
_Static_assert(O_NONBLOCK == %d, "target O_NONBLOCK");
_Static_assert(ECONNREFUSED == %d, "target ECONNREFUSED");
int main(void) {
    volatile int mode = O_NONBLOCK;
    volatile int error = -ECONNREFUSED;
    return mode != %d || error != -%d;
}
""" % (nonblock, refused, nonblock, refused)
    evaluator = CEvaluator(backend="self", target_triple=target)
    units = evaluator.compile_translation_units(
        [TranslationUnit("socket_constants.c", "socket_constants.c", source)],
        use_system_cpp=False, use_compile_cache=False,
    )
    return emit_owned_object(units[0][1], target)


@pytest.mark.parametrize("target,nonblock,refused", TARGETS)
def test_owned_socket_header_constants_target_object(monkeypatch, target, nonblock, refused):
    def forbidden(*args, **kwargs):
        raise AssertionError("socket constants attempted an external compiler")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    assert _socket_constants_object(target, nonblock, refused)


# native x86_64 Linux boundary
@pytest.mark.pcc_gate(probe=lambda: not (sys.platform != "linux" or platform.machine() != "x86_64"))
def test_owned_socket_header_constants_execute_linux(tmp_path, monkeypatch):
    target, nonblock, refused = TARGETS[0]
    obj = tmp_path / "socket_constants.o"
    start = tmp_path / "start.s"
    binary = tmp_path / "socket_constants"
    start.write_text(
        ".intel_syntax noprefix\n.text\n.globl _start\n_start:\n"
        "  call main\n  mov edi, eax\n  mov eax, 60\n  syscall\n",
        encoding="utf-8",
    )
    def forbidden(*args, **kwargs):
        raise AssertionError("socket constants attempted an external compiler")
    with monkeypatch.context() as owned:
        owned.setattr(subprocess, "Popen", forbidden)
        obj.write_bytes(_socket_constants_object(target, nonblock, refused))
        link_inputs(target=target, output=str(binary), objects=[str(obj)],
                    assembly=[str(start)])
    run = subprocess.run([str(binary)], capture_output=True, timeout=10)
    assert run.returncode == 0, run
    assert run.stdout == run.stderr == b""
