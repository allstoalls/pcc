import pytest

from pcc.frontends.c.evaluator.c_evaluator import CEvaluator


def test_global_redefinition_errors():
    pcc = CEvaluator()

    with pytest.raises(ValueError, match="redefinition of global 'value'"):
        pcc.evaluate("int value = 1; int value = 2; int main(void) { return value; }")


def test_function_redefinition_errors():
    pcc = CEvaluator()

    with pytest.raises(ValueError, match="redefinition of function 'helper'"):
        pcc.evaluate(
            "int helper(void) { return 1; }"
            "int helper(void) { return 2; }"
            "int main(void) { return helper(); }"
        )


def test_tentative_definition_after_definition_keeps_initializer():
    pcc = CEvaluator()

    assert pcc.evaluate("int value = 1; int value; int main(void) { return value; }") == 1


def test_multiple_tentative_definitions_are_allowed():
    pcc = CEvaluator()

    assert pcc.evaluate("int value; int value; int main(void) { return value; }") == 0


def test_definition_inherits_prior_internal_linkage():
    pcc = CEvaluator()

    assert pcc.evaluate(
        "static int helper(void); int helper(void) { return 7; } int main(void) { return helper(); }"
    ) == 7
