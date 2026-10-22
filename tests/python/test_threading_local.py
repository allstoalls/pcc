from __future__ import annotations

import inspect


def test_threading_local_class_shape():
    """Static check: ``threading.local`` exposes the explicit
    get/set/delete API and an instance ``_storage`` initializer.

    pcc's ``local`` is extern-bound (uses ``get_ident()`` from this same
    module, which is a runtime extern not callable from interpreted
    Python). A full-behavior smoke test would have to compile-and-run a
    program under pcc; that path is covered by other native threading
    tests once the no-libpython pipeline can lower per-thread dict
    storage. For now we lock the API surface so the patch cannot be
    silently regressed.
    """
    from pcc.stdlib.threading import local

    assert inspect.isclass(local)
    for method in ("get", "set", "delete", "_dict"):
        assert hasattr(local, method), method
    init_src = inspect.getsource(local.__init__)
    assert "_storage" in init_src
