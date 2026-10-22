from pcc.ir.compat import ir
from pcc.frontends.python import py_ast as pa
from pcc.frontends.python.codegen.expr_helper_lowering import ExprHelperLoweringMixin
from pcc.frontends.python.codegen.core_helpers import CoreHelperMixin
from pcc.frontends.python.codegen.method_call_lowering import _method_abi_type_matches


class _NoSextBuilder:
    def sext(self, value, target_ty, name=""):
        raise AssertionError("i64 value should not be sign-extended to i64")


class _FakeCodegen(ExprHelperLoweringMixin):
    def __init__(self):
        self.builder = _NoSextBuilder()
        self.module = None
        self.runtime = {}

    def _emit_expr(self, expr):
        # Deliberately allocate a fresh i64 type object. Self-hosted pcc stages
        # can see this as shape-equal but not identity-equal to the module-level
        # _I64 constant inside expr_helper_lowering.py.
        return ir.Constant(ir.IntType(64), 7)

    def _ir_type_matches(self, actual, expected):
        if actual is expected:
            return True
        if isinstance(actual, ir.IntType) and isinstance(expected, ir.IntType):
            return actual.width == expected.width
        return False


def test_emit_expr_as_i64_accepts_shape_equal_i64_type():
    expr = pa.Name(
        span=pa.SourceSpan("<test>", 1, 1, 1, 1),
        ty=pa.IntType("int", 64, True),
        ident="value",
    )

    out = _FakeCodegen()._emit_expr_as_i64(expr)

    assert isinstance(out.type, ir.IntType)
    assert out.type.width == 64


def test_method_abi_matches_distinct_literal_aggregate_types():
    host = CoreHelperMixin()
    actual = ir.LiteralStructType([ir.IntType(64), ir.IntType(64)])
    expected = ir.LiteralStructType([ir.IntType(64), ir.IntType(64)])
    assert actual is not expected
    assert _method_abi_type_matches(host, actual, expected)


def test_method_aggregate_abi_preserves_layout_and_address_space_boundaries():
    host = CoreHelperMixin()
    def aggregate(width=64, packed=False, addrspace=0, count=2):
        return ir.LiteralStructType([
            ir.ArrayType(ir.IntType(width), count),
            ir.IntType(8).as_pointer(addrspace),
        ], packed=packed)
    assert _method_abi_type_matches(host, aggregate(), aggregate())
    for incompatible in (aggregate(width=32), aggregate(packed=True),
                         aggregate(addrspace=1), aggregate(count=3)):
        assert not _method_abi_type_matches(host, aggregate(), incompatible)
    reverse = ir.LiteralStructType([ir.IntType(8).as_pointer(), ir.ArrayType(ir.IntType(64), 2)])
    assert not _method_abi_type_matches(host, aggregate(), reverse)
