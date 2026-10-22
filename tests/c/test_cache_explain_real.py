from pcc.diagnostics.cache_explain import CacheDecision, CacheInput, build_cache_key, format_cache_decision


def test_cache_key_changes_with_content(tmp_path):
    p = tmp_path / "x.c"
    p.write_text("int x;\n", encoding="utf-8")
    a = CacheInput.from_path(str(p))
    key_a = build_cache_key([a], flags=["-O2"])
    p.write_text("int y;\n", encoding="utf-8")
    b = CacheInput.from_path(str(p))
    key_b = build_cache_key([b], flags=["-O2"])
    assert key_a != key_b


def test_cache_decision_json_contains_reason(tmp_path):
    p = tmp_path / "x.c"; p.write_text("int x;\n", encoding="utf-8")
    inp = CacheInput.from_path(str(p))
    decision = CacheDecision(build_cache_key([inp], flags=[]), False, "source changed", (inp,))
    assert "source changed" in format_cache_decision(decision, fmt="json")
