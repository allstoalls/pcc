from __future__ import annotations

import gc
from types import SimpleNamespace

from pcc.frontends.python import pipeline_frontend_worker_execution as worker_execution


def test_direct_frontend_release_clears_top_level_owners_and_collects_cycles(
    monkeypatch,
) -> None:
    collect_calls = []
    monkeypatch.setattr(gc, "collect", lambda: collect_calls.append(True))

    module = SimpleNamespace(
        _functions=[],
        _globals=[object()],
        globals={"f": object()},
        metadata=[object()],
        _named_metadata={"dbg": object()},
        _name_counters={"tmp": 3},
    )
    function = SimpleNamespace(
        module=module,
        blocks=[],
        _metadata={"dbg": object()},
        _name_registry={"v": 1},
        _direct_indexed_builder=object(),
        _direct_indexed_function_cache=object(),
    )
    block = SimpleNamespace(
        parent=function,
        function=function,
        _instrs=[object()],
        _text_lines=["ret void"],
    )
    function.blocks.append(block)
    module._functions.append(function)

    class_lowering = SimpleNamespace(
        parent=None,
        classes={"C": object()},
        _field_arr_pool={"f": object()},
        _cname_pool={"C": object()},
        _base_arr_pool={"B": object()},
        _class_defs=[object()],
    )
    codegen = SimpleNamespace(
        module=module,
        ast_module=object(),
        _ast_body=(object(),),
        class_lowering=class_lowering,
        functions={"f": function},
        runtime={"runtime": object()},
        env={"x": object()},
        _module_globals={"x": object()},
        _module_global_init_flags={"x": object()},
        _funcdef_functions={1: function},
        _native_symbol_funcdefs={"f": object()},
        _fn_err_exit_blocks={"f": block},
        _native_module_exports={"other": object()},
        _native_function_object_exports={"f": True},
        _str_obj_pool={"x": object()},
        _str_pool={"x": object()},
        _direct_indexed_module=object(),
    )
    class_lowering.parent = codegen
    frozen_direct_owner = codegen._direct_indexed_module

    worker_execution._release_direct_frontend_state(codegen)

    assert collect_calls == [True]
    assert codegen._direct_indexed_module is None
    assert frozen_direct_owner is not None
    assert module._functions == []
    assert module._globals == []
    assert module.globals == {}
    assert codegen.functions == {}
    assert codegen.runtime == {}
    assert codegen.env == {}
    assert codegen._module_globals == {}
    assert codegen._module_global_init_flags == {}
    assert codegen._funcdef_functions == {}
    assert codegen._native_symbol_funcdefs == {}
    assert codegen._fn_err_exit_blocks == {}


def test_indexed_sidecar_worker_rejects_a_non_singleton_manifest(
    tmp_path,
    monkeypatch,
) -> None:
    result = tmp_path / "result.tsv"
    errors = []
    monkeypatch.setenv("PCC_DIRECT_INDEXED_SIDECAR", "1")
    manifest = {
        "result_path": str(result),
        "job_kind": "codegen",
        "src_paths": ["a.py", "b.py"],
        "module_names": ["a", "b"],
        "entry_module": "a",
        "sibling_inits": (),
        "libpython_mode": "off",
        "ir_scaffold_mode": "on",
        "verbose": False,
        "assigned_indices": [0, 1],
    }

    status = worker_execution.run_codegen_worker(
        "manifest",
        read_manifest=lambda _path: manifest,
        run_export_worker_callback=lambda _manifest: 0,
        run_summary_worker_callback=lambda _manifest: 0,
        worker_timing_enabled=lambda: False,
        native_worker_executable=lambda: True,
        read_native_exports_wire=lambda _path: ({}, {}),
        read_native_exports_wire_for_module=lambda _path, _module: ({}, {}, None, False),
        read_ast_wire=lambda _path: None,
        build_closed_world_context=lambda *_args, **_kwargs: ([], {}, {}),
        module_imports_native_extension=lambda *_args: False,
        contextual_host_params_for_module=lambda *_args: (),
        module_uses_default_native_exports=lambda _module: False,
        copy_native_module_exports=lambda value: value,
        closed_world_function_object_exports=lambda *_args: {},
        log=lambda *_args: None,
        ir_needs_libpython=lambda _text: False,
        safe_exception_text=str,
        write_worker_error=lambda _path, message: errors.append(message),
        pipeline_error=RuntimeError,
    )

    assert status == 1
    assert len(errors) == 1
    assert "requires a singleton worker manifest" in errors[0]
