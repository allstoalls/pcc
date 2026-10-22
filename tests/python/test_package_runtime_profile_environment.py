"""Runtime-policy changes must reuse one pcc package environment.

Exit contract for PKG-P1-RUNTIME-PROFILE-ENVIRONMENT-INVARIANCE: GC0..4,
LLVM/self, threaded-runtime, virtual-thread-scheduler, and accelerator
policy switches preserve one environment identity, identical
installed-artifact digests, and zero acquisition/rebuild work.
"""

from __future__ import annotations

from pathlib import Path

import pcc.package.environment as package_environment
import pcc.package.uv_lock_sync as uv_lock_sync
from pcc.package.runtime_profile import (
    RUNTIME_PROFILE_ENV_VARS,
    RUNTIME_PROFILE_SCHEMA,
    runtime_profile,
)
from pcc.package.environment import resolve_package_environment

from tests.python.package_environment_profile_contract import (
    assert_profile_environment_invariance,
    base_environment,
    identity_fields,
)

COMBINED_PROFILE = {
    "PCC_GC_BACKEND": "4",
    "PCC_REFCOUNT_KIND": "cycle",
    "PCC_BACKEND": "self",
    "PCC_WITH_THREADS": "1",
    "PCC_VTHREAD_PARKED": "1",
    "PCC_GPU_BACKEND": "metal",
    "PCC_METAL": "1",
    "PCC_DS": "1",
}


def test_each_runtime_profile_axis_preserves_environment_identity(tmp_path):
    env = base_environment(tmp_path)
    baseline = resolve_package_environment(env)
    for name in RUNTIME_PROFILE_ENV_VARS:
        profiled = dict(env)
        profiled[name] = COMBINED_PROFILE[name]
        report = resolve_package_environment(profiled)
        assert report == baseline, f"{name} must not key environment identity"


def test_combined_profile_switch_is_zero_work_with_identical_digests(tmp_path):
    assert_profile_environment_invariance(tmp_path, COMBINED_PROFILE)


def test_llvm_and_self_backend_owners_share_one_environment(tmp_path):
    env = base_environment(tmp_path)
    llvm = dict(env)
    llvm["PCC_BACKEND"] = "llvm"
    self_backend = dict(env)
    self_backend["PCC_BACKEND"] = "self"
    assert identity_fields(resolve_package_environment(llvm)) == (
        identity_fields(resolve_package_environment(self_backend))
    )


def test_runtime_profile_reports_declared_axes():
    profile = runtime_profile(COMBINED_PROFILE)
    assert profile["schema"] == RUNTIME_PROFILE_SCHEMA
    assert profile["values"] == COMBINED_PROFILE
    assert runtime_profile({})["values"] == {
        name: "" for name in RUNTIME_PROFILE_ENV_VARS
    }


def test_environment_and_sync_key_sources_never_read_profile_axes():
    """Source-level lock: identity/sync/build keys stay policy-free."""

    for module in (package_environment, uv_lock_sync):
        source = Path(module.__file__).read_text(encoding="utf-8")
        for name in RUNTIME_PROFILE_ENV_VARS:
            assert name not in source, f"{module.__name__} reads {name}"
