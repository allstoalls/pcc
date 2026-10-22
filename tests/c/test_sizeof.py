import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestSizeof(unittest.TestCase):
    def test_sizeof_int(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            int main(){
                return sizeof(int);
            }
            """,
            llvmdump=True,
        )
        assert ret == 4  # int is i32 (standard C)

    def test_sizeof_double(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            int main(){
                return sizeof(double);
            }
            """,
            llvmdump=True,
        )
        assert ret == 8

    def test_sizeof_variable(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            int main(){
                int x = 42;
                return sizeof(x);
            }
            """,
            llvmdump=True,
        )
        assert ret == 4  # int is i32

    def test_sizeof_struct_pointer_array_field(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            struct S {
                char buf[1024];
            };

            int main(){
                struct S *p = 0;
                return sizeof(p->buf);
            }
            """,
            llvmdump=True,
        )
        assert ret == 1024

    def test_sizeof_union_with_array_member(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            typedef union {
                long long align;
                char buf[16];
            } U;

            int main(){
                return sizeof(U);
            }
            """,
            llvmdump=True,
        )
        assert ret == 16

    def test_typedef_scalar_pointer_uses_underlying_element_type(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            typedef unsigned int U32;

            int main(){
                U32 values[2] = {1u, 2u};
                const U32 *p = values;
                return p[1];
            }
            """,
            llvmdump=True,
        )
        assert ret == 2

    def test_sizeof_string_literal_in_static_const_expr(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            int main(){
                static const unsigned long szloc = sizeof("local") - 1;
                return (int)szloc;
            }
            """,
            llvmdump=True,
        )
        assert ret == 5

    def test_sizeof_structref_in_local_array_dim(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            struct S {
                char buf[16];
            };

            int f(struct S *p){
                char tmp[sizeof(p->buf)];
                return sizeof(tmp) == 16 ? 0 : 1;
            }

            int main(){
                struct S s;
                return f(&s);
            }
            """,
            llvmdump=True,
        )
        assert ret == 0

    def test_sizeof_minus_offsetof_in_local_array_dim(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            typedef struct {
                int count;
                char tail[8];
            } Parse;

            int main(){
                char saveBuf[(sizeof(Parse)) - ((long) &((Parse *)0)->tail)];
                return sizeof(saveBuf) == 8 ? 0 : 1;
            }
            """,
            llvmdump=True,
        )
        assert ret == 0

    def test_sizeof_binary_pointer_arithmetic_structref(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            struct S {
                char pad;
                int value;
            };

            int main(){
                struct S items[2];
                return sizeof((items + 1)->value);
            }
            """,
            llvmdump=True,
        )
        assert ret == 4

    def test_sizeof_arrow_on_array_lvalue_decays_to_first_element_pointer(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            typedef struct {
                long c[4];
                long b, e, k;
            } PT;

            PT cases[2];

            int main(){
                return sizeof(cases->c) == sizeof(long) * 4 ? 0 : 1;
            }
            """,
            llvmdump=True,
        )
        assert ret == 0

    def test_sizeof_binary_expr_uses_arithmetic_result_type(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            int main(){
                int x = 1;
                return sizeof(x + 1);
            }
            """,
            llvmdump=True,
        )
        assert ret == 4


if __name__ == "__main__":
    unittest.main()
