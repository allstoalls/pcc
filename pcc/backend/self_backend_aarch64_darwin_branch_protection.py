from __future__ import annotations

"""AArch64-Darwin branch-protection (pac-ret + BTI) for the self backend.

Why this exists (S-track / SEC-P1-CFI)
--------------------------------------
The LLVM execution path hardens emitted AArch64 code with pointer
authentication (``pac-ret``) and branch-target-identification (``bti``) via the
``"branch-target-enforcement"`` / ``"sign-return-address"="non-leaf"`` /
``"sign-return-address-key"="a_key"`` function attributes, attached during IR
construction by ``CCodeGenerator``. The self
backend is a *first-class execution root* — it must not depend on LLVM for
security. Without the instructions emitted here, a ``--backend self`` binary
has zero return-address signing and zero forward-edge landing pads, leaving
ROP/JOP unmitigated on the self-backed root.

What is emitted
---------------
* Prologue: ``paciasp`` — sign the return address (LR / x30) using SP as the
  modifier, keyed with the A key (matching the LLVM path's ``a_key``). It is
  emitted *before* the frame save (``stp x29, x30, ...``) so the value pushed to
  the stack is the signed LR. ``paciasp`` is additionally a valid ``BTI c``
  landing pad, so it provides the forward-edge landing pad for indirect/direct
  calls at the same time (this is exactly what clang emits for a non-leaf
  function under ``-mbranch-protection=standard``).
* Epilogue: ``autiasp`` — authenticate LR using SP as the modifier, emitted
  *after* the frame restore (``ldp x29, x30, ...``) and immediately before
  ``ret``. If authentication fails (a corrupted return address) the CPU faults
  instead of returning to attacker-controlled code.

Correctness invariants relied upon
-----------------------------------
* The self-backend prologue *always* saves LR (``stp x29, x30, [sp, #-16]!``)
  and the epilogue *always* restores it symmetrically, so LR is live-saved for
  every function. Signing on entry and authenticating on exit is therefore
  correct for every emitted function; there is no path where LR is signed but
  not authenticated (or vice versa).
* SP at the ``paciasp`` point (function entry, before the frame is pushed) is
  identical to SP at the ``autiasp`` point (after the frame is fully popped),
  so the same PAC modifier is used to sign and to authenticate. This is the
  hard requirement for pac-ret correctness and it holds because the frame
  save/restore is exactly symmetric.

Scope of this slice
--------------------
This conservatively signs *every* function rather than only ``non-leaf`` ones.
That is a strict superset of the LLVM ``"non-leaf"`` policy: it never skips a
function that needs signing, and because the prologue already saves LR
unconditionally it never signs a function whose LR is not saved/restored. The
only cost is a couple of extra hint-space instructions in genuine leaf
functions. Refining to true leaf detection (skip ``paciasp`` when a function
provably makes no call and uses no aggregate ``bl memcpy`` helper) is a safe
follow-up optimization, not a correctness requirement — see the module tests.
"""

import os

from .self_backend_aarch64_darwin_mem import emitted_fixed_instruction_line

_ENV_TOGGLE = "PCC_SELF_BRANCH_PROTECTION"
_DISABLE_VALUES = {"0", "off", "false", "no", "none", "disable", "disabled"}


def branch_protection_enabled() -> bool:
    """Return whether self-backend AArch64 branch protection is active.

    ON by default for the aarch64-darwin self backend (this module is only
    imported on that target), matching the LLVM path's default. Opt out with
    ``PCC_SELF_BRANCH_PROTECTION=0`` (or ``off``/``false``/``no``/``none``).
    """
    raw = os.environ.get(_ENV_TOGGLE)
    if raw is None:
        return True
    return raw.strip().lower() not in _DISABLE_VALUES


def prologue_sign_return_address(func) -> list[str]:
    """Instructions to sign LR at function entry (also a BTI ``c`` landing pad).

    ``func`` is accepted for future leaf-detection refinement; the current
    conservative slice signs every function.
    """
    return [emitted_fixed_instruction_line("paciasp")]


def epilogue_authenticate_and_return(func) -> list[str]:
    """Authenticate LR then return.

    Emitted after the frame restore. Uses the separate ``autiasp`` + ``ret``
    form (rather than the fused ``retaa``) so it stays valid on assemblers that
    do not accept the combined return-and-authenticate mnemonic; the security
    guarantee is identical.
    """
    return [
        emitted_fixed_instruction_line("autiasp"),
        emitted_fixed_instruction_line("ret"),
    ]


__all__ = [
    "branch_protection_enabled",
    "epilogue_authenticate_and_return",
    "prologue_sign_return_address",
]
