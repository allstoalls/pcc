"""The standalone optimizer must execute the same memory tier as self builds."""

from pcc.native_ir.driver import optimize_ir
from pcc.py_frontend.compiled_owned_passes import run_owned_passes


_LOOP = """define i64 @total(i64 %limit) {
entry:
  %index = alloca i64
  %sum = alloca i64
  store i64 0, ptr %index
  store i64 0, ptr %sum
  br label %head
head:
  %i = load i64, ptr %index
  %more = icmp slt i64 %i, %limit
  br i1 %more, label %body, label %done
body:
  %old = load i64, ptr %sum
  %nextsum = add i64 %old, %i
  store i64 %nextsum, ptr %sum
  %next = add i64 %i, 1
  store i64 %next, ptr %index
  br label %head
done:
  %value = load i64, ptr %sum
  ret i64 %value
}
"""


def test_driver_memory_tier_matches_production_dispatch():
    expected = run_owned_passes(_LOOP, ["mem2reg", "sroa"], False)
    actual = optimize_ir(_LOOP, "mem2reg,sroa")
    assert actual == expected
    assert "alloca" not in actual
    assert "load i64" not in actual
    assert "store i64" not in actual
    assert actual.count(" = phi i64 ") == 2
