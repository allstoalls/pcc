"""Phase 4c.6a: pcc-Python port of py_exc_match.c.

Ports py_exc_matches — the hot MRO-aware class matcher used by every
try/except handler dispatch. Traceback frame growth and unhandled
stderr formatting live in sibling py_exc_traceback.py.

PyExceptionObject layout (offset 16 -> exc_class).

Public object type tags and class/instance layout offsets come from the
generated ``py_abi_constants`` module.
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import PYCLASSOBJECT_MRO_OFFSET, PYINSTANCEOBJECT_CLS_OFFSET, PY_TYPE_CLASS, PY_TYPE_EXC, PY_TYPE_INSTANCE, PY_TYPE_INT, PY_TYPE_USER_CLASS_START
from pcc.runtime.py.py_abi_constants import PYTUPLEOBJECT_ITEMS_OFFSET, PYTUPLEOBJECT_LEN_OFFSET, PY_TYPE_TUPLE
from pcc.extern import c_abi_export, c_ptr, c_int64, c_void, extern
from pcc.unsafe import (
    cstr,
    global_addr,
    is_tagged_int,
    load_i64,
    load_i32,
    load_ptr,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
)

pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_gc_note_relocation_read = extern("pcc_gc_note_relocation_read", (c_ptr,), c_ptr)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)


def _type_of(obj) -> int:
    if is_tagged_int(obj):
        return PY_TYPE_INT
    return load_i32(obj, 8)


def _to_class(obj):
    """Project a PyObject* down to its PyClassObject* form.
    Returns NULL if not usable as a class key."""
    if ptr_is_null(obj):
        return null()
    if is_tagged_int(obj):
        return null()
    obj = pcc_gc_note_relocation_read(obj)
    tag: int = load_i32(obj, 8)
    if tag == PY_TYPE_CLASS:                         # PY_TYPE_CLASS
        return obj
    if tag == PY_TYPE_EXC:                         # PY_TYPE_EXC
        return pcc_gc_load_ptr(obj, ptr_add(obj, 16))   # ->exc_class
    if tag == PY_TYPE_INSTANCE or tag >= PY_TYPE_USER_CLASS_START:
        # A raised user exception subclass is a PyInstanceObject; its ``cls``
        # is at offset 16 (right after the 16-byte header), same slot as
        # PyExceptionObject->exc_class. Project to it so the MRO walk matches
        # ``except MyError`` / ``except Exception``.
        return pcc_gc_load_ptr(
            obj, ptr_add(obj, PYINSTANCEOBJECT_CLS_OFFSET)
        )
    return null()


@c_abi_export("py_exc_matches")
def py_exc_matches(exc, type_) -> int:
    ecls = _to_class(exc)
    tcls = _to_class(type_)
    if ptr_is_null(ecls):
        return 0
    if ptr_is_null(tcls):
        return 0
    mro = load_ptr(ecls, PYCLASSOBJECT_MRO_OFFSET)
    if ptr_is_null(mro):
        if ptr_eq(ecls, tcls):
            return 1
        return 0
    n_mro: int = load_i32(ecls, 40)
    i: int = 0
    while i < n_mro:
        entry = pcc_gc_load_ptr(ecls, ptr_add(mro, i * 8))
        if ptr_eq(entry, tcls):
            return 1
        i = i + 1
    return 0


def _handler_class_valid(cls) -> int:
    # A handler must be a class, never an exception instance. The cache holds
    # the canonical BaseException identity; absence means that no exception
    # subclass has been constructed yet. Read it without a lazy allocation.
    if ptr_is_null(cls):
        return 0
    cls = pcc_gc_note_relocation_read(cls)
    if _type_of(cls) != PY_TYPE_CLASS:
        return 0
    base = pcc_gc_load_ptr(null(), global_addr("py_exc_classes"))
    if ptr_is_null(base):
        return 0
    return py_exc_matches(cls, base)


@c_abi_export("py_exc_match_handler")
def py_exc_match_handler(exc, type_) -> int:
    """Validate a flat handler tuple completely, then match its classes.

    Keep py_exc_matches' instance projection for its existing C-API/runtime
    callers. Except clauses have the stricter Python class-only contract.
    No allocation occurs on the matching path; the error path stops using
    borrowed arguments before constructing and raising TypeError.
    """
    valid: int = 1
    matched: int = 0
    if ptr_is_null(type_):
        valid = 0
    else:
        type_ = pcc_gc_note_relocation_read(type_)
        if _type_of(type_) == PY_TYPE_TUPLE:
            count: int = load_i64(type_, PYTUPLEOBJECT_LEN_OFFSET)
            i: int = 0
            while i < count:
                member = pcc_gc_load_ptr(type_, ptr_add(type_, PYTUPLEOBJECT_ITEMS_OFFSET + i * 8))
                if _handler_class_valid(member) == 0:
                    valid = 0
                    break
                if py_exc_matches(exc, member) != 0:
                    matched = 1
                i = i + 1
        else:
            valid = _handler_class_valid(type_)
            if valid != 0:
                matched = py_exc_matches(exc, type_)
    if valid == 0:
        error = py_exc_new(3, cstr("catching classes that do not inherit from BaseException is not allowed"))
        py_raise_owned(error)
        return -1
    return matched
