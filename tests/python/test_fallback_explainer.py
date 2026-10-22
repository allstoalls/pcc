from __future__ import annotations

import json

from pcc.diagnostics.fallback_explainer import FallbackExplainer, explain_import


def test_fallback_explainer_json():
    explainer = FallbackExplainer()
    explainer.add("import numpy", "import-resolution", "native port missing")
    assert json.loads(explainer.format_json())["count"] == 1


def test_explain_import_only_for_fallback():
    assert explain_import("math", "builtin_native_dispatch") is None
    assert explain_import("numpy", "cpython_fallback") is not None
