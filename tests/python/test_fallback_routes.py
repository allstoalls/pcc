from __future__ import annotations

import json

from pcc.diagnostics.fallback_routes import explain_routes, route_from_classification


def test_fallback_route_marks_native_stdlib_native():
    route = route_from_classification("json", "native_stdlib")
    assert route.native is True
    assert "pcc/stdlib" in route.reason


def test_fallback_route_json_marks_cpython_fallback():
    route = route_from_classification("numpy", "cpython_fallback")
    data = json.loads(explain_routes([route], fmt="json"))
    assert data["routes"][0]["native"] is False
    assert "libpython" in data["routes"][0]["reason"]
