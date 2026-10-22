"""Execute float's type-level conversion protocol with a source-matched runtime."""
from __future__ import annotations

import pytest

from tests.python.owned_regression_support import (
    assert_owned_program,
    assert_reference_program,
    explicit_owned_runtime,
)
from tests.python.test_tuple_protocol_consumers import ConsumerModel


PROGRAM = '''import gc
events = []
marker = ValueError("selected float failure")
class Number:
    def __float__(self):
        gc.collect()
        events.append("float")
        return 3.5
class Inherited(Number):
    pass
class Both(Number):
    def __index__(self):
        raise AssertionError("must not use index")
class IndexOnly:
    def __index__(self):
        gc.collect()
        return 1180591620717411303424
class BadFloat:
    def __float__(self):
        return 7
    def __index__(self):
        raise AssertionError("invalid float must not fall back")
class BoolFloat:
    def __float__(self):
        return True
class TextFloat:
    def __float__(self):
        return "7.5"
class DisabledFloat:
    __float__ = None
    def __index__(self):
        raise AssertionError("disabled float must not fall back")
class Raising:
    def __float__(self):
        gc.collect()
        raise marker
    def __index__(self):
        raise AssertionError("float exception must not fall back")
class MissingAttribute:
    def __float__(self):
        raise AttributeError("inside float")
    def __index__(self):
        raise AssertionError("AttributeError is not absence")
class BadIndex:
    def __index__(self):
        return 1.25
class OverflowIndex:
    def __index__(self):
        return 10 ** 400
def descriptor_value():
    return -2.25
class FloatDescriptor:
    def __get__(self, instance, owner):
        gc.collect()
        return descriptor_value
class Described:
    __float__ = FloatDescriptor()
class TextNumber(str):
    def __float__(self):
        return 6.25
class BypassLookup(Number):
    def __getattribute__(self, name):
        if name == "__float__":
            raise AssertionError("ordinary lookup must be bypassed")
        return object.__getattribute__(self, name)
class Plain:
    pass
def fake_float():
    return 99.0
def convert(value):
    return float(value)
def apply(function, value):
    return function(value)
def main():
    value = Number()
    value.__float__ = fake_float
    assert float(value) == 3.5
    assert convert(Inherited()) == 3.5
    assert float(Both()) == 3.5
    assert float(BypassLookup()) == 3.5
    assert float(IndexOnly()) == 1180591620717411303424.0
    assert float(Described()) == -2.25
    assert float(TextNumber("not a number")) == 6.25
    alias = float
    assert alias(value) == 3.5
    assert apply(float, value) == 3.5
    assert events == ["float", "float", "float", "float", "float", "float"]
    ignored = Plain()
    ignored.__float__ = fake_float
    for item in (BadFloat(), BoolFloat(), TextFloat(), DisabledFloat(), BadIndex(), ignored, None, [], {}):
        caught = False
        try:
            convert(item)
        except TypeError:
            caught = True
        assert caught
    caught = False
    try:
        alias(Raising())
    except ValueError as error:
        caught = error is marker
    assert caught
    caught = False
    try:
        convert(MissingAttribute())
    except AttributeError as error:
        caught = str(error) == "inside float"
    assert caught
    caught = False
    try:
        convert(OverflowIndex())
    except OverflowError:
        caught = True
    assert caught
    assert convert(" 2.5 ") == 2.5
    assert convert(True) == 1.0
    assert convert(7) == 7.0
    print("FLOAT_PROTOCOL_OK")
main()
'''


def test_float_protocol_reference(tmp_path):
    assert_reference_program(PROGRAM, "FLOAT_PROTOCOL_OK\n", tmp_path)


@pytest.mark.integration
@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_float_protocol_native_five_gc(
    python_program_compiler, request, explicit_owned_runtime, tmp_path, capfd,
):
    mode = request.node.callspec.params["python_program_compiler"]
    assert_owned_program(
        PROGRAM, "FLOAT_PROTOCOL_OK\n", tmp_path, python_program_compiler,
        mode, explicit_owned_runtime, capfd,
    )


@pytest.mark.parametrize("relocate", (False, True))
@pytest.mark.parametrize("kind", ("float", "index", "absent", "bad", "error", "none"))
def test_float_protocol_result_ownership(relocate, kind):
    # This executes the changed production dispatch bodies in the existing
    # moving-owner model. It is additional evidence, not native qualification.
    memory = ConsumerModel(relocate)
    receiver = memory.new("receiver")
    namespace = memory.env()
    float_tag = 13  # The model has its own synthetic, non-runtime type tags.
    namespace["PY_TYPE_FLOAT"] = float_tag
    namespace["store_f64"] = memory.store

    def to_float(value):
        assert memory.obj(value)["leases"]
        memory.park("float-conversion")
        return float(memory.obj(value)["value"])

    namespace["py_float_to_f64"] = to_float
    if kind in ("float", "bad"):
        tag = float_tag if kind == "float" else 1
        memory.methods["__float__"] = lambda *_: memory.new("result", tag, 3.5)
        memory.methods["__index__"] = lambda *_: pytest.fail("selected float fell back")
    elif kind == "index":
        memory.methods["__index__"] = lambda *_: memory.new("result", 1, 2 ** 70)
    elif kind == "error":
        def fail(*_):
            memory.pending = memory.new("callback-error", 80, (6, "inside float"))
            return None
        memory.methods["__float__"] = fail
        memory.methods["__index__"] = lambda *_: pytest.fail("exception fell back")
    elif kind == "none":
        memory.methods["__float__"] = None
        memory.methods["__index__"] = lambda *_: pytest.fail("None fell back")
    scalar, handled = memory.alloc(8), memory.alloc(8)
    result = namespace["py_user_special_dispatch"](
        receiver, "__float__", None, None, 0, 8, scalar, handled,
    )
    assert result is None
    assert memory.load(handled) == (kind != "absent")
    if kind in ("float", "index"):
        assert memory.load(scalar) == (3.5 if kind == "float" else float(2 ** 70))
        assert memory.pending is None
        assert "result" in memory.disposed
    elif kind == "absent":
        assert memory.pending is None
    else:
        assert memory.pending is not None
        assert memory.obj(memory.pending)["value"][0] == (6 if kind == "error" else 3)
    assert memory.obj(memory.current["receiver"])["refs"] == 1
    memory.assert_clean()


@pytest.mark.parametrize("relocate", (False, True))
@pytest.mark.parametrize("method,result_value,raw_double,overflow", (
    pytest.param("__index__", 10 ** 400, float("inf"), True, id="positive-huge-index"),
    pytest.param("__index__", -(10 ** 400), float("-inf"), True, id="negative-huge-index"),
    pytest.param("__index__", 2 ** 1024 - 2 ** 971, 1.7976931348623157e308, False, id="positive-max-finite"),
    pytest.param("__index__", -(2 ** 1024 - 2 ** 971), -1.7976931348623157e308, False, id="negative-max-finite"),
    pytest.param("__index__", 2 ** 1024 - 2 ** 970 - 1, 1.7976931348623157e308, False, id="positive-below-overflow"),
    pytest.param("__index__", -(2 ** 1024 - 2 ** 970 - 1), -1.7976931348623157e308, False, id="negative-below-overflow"),
    pytest.param("__index__", 2 ** 1024 - 2 ** 970, float("inf"), True, id="positive-overflow-threshold"),
    pytest.param("__index__", -(2 ** 1024 - 2 ** 970), float("-inf"), True, id="negative-overflow-threshold"),
    pytest.param("__index__", 0, 0.0, False, id="zero-index"),
    pytest.param("__float__", float("inf"), float("inf"), False, id="positive-infinite-float"),
    pytest.param("__float__", float("-inf"), float("-inf"), False, id="negative-infinite-float"),
    pytest.param("__float__", float("nan"), float("nan"), False, id="nan-float"),
))
def test_float_index_overflow_preserves_owned_error(
    relocate, method, result_value, raw_double, overflow,
):
    memory = ConsumerModel(relocate)
    receiver = memory.new("receiver")
    namespace = memory.env()
    float_tag = 13
    namespace["PY_TYPE_FLOAT"] = float_tag
    namespace["store_f64"] = memory.store

    def raw_conversion(value):
        assert memory.obj(value)["leases"]
        assert memory.obj(value)["value"] is result_value
        memory.park("raw-float-conversion")
        # Model the runtime's unchecked double conversion, including raw
        # infinities. CPython float(huge_int) would raise before the new guard.
        # These cases test the checked boundary, not the bigint rounding body.
        return raw_double

    namespace["py_float_to_f64"] = raw_conversion
    tag = 1 if method == "__index__" else float_tag
    memory.methods[method] = lambda *_: memory.new("result", tag, result_value)
    if method == "__float__":
        memory.methods["__index__"] = lambda *_: pytest.fail("selected float fell back")
    if overflow:
        # Inject a model cleanup error to verify that dropping the temporary
        # cannot replace the original owned OverflowError.
        memory.finalize_error.add("result")
    scalar, handled = memory.alloc(8), memory.alloc(8)
    result = namespace["py_user_special_dispatch"](
        receiver, "__float__", None, None, 0, 8, scalar, handled,
    )
    assert result is None and memory.load(handled) == 1
    assert memory.calls == (["__float__", "__index__"] if method == "__index__" else ["__float__"])
    if overflow:
        assert memory.obj(memory.pending)["value"] == (15, "int too large to convert to float")
        assert memory.load(scalar) == 0
        assert "finalizer-error" in memory.disposed
    else:
        assert memory.pending is None
        actual = memory.load(scalar)
        if raw_double != raw_double:
            assert actual != actual
        else:
            assert actual == raw_double
    assert memory.disposed.count("result") == 1
    assert memory.obj(memory.current["receiver"])["refs"] == 1
    memory.assert_clean()
