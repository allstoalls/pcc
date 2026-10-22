from __future__ import annotations

from pathlib import Path


def test_b1_b6_runtime_symbols_stay_wired():
    header = Path("pcc/runtime/include/py_runtime.h").read_text(encoding="utf-8")
    abi = Path("pcc/frontends/python/codegen/runtime_abi.py").read_text(encoding="utf-8")
    py_dispatch = Path("pcc/runtime/py/py_obj_ops_dispatch.py").read_text(encoding="utf-8")
    dunder_py = Path("pcc/runtime/py/py_dunder.py").read_text(encoding="utf-8")

    # B1 bytes
    assert "py_bytes_new" in header
    assert "py_bytes_getitem" in header
    assert "py_bytes_slice" in header

    # B2 type builtin
    assert "py_type_builtin" in header
    assert '"py_type_builtin": (_PYOBJ, [_PYOBJ], False)' in abi

    # B3 class variables
    assert "py_class_getattr" in abi
    assert "py_class_setattr" in abi
    assert "py_class_getattr" in py_dispatch

    # B4 user dunders
    for sym in [
        "py_user_str_dispatch",
        "py_user_hash_dispatch",
        "py_user_iter_dispatch",
        "py_user_next_dispatch",
    ]:
        assert sym in header
        assert sym in abi
        assert f'@c_abi_export("{sym}")' in dunder_py

    # B6 call splat / module attrs
    for sym in [
        "py_call_merge_posargs",
        "py_call_merge_kwargs",
        "py_obj_call_splat",
        "py_module_attr_set",
        "py_module_attr_get",
    ]:
        assert sym in header
        assert sym in abi


def test_b5_exception_accessors_stay_wired():
    header = Path("pcc/runtime/include/py_runtime.h").read_text(encoding="utf-8")
    abi = Path("pcc/frontends/python/codegen/runtime_abi.py").read_text(encoding="utf-8")
    exc_py = Path("pcc/runtime/py/py_exc_objects.py").read_text(encoding="utf-8")

    for sym in ["py_exc_get_cause", "py_exc_get_context", "py_exc_traceback_len"]:
        assert sym in header
        assert sym in abi
        assert sym in exc_py
