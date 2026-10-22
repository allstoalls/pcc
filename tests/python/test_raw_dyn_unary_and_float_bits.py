"""Unary operators on objects and uint64 float patterns in pcc's raw-int build.

Stage2 (pcc1 compiling pcc) stopped with ``OverflowError: Python int too
large to convert to C int64`` inside ``pcc.ir.support._float_bits``: the IEEE
pattern of a negative double is >= 2**63, and the ``-> int`` helpers carried
it in an i64 lane.  Before the lane raised, it read such a value as 0, so
pcc1 silently compiled negative double constants as +0.0 -- in IR constant
text, in self-backend register materialization, and in the float literal
pool, whose ``-2.5`` entry collided with ``0.0``'s key.  Chasing it exposed a
second silent miscompile: unary ``-``/``~``/``+`` on a dynamically-typed
operand went through the same lane, so ``-f`` for a dyn float was the int 0
(``_bits_to_float64`` returned 0.0 for every negative pattern).  Both paths
now keep exact objects: dyn operands dispatch through ``py_obj_neg`` /
``py_obj_pos`` / ``py_obj_invert``, and an exact int's negation stays exact.
"""

import os
import subprocess
import sys

_UNARY = '''from pcc.unsafe import null


def dyn_values(seed):
    # Built from object arithmetic so every value is dynamically typed.
    return [seed / 10.0, seed * 1, seed > 0, (seed << 70), seed * 1.5 - 1.0]


def main():
    for value in dyn_values(3):
        print(repr(-value), repr(+value))
    for value in dyn_values(-2):
        print(repr(-value))
    ints = [7 * 1, (1 << 70) + 1, -(1 << 63), True]
    for value in ints:
        print(repr(~value), repr(-value))
    big = 1 << 80
    negated = -big
    print(negated, -negated, ~big)
    try:
        print(-"text")
    except TypeError as exc:
        print("TypeError:", exc)


main()
'''

_FLOAT_BITS = '''from pcc.ir.support._float_bits import _bits_to_float64, _float32_to_bits, _float64_to_bits
from pcc.backend.self_backend_float_bits import u64_as_i64
from pcc.backend.self_backend_ir import TypeDesc
from pcc.backend.self_backend_aarch64_darwin_regs import emit_fp_constant, emit_fp_hex_constant
from pcc.ir import ir
from pcc.frontends.python.codegen.marshal import _float_literal_object


def main():
    double = TypeDesc("fp", 64)
    single = TypeDesc("fp", 32)
    for value in [-0.1, 0.1, -2.5, -0.0, -1e308, -5e-324]:
        bits = _float64_to_bits(value)
        print(hex(bits), repr(_bits_to_float64(bits)), u64_as_i64(bits), hex(_float32_to_bits(value)))
        print(str(ir.Constant(ir.DoubleType(), value)))
        print(" | ".join(emit_fp_constant(double, "d0", repr(value))))
        print(" | ".join(emit_fp_hex_constant(double, "d1", hex(bits))))
        print(" | ".join(emit_fp_hex_constant(single, "s2", hex(bits))))
    module = ir.Module(name="m")
    zero = _float_literal_object(module, 0.0)
    negative = _float_literal_object(module, -2.5)
    again = _float_literal_object(module, -2.5)
    print(zero is negative, negative is again, len(module.globals))


main()
'''


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _cpython(text):
    ran = subprocess.run(
        [sys.executable, "-c", text], capture_output=True, text=True,
        timeout=60, env=dict(os.environ, PYTHONPATH=_repo_root()),
    )
    assert ran.returncode == 0, ran.stderr
    return ran.stdout


def _pcc(tmp_path, name, text, compiler, archive, backends):
    source = tmp_path / (name + ".py")
    source.write_text(text, encoding="utf-8")
    # The float-bits program imports pcc's own modules; resolve them next to
    # the source, the way a pcc checkout compiles its tools.
    package = tmp_path / "pcc"
    if not package.exists():
        package.symlink_to(os.path.join(_repo_root(), "pcc"), target_is_directory=True)
    binary = tmp_path / name
    compiler(
        str(source), str(binary), backend="self", libpython_mode="off",
        runtime_archive=str(archive),
    )
    outputs = []
    for backend in backends:
        ran = subprocess.run(
            [str(binary)], capture_output=True, text=True, timeout=60,
            env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
        )
        assert ran.returncode == 0, f"GC{backend}: {ran.stderr}"
        outputs.append(ran.stdout)
    return outputs


def test_unary_operators_on_dynamic_operands(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    expected = _cpython(_UNARY)
    for output in _pcc(
        tmp_path, "raw_unary", _UNARY, python_program_compiler,
        pcc_runtime_archive, range(5),
    ):
        assert output == expected


def test_negative_double_patterns_stay_exact(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    expected = _cpython(_FLOAT_BITS)
    assert "False True 2" in expected
    for output in _pcc(
        tmp_path, "raw_float_bits", _FLOAT_BITS, python_program_compiler,
        pcc_runtime_archive, (0,),
    ):
        assert output == expected
