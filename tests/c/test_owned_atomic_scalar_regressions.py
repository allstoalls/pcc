"""Pointer exchange and unsigned lvalues retain their C type in owned output."""

import platform
import subprocess
import sys

import pytest

from pcc.backend import BackendUnavailable
from pcc.backend.owned_elf_link import link_inputs
from pcc.backend.owned_object_emit import emit_owned_object
from pcc.driver.project import TranslationUnit
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator


TARGETS = (
    "x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu",
    "arm64-apple-darwin", "x86_64-pc-windows-msvc",
)

POINTER_SOURCE = r"""
static int first = 3;
static int second = 9;
struct box { int before; int *value; int after; };
static struct box global_box = {17, &first, 29};
int exercise(struct box *box) {
    int *old = __atomic_exchange_n(&box->value, &second, __ATOMIC_ACQ_REL);
    if (old != &first || box->value != &second) return 1;
    if (*old + *box->value != 12) return 2;
    old = __atomic_exchange_n(&box->value, (int *)0, __ATOMIC_SEQ_CST);
    if (old != &second || box->value != (int *)0) return 3;
    if (box->before != 17 || box->after != 29) return 4;
    return 0;
}
int main(void) {
    struct box local_box = {17, &first, 29};
    int result = exercise(&local_box);
    if (result) return result;
    return exercise(&global_box);
}
"""


def _object(source, target):
    evaluator = CEvaluator(target_triple=target, backend="self")
    ir = evaluator.compile_translation_units(
        [TranslationUnit("atomic_scalar.c", "atomic_scalar.c", source)],
        use_system_cpp=False, frontend_opt_level=0,
    )[0][1]
    return emit_owned_object(ir, target)


def _forbid_process(*args, **kwargs):
    raise AssertionError("owned build attempted an external process")


@pytest.mark.parametrize("target", TARGETS)
def test_pointer_exchange_emits_owned_object(monkeypatch, target):
    monkeypatch.setattr(subprocess, "Popen", _forbid_process)
    assert _object(POINTER_SOURCE, target)


@pytest.mark.parametrize("target", TARGETS)
def test_pointer_arithmetic_atomicrmw_remains_rejected(monkeypatch, target):
    monkeypatch.setattr(subprocess, "Popen", _forbid_process)
    ir = "define ptr @bad(ptr %p, ptr %v) {\nentry:\n %r = atomicrmw add ptr %p, ptr %v seq_cst\n ret ptr %r\n}\n"
    with pytest.raises(BackendUnavailable, match="atomicrmw"):
        emit_owned_object(ir, target)


def _run_owned(source, tmp_path, monkeypatch):
    obj = tmp_path / "control.o"
    start = tmp_path / "start.s"
    binary = tmp_path / "control"
    start.write_text(".intel_syntax noprefix\n.text\n.globl _start\n_start:\n  call main\n  mov edi, eax\n  mov eax, 60\n  syscall\n")
    with monkeypatch.context() as guard:
        guard.setattr(subprocess, "Popen", _forbid_process)
        obj.write_bytes(_object(source, TARGETS[0]))
        link_inputs(target=TARGETS[0], output=str(binary), objects=[str(obj)], assembly=[str(start)])
    return subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)


# native x86_64 Linux boundary
@pytest.mark.pcc_gate(probe=lambda: not (sys.platform != "linux" or platform.machine() != "x86_64"))
def test_pointer_exchange_preserves_old_pointer_and_neighbors(tmp_path, monkeypatch):
    result = _run_owned(POINTER_SOURCE, tmp_path, monkeypatch)
    assert result.returncode == 0, result
    assert result.stdout == result.stderr == ""


# native x86_64 Linux boundary
@pytest.mark.pcc_gate(probe=lambda: not (sys.platform != "linux" or platform.machine() != "x86_64"))
@pytest.mark.parametrize("ctype, width, unsigned", [
    ("signed char", 8, False), ("unsigned char", 8, True),
    ("short", 16, False), ("unsigned short", 16, True),
], ids=["i8", "u8", "i16", "u16"])
def test_narrow_atomic_original_assertions_with_owned_entry(
    tmp_path, monkeypatch, ctype, width, unsigned,
):
    # Reuse every original semantic assertion with a minimal owned Linux
    # process entry, so this gate needs no runtime archive or provisioning.
    from tests.c import test_owned_narrow_atomics as original

    monkeypatch.setattr(original, "compile_and_run_owned_c", lambda source:
                        _run_owned(source, tmp_path, monkeypatch))
    original.test_owned_narrow_atomic_family_preserves_values_and_neighbors(
        ctype, width, unsigned,
    )
