import pytest

from pcc.library.effects import Continuation, UnhandledEffect, handle, perform


def test_perform_dispatches_to_nearest_handler():
    seen = []
    def h(req, k):
        seen.append((req.name, req.payload))
        return k.resume(req.payload + 1)
    with handle({"ask": h}):
        assert perform("ask", 41) == 42
    assert seen == [("ask", 41)]


def test_continuation_is_linear():
    k = Continuation(lambda value=None: value)
    assert k.resume(1) == 1
    with pytest.raises(RuntimeError):
        k.resume(2)


def test_unhandled_effect_raises():
    with pytest.raises(UnhandledEffect):
        perform("missing")
