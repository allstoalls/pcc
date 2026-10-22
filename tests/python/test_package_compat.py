from __future__ import annotations

import pytest

from pcc.package.compat import (
    LEVEL_C_EXTENSION_ABI,
    LEVEL_NOLIBPYTHON_PYTHON,
    get_package_target,
    iter_package_targets,
    level_name,
)


def test_extension_targets_are_abi_targets_before_acceleration_claims():
    for name in (
        "numpy",
        "mlx",
        "vllm",
        "tilelang",
        "vllm-metal",
        "cffi",
        "pybind11",
    ):
        target = get_package_target(name)
        assert target is not None
        assert target.level == LEVEL_C_EXTENSION_ABI
        assert target.summary


def test_requests_is_pure_python_nolibpython_target():
    requests = get_package_target("requests")
    assert requests is not None
    assert requests.level == LEVEL_NOLIBPYTHON_PYTHON


def test_all_targets_have_names_and_valid_level_names():
    for target in iter_package_targets():
        assert target.name
        assert level_name(target.level)


def test_unknown_level_raises():
    with pytest.raises(ValueError):
        level_name(999)
