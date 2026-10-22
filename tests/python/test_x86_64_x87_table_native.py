"""Native checks for the source-derived x87 prefix and whitespace predicate.

Production tables, result classes, function ABI and changed lookup branches
are copied from source. Unrelated encoder paths are explicitly excluded, so
these controls do not claim a complete native encoder or self-host fixed point.
The actual operand-name predicate and same-runtime preimage share this one
compilation and existing five-collector execution; no extra native node runs.
"""

import pytest

from tests.python.owned_regression_support import (
    assert_owned_program,
    explicit_owned_runtime,
)
from tests.x86_64_x87_source import x87_native_program


@pytest.mark.integration
@pytest.mark.parametrize("python_program_compiler", ("pcc0",), indirect=True)
def test_x87_tables_native_five_gc(
    tmp_path, python_program_compiler, request, explicit_owned_runtime, capfd,
):
    """Host-pcc0 native output under five collectors; not native-pcc1 evidence."""
    from scripts.verify_nolibpython import linked_libraries

    mode = request.node.callspec.params["python_program_compiler"]
    assert_owned_program(
        x87_native_program(), "X87_TABLE_GLOBALS_OK\n", tmp_path,
        python_program_compiler, mode, explicit_owned_runtime, capfd,
        provenance_probe="2",
    )
    payload = (tmp_path / "program.out").read_bytes()
    assert payload[:4] in (b"\x7fELF", b"\xcf\xfa\xed\xfe") or payload[:2] == b"MZ"
    if payload[:4] == b"\x7fELF":
        from pcc.backend.elf_x86_64 import parse_static_executable

        # The owned Linux artifact has no dynamic loader or dynamic table.
        # Validate its real program headers; an empty section-based library
        # list is not evidence that an arbitrary ELF image is independent.
        parse_static_executable(payload)
    else:
        assert not any("python" in name.lower() or "llvm" in name.lower()
                       for name in linked_libraries(payload))
