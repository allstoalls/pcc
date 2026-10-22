import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)

from pcc.frontends.c.evaluator.c_evaluator import CEvaluator

from tests.owned_c_execution import compile_and_run_owned_c


def test_static_const_union_scalar_initializer():
    source = r"""
        union U { int i; double d; };
        static const union U u = { 42 };

        int main() {
            return u.i == 42 ? 0 : 1;
        }
    """

    r = compile_and_run_owned_c(source)
    assert r.returncode == 0, r.stderr


def test_static_const_union_scalar_initializer_via_evaluator():
    source = r"""
        union U {
            int i;
            unsigned char bytes[4];
        };

        static const union U nativeendian = {1};

        int main() {
            return nativeendian.i == 1 && nativeendian.bytes[0] == 1 ? 0 : 1;
        }
    """

    ret = CEvaluator().evaluate(source, optimize=False)
    assert ret == 0


def test_static_const_union_nested_struct_initializer():
    source = r"""
        typedef union Value {
            void *p;
            long long raw;
            unsigned char ub;
        } Value;

        typedef union Node {
            struct {
                Value value_;
                unsigned char value_tt;
                unsigned char key_tt;
                int next;
                Value key_val;
            } u;
            struct {
                Value value_;
                unsigned char tt_;
            } i_val;
        } Node;

        static const Node dummynode = {
            {{0}, 16, 9, 0, {0}}
        };

        int main() {
            return dummynode.u.value_.p == 0 &&
                   dummynode.u.value_tt == 16 &&
                   dummynode.u.key_tt == 9 &&
                   dummynode.u.next == 0 &&
                   dummynode.u.key_val.p == 0 ? 0 : 1;
        }
    """

    r = compile_and_run_owned_c(source)
    assert r.returncode == 0, r.stderr
