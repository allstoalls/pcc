from pcc.library.effects import handle, perform


def test_effect_handler_returns_value_to_the_call_site():
    with handle({"ask": lambda effect, k: effect.payload + 1}):
        assert perform("ask", 41) == 42


def test_continuation_linear():
    saved = {}
    with handle({"k": lambda effect, k: saved.setdefault("k", k).resume("ok")}):
        result = perform("k")
    assert result == "ok"
    try:
        saved["k"].resume("again")
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected RuntimeError")
