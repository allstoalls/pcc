from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).absolute().parents[2]


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def test_exception_runtime_log_points_are_mirrored_between_c_and_pcc_python():
    py_tls = _read("pcc/runtime/py/py_exc_tls.py")
    py_obj = _read("pcc/runtime/py/py_exc_objects.py")

    for needle in ["6, 3", "6, 4"]:
        assert needle in py_tls
    for needle in ["6, 1", "6, 2", "6, 5", "6, 6", "6, 7"]:
        assert needle in py_obj


def test_dispatch_runtime_log_points_are_mirrored_between_c_and_pcc_python():
    py_src = (
        _read("pcc/runtime/py/py_obj_ops_dispatch.py")
        + _read("pcc/runtime/py/py_obj_ops_slice.py")
    )
    for event_code in range(1, 10):
        needle = f"7, {event_code}"
        assert needle in py_src


