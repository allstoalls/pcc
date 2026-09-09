from pathlib import Path

import pytest

from pcc.py_frontend import compile_cache, pipeline


def _plan(tmp_path, monkeypatch, *, compiler_text="compiler"):
    compiler = tmp_path / "pcc1"
    compiler.write_text(compiler_text, encoding="utf-8")
    entry = tmp_path / "entry.py"
    helper = tmp_path / "helper.py"
    entry.write_text("from helper import value\nprint(value())\n", encoding="utf-8")
    helper.write_text("def value():\n    return 3\n", encoding="utf-8")
    monkeypatch.setenv("PCC_PY_FRONTEND_IR_CACHE_IDENTITY", "source-tree-a")
    monkeypatch.setenv("PCC_PY_FRONTEND_IR_CACHE_DIR", str(tmp_path / "cache"))
    plan = compile_cache.plan_python_frontend_ir_cache(
        [str(entry), str(helper)],
        ["entry", "helper"],
        compiler_executable=str(compiler),
        host_python="python3",
        entry_module="entry",
        sibling_inits=("helper",),
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    assert plan is not None
    return plan, compiler, entry, helper


@pytest.mark.parametrize("codegen_name,codegen_value", [
    ("PCC_PYTHON_TYPED_INT_ABI", "boxed"),
    ("PCC_DISABLE_BULK_GENERATOR_FRAME_INIT", "1"),
    ("PCC_KNOWN_OBJECT_REFS", "1"),
])
def test_frontend_ir_cache_key_is_gc_invariant_but_codegen_sensitive(
    tmp_path, monkeypatch, codegen_name, codegen_value
):
    monkeypatch.delenv(codegen_name, raising=False)
    monkeypatch.setenv("PCC_GC_BACKEND", "0")
    first, compiler, entry, helper = _plan(tmp_path, monkeypatch)

    monkeypatch.setenv("PCC_GC_BACKEND", "4")
    second = compile_cache.plan_python_frontend_ir_cache(
        [str(entry), str(helper)],
        ["entry", "helper"],
        compiler_executable=str(compiler),
        host_python="python3",
        entry_module="entry",
        sibling_inits=("helper",),
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    assert second is not None
    assert second["key"] == first["key"]

    monkeypatch.setenv(codegen_name, codegen_value)
    third = compile_cache.plan_python_frontend_ir_cache(
        [str(entry), str(helper)],
        ["entry", "helper"],
        compiler_executable=str(compiler),
        host_python="python3",
        entry_module="entry",
        sibling_inits=("helper",),
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    assert third is not None
    assert third["key"] != first["key"]


def test_frontend_ir_cache_identity_is_independent_from_object_emitter_identity(
    tmp_path, monkeypatch
):
    first, compiler, entry, helper = _plan(tmp_path, monkeypatch)
    monkeypatch.setenv("PCC_SELF_BACKEND_OBJECT_CACHE", "off")
    assert compile_cache.python_frontend_ir_cache_enabled()
    monkeypatch.setenv("PCC_SELF_BACKEND_OBJECT_CACHE_IDENTITY", "emitter-b")
    same_frontend = compile_cache.plan_python_frontend_ir_cache(
        [str(entry), str(helper)],
        ["entry", "helper"],
        compiler_executable=str(compiler),
        host_python="python3",
        entry_module="entry",
        sibling_inits=("helper",),
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    monkeypatch.setenv("PCC_PY_FRONTEND_IR_CACHE_IDENTITY", "source-tree-b")
    changed_frontend = compile_cache.plan_python_frontend_ir_cache(
        [str(entry), str(helper)],
        ["entry", "helper"],
        compiler_executable=str(compiler),
        host_python="python3",
        entry_module="entry",
        sibling_inits=("helper",),
        libpython_mode="off",
        ir_scaffold_mode="on",
    )

    assert same_frontend is not None
    assert changed_frontend is not None
    assert same_frontend["key"] == first["key"]
    assert changed_frontend["key"] != first["key"]


def test_frontend_ir_cache_key_includes_compiler_and_source_bytes(
    tmp_path, monkeypatch
):
    first, compiler, entry, helper = _plan(tmp_path, monkeypatch)
    compiler.write_text("different compiler", encoding="utf-8")
    compiler_changed = compile_cache.plan_python_frontend_ir_cache(
        [str(entry), str(helper)],
        ["entry", "helper"],
        compiler_executable=str(compiler),
        host_python="python3",
        entry_module="entry",
        sibling_inits=("helper",),
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    assert compiler_changed is not None
    assert compiler_changed["key"] != first["key"]

    compiler.write_text("compiler", encoding="utf-8")
    helper.write_text("def value():\n    return 4\n", encoding="utf-8")
    source_changed = compile_cache.plan_python_frontend_ir_cache(
        [str(entry), str(helper)],
        ["entry", "helper"],
        compiler_executable=str(compiler),
        host_python="python3",
        entry_module="entry",
        sibling_inits=("helper",),
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    assert source_changed is not None
    assert source_changed["key"] != first["key"]


def test_frontend_ir_cache_key_uses_compiler_bytes_not_copy_path(tmp_path, monkeypatch):
    first, compiler, entry, helper = _plan(tmp_path, monkeypatch)
    copied_compiler = tmp_path / "gc4" / "pcc1"
    copied_compiler.parent.mkdir()
    copied_compiler.write_bytes(compiler.read_bytes())

    copied = compile_cache.plan_python_frontend_ir_cache(
        [str(entry), str(helper)],
        ["entry", "helper"],
        compiler_executable=str(copied_compiler),
        host_python="python3",
        entry_module="entry",
        sibling_inits=("helper",),
        libpython_mode="off",
        ir_scaffold_mode="on",
    )

    assert copied is not None
    assert copied["key"] == first["key"]


def test_frontend_ir_cache_round_trip_and_tamper_rejection(tmp_path, monkeypatch):
    plan, _compiler, _entry, _helper = _plan(tmp_path, monkeypatch)
    result = (
        [("entry", "; entry\n"), ("helper", "; helper\n")],
        False,
        True,
        17,
        [],
    )
    assert compile_cache.acquire_python_frontend_ir_cache(plan)
    try:
        assert compile_cache.publish_python_frontend_ir_cache(plan, result)
    finally:
        compile_cache.release_python_frontend_ir_cache(plan)

    assert (
        compile_cache.load_python_frontend_ir_cache(plan, ["entry", "helper"]) == result
    )
    bundle = Path(plan["entry"]) / "ir.bundle"
    bundle.write_text(bundle.read_text(encoding="utf-8") + "tamper", encoding="utf-8")
    assert (
        compile_cache.load_python_frontend_ir_cache(plan, ["entry", "helper"]) is None
    )


def test_frontend_ir_cache_coordinates_byte_identical_compiler_copies(
    tmp_path, monkeypatch
):
    first, compiler, entry, helper = _plan(tmp_path, monkeypatch)
    copied_compiler = tmp_path / "gc3" / "pcc1"
    copied_compiler.parent.mkdir()
    copied_compiler.write_bytes(compiler.read_bytes())
    second = compile_cache.plan_python_frontend_ir_cache(
        [str(entry), str(helper)],
        ["entry", "helper"],
        compiler_executable=str(copied_compiler),
        host_python="python3",
        entry_module="entry",
        sibling_inits=("helper",),
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    result = (
        [("entry", "; entry\n"), ("helper", "; helper\n")],
        False,
        False,
        18,
        [],
    )

    assert second is not None
    assert second["key"] == first["key"]
    assert compile_cache.acquire_python_frontend_ir_cache(first)
    assert not compile_cache.acquire_python_frontend_ir_cache(second)
    try:
        assert compile_cache.publish_python_frontend_ir_cache(first, result)
        assert (
            compile_cache.wait_python_frontend_ir_cache(
                second, ["entry", "helper"], timeout_seconds=1.0
            )
            == result
        )
    finally:
        compile_cache.release_python_frontend_ir_cache(first)


def test_parallel_frontend_cache_wrapper_publishes_and_reuses(monkeypatch):
    result = ([("entry", "; ir\n")], False, False, 5, [])
    uncached_calls = []
    published = []
    released = []
    cached = [None]

    monkeypatch.setattr(pipeline, "_can_spawn_python_frontend_worker", lambda: True)
    monkeypatch.setattr(
        pipeline, "_python_frontend_worker_command_prefix", lambda: ["/tmp/pcc1"]
    )
    monkeypatch.setattr(
        pipeline,
        "plan_python_frontend_ir_cache",
        lambda *_args, **_kwargs: {"key": "x"},
    )
    monkeypatch.setattr(
        pipeline,
        "load_python_frontend_ir_cache",
        lambda _plan, _names: cached[0],
    )
    monkeypatch.setattr(
        pipeline, "acquire_python_frontend_ir_cache", lambda _plan: True
    )
    monkeypatch.setattr(
        pipeline,
        "publish_python_frontend_ir_cache",
        lambda _plan, value: published.append(value) or True,
    )
    monkeypatch.setattr(
        pipeline,
        "release_python_frontend_ir_cache",
        lambda plan: released.append(plan),
    )
    monkeypatch.setattr(
        pipeline,
        "_compile_python_multi_codegen_parallel_uncached",
        lambda *_args, **_kwargs: uncached_calls.append(True) or result,
    )

    args = dict(
        jobs=2,
        entry_module="entry",
        sibling_inits=(),
        libpython_mode="off",
        ir_scaffold_mode="on",
        verbose=False,
    )
    assert (
        pipeline._compile_python_multi_codegen_parallel(["entry.py"], ["entry"], **args)
        == result
    )
    assert uncached_calls == [True]
    assert published == [result]
    assert released == [{"key": "x"}]

    cached[0] = result
    assert (
        pipeline._compile_python_multi_codegen_parallel(["entry.py"], ["entry"], **args)
        == result
    )
    assert uncached_calls == [True]
