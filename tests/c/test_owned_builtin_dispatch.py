"""Owned builtin handlers precede fallback libc aliases without ABI loss."""

import pytest

from pcc.frontends.c.codegen.c_codegen import (
    CCodeGenerator,
    postprocess_ir_text,
)
from pcc.frontends.c.parse import make_c_parser
from tests.owned_c_execution import compile_and_run_owned_c


def _generate_ir(source):
    generator = CCodeGenerator()
    generator.generate_code(make_c_parser().parse(source))
    return postprocess_ir_text(str(generator.module))


@pytest.mark.parametrize("no_builtin", [False, True])
@pytest.mark.parametrize(
    "builtin,c_type,ir_type,int_type",
    [
        ("__builtin_copysignf", "float", "float", "i32"),
        ("__builtin_copysign", "double", "double", "i64"),
    ],
)
def test_owned_copysign_handler_preserves_argument_and_result_widths(
    monkeypatch, no_builtin, builtin, c_type, ir_type, int_type
):
    if no_builtin:
        monkeypatch.setenv("PCC_NO_BUILTIN", "1")
    else:
        monkeypatch.delenv("PCC_NO_BUILTIN", raising=False)
    text = _generate_ir(
        f"{c_type} probe({c_type} value, {c_type} sign) {{ "
        f"return {builtin}(value, sign); }}"
    )
    assert f"define {ir_type} @probe({ir_type} " in text
    assert text.count(f"bitcast {ir_type} ") == 2
    assert f"or {int_type} " in text
    assert f" to {ir_type}" in text
    assert f"ret {ir_type} " in text
    assert "call " not in text
    assert "sitofp" not in text
    assert "fpext" not in text
    assert "fptrunc" not in text


def test_owned_copysignf_narrows_double_input_before_float_bit_operations():
    text = _generate_ir(
        "float probe(double value, float sign) { "
        "return __builtin_copysignf(value, sign); }"
    )
    assert "define float @probe(double " in text
    assert "fptrunc double " in text
    assert text.count("bitcast float ") == 2
    assert "or i32 " in text
    assert "ret float " in text
    assert "fpext" not in text
    assert "sitofp" not in text
    assert "call " not in text


@pytest.mark.parametrize("name", ["copysignf", "__builtin_copysignf"])
def test_explicit_float_function_binding_keeps_its_call(name):
    text = _generate_ir(
        f"float {name}(float value, float sign); "
        f"float probe(float value, float sign) {{ return {name}(value, sign); }}"
    )
    assert f"call float (float, float) @{name}(float " in text
    assert "copysignmagbits" not in text
    assert "sitofp" not in text


def test_local_builtin_name_function_pointer_is_not_rewritten_or_intrinsified():
    text = _generate_ir(
        "float probe(float (*__builtin_copysignf)(float, float), "
        "float value, float sign) { return __builtin_copysignf(value, sign); }"
    )
    assert "call float (float, float) %" in text
    assert "@copysignf" not in text
    assert "copysignmagbits" not in text


@pytest.mark.parametrize(
    "builtin,name,c_type,count",
    [
        ("__builtin_fma", "fma", "double", 3),
        ("__builtin_fmaf", "fmaf", "float", 3),
        ("__builtin_fabs", "fabs", "double", 1),
        ("__builtin_fabsf", "fabsf", "float", 1),
        ("__builtin_sqrt", "sqrt", "double", 1),
        ("__builtin_sqrtf", "sqrtf", "float", 1),
    ],
)
def test_unmatched_builtin_still_uses_declared_libc_alias(
    builtin, name, c_type, count
):
    parameters = ", ".join([c_type] * count)
    arguments = ", ".join(["value"] * count)
    text = _generate_ir(
        f"{c_type} {name}({parameters}); "
        f"{c_type} probe({c_type} value) {{ return {builtin}({arguments}); }}"
    )
    assert f"call {c_type} ({parameters}) @{name}({c_type} " in text
    assert f"@{builtin}" not in text


def test_owned_copysignf_and_copysign_execute_signed_zero_semantics():
    source = r"""
        float copy_float_sign(double value, float sign) {
            return __builtin_copysignf(value, sign);
        }
        double copy_double_sign(double value, double sign) {
            return __builtin_copysign(value, sign);
        }
        int main(void) {
            float signs[2] = {-0.0f, 0.0f};
            if (copy_float_sign(1.0, signs[0]) != -1.0f)
                return 1;
            float negative_zero = copy_float_sign(0.0, signs[0]);
            if (negative_zero != 0.0f || !__builtin_signbit(negative_zero))
                return 2;
            if (__builtin_signbit(copy_float_sign(-0.0, signs[1])))
                return 3;
            double negative_double_zero = copy_double_sign(0.0, -0.0);
            if (negative_double_zero != 0.0 ||
                !__builtin_signbit(negative_double_zero))
                return 4;
            if (__builtin_signbit(copy_double_sign(-0.0, 0.0)))
                return 5;
            return 0;
        }
    """
    result = compile_and_run_owned_c(source)
    assert result.returncode == 0, result.stdout + result.stderr
