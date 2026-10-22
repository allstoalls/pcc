from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).absolute().parents[2]


def _read(rel: str) -> str:
    return _abi_values((ROOT / rel).read_text(encoding="utf-8"))


def _abi_values(text: str) -> str:
    """Spell ABI constant names as their values, so a port may name them."""
    import re
    from pcc.runtime.py import py_abi_constants as abi

    values = {name: str(value) for name, value in vars(abi).items()
              if name.isupper() and type(value) is int}
    return re.sub(r"\b[A-Z][A-Z0-9_]+\b", lambda m: values.get(m.group(0), m.group(0)), text)


def test_hot_container_constructors_use_pcc_gc_alloc_in_pcc_python_ports():
    expectations = {
        "pcc/runtime/py/py_list.py": "pcc_gc_alloc(40, 5, 0)",
        "pcc/runtime/py/py_tuple.py": "pcc_gc_alloc(bytes_total, 7, 0)",
        "pcc/runtime/py/py_dict.py": "pcc_gc_alloc(56, 6, 0)",
        "pcc/runtime/py/py_set.py": "pcc_gc_alloc(48, 8, 0)",
    }
    for rel, needle in expectations.items():
        text = _read(rel)
        assert 'extern("pcc_gc_alloc"' in text
        assert needle in text, f"{rel} port bypasses pcc_gc_alloc"


def test_split_string_accessors_allocate_strings_through_gc():

    py_text = _read("pcc/runtime/py/py_str_accessors.py")
    assert 'extern("pcc_gc_alloc"' in py_text
    assert "pcc_gc_alloc(40 + byte_len + 1, 4, 0)" in py_text
    assert "s = malloc(40 + byte_len + 1)" not in py_text


def test_native_log_gate_expects_alloc_object_tags_for_main_containers():
    text = _read("tests/python/test_runtime_log_native_binary.py")
    assert "assert {5, 6, 7, 8}.issubset(alloc_tags)" in text
