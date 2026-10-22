"""Execute the unchanged dynamic-power comparison functions against CPython.

The five runtime selectors are requested modes, not observed-collection proof.
"""
from __future__ import annotations

import pytest

from tests.python.owned_regression_support import (
    assert_owned_program,
    explicit_owned_runtime,
)
from tests.python.test_dynamic_power_comparison_boundary import (
    FORMS,
    LIFETIME_SOURCE,
    OPS,
    matrix_source,
)


def native_program():
    chunks = [matrix_source(form) for form in FORMS]
    chunks.append(LIFETIME_SOURCE)
    calls = ["def main():"]
    values = (
        (2, 2, 8.0), (2, -2, 1.0), (2, 80, float(1 << 80)),
        (3, 5, 244.0), (2, 63, (1 << 63) - 1), (2, 63, 1 << 63),
        (2, 63, (1 << 63) + 1), (2, 53, (1 << 53) - 1),
        (2, 53, 1 << 53), (2, 53, (1 << 53) + 1),
    )
    for form in FORMS:
        for index, (base, exponent, value) in enumerate(values):
            for peer in ("float", "dyn"):
                actual = float(value) if peer == "float" else value
                for order in ("left", "right"):
                    for name, _symbol, _helper in OPS:
                        function = f"{form}_{peer}_{order}_{name}"
                        calls.append(f"    print({function!r}, {index}, {function}({base!r}, {exponent!r}, {actual!r}))")
    calls.append('''    events.clear()
    assert ordered_left(2, -2, 1.0) is True
    assert events == [1, 2, 3]
    events.clear()
    assert ordered_right(2, -2, 1.0) is True
    assert events == [3, 1, 2]
    events.clear()
    try:
        later_raises(2, -2)
    except ValueError as error:
        assert str(error) == "peer failed"
    else:
        raise AssertionError("later operand exception lost")
    assert events == [4]
    events.clear()
    try:
        power_raises(0, -2, 1.0)
    except ZeroDivisionError:
        pass
    else:
        raise AssertionError("negative-power zero error lost")
    assert events == [1, 2]
    assert nested_power(2, 53, 9007199254740992.0) is False
    assert negated_power(2, 53, -9007199254740991.0) is True
    print("DYNAMIC_POWER_NATIVE_OK")
main()
''')
    return "\n".join(chunks + calls)


def test_dynamic_power_native_source_reference():
    import contextlib
    import io
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        exec(compile(native_program(), "dynamic_power_reference.py", "exec"), {})
    assert len(stream.getvalue().splitlines()) == 721
    assert stream.getvalue().endswith("DYNAMIC_POWER_NATIVE_OK\n")


@pytest.mark.integration
@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_dynamic_power_comparisons_native_five_gc(
    tmp_path, python_program_compiler, explicit_owned_runtime, request, capfd,
):
    import contextlib
    import io
    program = native_program()
    expected = io.StringIO()
    with contextlib.redirect_stdout(expected):
        exec(compile(program, "dynamic_power_reference.py", "exec"), {})
    assert len(expected.getvalue().splitlines()) == 721
    assert_owned_program(
        program, expected.getvalue(), tmp_path, python_program_compiler,
        request.node.callspec.params["python_program_compiler"],
        explicit_owned_runtime, capfd,
    )
