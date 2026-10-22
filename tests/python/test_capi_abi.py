from pcc.frontends.c.capi_abi import extension_import_blockers, symbols_by_category


def test_refcount_symbols_grouped():
    assert {s.name for s in symbols_by_category("refcount")} == {"Py_INCREF", "Py_DECREF"}


def test_extension_import_blockers_ignore_implemented():
    assert "Py_INCREF" not in {s.name for s in extension_import_blockers({"Py_INCREF"})}
