"""Bit-precise contract shared by pcc's integer constant folders.

The helpers return ``("constant", raw_bits)``, ``("poison", 0)``, or
``("unsupported", 0)``.  ``raw_bits`` is always the unsigned bit pattern for
the requested width.  Consumers choose their preferred textual spelling.

This is intentionally not a general optimizer.  It is the finite semantic
kernel used by SCCP, instsimplify, instcombine, reassociate and loop-unroll
when both operands are integer constants.
"""

from __future__ import annotations

# Shared owned kernels; LLVM remains only in the legacy verification adapter.
from pcc.native_ir.integer_fold_contract import (
    FOLD_CONSTANT,
    FOLD_POISON,
    FOLD_UNSUPPORTED,
    LLVM_INTEGER_BINARY_OPS,
    LLVM_INTEGER_COMPARE_PREDS,
    unsigned_value,
    signed_value,
    fold_llvm_integer_binary,
    fold_llvm_integer_compare,
    _normalized_flags,
    _flags_valid_for_op,
)
