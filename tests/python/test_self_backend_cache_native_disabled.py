from __future__ import annotations

from pathlib import Path

from pcc.frontends.python.pipeline import compile_python


def _function_ir(ir_text: str, name: str) -> str:
    result_type = {"maintain": "void", "plan": "ptr", "publish": "i1"}[name]
    signature = (
        "define external " + result_type
        + " @user_pcc_frontends_python_pipeline_self_backend_cache_" + name + "("
    )
    start = ir_text.index(signature)
    end = ir_text.index("\n}", start) + 2
    return ir_text[start:end]


def test_disabled_object_cache_entrypoints_remain_native(tmp_path):
    root = Path(__file__).resolve().parents[2]
    source = root / "pcc/frontends/python/pipeline_self_backend_cache.py"
    ir_path = tmp_path / "cache.ll"
    compile_python(
        str(source),
        str(ir_path),
        emit_llvm_only=True,
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    ir_text = ir_path.read_text(encoding="utf-8")
    for name in ("maintain", "plan", "publish"):
        body = _function_ir(ir_text, name)
        assert "strict.nolib.stub" not in body, name
        assert "@py_cpy_" not in body, name


def test_disabled_object_cache_does_not_touch_cache_files(monkeypatch, tmp_path):
    from pcc.frontends.python import pipeline_self_backend_cache as cache

    monkeypatch.delenv(cache.OBJECT_CACHE_IDENTITY_ENV, raising=False)
    missing = str(tmp_path / "missing.ll")
    items = [(str(tmp_path / "result"), str(tmp_path / "object"), missing)]
    plan = cache.plan(
        items, "self-aarch64-darwin-v0", "cc", str(tmp_path),
        host_python_command=None, plan_host_code="", small_int_decimal=None,
    )
    assert plan == [("", "off")]
    assert cache.publish(
        items, plan, str(tmp_path),
        host_python_command=None, publish_host_code="",
    )
    cache.maintain(
        [str(tmp_path / "object")],
        host_python_command=None, pcc_source_root=None, retention_host_code="",
    )
    assert not (tmp_path / "object").exists()
