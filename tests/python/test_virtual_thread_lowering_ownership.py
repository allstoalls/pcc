"""Static ownership regressions for native virtual-thread lowering.

These checks intentionally inspect the lowering source rather than relying on
one optimizer-specific LLVM spelling.  Runtime execution remains covered by
the virtual-thread frontend and GC production-contract suites.
"""

import textwrap
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
LOWERING = REPO / "pcc" / "py_frontend" / "codegen" / "native_virtual_thread.py"


def _source() -> str:
    return LOWERING.read_text(encoding="utf-8")


def _between(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    return source[start_index:source.index(end, start_index)]


def test_worker_completion_drops_the_independent_owned_result() -> None:
    resume = _between(
        _source(),
        "    def _emit_virtual_thread_resume_function(",
        "    def _emit_virtual_thread_spawn(",
    )

    complete = resume.index('self.runtime["py_virtual_thread_complete"]')
    release = resume.index("self._gc_release(result_obj)", complete)
    returned = resume.index("self.builder.ret(rc)", release)
    assert "if ret_ty is not None and not isinstance(ret_ty, NoneType):" in resume
    assert complete < release < returned


def test_spawn_failure_releases_vthread_before_allocating_owned_error() -> None:
    source = _source()
    ordinary = _between(
        source,
        "    def _emit_virtual_thread_spawn(",
        "    def _emit_virtual_thread_generator_spawn(",
    )
    generator = _between(
        source,
        "    def _emit_virtual_thread_generator_spawn(",
        "    def _emit_native_virtual_thread_value_call(",
    )

    for failure_path in (ordinary, generator):
        release_thread = failure_path.index("self._gc_release(vt)")
        allocate_error = failure_path.index('self.runtime["py_exc_new"]', release_thread)
        raise_error = failure_path.index(
            'self.runtime["py_raise"]', allocate_error
        )
        release_error = failure_path.index("self._gc_release(exc)", raise_error)
        assert release_thread < allocate_error < raise_error < release_error


def test_dynamic_vthread_arguments_release_through_owned_expression_policy() -> None:
    source = _source()
    boundaries = (
        ("cancel", "run", "py_virtual_thread_cancel"),
        ("result", "exception", "py_virtual_thread_result"),
        ("exception", "outcome", "py_virtual_thread_exception"),
        ("outcome", "state", "py_virtual_thread_outcome"),
        ("state", "sleep", "py_virtual_thread_state"),
    )

    for operation, next_operation, runtime_name in boundaries:
        branch = _between(
            source,
            f'        if kind == "pcc.virtual_thread.{operation}":',
            f'        if kind == "pcc.virtual_thread.{next_operation}":',
        )
        emitted = branch.index("target_obj = self._emit_as_object(args[0])")
        called = branch.index(f'self.runtime["{runtime_name}"]', emitted)
        released = branch.index(
            "self._release_virtual_thread_argument(target_obj, args[0])", called
        )
        assert emitted < called < released


def test_multioperand_vthread_calls_trace_reload_and_lifo_release_early_objects() -> None:
    source = _source()
    branches = (
        ("send", "recv", "py_virtual_thread_channel_send_begin"),
        ("recv", "close_sender", "py_virtual_thread_channel_recv_begin"),
        ("select2", "join", "py_virtual_thread_channel_select2_begin"),
        ("join", "cancel", "py_virtual_thread_join"),
        ("sleep", "block_on_fd", "py_virtual_thread_sleep"),
        ("block_on_fd", "block_current_on_fd", "py_virtual_thread_block_on_fd"),
    )

    for operation, next_operation, runtime_name in branches:
        branch = _between(
            source,
            f'        if kind == "pcc.virtual_thread.{operation}":',
            f'        if kind == "pcc.virtual_thread.{next_operation}":',
        )
        rooted = branch.index("self._enter_virtual_thread_operand_root(")
        later_operand = branch.index(
            "self._emit_expr_with_cpy_operand_cleanup(", rooted
        )
        runtime_call = branch.index(f'self.runtime["{runtime_name}"]')
        reloaded = branch.index(
            "self._load_virtual_thread_operand_root(", later_operand
        )
        released = branch.index(
            "self._release_rooted_pcc_lifetimes(", runtime_call
        )
        assert "rooted_pcc_lifetimes=" in branch
        assert rooted < later_operand < runtime_call < reloaded < released


def test_current_wait_and_sleep_current_root_current_across_scalar_lowering() -> None:
    source = _source()
    current_wait = _between(
        source,
        "    def _emit_virtual_thread_current_fd_wait(",
        "    def _emit_virtual_thread_resume_function(",
    )
    sleep_current = _between(
        source,
        '        if kind == "pcc.virtual_thread.sleep_current":',
        '        if kind == "pcc.virtual_thread.result":',
    )

    for branch, runtime_name in (
        (current_wait, "py_virtual_thread_block_on_fd"),
        (sleep_current, "py_virtual_thread_sleep"),
    ):
        root = branch.index("self._enter_virtual_thread_operand_root(")
        scalar = branch.index("as_i64=True", root)
        reload = branch.index(
            "self._load_virtual_thread_operand_root(", scalar
        )
        runtime_call = branch.index(f'self.runtime["{runtime_name}"]')
        release = branch.index(
            "self._release_rooted_pcc_lifetimes(", runtime_call
        )
        assert "rooted_pcc_lifetimes=" in branch
        assert root < scalar < runtime_call < reload < release


def test_vthread_callback_roots_callable_during_dynamic_argument_build() -> None:
    callback = _between(
        _source(),
        "    def _emit_virtual_thread_callback_call(",
        "    def _virtual_thread_frame_map(",
    )
    root = callback.index("self._enter_virtual_thread_operand_root(")
    args_tuple = callback.index(
        "self._emit_virtual_thread_dynamic_args_with_roots(", root
    )
    reload = callback.index(
        "self._load_virtual_thread_operand_root(", args_tuple
    )
    runtime_call = callback.index('self.runtime["py_obj_call"]')
    release = callback.index(
        "self._release_rooted_pcc_lifetimes(", runtime_call
    )
    args_reload = callback.index(
        "self._load_virtual_thread_operand_root(args_root)", args_tuple
    )
    null_guard = callback.index("vthread.call.result.is_null", runtime_call)
    releases = []
    search_at = null_guard
    while True:
        try:
            found = callback.index(
                "self._release_rooted_pcc_lifetimes(", search_at
            )
        except ValueError:
            break
        releases.append(found)
        search_at = found + 1
    error_release = releases[0]
    ready_release = releases[1]
    store_result = callback.index('self.runtime["pcc_gc_store_root"]', error_release)
    assert release == error_release
    assert root < args_tuple < runtime_call < reload < error_release
    assert runtime_call < reload < args_reload < null_guard < error_release
    assert error_release < store_result < ready_release
    assert callback.index("self._emit_post_call_err_check(expr.span)", error_release) < (
        store_result
    )
    assert callback.index("self.builder.unreachable()", error_release) < store_result
    assert "args_owned" not in callback


def test_vthread_callback_args_tuple_is_traced_across_each_later_operand() -> None:
    source = _source()
    builder = _between(
        source,
        "    def _emit_virtual_thread_rooted_args_tuple(",
        "    def _emit_virtual_thread_current_fd_wait(",
    )
    allocation = builder.index('self.runtime[runtime_new]')
    root = builder.index("self._enter_virtual_thread_operand_root(", allocation)
    operand = builder.index("self._emit_expr_with_cpy_operand_cleanup(", root)
    reload = builder.index(
        "self._load_virtual_thread_operand_root(", operand
    )
    store = builder.index('self.runtime["py_tuple_set_item"]', reload)
    checked = builder.index(
        "self._emit_virtual_thread_container_call_check(", store
    )
    assert allocation < root < operand < reload < store < checked
    assert "rooted_pcc_lifetimes=(container_root,)" in builder
    assert '"vthread.call.args.value"' in builder
    assert "rooted_value = self._load_virtual_thread_operand_root(value_root)" in (
        builder
    )
    assert "(container_root, value_root)" in builder
    assert "self._release_rooted_pcc_lifetimes((value_root,))" in builder
    assert 'self.runtime["pcc_gc_pin"]' not in builder
    assert 'operation = "py_list_extend" if is_splat else "py_list_append"' in (
        builder
    )
    assert 'self.runtime["py_tuple_from_list"]' in builder
    assert "(container_root, tuple_root)" in builder


def test_vthread_args_container_errors_unwind_before_callable_root() -> None:
    source = _source()
    check = _between(
        source,
        "    def _emit_virtual_thread_container_call_check(",
        "    def _emit_virtual_thread_rooted_args_tuple(",
    )
    wrapper = _between(
        source,
        "    def _emit_virtual_thread_dynamic_args_with_roots(",
        "    def _emit_virtual_thread_container_call_check(",
    )

    assert "rooted_pcc_lifetimes=container_roots" not in check
    assert "container_roots," in check
    assert "pinned_values" not in check
    assert "self._try_err_block = cleanup" in check
    assert "roots," in wrapper
    assert "self._try_err_block = pcc_cleanup" in wrapper


def test_rooted_operand_cleanup_reloads_and_unwinds_both_error_kinds() -> None:
    cleanup_source = (
        REPO / "pcc" / "py_frontend" / "codegen" / "cpy_call_lowering.py"
    ).read_text(encoding="utf-8")
    release = _between(
        cleanup_source,
        "    def _release_rooted_pcc_lifetimes(",
        "    def _emit_expr_with_cpy_operand_cleanup(",
    )
    evaluate = _between(
        cleanup_source,
        "    def _emit_expr_with_cpy_operand_cleanup(",
        "    def _release_cpy_callable_if_owned(",
    )

    assert "for root_slot, release_owned in reversed(roots):" in release
    loaded = release.index('self.runtime["pcc_gc_load_ptr"]')
    left = release.index("self._leave_container_temp_root(root_slot)", loaded)
    balanced = release.index("self._gc_release(rooted_value, known_object=True)", left)
    assert loaded < left < balanced
    # Separate pcc and CPython unwind blocks each receive the same live roots.
    assert evaluate.count("rooted_pcc_lifetimes,") >= 2
    assert "self._try_err_block = pcc_cleanup" in evaluate
    assert "self._cpy_operand_cleanup_block = cpy_cleanup" in evaluate


def test_vthread_root_helpers_are_in_the_pcc1_host_method_closure() -> None:
    host_contract = (
        REPO / "pcc" / "py_frontend" / "codegen" / "host_contract.py"
    ).read_text(encoding="utf-8")
    static_methods = (
        REPO
        / "pcc"
        / "py_frontend"
        / "codegen"
        / "_l1_codegen_static_methods.py"
    ).read_text(encoding="utf-8")
    helpers = (
        "_emit_virtual_thread_container_call_check",
        "_emit_virtual_thread_dynamic_args_with_roots",
        "_emit_virtual_thread_rooted_args_tuple",
        "_enter_virtual_thread_operand_root",
        "_load_virtual_thread_operand_root",
        "_release_rooted_pcc_lifetimes",
    )
    for helper in helpers:
        assert f'"{helper}"' in host_contract
        # The generated table stores compact ``_append_method(out, '<name>',
        # ...)`` entries; the tuple-of-dicts ``'name': ...`` schema is built
        # eagerly at import, not written to the file.
        assert f"_append_method(out, '{helper}'," in static_methods


def test_dynamic_vthread_result_producers_override_ambiguous_any_ownership() -> None:
    helper = _between(
        _source(),
        "    def _release_virtual_thread_argument(",
        "    def _emit_virtual_thread_current_fd_wait(",
    )
    for producer in ("spawn", "call", "join", "current", "result", "exception"):
        assert f'"pcc.virtual_thread.{producer}"' in helper
    assert "self._gc_release(obj)" in helper
    assert "self._gc_release_if_owned(obj, source_expr)" in helper


def test_vthread_runtime_error_helper_releases_local_exception_owner() -> None:
    helper = _between(
        _source(),
        "    def _emit_virtual_thread_rc_check(",
        "    def _emit_virtual_thread_current_fd_wait(",
    )
    raised = helper.index('self.runtime["py_raise"]')
    released = helper.index("self._gc_release(exc)", raised)
    assert raised < released


def test_vthread_owned_result_classifier_is_exact_and_raw_scaffold_equal() -> None:
    ownership = (
        REPO / "pcc" / "py_frontend" / "codegen" / "ownership_lowering.py"
    ).read_text(encoding="utf-8")
    object_classifier = _between(
        ownership,
        "    def _expr_returns_owned_object(",
        "    def _return_type_is_owned_object(",
    )
    raw_classifier = _between(
        ownership,
        "    def _raw_scaffold_object_rhs_is_owned(",
        "    def _valueclass_payload_expr_fields_are_owned(",
    )
    owned = ("spawn", "call", "join", "current", "result", "exception")
    scalar_or_none = ("cancel", "outcome", "state", "sleep", "block_on_fd")

    for classifier in (object_classifier, raw_classifier):
        assert "self._native_builtin_value_kind_for_expr(expr.func)" in classifier
        for operation in owned:
            assert f'"pcc.virtual_thread.{operation}"' in classifier
        for operation in scalar_or_none:
            assert f'"pcc.virtual_thread.{operation}"' not in classifier

    # C-ABI-exporting runtime modules normally opt out of automatic ownership,
    # but exact native vthread producers remain owned there as well.
    assert raw_classifier.index("native_call =") < raw_classifier.index(
        "if self._module_has_c_abi_export:"
    )


def _compile_dynamic_call_owned_ir(tmp_path, import_source: str, call_name: str) -> str:
    from pcc.py_frontend.pipeline import compile_python

    source_path = tmp_path / (call_name.replace(".", "_") + "_owned.py")
    output_path = source_path.with_suffix(".ll")
    source_path.write_text(
        textwrap.dedent(
            f"""
            from pcc.extern import c_int64, extern
            {import_source}

            monotonic_us = extern("pcc_runtime_monotonic_us", (), c_int64)

            def callback(value: int) -> str:
                return str(value)

            def dispatch(callback, value: int):
                response = {call_name}(callback, value)
                response = {call_name}(callback, value + 1)
                return response
            """
        ).lstrip(),
        encoding="utf-8",
    )
    compile_python(
        str(source_path),
        str(output_path),
        emit_llvm_only=True,
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    return output_path.read_text(encoding="utf-8")


def test_vthread_call_module_alias_result_gets_owned_local_management(tmp_path) -> None:
    ir_text = _compile_dynamic_call_owned_ir(
        tmp_path,
        "import pcc.virtual_thread as vt",
        "vt.call",
    )
    assert "response.owned.resolve" in ir_text


def test_vthread_call_import_from_alias_result_gets_owned_local_management(
    tmp_path,
) -> None:
    ir_text = _compile_dynamic_call_owned_ir(
        tmp_path,
        "from pcc.virtual_thread import call as invoke",
        "invoke",
    )
    assert "response.owned.resolve" in ir_text
