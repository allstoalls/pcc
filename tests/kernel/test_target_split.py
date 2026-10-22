"""K-P0-TARGET-SPLIT — TargetMachine resolution + the no-fallback rule.

Asserts the REAL invariants: host/device resolve correctly, --backend=self
NEVER silently falls back to LLVM, and no device finalize runs during a
host-only compile.
"""

import pytest

from pcc.kernel_ir.target_split import (
    DeviceTarget,
    HostBackend,
    TargetSplitError,
    assert_no_device_finalize_during_host_only,
    resolve,
)


def test_resolve_self_none():
    m = resolve("self", "none")
    assert m.host.backend is HostBackend.SELF
    assert m.device.device is DeviceTarget.NONE
    assert m.host.first_class_root is True
    assert m.runs_device_finalize is False


def test_resolve_self_metal():
    m = resolve("self", "metal")
    assert m.host.backend is HostBackend.SELF
    assert m.device.device is DeviceTarget.METAL
    assert m.runs_device_finalize is True
    plan = m.finalize_plan()
    assert plan["host_finalize"] == "self_host_finalize"
    assert plan["device_finalize"] == "metal_device_finalize"


def test_resolve_c_reference_host():
    c = resolve("c", "metal")
    assert c.host.host_finalize == "c_host_finalize"
    assert c.host.first_class_root is False  # c is not a first-class root


def test_shared_front_half_is_target_neutral():
    # Both a device build and a host-only build share the identical front half.
    a = resolve("self", "metal").shared_front_half
    b = resolve("c", "none").shared_front_half
    assert a == b
    assert "plain_tir_freeze" in a
    assert "split_host_device" in a


def test_removed_llvm_host_is_rejected():
    with pytest.raises(TargetSplitError, match="unknown host backend"):
        resolve("llvm", "none")


def test_removed_llvm_fallback_option_is_rejected():
    with pytest.raises(TypeError, match="allow_llvm_fallback"):
        resolve("self", "none", allow_llvm_fallback=True)


def test_host_only_schedules_no_device_finalize():
    m = resolve("self", "none")
    # Invariant guard does not raise for a correct host-only machine.
    assert_no_device_finalize_during_host_only(m) is None
    assert m.finalize_plan()["device_finalize"] is None


def test_unknown_host_and_device_raise():
    with pytest.raises(TargetSplitError, match="unknown host backend"):
        resolve("cuda_host", "none")
    with pytest.raises(TargetSplitError, match="unknown device target"):
        resolve("self", "webgpu")


def test_enum_inputs_accepted():
    m = resolve(HostBackend.SELF, DeviceTarget.METAL)
    assert m.runs_device_finalize is True
