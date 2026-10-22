
from __future__ import annotations

from pcc.driver.c_cache_key import build_cache_key


def test_cache_key_changes_on_content_not_mtime(tmp_path):
    src = tmp_path / "x.c"
    src.write_text("int main(){return 0;}\n", encoding="utf-8")
    k1 = build_cache_key(
        compiler_version="pcc-test",
        target_triple="x86_64-linux",
        opt_level=2,
        source_paths=[src],
        cpp_args=["-DDEBUG=0"],
    )
    src.write_text("int main(){return 1;}\n", encoding="utf-8")
    k2 = build_cache_key(
        compiler_version="pcc-test",
        target_triple="x86_64-linux",
        opt_level=2,
        source_paths=[src],
        cpp_args=["-DDEBUG=0"],
    )
    assert k1.digest() != k2.digest()
    assert k1.explain_miss(k2) == [f"input_changed:{src}"]


def test_cache_explain_reports_option_changes(tmp_path):
    src = tmp_path / "x.c"
    src.write_text("int x;\n", encoding="utf-8")
    k1 = build_cache_key(compiler_version="v", target_triple="t", opt_level=0, source_paths=[src])
    k2 = build_cache_key(compiler_version="v", target_triple="t", opt_level=3, source_paths=[src])
    assert k1.explain_miss(k2) == ["opt_level"]
