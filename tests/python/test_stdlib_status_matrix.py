from __future__ import annotations

from pcc.diagnostics.stdlib_status import (
    MODE_COMPAT_BRIDGE,
    MODE_NATIVE_DYNAMIC,
    MODE_NATIVE_STATIC,
    MODE_UNSUPPORTED,
    get_status,
    iter_statuses,
    validate_statuses,
)


def test_stdlib_status_matrix_is_valid_and_nonempty():
    validate_statuses()
    statuses = iter_statuses()
    assert statuses
    assert {s.mode for s in statuses} >= {
        MODE_NATIVE_DYNAMIC,
        MODE_NATIVE_STATIC,
        MODE_COMPAT_BRIDGE,
        MODE_UNSUPPORTED,
    }


def test_core_native_modules_have_test_pointers():
    for name in ("sys", "os", "gc", "weakref", "threading"):
        status = get_status(name)
        assert status.mode == MODE_NATIVE_DYNAMIC
        assert status.tests, f"{name} should point at real behavior tests"


def test_unknown_module_defaults_to_bridge_not_native_claim():
    status = get_status("totally_missing_module")
    assert status.mode == MODE_COMPAT_BRIDGE
    assert "not in pcc native matrix" in status.summary
