from __future__ import annotations

import builtins

import pytest

from pcc.backend import all_backend_names, resolve_backend
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
from pcc.driver.project import TranslationUnit


@pytest.fixture(autouse=True)
def deny_llvm(monkeypatch):
    original = builtins.__import__

    def checked(name, *args, **kwargs):
        if name == "llvmlite" or name.startswith("llvmlite."):
            raise AssertionError("owned execution imported " + name)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", checked)
    monkeypatch.delenv("PCC_BACKEND", raising=False)


def test_default_backend_is_owned_and_external_backends_fail():
    assert resolve_backend().kind == "self"
    assert all_backend_names() == ("self",)
    for name in ("llvm", "ir", "llvmlite", "llvm-capi"):
        with pytest.raises(ValueError, match="expected one of: self"):
            CEvaluator(backend=name)


@pytest.mark.parametrize("source, expected", [
    ("int helper(void) { return 123456789; } int main(void) { return -helper(); }", -123456789),
    ("long long helper(void) { return 1099511627776LL; } long long main(void) { return helper() + 37; }", 2 ** 40 + 37),
    ("double helper(void) { return 1.25; } double main(void) { return helper() - 8.5; }", -7.25),
    ("float helper(void) { return 1.25f; } float main(void) { return helper(); }", 1.25),
    ("void helper(void) {} void main(void) { helper(); }", None),
])
def test_owned_execution_preserves_full_scalar_result(source, expected):
    assert CEvaluator().evaluate(source, use_compile_cache=False) == expected


def test_owned_execution_supports_named_entry_and_typed_arguments():
    evaluator = CEvaluator()
    assert evaluator.evaluate("int subtract(int a, int b) { return a - b; }",
                              entry="subtract", args=[4, 7], use_compile_cache=False) == -3


def test_named_entry_does_not_collide_with_existing_main():
    assert CEvaluator().evaluate("int helper(void) {return 42;} int main(void) {return 7;}",
                                entry="helper", use_compile_cache=False) == 42


def test_result_bridge_symbols_do_not_collide_with_user_data():
    source = "int __pcc_evaluate_entry = 5; int main(void) {return __pcc_evaluate_entry;}"
    assert CEvaluator().evaluate(source, use_compile_cache=False) == 5


def test_named_entry_can_execute_translation_unit_private_function():
    assert CEvaluator().evaluate("static int helper(void) {return 42;}",
                                entry="helper", use_compile_cache=False) == 42


@pytest.mark.parametrize("arguments", [None, [], ["ignored"]])
def test_no_parameter_main_accepts_program_argument_list(arguments):
    assert CEvaluator().evaluate("int helper(void) {return 1234;} int main(void) {return helper();}",
                                prog_args=arguments, use_compile_cache=False) == 1234


def test_live_pointer_result_owns_native_allocation():
    import gc

    pointer = CEvaluator().evaluate("int value = 4; int *main(void) {return &value;}", use_compile_cache=False)
    gc.collect()
    assert pointer.contents.value == 4
    pointer.contents.value = 99
    assert pointer.contents.value == 99


def test_named_pointer_return_preserves_selected_entry_type():
    pointer = CEvaluator().evaluate("int value = 37; int *answer(void) {return &value;} double main(void) {return 2.5;}",
                                   entry="answer", use_compile_cache=False)
    assert pointer.contents.value == 37


def test_host_pointer_argument_is_modified_in_place():
    import ctypes

    value = ctypes.c_int32(4)
    result = CEvaluator().evaluate("int update(int *value) {*value += 38; return *value;}",
                                  entry="update", args=[ctypes.byref(value)], use_compile_cache=False)
    assert result == value.value == 42


def test_out_parameter_keeps_published_module_address_alive():
    import ctypes
    import gc

    output = ctypes.POINTER(ctypes.c_int32)()
    result = CEvaluator().evaluate("int value = 99; int record(int **out) {*out = &value; return 7;}",
                                  entry="record", args=[ctypes.byref(output)], use_compile_cache=False)
    gc.collect()
    assert result == 7 and output.contents.value == 99


@pytest.mark.parametrize("pointee", [None, ("void",)])
def test_opaque_pointer_descriptor_keeps_module_and_input_alive(pointee):
    import ctypes
    import gc

    evaluator = CEvaluator()
    units = evaluator.compile_translation_units([TranslationUnit(
        name="pointer.c", path="", source="void *main(void *value) {return value;}"
    )], use_compile_cache=False)
    units = [(name, text, ("ptr", pointee), definitions) for name, text, _, definitions in units]
    address = evaluator.evaluate_compiled_translation_units(units, args=[b"ABC"])
    del evaluator
    gc.collect()
    assert ctypes.string_at(address) == b"ABC"


def test_pointer_execution_honours_base_directory_and_restores_host_cwd(tmp_path):
    import os

    (tmp_path / "input.bin").write_bytes(b"ABCD")
    previous = os.getcwd()
    source = 'char buffer[5]; char *main(void) {void *stream=fopen("input.bin", "rb"); if (!stream) return 0; fread(buffer, 1, 4, stream); fclose(stream); return buffer;}'
    pointer = CEvaluator().evaluate(source, base_dir=str(tmp_path), use_compile_cache=False)
    assert pointer[0] == 65
    assert os.getcwd() == previous


@pytest.mark.parametrize("source, expected", [
    ("unsigned int main(void) {return 4294967295U;}", 2 ** 32 - 1),
    ("unsigned long long main(void) {return 18446744073709551615ULL;}", 2 ** 64 - 1),
])
def test_unsigned_scalar_result_preserves_source_signedness(source, expected):
    assert CEvaluator().evaluate(source, use_compile_cache=False) == expected


@pytest.mark.parametrize("value, index, expected", [("é", 0, 195), ("A\0B", 1, 0), (b"\xff", 0, 255)])
def test_pointer_string_argument_preserves_bytes(value, index, expected):
    assert CEvaluator().evaluate("int read_byte(char *s, int index) {return (unsigned char)s[index];}",
                                entry="read_byte", args=[value, index], use_compile_cache=False) == expected


@pytest.mark.parametrize("value", [float("inf"), -float("inf"), float("nan")])
def test_nonfinite_float_arguments(value):
    import math

    result = CEvaluator().evaluate("double identity(double value) {return value;}",
                                  entry="identity", args=[value], use_compile_cache=False)
    assert math.isnan(result) if math.isnan(value) else result == value


def test_owned_execution_preserves_program_arguments_and_stdout(capfd):
    source = 'int helper(char **argv) { puts("visible output"); return atoi(argv[1]); } int main(int argc, char **argv) { return helper(argv) + argc; }'
    assert CEvaluator().evaluate(source, prog_args=["1024"], use_compile_cache=False) == 1026
    assert "visible output\n" in capfd.readouterr().out


def test_separate_translation_units_preserve_full_result():
    evaluator = CEvaluator()
    units = evaluator.compile_translation_units([
        TranslationUnit(name="helper.c", path="", source="int helper(void) { return -123456789; }"),
        TranslationUnit(name="main.c", path="", source="int helper(void); int main(void) { return helper(); }"),
    ], use_compile_cache=False)
    assert evaluator.evaluate_compiled_translation_units(units) == -123456789


def test_owned_c_emit_link_execute(tmp_path):
    import subprocess

    evaluator = CEvaluator()
    units = evaluator.compile_translation_units([
        TranslationUnit(name="main.c", path="", source="int helper(void) {return 42;} int main(void) {return helper();}")
    ], use_compile_cache=False)
    executable = tmp_path / "native"
    evaluator.emit_executable(units, str(executable))
    assert subprocess.run([str(executable)], timeout=10).returncode == 42
