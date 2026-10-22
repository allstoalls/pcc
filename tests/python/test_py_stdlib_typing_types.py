from __future__ import annotations

from pcc.stdlib import typing as t
from pcc.stdlib import types as ty


def test_typing_generic_markers_preserve_origin_and_args():
    marker = t.Optional[int]
    assert t.get_origin(marker) is t.Optional
    assert t.get_args(marker) == (int,)

    u = t.Union[int, str]
    assert t.get_origin(u) is t.Union
    assert t.get_args(u) == (int, str)


def test_typing_typevar_newtype_and_decorators_are_runtime_noops():
    T = t.TypeVar("T")
    assert repr(T) == "~T"
    UserId = t.NewType("UserId", int)
    assert UserId(42) == 42
    assert UserId.__name__ == "UserId"
    assert UserId.__supertype__ is int

    def f(x):
        return x

    assert t.cast(int, "x") == "x"
    assert t.overload(f) is f
    assert t.final(f) is f
    assert t.no_type_check(f) is f


def test_protocol_and_generic_are_usable_as_bases():
    class P(t.Protocol):
        pass

    class G(t.Generic):
        pass

    assert P is not None
    assert G is not None


def test_types_simple_namespace_and_module_type():
    ns = ty.SimpleNamespace(a=1, b="x")
    assert ns.a == 1
    assert ns.b == "x"
    assert "a=1" in repr(ns)

    mod = ty.ModuleType("demo", "doc")
    assert mod.__name__ == "demo"
    assert mod.__doc__ == "doc"
    assert "demo" in repr(mod)


def test_types_mapping_proxy_is_read_only_view():
    data = {"a": 1}
    proxy = ty.MappingProxyType(data)
    assert proxy["a"] == 1
    assert "a" in proxy
    assert len(proxy) == 1
    data["b"] = 2
    assert proxy["b"] == 2
