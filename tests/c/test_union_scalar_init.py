"""Regression: scalar initializer for a union targets its first member.

Before the fix, initializing a union (or a union-typed struct member) with a
scalar — e.g. `union u x = 0;` or a designated `{ .m = 0 }` where `m` is a
union — made the C codegen store the scalar as the whole union aggregate,
emitting invalid LLVM IR (`store <union> <int>`, "integer constant must have
integer type"). C99 6.7.9p17 says a scalar initializer for a union initializes
its first named member. Minimized from gcc-torture pr33631.c.
"""
import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)

from pcc.frontends.c.evaluator.c_evaluator import CEvaluator


def _evaluate(source):
    return CEvaluator().evaluate(source, optimize=False)


def test_struct_with_union_member_designated_scalar_init():
    # pr33631.c shape: `.m = 0` inits the union member; the unmentioned `.c`
    # must be zero-initialized.
    source = r"""
        typedef union { int lock; } mtx_t;
        int main(void) {
            struct { int c; mtx_t m; } r = { .m = 0 };
            return r.c == 0 ? 0 : 1;
        }
    """
    assert _evaluate(source) == 0


def test_union_member_scalar_init_reaches_first_member():
    source = r"""
        typedef union { int lock; } mtx_t;
        int main(void) {
            struct { int c; mtx_t m; } r = { .m = 5 };
            return (r.c == 0 && r.m.lock == 5) ? 0 : 1;
        }
    """
    assert _evaluate(source) == 0


def test_union_variable_scalar_init_uses_first_member():
    source = r"""
        union U { int a; char b; };
        int main(void) {
            union U u = 7;
            return u.a == 7 ? 0 : 1;
        }
    """
    assert _evaluate(source) == 0


def test_typedef_union_variable_scalar_init_uses_first_member():
    source = r"""
        typedef union { int a; char b; } U;
        int main(void) {
            U u = 11;
            return u.a == 11 ? 0 : 1;
        }
    """
    assert _evaluate(source) == 0
