from __future__ import annotations

import subprocess
from pathlib import Path


def test_exception_accessor_symbols_are_wired_in_c_py_and_abi():
    py_src = Path("pcc/runtime/py/py_exc_objects.py").read_text(encoding="utf-8")
    header = Path("pcc/runtime/include/py_runtime.h").read_text(encoding="utf-8")
    abi = Path("pcc/frontends/python/codegen/runtime_abi.py").read_text(encoding="utf-8")

    assert '@c_abi_export("py_exc_get_cause")' in py_src
    assert '@c_abi_export("py_exc_get_context")' in py_src
    assert '@c_abi_export("py_exc_traceback_len")' in py_src

    assert "PyObject *py_exc_get_cause(PyObject *exc);" in header
    assert '"py_exc_get_cause": (_PYOBJ, [_PYOBJ], False)' in abi
    assert '"py_exc_traceback_len": (_I64, [_PYOBJ], False)' in abi


def test_source_aware_traceback_contract_is_mirrored_and_outermost_first():
    py_src = Path("pcc/runtime/py/py_exc_traceback.py").read_text(
        encoding="utf-8"
    )
    header = Path("pcc/runtime/include/py_runtime.h").read_text(
        encoding="utf-8"
    )
    abi = Path("pcc/frontends/python/codegen/runtime_abi.py").read_text(
        encoding="utf-8"
    )
    lowering = Path("pcc/frontends/python/codegen/exception_lowering.py").read_text(
        encoding="utf-8"
    )

    assert "const char *source_line;" in Path(
        "pcc/runtime/src/py_internal.h"
    ).read_text(encoding="utf-8")
    assert "void py_exc_append_frame_source(PyObject *exc," in header
    assert '"py_exc_append_frame_source": (' in abi
    assert "self.runtime[\"py_exc_append_frame_source\"]" in lowering
    assert "source_stream.read().splitlines()" in lowering

    assert "i: int = n_frames - 1" in py_src
    assert "while i >= 0:" in py_src
    assert "source_line = load_ptr(fr, 16)" in py_src


def test_nested_unhandled_traceback_is_outermost_first_with_source(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    source_path = tmp_path / "nested_traceback_order.py"
    source_path.write_text(
        "def leaf() -> None:\n"
        "    raise RuntimeError(\"boom\")\n"
        "\n"
        "def middle() -> None:\n"
        "    leaf()\n"
        "\n"
        "def outer() -> None:\n"
        "    middle()\n"
        "\n"
        "outer()\n",
        encoding="utf-8",
    )
    executable = tmp_path / "nested_traceback_order.out"
    compile_python(
        str(source_path),
        str(executable),
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
    )
    run = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert run.returncode == 1
    expected_frames = (
        f'File "{source_path}", line 10, in <module>',
        f'File "{source_path}", line 8, in outer',
        f'File "{source_path}", line 5, in middle',
        f'File "{source_path}", line 2, in leaf',
    )
    offsets = []
    for frame in expected_frames:
        assert frame in run.stderr
        offsets.append(run.stderr.index(frame))
    assert offsets == sorted(offsets)
    for source_line in (
        "    outer()\n",
        "    middle()\n",
        "    leaf()\n",
        '    raise RuntimeError("boom")\n',
    ):
        assert source_line in run.stderr


def test_unhandled_implicit_chain_keeps_the_original_failure(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    source_path = tmp_path / "implicit_exception_chain.py"
    source_path.write_text(
        "def wrap() -> None:\n"
        "    try:\n"
        "        raise ValueError(\"root failure\")\n"
        "    except ValueError:\n"
        "        raise RuntimeError(\"reported failure\")\n"
        "\n"
        "wrap()\n",
        encoding="utf-8",
    )
    executable = tmp_path / "implicit_exception_chain.out"
    compile_python(
        str(source_path),
        str(executable),
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
    )
    run = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert run.returncode == 1
    root = "ValueError: root failure"
    separator = (
        "During handling of the above exception, another exception occurred:"
    )
    reported = "RuntimeError: reported failure"
    for fragment in (root, separator, reported):
        assert fragment in run.stderr
    assert run.stderr.index(root) < run.stderr.index(separator)
    assert run.stderr.index(separator) < run.stderr.index(reported)


def test_runtime_contract_error_names_its_helper_in_an_innermost_frame():
    py_traceback = Path("pcc/runtime/py/py_exc_traceback.py").read_text(
        encoding="utf-8"
    )
    header = Path("pcc/runtime/include/py_runtime.h").read_text(
        encoding="utf-8"
    )
    abi = Path("pcc/frontends/python/codegen/runtime_abi.py").read_text(
        encoding="utf-8"
    )
    dispatch_py = Path("pcc/runtime/py/py_obj_ops_dispatch.py").read_text(
        encoding="utf-8"
    )
    func_py = Path("pcc/runtime/py/py_func.py").read_text(encoding="utf-8")

    assert "PyObject *py_runtime_error_if_unset(" in header
    assert '"py_runtime_error_if_unset": (_PYOBJ, [_CSTR, _CSTR], False)' in abi
    for source in (py_traceback,):
        assert "py_runtime_error_if_unset" in source
        assert '"<pcc runtime>"' in source
        assert "runtime contract: NULL result without an exception" in source
        assert "py_exc_append_frame_source" in source

    for source in (dispatch_py, func_py):
        assert "py_runtime_error_if_unset" in source
    for source in (dispatch_py,):
        assert '"py_tuple_new"' in source
        assert "bound method call could not allocate its argument tuple" in source
    assert 'cstr("py_func_bind_signature")' in func_py
    # The call body keeps the callee in owned slot 64; offset 72 is its name.
    assert "pcc_gc_store_root(ptr_add(slots, 64), callable_obj)" in func_py
    assert "entry_name = load_ptr(load_ptr(slots, 64), 72)" in func_py


def test_call_boundaries_set_or_preserve_the_callee_owned_exception():
    dispatch_py = Path("pcc/runtime/py/py_obj_ops_dispatch.py").read_text(
        encoding="utf-8"
    )
    func_py = Path("pcc/runtime/py/py_func.py").read_text(encoding="utf-8")
    capi_py = Path(
        "pcc/runtime/py/py_capi_object_call_runtime.py"
    ).read_text(encoding="utf-8")

    for source in (dispatch_py,):
        assert "py_obj_call received NULL callable" in source
        assert "returned NULL without setting an exception" in source
        assert "instance has no __call__ method" in source
        assert "py_obj_call_method1 received NULL object" in source
        assert "py_obj_call_method1 received NULL method name" in source
        assert "py_obj_call_method1 received NULL argument" in source
        assert "py_obj_call_method1 callee returned NULL" in source

    for source in (func_py,):
        assert "native function call received NULL callable" in source
        assert "native function object has no entry point" in source
        assert "compiled native function returned NULL without exception" in source

    contract_message = "py_obj_call returned NULL without setting an exception"
    for source in (capi_py,):
        assert contract_message in source
        call_pos = source.index("result = py_obj_call(callable, call_args, kwargs)")
        guard_pos = source.index("py_runtime_error_if_unset(", call_pos)
        cleanup_pos = source.index("py_decref(call_args)", call_pos)
        assert call_pos < guard_pos < cleanup_pos

    py_method_call = dispatch_py.index("out = call_ptr2(method, self_obj, a0)")
    py_method_guard = dispatch_py.index(
        "bound native method returned NULL without setting an exception",
        py_method_call,
    )
    py_method_cleanup = dispatch_py.index("py_decref(a0)", py_method_call)
    assert py_method_call < py_method_guard < py_method_cleanup

    for source, cleanup in ((dispatch_py, "if ptr_is_null(arg) == 0:"),):
        guard_pos = source.index(
            "native builtin constructor returned NULL without setting an exception"
        )
        cleanup_pos = source.index(cleanup, guard_pos)
        assert guard_pos < cleanup_pos

    for source, call, cleanup in ((
            dispatch_py,
            'out = py_obj_call(method, args, global_load_ptr("py_None"))',
            "py_decref(method)",
        ),):
        call_pos = source.index(call)
        guard_pos = source.index(
            "py_obj_call_method1 callee returned NULL without setting an exception",
            call_pos,
        )
        cleanup_pos = source.index(cleanup, call_pos)
        assert call_pos < guard_pos < cleanup_pos

    assert "pcc-Python bound native method supports at most one argument" in dispatch_py
    assert '_type_error(cstr("object is not callable"))' not in capi_py


def test_c_extension_pointer_slots_guard_silent_null_at_the_callback_boundary():
    port = Path("pcc/runtime/py/py_capi_cext_runtime.py").read_text(
        encoding="utf-8"
    )
    number_port = Path(
        "pcc/runtime/py/py_capi_number_runtime.py"
    ).read_text(encoding="utf-8")

    callback_contracts = (
        "tp_iter returned NULL without setting an exception",
        "tp_repr returned NULL without setting an exception",
        "mp_subscript returned NULL without setting an exception",
        "sq_item returned NULL without setting an exception",
        "tp_getattro returned NULL without setting an exception",
        "nb_absolute returned NULL without setting an exception",
        "tp_new returned NULL without setting an exception",
        "getset getter returned NULL without setting an exception",
    )
    for source in (port,):
        assert "py_runtime_error_if_unset" in source
        for message in callback_contracts:
            assert message in source

    # getattr owns the temporary name object.  Attribute callbacks must be
    # checked before that cleanup can run arbitrary deallocators and obscure
    # the original silent-NULL contract violation.
    for source, call, cleanup in ((port, "result = call_ptr2(getattro, o, name_obj)", "py_decref(name_obj)"),):
        call_pos = source.index(call)
        guard_pos = source.index(
            "tp_getattro returned NULL without setting an exception", call_pos
        )
        cleanup_pos = source.index(cleanup, call_pos)
        assert call_pos < guard_pos < cleanup_pos

    # tp_iternext is the one intentional exception: NULL without an error is
    # normal exhaustion and must become StopIteration, not RuntimeError.
    for source, start, end in ((
            port,
            "def pcc_capi_cext_object_next(",
            "def pcc_capi_cext_object_is_iterator(",
        ),):
        body = source[source.index(start) : source.index(end, source.index(start))]
        assert "StopIteration" in body
        assert "py_runtime_error_if_unset" not in body

    for source in (number_port,):
        call_pos = source.index("result = ", source.index("call_int_conversion_slot"))
        guard_pos = source.index(
            "integer conversion slot returned NULL without setting an exception",
            call_pos,
        )
        type_check_pos = source.index("is_intlike", call_pos)
        assert call_pos < guard_pos < type_check_pos


def test_c_extension_status_slots_guard_failure_before_owned_cleanup():
    port = Path("pcc/runtime/py/py_capi_cext_runtime.py").read_text(
        encoding="utf-8"
    )
    messages = (
        "mp_length returned a negative result without setting an exception",
        "sq_length returned a negative result without setting an exception",
        "tp_setattro returned failure without setting an exception",
        "getset setter returned failure without setting an exception",
        "tp_init returned failure without setting an exception",
    )
    for source in (port,):
        for message in messages:
            assert message in source

    for source, call, guard, cleanup in ((
            port,
            "call_ptr3(setattro, o, name_obj, value)",
            "tp_setattro returned failure without setting an exception",
            "py_decref(name_obj)",
        ), (
            port,
            "call_i64_ptr3(tp_init, result, args, call_kwargs)",
            "tp_init returned failure without setting an exception",
            "py_decref(result)",
        )):
        call_pos = source.index(call)
        guard_pos = source.index(guard, call_pos)
        cleanup_pos = source.index(cleanup, call_pos)
        assert call_pos < guard_pos < cleanup_pos

def test_user_protocol_dunder_calls_guard_silent_null_before_cleanup():
    protocol_py = Path(
        "pcc/runtime/py/py_protocol_runtime.py"
    ).read_text(encoding="utf-8")
    dunder_py = Path("pcc/runtime/py/py_dunder.py").read_text(
        encoding="utf-8"
    )

    # A missing method is a deliberate lookup sentinel.  Once a method was
    # found, however, both raw and PyFunc call paths must preserve its pending
    # exception or synthesize an attributed runtime-contract error.
    for source in (protocol_py,):
        assert "protocol_require_result" in source
        assert "user protocol argument tuple allocation failed" in source
        assert "user protocol callback returned NULL without an exception" in source

    for source, call, guard, cleanup in ((
            protocol_py,
            "result = py_func_call(method, args)",
            "_protocol_require_result(",
            "py_decref(args)",
        ),):
        call_pos = source.index(call)
        guard_pos = source.index(guard, call_pos)
        cleanup_pos = source.index(cleanup, call_pos)
        assert call_pos < guard_pos < cleanup_pos

    for source in (dunder_py,):
        assert "dunder_require_result" in source
        assert "user dunder argument tuple allocation failed" in source
        assert "user dunder callback returned NULL without an exception" in source

    for source, call, guard, cleanup in ((
            dunder_py,
            "out = py_func_call(func, args)",
            "_dunder_require_result(",
            "py_decref(args)",
        ),):
        call_pos = source.index(call)
        guard_pos = source.index(guard, call_pos)
        cleanup_pos = source.index(cleanup, call_pos)
        assert call_pos < guard_pos < cleanup_pos


def test_class_descriptor_callbacks_guard_silent_null_before_cleanup():
    class_py = Path("pcc/runtime/py/py_class.py").read_text(encoding="utf-8")
    for source in (class_py,):
        assert "require_result" in source
        assert "class callback argument tuple allocation failed" in source
        assert "class callback returned NULL without setting an exception" in source

    for source, call, guard, cleanup in ((
            class_py,
            "out = py_obj_call(fget, args, global_load_ptr(\"py_None\"))",
            "_class_require_result(",
            "py_decref(args)",
        ),):
        call_pos = source.index(call)
        guard_pos = source.index(guard, call_pos)
        cleanup_pos = source.index(cleanup, call_pos)
        assert call_pos < guard_pos < cleanup_pos


def test_format_and_copy_protocols_guard_silent_null_before_cleanup():
    format_py = Path("pcc/runtime/py/py_format_runtime.py").read_text(
        encoding="utf-8"
    )
    copy_py = Path(
        "pcc/runtime/py/py_pickle_copy_runtime.py"
    ).read_text(encoding="utf-8")

    for source in (format_py,):
        assert "format_require_result" in source
        assert "format callback argument tuple allocation failed" in source
        assert "format callback returned NULL without setting an exception" in source
    for source in (copy_py,):
        assert "copy_require_result" in source
        assert "copy callback argument tuple allocation failed" in source
        assert "copy callback returned NULL without setting an exception" in source

    # The format callback runs in a slot body: its result is checked there,
    # and the wrapper drops the argument slots only after the body returns.
    body = format_py.split("def _format_callback_body(", 1)[1].split("\ndef ", 1)[0]
    call_pos = body.index("py_obj_call(\n        load_ptr(slots, _FORMAT_METHOD * C_POINTER_SIZE),")
    guard_pos = body.index('cstr("format callback returned NULL without setting an exception")', call_pos)
    assert call_pos < guard_pos
    wrapper = format_py.split("def _call_object_format(", 1)[1].split("\ndef ", 1)[0]
    assert wrapper.index("_format_callback_body(slots, tokens, borrowed)") < wrapper.index(
        "_format_callback_drop(slots, tokens, _FORMAT_ARGS)"
    )
    call_pos = copy_py.index("result = py_func_call(method, full_args)")
    guard_pos = copy_py.index("_copy_require_result(", call_pos)
    cleanup_pos = copy_py.index("py_decref(full_args)", call_pos)
    assert call_pos < guard_pos < cleanup_pos


def test_weakref_callback_is_explicit_unraisable_owned_result_boundary():
    py_source = Path("pcc/runtime/py/py_weakref.py").read_text(
        encoding="utf-8"
    )

    for source, call, result_cleanup, args_cleanup, clear in ((
            py_source,
            "result = py_obj_call(callback, args, _py_none())",
            "py_decref(result)",
            "py_decref(args)",
            "py_clear_exception()",
        ),):
        call_pos = source.index(call)
        result_cleanup_pos = source.index(result_cleanup, call_pos)
        args_cleanup_pos = source.index(args_cleanup, result_cleanup_pos)
        clear_pos = source.index(clear, args_cleanup_pos)
        assert call_pos < result_cleanup_pos < args_cleanup_pos < clear_pos
        assert "unraisable boundary" in source[call_pos - 500 : clear_pos]


def test_splat_call_boundaries_attribute_silent_null_before_cleanup():
    py_source = Path("pcc/runtime/py/py_call_splat_runtime.py").read_text(
        encoding="utf-8"
    )

    messages = (
        "call splat could not allocate the base argument tuple",
        "call splat could not allocate the merged argument tuple",
        "call splat could not read a base positional argument",
        "call splat could not read a starred positional argument",
        "zip splat could not read an input row",
        "zip splat row length failed without setting an exception",
        "zip splat element lookup failed without setting an exception",
        "call splat could not allocate the merged keyword dictionary",
        "call splat could not merge positional arguments",
        "call splat could not merge keyword arguments",
        "call splat callee returned NULL without setting an exception",
    )
    for source in (py_source,):
        assert "py_runtime_error_if_unset" in source
        for message in messages:
            assert message in source

    for source, call, guard, cleanup in ((
            py_source,
            "out = py_obj_call(callable_obj, args, kwargs)",
            "call splat callee returned NULL without setting an exception",
            "py_decref(args)",
        ), (
            py_source,
            "out = py_tuple_new(base_len + star_len)",
            "call splat could not allocate the merged argument tuple",
            "py_decref(base_tuple)",
        )):
        call_pos = source.index(call)
        guard_pos = source.index(guard, call_pos)
        cleanup_pos = source.index(cleanup, call_pos)
        assert call_pos < guard_pos < cleanup_pos
