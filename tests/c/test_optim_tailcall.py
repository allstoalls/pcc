from __future__ import annotations

from pcc.library.optim import OptimizationLog, is_tailcall_enabled, tailcall


def test_tailcall_decorator_marks_function_without_wrapping_identity():
    def f(x):
        return x

    marked = tailcall(f)
    assert marked is f
    assert is_tailcall_enabled(f)


def test_unmarked_function_is_not_tailcall_enabled():
    def f(x):
        return x

    assert not is_tailcall_enabled(f)


def test_optimization_log_text_is_stable():
    log = OptimizationLog()
    log.record_tailcall("fact", 3)
    assert log.events() == (("tailcall_eliminated", "fact", 3),)
    assert log.as_text() == "tailcall_eliminated=fact count=3"
