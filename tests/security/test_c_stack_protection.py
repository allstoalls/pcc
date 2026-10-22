"""Code-hardening features in the machine code pcc emits (C frontend).

Source: *Low-Level Software Security for Compiler Developers* —
  * "Stack buffer overflows" -> stack canaries (``-fstack-protector``).
  * "Code reuse attacks" / "Control-flow Integrity" -> pointer authentication
    (``pac-ret``) and branch target identification (``bti``) on AArch64.
  * Sensitive-data handling -> a ``memset`` that clears a secret before the
    buffer dies must not be removed by dead-store elimination (CWE-14).

These inspect the *emitted assembly* (``llvmdump`` writes ``temp.bcode``),
because that is the layer an attacker actually faces. A test PASSES when the
hardening is present; the gaps are marked ``xfail(strict=True)`` so that if pcc
later closes them the test flips to XPASS and forces the marker to be removed.
"""
from __future__ import annotations

import os
import sys

import pytest

this_dir = os.path.dirname(__file__)
repo_root = os.path.dirname(os.path.dirname(this_dir))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from pcc.frontends.c.evaluator.c_evaluator import CEvaluator


def _emit_asm(source: str, optimize, monkeypatch, tmp_path) -> str:
    """Compile ``source`` and return the target assembly text."""
    monkeypatch.chdir(tmp_path)
    CEvaluator().evaluate(source, optimize=optimize, llvmdump=tmp_path)
    return (tmp_path / "temp.bcode").read_text(encoding="utf-8")


def _emit_optimized_ir(source: str, monkeypatch, tmp_path) -> str:
    """Compile ``source`` at -O2 and return the post-optimization LLVM IR text.

    ``temp.ooptimize.bcode`` is written by CEvaluator when llvmdump is on and
    optimization ran, so we can assert on IR that survived the optimizer."""
    monkeypatch.chdir(tmp_path)
    CEvaluator().evaluate(source, optimize=True, llvmdump=tmp_path)
    return (tmp_path / "temp.ooptimize.bcode").read_text(encoding="utf-8")


def _function_region(asm: str, name: str) -> str:
    lines = asm.splitlines()
    out = []
    capturing = False
    for line in lines:
        if (f"{name}:" in line) or line.strip() == f"_{name}:":
            capturing = True
        if capturing:
            out.append(line)
            if line.strip() in ("ret", "retab", "retaa"):
                break
    return "\n".join(out)


_BUFFER_FN = r"""
#include <string.h>
int victim(const char *s){ char buf[16]; strcpy(buf, s); return (int)buf[0]; }
int main(void){ return victim("hi"); }
"""


def test_stack_canary_emitted_for_buffer_function(monkeypatch, tmp_path):
    # A function with a fixed stack buffer + unbounded copy must get a stack
    # canary. pcc marks the function `sspstrong` and the LLVM backend lowers a
    # real guard load/compare/__stack_chk_fail branch into the machine code.
    asm = _emit_asm(_BUFFER_FN, False, monkeypatch, tmp_path)
    assert "stack_chk" in asm, (
        "no stack-protector guard found in emitted machine code"
    )


def test_control_flow_protection_pac_or_bti_emitted(monkeypatch, tmp_path):
    source = r"""
    int cb(int x){ return x + 1; }
    int dispatch(int (*f)(int), int x){ return f(x); }
    int main(void){ return dispatch(cb, 41) == 42 ? 0 : 1; }
    """
    asm = _emit_asm(source, False, monkeypatch, tmp_path)
    assert (
        ("paciasp" in asm)
        or ("pacibsp" in asm)
        or ("\tbti" in asm)
        or ("bti\t" in asm)
        or ("hint\t#25" in asm)  # PACIASP on older LLVM assemblers.
        or ("hint\t#34" in asm)  # BTI c on older LLVM assemblers.
    ), "no PAC/BTI control-flow-integrity instructions in emitted machine code"


def test_secret_clearing_memset_survives_optimization(monkeypatch, tmp_path):
    source = r"""
    #include <string.h>
    int use_secret(int n){
        char key[64];
        for (int i = 0; i < 64; i++) key[i] = (char)(n + i * 7);
        int r = 0;
        for (int i = 0; i < 64; i++) r += key[i];
        memset(key, 0, sizeof key);   /* clear secret before it dies */
        return r;
    }
    int main(void){ return use_secret(3) & 0; }
    """
    asm = _emit_asm(source, True, monkeypatch, tmp_path)  # -O2
    body = _function_region(asm, "use_secret")
    cleared = (
        ("memset" in body)
        or ("bzero" in body)
        or ("xzr" in body)   # zero-register stores
        or ("movi" in body)  # vector zero
        or ("wzr" in body)
    )
    assert cleared, (
        "secret-clearing memset was eliminated; no zeroing survives in the "
        "optimized machine code"
    )


_EXPLICIT_BZERO_FN = r"""
#include <string.h>
int use_key(int n){
    char key[64];
    for (int i = 0; i < 64; i++) key[i] = (char)(n + i * 7);
    int r = 0;
    for (int i = 0; i < 64; i++) r += key[i];
    explicit_bzero(key, sizeof key);   /* scrub secret before it dies */
    return r;
}
int main(void){ return use_key(3) & 0; }
"""


def test_secret_clearing_explicit_bzero_survives_optimization(monkeypatch, tmp_path):
    # explicit_bzero() must not be dropped by dead-store elimination even
    # though its result is never observed: pcc lowers it to a volatile
    # llvm.memset (a store the optimizer may not remove). Assert the zeroing
    # survives in the -O2 machine code (same inspection as the memset test).
    asm = _emit_asm(_EXPLICIT_BZERO_FN, True, monkeypatch, tmp_path)
    body = _function_region(asm, "use_key")
    cleared = (
        ("memset" in body)
        or ("bzero" in body)
        or ("xzr" in body)   # zero-register stores
        or ("movi" in body)  # vector zero
        or ("wzr" in body)
        or ("stp\tq" in body)  # paired vector zero stores
    )
    assert cleared, (
        "explicit_bzero secret scrub was eliminated; no zeroing survives in "
        "the optimized machine code"
    )


def test_secret_clearing_uses_volatile_memset_not_inline_asm(monkeypatch, tmp_path):
    # The secret-clearing lowering relies on the *volatile* llvm.memset (a store
    # the optimizer may not remove), NOT an inline-asm memory barrier. An empty
    # `asm sideeffect ""` barrier is redundant given the volatile store AND the
    # LLVM-free `self` backend cannot parse an inline-asm call, so emitting one
    # breaks real programs that memset internally (regressed lz4 on `--backend
    # self`). This guards against reintroducing the barrier.
    ir_text = _emit_optimized_ir(_EXPLICIT_BZERO_FN, monkeypatch, tmp_path)
    assert "asm sideeffect" not in ir_text, (
        "secret-clearing lowering emitted an inline-asm barrier; it is "
        "redundant with the volatile memset and breaks the self backend's IR "
        "parser (lz4 regression) — the volatile store alone is DSE-immune"
    )


def test_secret_clearing_memset_s_survives_optimization(monkeypatch, tmp_path):
    # memset_s(ptr, smax, ch, n) is the C11 Annex K bounded secure clear; it
    # must survive optimization the same way. It returns errno_t (0 == success).
    source = r"""
    #include <string.h>
    int use_secret(int n){
        char key[64];
        for (int i = 0; i < 64; i++) key[i] = (char)(n + i * 7);
        int r = 0;
        for (int i = 0; i < 64; i++) r += key[i];
        memset_s(key, sizeof key, 0, sizeof key);
        return r;
    }
    int main(void){ return use_secret(3) & 0; }
    """
    asm = _emit_asm(source, True, monkeypatch, tmp_path)
    body = _function_region(asm, "use_secret")
    cleared = (
        ("memset" in body)
        or ("bzero" in body)
        or ("xzr" in body)
        or ("movi" in body)
        or ("wzr" in body)
        or ("stp\tq" in body)
    )
    assert cleared, (
        "memset_s secret scrub was eliminated; no zeroing survives in the "
        "optimized machine code"
    )


def test_secret_clearing_memset_s_is_bounded_by_smax(monkeypatch, tmp_path):
    # memset_s is a bounded clear. The compiler may lower it inline, but it
    # must not turn n > smax into a volatile write of n bytes.
    source = r"""
    #include <string.h>
    int scrub(char *p, unsigned long smax, unsigned long n){
        return memset_s(p, smax, 0, n);
    }
    int main(void){ char key[8]; return scrub(key, 4, 8) == 0; }
    """
    ir_text = _emit_optimized_ir(source, monkeypatch, tmp_path)
    assert "icmp u" in ir_text, ir_text
    assert "llvm.umin.i64" in ir_text or "memset_s.size" in ir_text, ir_text
    assert " i32 22" in ir_text or " i64 22" in ir_text, ir_text
    assert "llvm.memset.p0.i64" in ir_text and "i1 true" in ir_text, ir_text
