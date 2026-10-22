from __future__ import annotations

from pathlib import Path


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "pcc" / "runtime" / "py" / "py_coroutine.py").exists():
            return parent
    raise RuntimeError("could not locate pcc repository root")


REPO_ROOT = _repo_root()


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_current_coroutine_runtime_is_synchronous_no_suspension_subset():
    """Baseline for No.42: today's coroutine object is not a continuation."""

    py_src = _read("pcc/runtime/py/py_coroutine.py")

    assert (
        'return py_coroutine_new_native(cstr("sleep"), null(), null(), null())'
        in py_src
    )


def test_virtual_thread_continuation_object_model_exists():
    runtime_header = _read("pcc/runtime/include/py_runtime.h")
    coroutine_py = _read("pcc/runtime/py/py_coroutine.py")
    abi = _read("pcc/frontends/python/codegen/runtime_abi.py")

    assert "PyContinuationObject" in runtime_header
    assert "py_continuation_new" in runtime_header
    assert "py_continuation_mount" in runtime_header
    assert "py_continuation_unmount" in runtime_header
    assert '@c_abi_export("py_continuation_new")' in coroutine_py
    assert '"py_continuation_unmount": (_I64, [_PYOBJ, _PTR, _PTR], False)' in abi


def test_virtual_thread_scheduler_api_exists():
    runtime_header = _read("pcc/runtime/include/py_runtime.h")

    assert "py_virtual_thread_new" in runtime_header
    assert "py_virtual_thread_start" in runtime_header
    assert "py_virtual_thread_park" in runtime_header
    assert "py_virtual_thread_unpark" in runtime_header
    assert "py_virtual_thread_run_once" in runtime_header
    assert "py_virtual_thread_run_until_idle" in runtime_header
    assert "py_virtual_thread_run_carrier_pool" in runtime_header


def test_gc_has_suspended_continuation_root_hooks():
    """No.42 Phase 2: runtime can expose suspended-frame root hooks.

    This is not the continuation object/stack-chunk runtime. It only locks the
    GC-facing root-map surface that a future continuation object will use.
    """

    runtime_header = _read("pcc/runtime/include/py_runtime.h")

    assert "pcc_gc_trace_continuation_roots" in runtime_header
    assert "pcc_gc_register_continuation_root" in runtime_header
    assert "pcc_gc_rewrite_continuation_roots" in runtime_header
    assert "PY_TYPE_CONTINUATION" in runtime_header


def test_virtual_thread_blocking_and_poller_api_exists():
    runtime_header = _read("pcc/runtime/include/py_runtime.h")
    abi = _read("pcc/frontends/python/codegen/runtime_abi.py")

    assert "py_virtual_thread_sleep" in runtime_header
    assert "py_virtual_thread_poll_timers" in runtime_header
    assert "py_virtual_thread_timer_count" in runtime_header
    assert "py_virtual_thread_block_on_fd" in runtime_header
    assert "py_virtual_thread_poll_io" in runtime_header
    assert "py_virtual_thread_io_wait_count" in runtime_header
    assert "py_virtual_thread_pin_enter" in runtime_header
    assert "py_virtual_thread_pin_leave" in runtime_header
    assert "py_virtual_thread_pin_event_count" in runtime_header
    assert '"py_virtual_thread_sleep": (_I64, [_PYOBJ, _I64], False)' in abi
    assert '"py_virtual_thread_run_once": (_I64, [], False)' in abi
    assert (
        '"py_virtual_thread_block_on_fd": (_I64, [_PYOBJ, _I64, _I64, _I64], False)'
        in abi
    )
