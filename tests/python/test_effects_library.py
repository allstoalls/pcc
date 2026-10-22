from __future__ import annotations

import pytest

from pcc.library.effects import Continuation, UnhandledEffect, handle, installed_handlers, perform


def test_perform_dispatches_to_nearest_handler():
    seen = []

    def outer(effect, k):
        seen.append(("outer", effect.payload))
        return k.resume("outer")

    def inner(effect, k):
        seen.append(("inner", effect.payload))
        return k.resume("inner")

    with handle({"ask": outer}):
        assert perform("ask", 1) == "outer"
        with handle({"ask": inner}):
            assert perform("ask", 2) == "inner"
    assert seen == [("outer", 1), ("inner", 2)]


def test_unhandled_effect_is_explicit():
    with pytest.raises(UnhandledEffect):
        perform("missing")


def test_continuation_is_linear():
    k = Continuation(lambda value=None: value)
    assert k.resume(3) == 3
    with pytest.raises(RuntimeError, match="already resumed"):
        k.resume(4)


def test_installed_handlers_reports_current_dynamic_scope():
    assert installed_handlers() == ()
    with handle({"a": lambda e, k: None, "b": lambda e, k: None}):
        assert installed_handlers() == ("a", "b")


def test_unmatched_inner_scope_uses_outer_handler_and_unwinds_on_error():
    with handle({"ask": lambda effect, k: effect.payload + 1}):
        with pytest.raises(ValueError, match="stop"):
            with handle({"other": lambda effect, k: None}):
                assert perform("ask", 41) == 42
                raise ValueError("stop")
        assert installed_handlers() == ("ask",)
    assert installed_handlers() == ()


def test_handler_scopes_are_isolated_between_threads():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    ready = Barrier(2)

    def run(value):
        with handle({"ask": lambda effect, k: value}):
            ready.wait(timeout=5)
            return perform("ask")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run, 17)
        second = pool.submit(run, 29)
        assert first.result(timeout=10) == 17
        assert second.result(timeout=10) == 29
    assert installed_handlers() == ()
