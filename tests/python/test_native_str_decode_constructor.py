"""``str(object, encoding[, errors])`` is an owned value for any consumer.

The two- and three-argument str() constructor decoded into a pinned SSA
value returned after its operands' cleanup, so as a call or method receiver
(``str(data, "ascii").lower()``, as test_codecs and test_zlib do) the
compile failed with "slot-call Call lacks an immediate owned-result
handoff". It now decodes into the caller's output root with owned inputs,
as bytes.decode() does.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


PROGRAM = r'''
def take(text):
    return text.upper()


def failure(*args):
    try:
        str(*args)
    except (TypeError, UnicodeDecodeError, LookupError) as caught:
        return type(caught).__name__ + ": " + str(caught)
    return "no error"


def main():
    data = "café au lait".encode("utf-8")
    print(str(data, "utf-8").title(), take(str(b"abc", "ascii")))
    print(str(b"\xff-x", "ascii", "replace"), len(str(data, "latin-1")))
    print([str(item, "ascii").lower() for item in (b"A", b"B")])
    print(failure(b"\xff", "ascii"))
    print(failure("text", "ascii"))
    print(failure(b"x", "no-such-codec"))


main()
'''


@pytest.mark.integration
def test_str_decode_constructor_as_operand_on_five_collectors(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "str_decode.py"
    source.write_text(PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "str_decode"
    python_program_compiler(
        str(source), str(binary), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        environment = dict(os.environ, PCC_GC_BACKEND=str(backend))
        environment.pop("LC_ALL", None)
        ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60, env=environment)
        assert ran.returncode == 0, f"GC{backend}: {ran.stdout}{ran.stderr}"
        assert ran.stdout == expected.stdout, f"GC{backend}: {ran.stdout}"
