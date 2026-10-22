from __future__ import annotations

import inspect

from pcc.frontends.c.codegen.c_codegen import (
    CCodeGenerator,
    _decide_usual_integer_conversion,
)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator


def test_integer_conversion_decision_covers_rank_and_signedness_cases():
    same_signed = _decide_usual_integer_conversion(32, False, 64, False)
    assert (same_signed.target_order, same_signed.is_unsigned, same_signed.source) == (
        64,
        False,
        "rhs",
    )

    same_rank_mixed = _decide_usual_integer_conversion(64, False, 64, True)
    assert (
        same_rank_mixed.target_order,
        same_rank_mixed.is_unsigned,
        same_rank_mixed.source,
    ) == (64, True, "rhs")

    wider_signed = _decide_usual_integer_conversion(64, False, 32, True)
    assert (wider_signed.target_order, wider_signed.is_unsigned, wider_signed.source) == (
        64,
        False,
        "lhs",
    )


def test_integer_conversion_paths_share_one_decision_owner():
    users = (
        CCodeGenerator._usual_arithmetic_conversion_ir_type,
        CCodeGenerator._usual_arithmetic_conversion,
        CCodeGenerator._generic_usual_arithmetic_conversion_key,
        CCodeGenerator._eval_const_expr,
    )
    for user in users:
        assert "_decide_usual_integer_conversion(" in inspect.getsource(user)


def test_integer_conversion_runtime_sizeof_and_constexpr_parity():
    source = r"""
        enum {
            SAME_RANK = (-2L < 1UL),
            WIDER_SIGNED = (-2L < 1U),
            CONST_WRAP = ((1U - 2) > 0)
        };

        int main(void) {
            long s = -2L;
            unsigned long u = 1UL;
            unsigned int narrow = 1U;

            if ((s < u) != SAME_RANK) return 1;
            if ((s < narrow) != WIDER_SIGNED) return 2;
            if (((1U - 2) > 0) != CONST_WRAP) return 3;
            if (sizeof(s + u) != sizeof(unsigned long)) return 4;
            if (sizeof(s + narrow) != sizeof(long)) return 5;
            if (_Generic((s + u), unsigned long: 1, default: 0) != 1) return 6;
            if (_Generic((s + narrow), long: 1, default: 0) != 1) return 7;
            return 0;
        }
    """

    assert CEvaluator().evaluate(source, optimize=False) == 0
