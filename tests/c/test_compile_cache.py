import pcc.evaluater.c_evaluator as c_evaluator

from click.testing import CliRunner
from pathlib import Path

from pcc.evaluater.c_evaluator import CEvaluator
from pcc.pcc import main
from pcc.project import TranslationUnit


def test_default_compile_cache_dir_prefers_xdg_cache_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    monkeypatch.delenv("PCC_COMPILE_CACHE_DIR", raising=False)

    path = Path(c_evaluator._default_compile_cache_dir())

    assert path == tmp_path / "xdg-cache" / "pcc" / "compile-cache"


def test_default_compile_cache_dir_falls_back_to_home_cache(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.delenv("PCC_COMPILE_CACHE_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    path = Path(c_evaluator._default_compile_cache_dir())

    assert path == tmp_path / "home" / ".cache" / "pcc" / "compile-cache"


def test_evaluate_uses_disk_compile_cache_by_default(tmp_path, monkeypatch):
    cache_dir = tmp_path / "compile-cache"
    monkeypatch.setenv("PCC_COMPILE_CACHE_DIR", str(cache_dir))

    source = "int main(void) { return 7; }\n"

    assert CEvaluator().evaluate(source, optimize=False, use_system_cpp=False) == 7

    def unexpected_cache_miss(unit_name, codestr):
        raise AssertionError(f"unexpected cache miss for {unit_name}")

    monkeypatch.setattr(
        c_evaluator,
        "_compile_preprocessed_translation_unit_artifact",
        unexpected_cache_miss,
    )

    assert CEvaluator().evaluate(source, optimize=False, use_system_cpp=False) == 7


def test_compile_translation_units_recompiles_only_dirty_units(tmp_path, monkeypatch):
    cache_dir = tmp_path / "compile-cache"

    unit_a = TranslationUnit(
        name="a.c",
        path=str(tmp_path / "a.c"),
        source="int helper(void) { return 1; }\n",
    )
    unit_b = TranslationUnit(
        name="b.c",
        path=str(tmp_path / "b.c"),
        source="int helper(void);\nint main(void) { return helper(); }\n",
    )

    compiled = CEvaluator().compile_translation_units(
        [unit_a, unit_b],
        use_system_cpp=False,
        jobs=1,
        cache_dir=str(cache_dir),
    )
    assert len(compiled) == 2

    original_compile = c_evaluator._compile_preprocessed_translation_unit_artifact
    compiled_names = []

    def tracking_compile(unit_name, codestr, **kwargs):
        compiled_names.append(unit_name)
        return original_compile(unit_name, codestr, **kwargs)

    monkeypatch.setattr(
        c_evaluator,
        "_compile_preprocessed_translation_unit_artifact",
        tracking_compile,
    )

    dirty_unit_b = TranslationUnit(
        name="b.c",
        path=str(tmp_path / "b.c"),
        source="int helper(void);\nint main(void) { return helper() + 1; }\n",
    )

    compiled = CEvaluator().compile_translation_units(
        [unit_a, dirty_unit_b],
        use_system_cpp=False,
        jobs=1,
        cache_dir=str(cache_dir),
    )

    assert len(compiled) == 2
    assert compiled_names == ["b.c"]


def test_cli_uses_disk_compile_cache_by_default(tmp_path, monkeypatch):
    cache_dir = tmp_path / "compile-cache"
    main_path = tmp_path / "main.c"
    main_path.write_text("int main(void) { return 0; }\n", encoding="utf-8")

    result = CliRunner().invoke(
        main,
        ["--cache-dir", str(cache_dir), str(main_path)],
    )
    assert result.exit_code == 0, result.output

    def unexpected_cache_miss(unit_name, codestr):
        raise AssertionError(f"unexpected cache miss for {unit_name}")

    monkeypatch.setattr(
        c_evaluator,
        "_compile_preprocessed_translation_unit_artifact",
        unexpected_cache_miss,
    )

    result = CliRunner().invoke(
        main,
        ["--cache-dir", str(cache_dir), str(main_path)],
    )
    assert result.exit_code == 0, result.output


def test_cli_no_cache_bypasses_disk_compile_cache(tmp_path, monkeypatch):
    cache_dir = tmp_path / "compile-cache"
    main_path = tmp_path / "main.c"
    main_path.write_text("int main(void) { return 0; }\n", encoding="utf-8")

    compiled_names = []
    original_compile = c_evaluator._compile_preprocessed_translation_unit_artifact

    def tracking_compile(unit_name, codestr, **kwargs):
        compiled_names.append(unit_name)
        return original_compile(unit_name, codestr, **kwargs)

    monkeypatch.setattr(
        c_evaluator,
        "_compile_preprocessed_translation_unit_artifact",
        tracking_compile,
    )

    result = CliRunner().invoke(
        main,
        ["--cache-dir", str(cache_dir), "--no-cache", str(main_path)],
    )
    assert result.exit_code == 0, result.output
    assert compiled_names == ["__pcc_eval__.c"]


def test_compiler_cache_fingerprint_tracks_c_codegen_and_ir_analysis_package_files():
    tracked_files = {
        Path(path)
        .relative_to(Path(c_evaluator.__file__).resolve().parents[1])
        .as_posix()
        for path in c_evaluator._compiler_cache_tracked_files()
    }

    assert "codegen/__init__.py" in tracked_files
    assert "codegen/c_codegen.py" in tracked_files
    assert "codegen/c_varargs.py" in tracked_files
    assert "ir_passes/__init__.py" in tracked_files
    assert "ir_passes/instcombine.py" in tracked_files
    assert "ir_passes/parity.py" in tracked_files
    assert "ssa/__init__.py" in tracked_files
    assert "ssa/builder.py" in tracked_files
    assert "ssa/sccp.py" in tracked_files
    assert "parse/c_parse_driver.py" in tracked_files


def test_compiler_cache_fingerprint_tracks_bytes_when_metadata_is_unchanged(tmp_path, monkeypatch):
    import os

    source = tmp_path / "compiler.py"
    source.write_bytes(b"old")
    before = source.stat()
    monkeypatch.setattr(c_evaluator, "_compiler_cache_tracked_files", lambda: [str(source)])
    first = c_evaluator._host_compiler_cache_fingerprint()
    source.write_bytes(b"new")
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert c_evaluator._host_compiler_cache_fingerprint() != first


def test_cache_publish_is_atomic_under_same_process_competing_writers(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import json

    key = "a" * 64
    values = [{"unit_name": "x.c", "ir_text": str(i) * 1000} for i in range(6)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda value: c_evaluator._store_compiled_artifact(str(tmp_path), key, value), values))
    output = Path(c_evaluator._compile_cache_path(str(tmp_path), key))
    assert json.loads(output.read_text()) in values
    assert list(output.parent.iterdir()) == [output]
    assert output.stat().st_mode & 0o777 == 0o600


def test_compile_cache_key_tracks_disabled_pass_selection(monkeypatch):
    source = "int main(void) { return 0; }\n"

    default_key = c_evaluator._compile_cache_key("probe.c", source)

    monkeypatch.setenv("PCC_DISABLE_PASSES", "adce")
    disabled_key = c_evaluator._compile_cache_key("probe.c", source)

    assert disabled_key != default_key


def test_compile_cache_key_tracks_backend_selection():
    source = "int main(void) { return 0; }\n"

    llvm_key = c_evaluator._compile_cache_key(
        "probe.c",
        source,
        backend_sig="llvm:llvmlite-default:support",
    )
    llvm_capi_key = c_evaluator._compile_cache_key(
        "probe.c",
        source,
        backend_sig="llvm_capi:llvm-capi-wip:support",
    )

    assert llvm_capi_key != llvm_key


def test_evaluate_cache_misses_when_disabled_pass_selection_changes(
    tmp_path,
    monkeypatch,
):
    cache_dir = tmp_path / "compile-cache"
    monkeypatch.setenv("PCC_COMPILE_CACHE_DIR", str(cache_dir))

    source = "int main(void) { int x = 1 + 2; x = 0; return x; }\n"
    evaluator = CEvaluator()

    assert evaluator.evaluate(source, optimize=False, use_system_cpp=False) == 0

    original_compile = c_evaluator._compile_translation_unit_artifact_job
    compiled_names = []

    def tracking_compile(*args, **kwargs):
        compiled_names.append(args[0].name)
        return original_compile(*args, **kwargs)

    monkeypatch.setattr(
        c_evaluator,
        "_compile_translation_unit_artifact_job",
        tracking_compile,
    )
    monkeypatch.setenv("PCC_DISABLE_PASSES", "adce")

    assert evaluator.evaluate(source, optimize=False, use_system_cpp=False) == 0
    assert compiled_names == ["__pcc_eval__.c"]


def test_evaluate_cache_misses_when_backend_changes(tmp_path, monkeypatch):
    cache_dir = tmp_path / "compile-cache"
    monkeypatch.setenv("PCC_COMPILE_CACHE_DIR", str(cache_dir))

    source = "int main(void) { return 0; }\n"

    assert CEvaluator().evaluate(source, optimize=False, use_system_cpp=False) == 0

    original_compile = c_evaluator._compile_translation_unit_artifact_job
    compiled_names = []

    def tracking_compile(*args, **kwargs):
        compiled_names.append(args[0].name)
        return original_compile(*args, **kwargs)

    monkeypatch.setattr(
        c_evaluator,
        "_compile_translation_unit_artifact_job",
        tracking_compile,
    )

    assert CEvaluator(backend="llvm_capi").evaluate(
        source,
        optimize=False,
        use_system_cpp=False,
    ) == 0
    assert compiled_names == ["__pcc_eval__.c"]


def test_evaluator_backend_self_env_can_run_simple_program(monkeypatch):
    monkeypatch.setenv("PCC_BACKEND", "self")

    assert CEvaluator().evaluate(
        "int main(void) { return 0; }\n",
        optimize=False,
        use_system_cpp=False,
    ) == 0
