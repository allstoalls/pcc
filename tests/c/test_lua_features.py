"""Tests for features needed by Lua compilation: struct member access via ->,
pointer compound assignment, implicit int-to-pointer, opaque structs, etc."""

import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestPointerCompoundAssign(unittest.TestCase):
    def test_ptr_plus_equal(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            int main(){
                int a[5] = {10, 20, 30, 40, 50};
                int *p = &a[0];
                p += 2;
                return *p;
            }
        """,
            optimize=False,
        )
        assert ret == 30

    def test_ptr_minus_equal(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            int main(){
                int a[5] = {10, 20, 30, 40, 50};
                int *p = &a[4];
                p -= 3;
                return *p;
            }
        """,
            optimize=False,
        )
        assert ret == 20


class TestImplicitIntToPointer(unittest.TestCase):
    def test_assign_zero_to_ptr(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            int main(){
                int *p = 0;
                if (!p) return 1;
                return 0;
            }
        """,
            optimize=False,
        )
        assert ret == 1


class TestStructMemberIncDec(unittest.TestCase):
    def test_arrow_postinc(self):
        """z->p++ on struct pointer member."""
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            int main(){
                struct { int *p; int n; } z;
                int data[3] = {10, 20, 30};
                z.p = &data[0];
                z.n = 3;
                int first = *z.p;
                z.p++;
                int second = *z.p;
                return first * 10 + second;
            }
        """,
            optimize=False,
        )
        assert ret == 120  # 10*10 + 20

    def test_arrow_predec(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            int main(){
                struct { int val; } s;
                s.val = 10;
                --s.val;
                return s.val;
            }
        """,
            optimize=False,
        )
        assert ret == 9


class TestOpaqueStructPtr(unittest.TestCase):
    def test_forward_declared_struct_ptr(self):
        """Can pass around pointers to forward-declared structs."""
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            typedef struct MyStruct MyStruct;
            int get_val(MyStruct *s);
            struct MyStruct { int x; int y; };
            int get_val(MyStruct *s) { return s->x + s->y; }
            int main(){
                struct MyStruct m;
                m.x = 3;
                m.y = 4;
                return get_val(&m);
            }
        """,
            optimize=False,
        )
        assert ret == 7

    def test_union_member_preserves_forward_declared_struct_ptr_type(self):
        """Union members and comma expressions should preserve semantic pointer types."""
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            #define PASS(c, e) ((c), (e))
            typedef struct Node Node;
            typedef union Value {
                Node *gc;
                long long raw;
            } Value;
            struct Node {
                Value next;
                int marked;
            };
            int main(){
                Node *p = malloc(sizeof(Node));
                p->next.gc = p;
                p->marked = 77;
                int direct = p->next.gc->marked;
                int via_comma = PASS(0, p->next.gc)->marked;
                free(p);
                return direct == 77 && via_comma == 77;
            }
        """,
            optimize=False,
        )
        assert ret == 1


class TestMacroStructMember(unittest.TestCase):
    def test_macro_expands_to_struct_fields(self):
        pcc = CEvaluator()
        ret = pcc.evaluate("""
            #define HEADER int type; int flags
            int main(){
                struct { HEADER; int val; } obj;
                obj.type = 1;
                obj.flags = 2;
                obj.val = 3;
                return obj.type + obj.flags + obj.val;
            }
        """)
        assert ret == 6


class TestVoidStarAssignment(unittest.TestCase):
    def test_void_star_to_int_star(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            int main(){
                void *v = malloc(8);
                int *p = v;
                *p = 42;
                int r = *p;
                free(v);
                return r;
            }
        """,
            optimize=False,
        )
        assert ret == 42


class TestTypedefChain(unittest.TestCase):
    def test_double_typedef(self):
        pcc = CEvaluator()
        ret = pcc.evaluate("""
            typedef unsigned char byte;
            typedef byte u8;
            int main(){
                u8 x = 42;
                return x;
            }
        """)
        assert ret == 42


if __name__ == "__main__":
    unittest.main()
