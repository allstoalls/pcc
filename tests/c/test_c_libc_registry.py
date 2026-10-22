from __future__ import annotations

import pytest

from pcc.frontends.c.c_libc_registry import (
    LibcSignature,
    iter_signatures,
    lookup_signature,
    register_signature,
    validate_registry,
)


def test_registry_contains_platform_specific_errno_without_duplicates():
    validate_registry()
    assert lookup_signature("__errno_location", "linux") is not None
    assert lookup_signature("__errno_location", "darwin") is None
    assert lookup_signature("__error", "darwin") is not None
    assert lookup_signature("__error", "linux") is None


def test_common_libc_signatures_have_headers_and_types():
    for name in ("malloc", "free", "memcpy", "printf", "open", "read", "write"):
        sig = lookup_signature(name)
        assert sig is not None
        assert sig.header
        assert sig.return_type


def test_duplicate_registration_requires_replace():
    with pytest.raises(ValueError, match="duplicate"):
        register_signature(LibcSignature("malloc", "void*", ("size_t",), "stdlib.h"))

    register_signature(
        LibcSignature("pcc_test_custom", "int", ("int",), "pcc_test.h"),
        replace=False,
    )
    assert lookup_signature("pcc_test_custom").header == "pcc_test.h"


def test_platform_iteration_filters_results():
    linux = {sig.name for sig in iter_signatures("linux")}
    darwin = {sig.name for sig in iter_signatures("darwin")}
    assert "__errno_location" in linux
    assert "__errno_location" not in darwin
    assert "__error" in darwin
