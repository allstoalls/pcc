from __future__ import annotations

import ctypes
import math
import os
import platform
import random
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from pcc.backend.self_backend_x86_64_linux import emit_x86_64_linux_asm
from pcc.frontends.python import pipeline
from pcc.tools.ir_to_obj import emit_object


REPO_ROOT = Path(__file__).resolve().parents[2]
NUMERIC_SOURCE = (
    REPO_ROOT / "pcc" / "runtime" / "py" / "freestanding_libc_numeric.py"
)
ERRNO_SOURCE = (
    REPO_ROOT / "pcc" / "runtime" / "py" / "freestanding_errno.py"
)
MATH_SYMBOLS = {
    "atan2",
    "cos",
    "ceil",
    "log2",
    "log10",
    "tan",
    "exp",
    "fabs",
    "floor",
    "fmod",
    "hypot",
    "log",
    "pow",
    "rint",
    "scalbn",
    "sin",
    "sqrt",
    "strtod",
}
CALLABLE_MATH_SYMBOLS = MATH_SYMBOLS - {"strtod"}


def _numeric_ir(
    tmp_path: Path, *, target_triple: str | None = None
) -> Path:
    suffix = "host" if target_triple is None else target_triple.split("-")[0]
    llvm_ir = tmp_path / f"freestanding_libc_numeric.{suffix}.ll"
    pipeline.compile_python(
        str(NUMERIC_SOURCE),
        str(llvm_ir),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
        target_triple=target_triple,
    )
    return llvm_ir


def _numeric_object(tmp_path: Path, *, target_triple: str | None = None) -> Path:
    llvm_ir = _numeric_ir(tmp_path, target_triple=target_triple)
    suffix = "host" if target_triple is None else target_triple.split("-")[0]
    obj = tmp_path / f"freestanding_libc_numeric.{suffix}.o"
    obj.write_bytes(
        emit_object(
            llvm_ir.read_text(encoding="utf-8"), target_triple=target_triple
        )
    )
    return obj


def _nm_names(obj: Path, *args: str) -> set[str]:
    nm = shutil.which("llvm-nm") or shutil.which("nm")
    assert nm is not None
    result = subprocess.run(
        [nm, *args, str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return {
        line.split()[-1].lstrip("_")
        for line in result.stdout.splitlines()
        if line.strip()
    }


def _defined_global_names(obj: Path) -> set[str]:
    nm = shutil.which("llvm-nm") or shutil.which("nm")
    assert nm is not None
    result = subprocess.run(
        [nm, "-g", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    names: set[str] = set()
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 2 or fields[-2] in {"U", "u"}:
            continue
        names.add(fields[-1].lstrip("_"))
    return names


def _f64_from_bits(bits: int) -> float:
    return struct.unpack(">d", struct.pack(">Q", bits))[0]


def _ordered_f64_bits(value: float) -> int:
    bits = struct.unpack(">Q", struct.pack(">d", value))[0]
    if (bits >> 63) != 0:
        return (~bits) & 0xFFFF_FFFF_FFFF_FFFF
    return bits | 0x8000_0000_0000_0000


def _ulp_distance(left: float, right: float) -> int:
    return abs(_ordered_f64_bits(left) - _ordered_f64_bits(right))


def test_numeric_object_owns_every_runtime_math_abi_without_recursive_libcalls(
    tmp_path: Path,
) -> None:
    for target_triple in (None, "x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu"):
        obj = _numeric_object(tmp_path, target_triple=target_triple)
        assert MATH_SYMBOLS <= _defined_global_names(obj)
        assert MATH_SYMBOLS.isdisjoint(_nm_names(obj, "-u"))
        assert _nm_names(obj, "-u") <= {"pcc_errno_set"}

    linux_ir = _numeric_ir(
        tmp_path, target_triple="x86_64-unknown-linux-gnu"
    ).read_text(encoding="utf-8")
    self_assembly = emit_x86_64_linux_asm(linux_ir)
    for symbol in MATH_SYMBOLS:
        assert f"{symbol}:" in self_assembly


def test_numeric_object_resolves_a_native_c_math_consumer(tmp_path: Path) -> None:
    """External C ABI reference; the static leaf probes establish ownership."""
    compiler = shutil.which(os.environ.get("CC", "cc"))
    assert compiler is not None
    consumer_source = tmp_path / "numeric_consumer.c"
    consumer_source.write_text(
        """
        extern double atan2(double, double);
        extern double cos(double);
        extern double ceil(double);
        extern double log2(double);
        extern double log10(double);
        extern double tan(double);
        extern double exp(double);
        extern double fabs(double);
        extern double floor(double);
        extern double fmod(double, double);
        extern double hypot(double, double);
        extern double log(double);
        extern double pow(double, double);
        extern double rint(double);
        extern double scalbn(double, int);
        extern double sin(double);
        extern double sqrt(double);

        double pcc_numeric_link_probe(double value, int exponent) {
            return atan2(value, 2.0) + cos(value) + exp(value)
                + fabs(value) + floor(value) + fmod(value, 3.0)
                + hypot(value, 4.0) + log(value) + pow(value, 2.5)
                + rint(value) + scalbn(value, exponent) + sin(value)
                + sqrt(value) + ceil(value) + log2(value) + log10(value) + tan(value);
        }
        """,
        encoding="utf-8",
    )
    consumer_obj = tmp_path / "numeric_consumer.o"
    compile_result = subprocess.run(
        [
            compiler,
            "-std=c11",
            "-O0",
            "-fno-builtin",
            "-c",
            str(consumer_source),
            "-o",
            str(consumer_obj),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert compile_result.returncode == 0, (
        compile_result.stdout + compile_result.stderr
    )
    assert CALLABLE_MATH_SYMBOLS <= _nm_names(consumer_obj, "-u")

    linked_obj = tmp_path / "numeric_linked.o"
    link_result = subprocess.run(
        [
            compiler,
            "-r",
            str(consumer_obj),
            str(_numeric_object(tmp_path)),
            "-o",
            str(linked_obj),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert link_result.returncode == 0, link_result.stdout + link_result.stderr
    assert CALLABLE_MATH_SYMBOLS.isdisjoint(_nm_names(linked_obj, "-u"))


def _native_math_functions(tmp_path: Path):
    from pcc.backend.host_owned_load import load_functions
    from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
    from pcc.driver.project import TranslationUnit
    from pcc.frontends.python.pipeline_targets import host_target_triple

    shim = CEvaluator().compile_translation_units([TranslationUnit(
        name="errno-shim.c", path="", source="int owned_errno; void pcc_errno_set(int value) {owned_errno = value;}"
    )], use_compile_cache=False)
    numeric_ir = _numeric_ir(tmp_path).read_text(encoding="utf-8")
    memory, symbols = load_functions([("numeric", numeric_ir)] + shim, host_target_triple())
    errno_value = ctypes.c_int32.from_address(symbols["_owned_errno"])
    errno_set = ctypes.CFUNCTYPE(None, ctypes.c_int32)(symbols["_pcc_errno_set"])
    errno_set._pcc_owned_memory = memory

    def bind(name: str, *argument_types):
        address = symbols["_" + name]
        assert address != 0
        function = ctypes.CFUNCTYPE(ctypes.c_double, *argument_types)(address)
        function._pcc_owned_memory = memory
        return function

    unary = {
        name: bind(name, ctypes.c_double)
        for name in (
            "cos",
            "ceil",
            "log2",
            "log10",
            "tan",
            "exp",
            "fabs",
            "floor",
            "log",
            "rint",
            "sin",
            "sqrt",
        )
    }
    binary = {
        name: bind(name, ctypes.c_double, ctypes.c_double)
        for name in ("atan2", "fmod", "hypot", "pow")
    }
    functions = unary | binary
    functions["scalbn"] = bind("scalbn", ctypes.c_double, ctypes.c_int)
    strtod_address = symbols["_strtod"]
    assert strtod_address != 0
    functions["strtod"] = ctypes.CFUNCTYPE(
        ctypes.c_double,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    )(strtod_address)
    functions["strtod"]._pcc_owned_memory = memory
    return memory, symbols, functions, errno_value, errno_set


@pytest.mark.pcc_gate(unavailable=None if sys.platform == "darwin" and platform.machine() == "arm64" else "owned in-process loader requires Darwin arm64; executable ABI tests cover other targets")
def test_numeric_exports_execute_without_host_libm(tmp_path: Path) -> None:
    # Calling the emitted functions through their C ABI keeps this an execution
    # test of the production IR, while the object-level test above proves that
    # none of these calls can be satisfied by the host libm.
    engine, module, fn, errno_value, errno_set = _native_math_functions(tmp_path)
    assert engine is not None and module is not None
    assert errno_set is not None

    assert fn["fabs"](-3.5) == 3.5
    negative_nan = -float("nan")
    assert math.copysign(1.0, fn["fabs"](negative_nan)) == 1.0
    assert fn["fabs"](float("-inf")) == float("inf")
    assert fn["floor"](-1.25) == -2.0
    assert math.copysign(1.0, fn["floor"](-0.0)) == -1.0
    assert fn["floor"](float("inf")) == float("inf")
    assert math.isnan(fn["floor"](float("nan")))
    assert [fn["rint"](value) for value in (-2.5, -0.5, 0.5, 1.5, 2.5)] == [
        -2.0,
        -0.0,
        0.0,
        2.0,
        2.0,
    ]
    assert math.copysign(1.0, fn["rint"](-0.5)) == -1.0
    assert fn["rint"](float("-inf")) == float("-inf")
    assert math.isnan(fn["rint"](float("nan")))

    for value in (5e-324, 1e-300, 0.5, 2.0, 3.0, 10.0, 1e300):
        assert fn["sqrt"](value) == math.sqrt(value)
    assert math.isnan(fn["sqrt"](float("-inf")))
    assert fn["sqrt"](float("inf")) == float("inf")
    assert fn["hypot"](3.0, 4.0) == 5.0
    assert fn["hypot"](float("inf"), float("nan")) == float("inf")

    for value in (-700.0, -1.0, -0.1, 0.0, 0.1, 1.0, 100.0, 700.0):
        assert _ulp_distance(fn["exp"](value), math.exp(value)) <= 4
    for value in (5e-324, 1e-300, 0.1, 0.5, 1.0, 2.0, 1e300):
        assert _ulp_distance(fn["log"](value), math.log(value)) <= 2
    assert fn["exp"](float("-inf")) == 0.0
    assert fn["exp"](float("inf")) == float("inf")
    underflow_edge = -745.1332191019411
    for value in (
        math.nextafter(underflow_edge, -math.inf),
        underflow_edge,
        math.nextafter(underflow_edge, math.inf),
    ):
        assert _ulp_distance(fn["exp"](value), math.exp(value)) <= 1
    overflow_edge = 709.782712893384
    for value in (
        math.nextafter(overflow_edge, -math.inf),
        overflow_edge,
    ):
        assert _ulp_distance(fn["exp"](value), math.exp(value)) <= 1
    assert fn["log"](0.0) == float("-inf")
    assert fn["log"](float("inf")) == float("inf")
    assert math.isnan(fn["log"](-1.0))

    for value in (-1e6, -100.0, -1.0, -0.1, 0.0, 0.1, 1.0, 100.0, 1e6):
        assert _ulp_distance(fn["sin"](value), math.sin(value)) <= 2
        assert _ulp_distance(fn["cos"](value), math.cos(value)) <= 2
    assert math.isnan(fn["sin"](float("inf")))
    assert math.copysign(1.0, fn["sin"](-0.0)) == -1.0
    assert fn["cos"](-0.0) == 1.0
    trig_edge = math.nextafter(1048576.0, -math.inf)
    assert _ulp_distance(fn["sin"](trig_edge), math.sin(trig_edge)) <= 2
    assert _ulp_distance(fn["cos"](trig_edge), math.cos(trig_edge)) <= 2
    for value in (1048576.0, 1e20, 1e100, 1e300):
        assert _ulp_distance(fn["sin"](value), math.sin(value)) <= 3
        assert _ulp_distance(fn["cos"](value), math.cos(value)) <= 3

    for y_value, x_value in (
        (1.0, 1.0),
        (1.0, -1.0),
        (-1.0, 1.0),
        (-1.0, -1.0),
        (1e300, 1e-300),
    ):
        assert _ulp_distance(
            fn["atan2"](y_value, x_value), math.atan2(y_value, x_value)
        ) <= 1
    assert fn["atan2"](-0.0, -1.0) == -math.pi

    for left, right in ((5.5, 2.0), (-5.5, 2.0), (1e300, math.pi)):
        assert fn["fmod"](left, right) == math.fmod(left, right)
    assert math.copysign(1.0, fn["fmod"](-1e300, 3.0)) == -1.0
    assert _ulp_distance(fn["pow"](10.0, 2.5), math.pow(10.0, 2.5)) <= 8
    assert fn["pow"](-2.0, 3.0) == -8.0
    assert math.isnan(fn["pow"](-2.0, 2.5))
    assert fn["pow"](1.0, float("nan")) == 1.0
    assert fn["pow"](float("nan"), 0.0) == 1.0
    assert fn["pow"](2.0, float("inf")) == float("inf")
    assert fn["pow"](0.5, float("inf")) == 0.0
    assert fn["pow"](2.0, float("-inf")) == 0.0
    assert fn["pow"](0.5, float("-inf")) == float("inf")
    assert fn["pow"](-1.0, 1e300) == 1.0
    assert fn["pow"](2.0, 1e300) == float("inf")
    assert fn["pow"](0.5, 1e300) == 0.0
    assert math.copysign(1.0, fn["pow"](-0.0, 3.0)) == -1.0
    assert fn["pow"](-0.0, -3.0) == float("-inf")
    assert math.copysign(1.0, fn["pow"](float("-inf"), -3.0)) == -1.0
    assert fn["pow"](1e-300, -2.0) == float("inf")

    assert fn["scalbn"](1.0, -1074) == 5e-324
    scale_rounding_edge = math.nextafter(1.0, 2.0)
    assert fn["scalbn"](scale_rounding_edge, -1075) == math.ldexp(
        scale_rounding_edge, -1075
    )
    assert fn["scalbn"](-1.0, -5000) == -0.0
    assert math.copysign(1.0, fn["scalbn"](-1.0, -5000)) == -1.0
    assert fn["scalbn"](1.0, 5000) == float("inf")

    rng = random.Random(0x5A17)
    for _ in range(2048):
        value = _f64_from_bits(rng.randrange(1, 0x7FF0_0000_0000_0000))
        assert fn["sqrt"](value) == math.sqrt(value)

    for _ in range(512):
        exp_input = rng.uniform(-745.0, 709.0)
        assert _ulp_distance(fn["exp"](exp_input), math.exp(exp_input)) <= 4

        log_input = _f64_from_bits(rng.randrange(1, 0x7FF0_0000_0000_0000))
        assert _ulp_distance(fn["log"](log_input), math.log(log_input)) <= 2

        trig_input = rng.uniform(-1e6, 1e6)
        assert _ulp_distance(fn["sin"](trig_input), math.sin(trig_input)) <= 2
        assert _ulp_distance(fn["cos"](trig_input), math.cos(trig_input)) <= 2

        y_value = rng.uniform(-1e300, 1e300)
        x_value = rng.uniform(-1e300, 1e300)
        assert (
            _ulp_distance(
                fn["atan2"](y_value, x_value), math.atan2(y_value, x_value)
            )
            <= 1
        )

        left = math.ldexp(rng.random() + 0.5, rng.randrange(-1000, 1000))
        right = math.ldexp(rng.random() + 0.5, rng.randrange(-1000, 1000))
        assert (
            _ulp_distance(fn["hypot"](left, right), math.hypot(left, right))
            <= 2
        )

        dividend = _f64_from_bits(rng.randrange(1, 0x7FF0_0000_0000_0000))
        divisor = _f64_from_bits(rng.randrange(1, 0x7FF0_0000_0000_0000))
        if rng.randrange(2) != 0:
            dividend = -dividend
        assert _ordered_f64_bits(fn["fmod"](dividend, divisor)) == (
            _ordered_f64_bits(math.fmod(dividend, divisor))
        )

        scale_input = math.ldexp(rng.random() + 0.5, rng.randrange(-900, 900))
        scale_exponent = rng.randrange(-100, 101)
        assert _ordered_f64_bits(fn["scalbn"](scale_input, scale_exponent)) == (
            _ordered_f64_bits(math.ldexp(scale_input, scale_exponent))
        )

        # The generic fractional-power lane deliberately carries a bounded
        # ULP contract (integer and +/-0.5 exponents use their exact/direct
        # paths).  Keep this finite domain explicit instead of implying
        # correctly-rounded pow for every binary64 pair.
        pow_base = math.exp(rng.uniform(-3.0, 3.0))
        pow_exponent = rng.uniform(-8.0, 8.0) + 0.125
        assert (
            _ulp_distance(
                fn["pow"](pow_base, pow_exponent),
                math.pow(pow_base, pow_exponent),
            )
            <= 32
        )

    for _ in range(512):
        exponent_bits = rng.randrange(1043, 2047)
        mantissa_bits = rng.randrange(0, 1 << 52)
        bits = (exponent_bits << 52) | mantissa_bits
        if rng.randrange(2) != 0:
            bits |= 1 << 63
        value = _f64_from_bits(bits)
        assert _ulp_distance(fn["sin"](value), math.sin(value)) <= 3
        assert _ulp_distance(fn["cos"](value), math.cos(value)) <= 3


def _call_strtod(function, text: str) -> tuple[float, int]:
    encoded = text.encode("ascii")
    buffer = ctypes.create_string_buffer(encoded)
    end = ctypes.c_void_p()
    value = function(ctypes.cast(buffer, ctypes.c_void_p), ctypes.byref(end))
    start_address = ctypes.addressof(buffer)
    assert end.value is not None
    return value, end.value - start_address


def _host_strtod() -> object:
    function = ctypes.CDLL(None, use_errno=True).strtod
    function.restype = ctypes.c_double
    function.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    return function


@pytest.mark.pcc_gate(unavailable=None if sys.platform == "darwin" and platform.machine() == "arm64" else "owned in-process loader requires Darwin arm64; executable ABI tests cover other targets")
def test_strtod_full_precision_hex_endptr_and_errno_match_c_oracle(
    tmp_path: Path,
) -> None:
    engine, module, fn, errno_value, errno_set = _native_math_functions(tmp_path)
    assert engine is not None and module is not None and errno_set is not None
    oracle = _host_strtod()
    corpus = (
        "0",
        "-0",
        "0.1",
        "9007199254740993",
        "12345678901234567890",
        "1.000000000000000000000000000000000000001",
        "4.9406564584124654e-324",
        "2.2250738585072011e-308",
        "1.7976931348623157e308",
        "1.7976931348623159e308",
        "0x1p0",
        "-0x1.8p1",
        "0x1p-1074",
        "0x1.fffffffffffffp1023",
        "nan(payload)tail",
        "infinity!",
        "  12.5xyz",
        "abc",
        ".",
        "1e+",
        "0x",
    )
    for text in corpus:
        actual, actual_end = _call_strtod(fn["strtod"], text)
        expected, expected_end = _call_strtod(oracle, text)
        assert actual_end == expected_end, text
        if math.isnan(expected):
            assert math.isnan(actual), text
        else:
            assert _ordered_f64_bits(actual) == _ordered_f64_bits(expected), text

    rng = random.Random(0x57D0D)
    for _ in range(1024):
        digit_count = rng.randrange(1, 96)
        digits = str(rng.randrange(1, 10)) + "".join(
            str(rng.randrange(10)) for _ in range(digit_count - 1)
        )
        radix = rng.randrange(0, digit_count + 1)
        decimal = digits[:radix] + "." + digits[radix:]
        exponent = rng.randrange(-400, 401)
        text = ("-" if rng.randrange(2) else "") + decimal + "e" + str(exponent)
        actual, actual_end = _call_strtod(fn["strtod"], text)
        expected, expected_end = _call_strtod(oracle, text)
        assert actual_end == expected_end == len(text)
        assert _ordered_f64_bits(actual) == _ordered_f64_bits(expected), text

    errno_value.value = 0
    _call_strtod(fn["strtod"], "1e9999")
    assert errno_value.value == 34
    errno_value.value = 0
    _call_strtod(fn["strtod"], "1e-9999")
    assert errno_value.value == 34
    errno_value.value = 0
    _call_strtod(fn["strtod"], "not-a-number")
    assert errno_value.value == 22


def _errno_ir(tmp_path: Path) -> Path:
    llvm_ir = tmp_path / "freestanding_errno.numeric.ll"
    pipeline.compile_python(
        str(ERRNO_SOURCE),
        str(llvm_ir),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    return llvm_ir


def _numeric_and_errno_objects(tmp_path: Path, emitter: str) -> tuple[Path, Path]:
    compiler = shutil.which(os.environ.get("CC", "cc"))
    assert compiler is not None
    objects: list[Path] = []
    for label, llvm_ir in (
        ("numeric", _numeric_ir(tmp_path)),
        ("errno", _errno_ir(tmp_path)),
    ):
        source = llvm_ir
        if emitter == "self":
            from pcc.backend.self_backend_dispatch import emit_self_asm

            source = tmp_path / f"{label}.self.s"
            source.write_text(
                emit_self_asm(llvm_ir.read_text(encoding="utf-8")),
                encoding="utf-8",
            )
        output = tmp_path / f"{label}.{emitter}.o"
        result = subprocess.run(
            [compiler, "-c", str(source), "-o", str(output)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        objects.append(output)
    return objects[0], objects[1]


# self backend execution requires arm64 Darwin or x86_64 Linux
@pytest.mark.pcc_gate(probe=lambda: (sys.platform == "darwin" and platform.machine() == "arm64")
                      or (sys.platform.startswith("linux") and platform.machine() == "x86_64"))
@pytest.mark.parametrize("emitter", ["llvm", "self"])
def test_numeric_errno_and_fenv_contract_executes_in_both_backends(
    tmp_path: Path, emitter: str
) -> None:

    compiler = shutil.which(os.environ.get("CC", "cc"))
    assert compiler is not None
    numeric_obj, errno_obj = _numeric_and_errno_objects(tmp_path, emitter)
    harness = tmp_path / f"numeric_fenv_{emitter}.c"
    executable = tmp_path / f"numeric_fenv_{emitter}"
    harness.write_text(
        r'''
#pragma STDC FENV_ACCESS ON
#include <errno.h>
#include <fenv.h>
#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

int pcc_errno_get(void);
void pcc_errno_set(int);

static uint64_t bits(double value) {
    uint64_t result;
    memcpy(&result, &value, sizeof(result));
    return result;
}

static uint64_t ordered(double value) {
    uint64_t raw = bits(value);
    return raw >> 63 ? ~raw : raw | UINT64_C(0x8000000000000000);
}

static uint64_t ulps(double left, double right) {
    uint64_t a = ordered(left);
    uint64_t b = ordered(right);
    return a >= b ? a - b : b - a;
}

int main(void) {
    volatile double half = 0.5;
    volatile double one_half = 1.5;
    volatile double neg_one_half = -1.5;

    /* Execute every public numeric owner through the selected backend before
       the edge/fenv checks below. */
    if (fabs(-3.5) != 3.5) return 1;
    if (floor(-1.25) != -2.0) return 2;
    if (sqrt(9.0) != 3.0 || hypot(3.0, 4.0) != 5.0) return 3;
    if (exp(0.0) != 1.0 || log(1.0) != 0.0) return 4;
    if (sin(0.0) != 0.0 || cos(0.0) != 1.0) return 5;
    if (atan2(0.0, 1.0) != 0.0 || fmod(5.5, 2.0) != 1.5) return 6;
    if (pow(2.0, 10.0) != 1024.0 || scalbn(1.0, 3) != 8.0) return 7;
    if (sqrt(2.0) != 0x1.6a09e667f3bcdp+0) return 40;
    if (ulps(exp(1.0), 0x1.5bf0a8b145769p+1) > 4) return 41;
    if (ulps(log(2.0), 0x1.62e42fefa39efp-1) > 2) return 42;
    if (ulps(sin(1.0), 0x1.aed548f090ceep-1) > 2) return 43;
    if (ulps(cos(1.0), 0x1.14a280fb5068cp-1) > 2) return 44;
    if (ulps(atan2(1.0, 1.0), 0x1.921fb54442d18p-1) > 1) return 45;
    if (ulps(pow(10.0, 2.5), 316.22776601683796) > 8) return 46;

    if (fesetround(FE_TONEAREST) != 0) return 10;
    if (rint(half) != 0.0 || rint(one_half) != 2.0) return 11;
    if (bits(rint(-half)) != UINT64_C(0x8000000000000000)) return 32;
    if (fesetround(FE_DOWNWARD) != 0) return 12;
    if (rint(one_half) != 1.0 || rint(neg_one_half) != -2.0) return 13;
    if (fesetround(FE_UPWARD) != 0) return 14;
    if (rint(one_half) != 2.0 || rint(neg_one_half) != -1.0) return 15;
    if (fesetround(FE_TOWARDZERO) != 0) return 16;
    if (rint(one_half) != 1.0 || rint(neg_one_half) != -1.0) return 17;
    if (fesetround(FE_TONEAREST) != 0) return 18;

    feclearexcept(FE_ALL_EXCEPT);
    (void)rint(half);
    if ((fetestexcept(FE_INEXACT) & FE_INEXACT) == 0) return 19;
    feclearexcept(FE_ALL_EXCEPT);
    pcc_errno_set(0);
    (void)log(-1.0);
    if (pcc_errno_get() != EDOM) return 20;
    if ((fetestexcept(FE_INVALID) & FE_INVALID) == 0) return 21;
    feclearexcept(FE_ALL_EXCEPT);
    pcc_errno_set(0);
    (void)log(0.0);
    if (pcc_errno_get() != ERANGE) return 22;
    if ((fetestexcept(FE_DIVBYZERO) & FE_DIVBYZERO) == 0) return 23;
    feclearexcept(FE_ALL_EXCEPT);
    pcc_errno_set(0);
    (void)exp(1000.0);
    if (pcc_errno_get() != ERANGE) return 24;
    if ((fetestexcept(FE_OVERFLOW) & FE_OVERFLOW) == 0) return 25;
    feclearexcept(FE_ALL_EXCEPT);
    pcc_errno_set(0);
    (void)exp(-1000.0);
    if (pcc_errno_get() != ERANGE) return 26;
    if ((fetestexcept(FE_UNDERFLOW) & FE_UNDERFLOW) == 0) return 27;

    feclearexcept(FE_ALL_EXCEPT);
    pcc_errno_set(123);
    if (!isinf(exp(INFINITY)) || pcc_errno_get() != 123) return 33;
    if ((fetestexcept(FE_OVERFLOW) & FE_OVERFLOW) != 0) return 34;
    uint64_t signaling_bits = UINT64_C(0x7ff0000000000001);
    double signaling_nan;
    memcpy(&signaling_nan, &signaling_bits, sizeof(signaling_nan));
    feclearexcept(FE_ALL_EXCEPT);
    if (!isnan(sqrt(signaling_nan))) return 35;
    if ((fetestexcept(FE_INVALID) & FE_INVALID) == 0) return 36;

    char *end = 0;
    if (bits(strtod("-0", &end)) != UINT64_C(0x8000000000000000)) return 37;
    if (bits(strtod("0x1p-1074!", &end)) != UINT64_C(1) || *end != '!') return 28;
    if (strtod("12345678901234567890x", &end) != 12345678901234567890.0
        || *end != 'x') return 29;
    if (!isnan(strtod("nan(payload)!", &end)) || *end != '!') return 30;
    pcc_errno_set(0);
    if (!isinf(strtod("1e9999", &end)) || pcc_errno_get() != ERANGE) return 31;
    return 0;
}
''',
        encoding="utf-8",
    )
    build = subprocess.run(
        [
            compiler,
            "-std=c11",
            "-O0",
            "-fno-builtin",
            "-frounding-math",
            str(harness),
            str(numeric_obj),
            str(errno_obj),
            "-lm",
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, run.stdout + run.stderr


@pytest.mark.pcc_gate(probe=lambda: sys.platform.startswith("linux") and platform.machine() in ("x86_64", "amd64"))
def test_missing_stage1_math_exports_execute_owned_linux_leaf(tmp_path):
    # CPython math is a labeled reference oracle only. The candidate is
    # pcc-Python -> owned object -> owned static ELF -> native Linux syscalls.
    from tests.python.test_freestanding_linux_libc import _run_leaf

    rng = random.Random(0xCE110610)
    finite = [
        0.0, -0.0, 5e-324, -5e-324, sys.float_info.min, -sys.float_info.min,
        0.25, -0.25, 0.5, -0.5, 1.0, -1.0, 1.25, -1.25,
        math.nextafter(1.0, 0.0), math.nextafter(1.0, math.inf),
        4503599627370495.5, -4503599627370495.5,
        4503599627370496.0, -4503599627370496.0,
        sys.float_info.max, -sys.float_info.max,
    ]
    finite += [_f64_from_bits(rng.getrandbits(64)) for _ in range(512)]
    special = [math.inf, -math.inf, math.nan, _f64_from_bits(0x7ff0000000000001)]
    logarithms = [abs(value) for value in finite if math.isfinite(value)]
    logarithms += [math.ldexp(1.0, power) for power in range(-1074, 1024)]
    logarithms += [10.0 ** power for power in range(-323, 309)]
    logarithms += [-1.0, -sys.float_info.max, -0.0] + special
    trig = finite + special
    for value in [math.pi / 4, math.pi / 2, math.pi, 3 * math.pi / 2,
                  1048576.0, 1e20, 1e100, 1e300]:
        trig += [math.nextafter(value, -math.inf), value, math.nextafter(value, math.inf)]
        trig += [-math.nextafter(value, -math.inf), -value, -math.nextafter(value, math.inf)]
    vectors = [("ceil", value) for value in finite + special]
    vectors += [(name, value) for name in ("log2", "log10") for value in logarithms]
    vectors += [("tan", value) for value in trig]
    names = ("ceil", "log2", "log10", "tan")
    encoded = "".join(
        str(names.index(name)) + struct.pack(">d", value).hex()
        for name, value in vectors
    )
    source = f'''
from pcc import i64
from pcc.extern import extern, c_abi_export, c_double, c_int32, c_void
from pcc.unsafe import cstr, stack_alloc, store_i64, load_i8, load_f64, store_f64, syscall6
__pcc_freestanding__ = True
ceil = extern("ceil", (c_double,), c_double)
log2 = extern("log2", (c_double,), c_double)
log10 = extern("log10", (c_double,), c_double)
tan = extern("tan", (c_double,), c_double)
errno_set = extern("pcc_errno_set", (c_int32,), c_void)
errno_get = extern("pcc_errno_get", (), c_int32)
@c_abi_export("_start")
def start(initial_stack) -> None:
    table = cstr({encoded!r})
    output = stack_alloc(16)
    offset: i64 = 0
    while offset < {len(encoded)}:
        bits: i64 = 0
        digit: i64 = 1
        while digit <= 16:
            character: i64 = load_i8(table, offset + digit)
            nibble: i64 = character - 48
            if character >= 97:
                nibble = character - 87
            bits = (bits << 4) | nibble
            digit = digit + 1
        store_i64(output, 0, bits)
        value: float = load_f64(output, 0)
        operation: i64 = load_i8(table, offset)
        errno_set(123)
        result: float = 0.0
        if operation == 48:
            result = ceil(value)
        elif operation == 49:
            result = log2(value)
        elif operation == 50:
            result = log10(value)
        else:
            result = tan(value)
        store_f64(output, 0, result)
        store_i64(output, 8, errno_get())
        syscall6(1, 1, output, 16, 0, 0, 0)
        offset = offset + 17
    syscall6(60, 0, 0, 0, 0, 0, 0)
'''
    output = _run_leaf(tmp_path, NUMERIC_SOURCE, source)
    records = list(struct.iter_unpack("<dq", output))
    assert len(records) == len(vectors)
    for (name, value), (actual, errno) in zip(vectors, records):
        context = (name, value.hex(), actual.hex(), errno)
        if math.isnan(value):
            assert math.isnan(actual), context
            assert errno == 123, context
        elif name in ("log2", "log10") and value == 0.0:
            assert actual == -math.inf and errno == 34, context
        elif (name in ("log2", "log10") and value < 0.0) or (name == "tan" and math.isinf(value)):
            assert math.isnan(actual) and errno == 33, context
        else:
            assert errno == 123, context
            if name == "ceil":
                expected = value if not math.isfinite(value) else float(math.ceil(value))
                if expected == 0.0:
                    expected = math.copysign(0.0, value)
                assert _ordered_f64_bits(actual) == _ordered_f64_bits(expected), context
            else:
                expected = getattr(math, name)(value)
                assert _ulp_distance(actual, expected) <= 3, (context, expected.hex())
                if expected == 0.0:
                    assert math.copysign(1.0, actual) == math.copysign(1.0, expected), context
                if name == "log2" and value > 0.0 and math.frexp(value)[0] == 0.5:
                    assert actual == expected, context


@pytest.mark.pcc_gate(probe=lambda: sys.platform.startswith("linux") and platform.machine() in ("x86_64", "amd64"))
def test_stage1_math_exports_owned_linux_fenv_and_rounding(tmp_path):
    from pcc.backend.elf_x86_64 import link_static_executable, parse_relocatable, parse_static_executable
    from pcc.backend.owned_elf_link import assemble
    from tests.python.test_freestanding_linux_libc import _LEAF_BOUNDARIES, _object

    target = "x86_64-unknown-linux-gnu"
    # The owned assembler test entry sets/reads the hardware MXCSR directly;
    # no host fenv/libm library participates in this candidate execution.
    cases = []
    quiet = _f64_from_bits(0x7ff8000000000042)
    signaling = _f64_from_bits(0x7ff0000000000042)
    for name in ("ceil", "log2", "log10", "tan"):
        cases.extend([(name, quiet, 0, 123, 0, None),
                      (name, signaling, 0, 123, 1, None)])
    for name in ("log2", "log10"):
        cases.extend([(name, -1.0, 0, 33, 1, None),
                      (name, -math.inf, 0, 33, 1, None),
                      (name, 0.0, 0, 34, 4, -math.inf),
                      (name, -0.0, 0, 34, 4, -math.inf),
                      (name, math.inf, 0, 123, 0, math.inf)])
    cases.extend([("tan", math.inf, 0, 33, 1, None),
                  ("tan", -math.inf, 0, 33, 1, None),
                  ("ceil", math.inf, 0, 123, 0, math.inf),
                  ("ceil", -math.inf, 0, 123, 0, -math.inf)])
    for mode in (0, 0x2000, 0x4000, 0x6000):
        for value, expected in ((-0.25, -0.0), (0.25, 1.0), (-1.25, -1.0), (1.25, 2.0)):
            cases.append(("ceil", value, mode, 123, None, expected))
    lines = [".intel_syntax noprefix", ".text", ".globl _start", "_start:",
             "  and rsp, -16", "  sub rsp, 32"]
    for name, value, mode, errno, flags, expected in cases:
        raw = struct.unpack("<Q", struct.pack("<d", value))[0]
        lines.extend([
            "  mov edi, 123", "  call pcc_errno_set",
            f"  mov DWORD PTR [rsp + 24], {0x1f80 | mode}",
            "  ldmxcsr DWORD PTR [rsp + 24]",
            f"  mov rax, {raw}", "  movq xmm0, rax", f"  call {name}",
            "  movsd QWORD PTR [rsp], xmm0", "  stmxcsr DWORD PTR [rsp + 24]",
            "  mov eax, DWORD PTR [rsp + 24]", "  mov QWORD PTR [rsp + 16], rax",
            "  call pcc_errno_get", "  mov QWORD PTR [rsp + 8], rax",
            "  mov eax, 1", "  mov edi, 1", "  mov rsi, rsp", "  mov edx, 24", "  syscall",
        ])
    lines.extend(["  mov eax, 60", "  xor edi, edi", "  syscall"])
    boundary = tmp_path / "fenv_boundaries.py"
    boundary.write_text(_LEAF_BOUNDARIES)
    numeric = _numeric_object(tmp_path, target_triple=target)
    image = link_static_executable([
        parse_relocatable(numeric.read_bytes()),
        parse_relocatable(_object(boundary, tmp_path, target)[1]),
        assemble("\n".join(lines) + "\n", target),
    ])
    parse_static_executable(image)
    executable = tmp_path / "fenv"
    executable.write_bytes(image)
    executable.chmod(0o755)
    result = subprocess.run([str(executable)], capture_output=True, timeout=15)
    assert result.returncode == 0, (result.returncode, result.stderr)
    assert result.stderr == b""
    records = list(struct.iter_unpack("<dqq", result.stdout))
    assert len(records) == len(cases)
    for case, (actual, errno, flags) in zip(cases, records):
        name, value, mode, expected_errno, expected_flags, expected = case
        context = (case, actual.hex(), errno, flags & 63)
        assert errno == expected_errno, context
        if expected_flags is not None:
            assert flags & 63 == expected_flags, context
        if expected is None:
            assert math.isnan(actual), context
        else:
            assert _ordered_f64_bits(actual) == _ordered_f64_bits(expected), context
