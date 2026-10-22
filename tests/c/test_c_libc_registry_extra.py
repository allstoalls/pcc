from pcc.frontends.c.c_libc_registry_extra import EXTRA_SIGNATURES


def test_extra_signatures_are_named():
    names = {sig.name for sig in EXTRA_SIGNATURES}
    assert {"calloc", "realloc", "dlopen", "dlsym"} <= names
    assert all(sig.header for sig in EXTRA_SIGNATURES)
