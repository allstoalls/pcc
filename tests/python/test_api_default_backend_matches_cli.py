"""`pcc.api.build` must default to the same backend the CLI ships.

`api.build(backend=None)` resolved to the llvm backend while the CLI defaults
to `DEFAULT_PUBLIC_BACKEND` ("self"), so the two emitted different objects for
the same source by construction -- which is exactly what
`tests/c/test_api_cli_object_parity.py` exists to catch, and it also meant the
public Python API pulled in llvmlite on its default path.

`CEvaluator()`'s own default stays llvm on purpose: the C suite uses it as the
differential oracle.  The contract here is about the *public build API*.
"""

from __future__ import annotations

import inspect

from pcc import api
from pcc.cli_contract import DEFAULT_PUBLIC_BACKEND


def test_public_build_api_defaults_to_the_cli_backend():
    resolved = api._resolve_public_backend(None)
    assert resolved == DEFAULT_PUBLIC_BACKEND


def test_explicit_backend_is_still_honoured():
    assert api._resolve_public_backend("llvm") == "llvm"
    assert api._resolve_public_backend("self") == "self"


def test_build_signature_keeps_backend_optional():
    signature = inspect.signature(api.build)
    assert signature.parameters["backend"].default is None
