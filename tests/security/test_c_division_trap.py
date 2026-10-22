"""Division / signed-overflow undefined behavior in the C frontend.

Source: *Low-Level Software Security for Compiler Developers* — division by
zero and ``INT_MIN / -1`` are undefined behavior in C. The book's relevant
mitigation is *UBSan-style instrumentation* (``-fsanitize=undefined``), which
inserts an explicit trap; pcc currently emits no such guard, so the observable
behavior is whatever the target hardware does. These tests pin that behavior
on AArch64 (where ``sdiv`` does not trap) so a regression that started
*miscomputing* — rather than just not trapping — would be caught, and they
document the portability hazard for the SEC-P1-UBSAN task.

NOTE: on x86_64 the same expressions raise SIGFPE; the tests are AArch64-gated
so they neither crash the in-process JIT worker nor make a false portability
claim.
"""
from __future__ import annotations

import os
import platform
import sys

import pytest

this_dir = os.path.dirname(__file__)
repo_root = os.path.dirname(os.path.dirname(this_dir))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from pcc.frontends.c.evaluator.c_evaluator import CEvaluator

_IS_AARCH64 = platform.machine().lower() in ("arm64", "aarch64")
_aarch64_only = pytest.mark.pcc_gate(
    unavailable=None
    if _IS_AARCH64
    else "signed-div UB traps (SIGFPE) on x86_64; behavior pinned only on AArch64"
)


def _ev(source: str) -> int:
    return CEvaluator().evaluate(source, optimize=False)


@_aarch64_only
def test_intmin_divide_minus_one_does_not_trap_on_aarch64():
    # INT_MIN / -1 overflows the signed result; AArch64 sdiv yields INT_MIN.
    # pcc inserts no overflow guard, so this is the bare-metal result.
    assert _ev(
        r"""
        int main(void){
            volatile int a = -2147483647 - 1;  /* INT_MIN */
            volatile int b = -1;
            int c = a / b;
            return c == (-2147483647 - 1) ? 0 : 1;
        }
        """
    ) == 0


@_aarch64_only
def test_division_by_zero_does_not_trap_on_aarch64():
    # AArch64 integer divide-by-zero returns 0 (no #DE trap). pcc emits no
    # check, so 1 / 0 evaluates to 0 here. Documents the absence of a
    # divide-by-zero guard (SEC-P1-UBSAN).
    assert _ev(
        r"""
        int main(void){
            volatile int a = 1;
            volatile int b = 0;
            int c = a / b;
            return c == 0 ? 0 : 1;
        }
        """
    ) == 0
