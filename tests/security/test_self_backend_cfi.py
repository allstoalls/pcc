"""AArch64 branch-protection (pac-ret + BTI) in the pcc *self* backend.

Context (SEC-P1-CFI / S-track)
------------------------------
The LLVM execution path hardens emitted AArch64 code with pointer
authentication and branch-target identification via IR function attributes
(``pcc/frontends/c/codegen/c_codegen.py::_AARCH64_BRANCH_PROTECTION_ATTRS``), covered by
``tests/security/test_c_stack_protection.py::
test_control_flow_protection_pac_or_bti_emitted``.

The ``self`` backend is a first-class execution root and must not depend on
LLVM for control-flow-integrity hardening. Before SEC-P1-CFI a
``--backend self`` binary emitted *zero* return-address signing and *zero*
forward-edge landing pads (``paciasp`` count 0), leaving ROP/JOP unmitigated on
the self-backed root. These tests prove the self backend now emits, in its own
prologue/epilogue lowering:

* ``paciasp`` at function entry (sign LR with SP as modifier; keyed with the A
  key, matching the LLVM path's ``a_key``). ``paciasp`` is also a valid ``BTI
  c`` landing pad, so it provides the forward-edge target for ``bl``/``blr``
  callers.
* ``autiasp`` before ``ret`` in the epilogue (authenticate the signed LR).

The tests inspect the *emitted assembly* (and, when tools are available, the
*disassembled machine code*), because that is the layer an attacker faces.
They also assemble and run the hardened binary to prove the signing/auth pair
does not corrupt the frame or return path.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys

import pytest

_THIS_DIR = os.path.dirname(__file__)
_REPO_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Branch protection is emitted only by the aarch64-darwin self-backend emitter,
# and the assemble/run steps need an aarch64 macOS toolchain.
_IS_AARCH64_DARWIN = (
    sys.platform == "darwin" and platform.machine() in ("arm64", "aarch64")
)

pytestmark = pytest.mark.pcc_gate(
    unavailable=None
    if _IS_AARCH64_DARWIN
    else "self-backend AArch64 branch protection only applies on arm64 macOS"
)

# An indirect call through a function pointer exercises both edges: the callee
# needs a forward-edge landing pad, and every function needs its return address
# signed against a corrupted-return-address (ROP) attack.
_INDIRECT_CALL_C = """
int cb(int x) { return x + 1; }
int dispatch(int (*f)(int), int x) { return f(x); }
int main(void) { return dispatch(cb, 41) == 42 ? 0 : 1; }
"""


def _emit_self_asm(source: str, tmp_path) -> str:
    """Compile ``source`` through the pcc self backend and return the asm text."""
    from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
    from pcc.driver.project import TranslationUnit

    unit = TranslationUnit(
        name="main.c",
        path=str(tmp_path / "main.c"),
        source=source,
    )
    ev = CEvaluator(backend="self", allow_unimplemented_backend=True)
    compiled_units = ev.compile_translation_units(
        [unit],
        use_system_cpp=False,
        frontend_opt_level=0,
    )
    asm_path = tmp_path / "main.s"
    ev.emit_compiled_units(compiled_units, emit_asm=str(asm_path), optimize=0)
    return asm_path.read_text(encoding="utf-8")


def test_self_backend_prologue_signs_return_address(tmp_path):
    asm = _emit_self_asm(_INDIRECT_CALL_C, tmp_path)
    # Reverse-edge: LR is signed on entry (paciasp) and authenticated before the
    # return (autiasp). paciasp doubles as the BTI `c` forward-edge landing pad.
    assert "paciasp" in asm, (
        "self-backend prologue emitted no return-address signing (paciasp); "
        "ROP/JOP unmitigated on the self-backed execution root"
    )
    assert "autiasp" in asm, (
        "self-backend epilogue emitted no return-address authentication "
        "(autiasp); a signed LR would never be checked before ret"
    )
    # Signing and authentication must be paired: an odd count means some return
    # path signs-without-auth (or vice versa), which corrupts the return.
    assert asm.count("paciasp") == asm.count("autiasp"), (
        "paciasp/autiasp counts differ: some function signs but never "
        "authenticates its return address (or the reverse)"
    )


def test_self_backend_branch_protection_can_be_disabled(tmp_path, monkeypatch):
    """The hardening is a self-backend option (ON by default, opt-out env)."""
    monkeypatch.setenv("PCC_SELF_BRANCH_PROTECTION", "0")
    asm = _emit_self_asm(_INDIRECT_CALL_C, tmp_path)
    assert "paciasp" not in asm
    assert "autiasp" not in asm
    # Sanity: without protection the plain frame return path is still emitted.
    assert "ret" in asm


def test_self_backend_hardened_binary_assembles_and_runs(tmp_path):
    """The signed/authenticated return path must not corrupt execution.

    Assemble the self-backend asm with the system toolchain, disassemble the
    real binary, assert PAC/BTI instructions survive into machine code, then
    run it: ``main`` returns 0 iff the indirect call and the authenticated
    returns all behaved correctly.
    """
    cc = shutil.which("cc")
    if cc is None:
        pytest.fail("no system cc available to assemble the self-backend asm")

    asm = _emit_self_asm(_INDIRECT_CALL_C, tmp_path)
    asm_path = tmp_path / "cfi.s"
    asm_path.write_text(asm, encoding="utf-8")
    exe_path = tmp_path / "cfi.out"
    subprocess.run(
        [cc, str(asm_path), "-o", str(exe_path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )

    otool = shutil.which("otool")
    objdump = shutil.which("objdump")
    disasm_text = ""
    if otool is not None:
        disasm_text = subprocess.run(
            [otool, "-tv", str(exe_path)],
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout
    elif objdump is not None:
        disasm_text = subprocess.run(
            [objdump, "-d", str(exe_path)],
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout

    if disasm_text:
        # Assemblers may keep paciasp/autiasp or fold to the pacia/retaa family
        # or hint encodings; accept any of the CFI-signalling forms.
        assert (
            ("paciasp" in disasm_text)
            or ("pacia" in disasm_text)
            or ("hint #25" in disasm_text)  # PACIASP hint encoding
        ), "no PAC signing instruction survived into the self-backend binary"
        assert (
            ("autiasp" in disasm_text)
            or ("retaa" in disasm_text)
            or ("autia" in disasm_text)
            or ("hint #29" in disasm_text)  # AUTIASP hint encoding
        ), "no PAC authentication instruction survived into the self-backend binary"

    run = subprocess.run(
        [str(exe_path)], capture_output=True, text=True, timeout=60
    )
    assert run.returncode == 0, (
        "hardened self-backend binary returned "
        f"{run.returncode}; the paciasp/autiasp pair corrupted the return path "
        "or the indirect call"
    )
