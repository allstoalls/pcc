from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).absolute().parents[2]


def _text(path: str) -> str:
    return _abi_values((ROOT / path).read_text(encoding="utf-8"))


def _abi_values(text: str) -> str:
    """Spell ABI constant names as their values, so a port may name them."""
    import re
    from pcc.runtime.py import py_abi_constants as abi

    values = {name: str(value) for name, value in vars(abi).items()
              if name.isupper() and type(value) is int}
    return re.sub(r"\b[A-Z][A-Z0-9_]+\b", lambda m: values.get(m.group(0), m.group(0)), text)


def test_pcc_py_scalar_and_object_constructors_route_through_pcc_gc_alloc():
    checks = {
        "pcc/runtime/py/py_str.py": ["pcc_gc_alloc(40 + byte_len + 1, 4, 0)"],
        "pcc/runtime/py/py_obj_stubs.py": [
            "pcc_gc_alloc(24, 3, 0)",
            "pcc_gc_alloc(32, 16, 0)",
            "pcc_gc_alloc(24 + byte_len + 1, 17, 0)",
            "pcc_gc_alloc(24 + byte_len + 1, 18, 0)",
            # memoryview: header, base and the field at offset 24.
            "pcc_gc_alloc(32, 19, 0)",
        ],
        # ``PyClassObject`` is 120 bytes — header (24) + name (8) +
        # bases (8) + n_bases (8) + methods (8) + n_methods (8) +
        # del_method (8) + attrs (8) + metaclass (8) + various other
        # slots. The metaclass field at offset 112 brought the total
        # to 120 (was 112 before that field was added). Verified
        # against ``pcc/runtime/include/py_runtime.h`` and
        # ``pcc/runtime/src/py_internal.h::PyClassObject``.
        "pcc/runtime/py/py_class.py": [
            "pcc_gc_alloc(120, 10, 0)",
            "pcc_gc_alloc(size, load_i32(cls, 92), 0)",
        ],
        "pcc/runtime/py/py_weakref.py": ["pcc_gc_alloc(48, 21, 0)"],
        "pcc/runtime/py/py_exc_objects.py": ["pcc_gc_alloc(64, 12, 0)"],
    }
    def compact(code: str) -> str:
        # Call layout (line breaks, a trailing comma) is not the contract.
        return "".join(code.split()).replace(",)", ")")

    for path, needles in checks.items():
        text = compact(_text(path))
        for needle in needles:
            assert compact(needle) in text, f"{needle!r} missing from {path}"


