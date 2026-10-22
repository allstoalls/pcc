"""Native low-level thread primitives used by standard-library consumers."""

from pcc.extern import c_int64, c_ptr, extern, c_obj

_get_ident = extern("py_threading_get_ident", (), c_int64)
_box_int = extern("py_int_from_i64", (c_int64,), c_obj)


def get_ident():
    return _box_int(_get_ident())
