from pcc.ir.ir import ArrayType, Constant
from pcc.ir.ir import IntType


def test_array_repr():
    int8 = IntType(8)
    tp = ArrayType(int8, 3)
    int8_1 = Constant(int8, 1)
    tp1 = tp.gep(int8_1)
    print(tp1, type(tp1))
    assert str(tp1) == "i8"
    values = [Constant(int8, x) for x in (5, 10, -15)]
    c = Constant(tp, values)
    print(c)
    # pcc.ir renders a value as its operand reference; callers prefix the
    # type (llvmlite's str() included it).
    assert str(c) == "[i8 5, i8 10, i8 -15]"
    assert f"{c.type} {c}" == "[3 x i8] [i8 5, i8 10, i8 -15]"
    c = Constant(tp, bytearray(b"\x01\x02\x03"))
    print(c)
    assert f"{c.type} {c}" == '[3 x i8] c"\\01\\02\\03"'

if __name__ == "__main__":
    test_array_repr()