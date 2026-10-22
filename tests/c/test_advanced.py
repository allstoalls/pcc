"""Advanced feature tests: nested structs, typedef structs, array decay, pointer subtraction."""
import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestNestedStruct(unittest.TestCase):
    def test_nested_struct_access(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                struct {
                    int a;
                    struct { int x; int y; } inner;
                } s;
                s.a = 1;
                s.inner.x = 10;
                s.inner.y = 20;
                return s.a + s.inner.x + s.inner.y;
            }
        ''')
        assert ret == 31


class TestTypedefStruct(unittest.TestCase):
    def test_typedef_struct(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            typedef struct { int x; int y; } Point;
            int main(){
                Point p;
                p.x = 3;
                p.y = 4;
                return p.x + p.y;
            }
        ''')
        assert ret == 7


class TestArrayDecay(unittest.TestCase):
    def test_array_to_pointer_in_call(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            void fill(int *a, int n, int v){
                int i;
                for(i = 0; i < n; i++){
                    *(a + i) = v;
                }
            }
            int main(){
                int a[5] = {0, 0, 0, 0, 0};
                fill(a, 5, 7);
                return a[0] + a[4];
            }
        ''', optimize=False)
        assert ret == 14

    def test_array_sum_via_ptr(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int sum(int *arr, int n){
                int s = 0;
                int i;
                for(i = 0; i < n; i++){
                    s += *(arr + i);
                }
                return s;
            }
            int main(){
                int a[4] = {10, 20, 30, 40};
                return sum(a, 4);
            }
        ''', optimize=False)
        assert ret == 100


class TestPtrSubtraction(unittest.TestCase):
    def test_ptr_minus_ptr(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a[5] = {1, 2, 3, 4, 5};
                int *p = &a[0];
                int *q = &a[3];
                return q - p;
            }
        ''', optimize=False)
        assert ret == 3

    def test_char_pointer_subtraction_uses_byte_offsets(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            struct Mixed {
                char a;
                int b;
                char c;
            };
            int main(){
                struct Mixed m;
                return (int)((char*)&m.b - (char*)&m);
            }
        ''', optimize=False)
        assert ret == 4


class TestEnumBitwise(unittest.TestCase):
    def test_enum_flags(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            enum { READ = 1, WRITE = 2, EXEC = 4 };
            int main(){
                int perms = READ | WRITE | EXEC;
                return perms;
            }
        ''')
        assert ret == 7


class TestArrayLength(unittest.TestCase):
    def test_sizeof_array_div(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a[10];
                return sizeof(a) / sizeof(int);
            }
        ''')
        assert ret == 10


class TestFunctionDeclarationCompatibility(unittest.TestCase):
    def test_empty_parameter_list_declaration_matches_definition(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main();
            int main(){
                return 0;
            }
        ''', optimize=False)
        assert ret == 0

    def test_extern_empty_parameter_list_matches_definition(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            extern void touch();
            int g = 0;
            void touch(){
                g = 7;
            }
            int main(){
                touch();
                return g;
            }
        ''', optimize=False)
        assert ret == 7

    def test_empty_parameter_list_definition_accepts_extra_aggregate_arg(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            struct S {
                int *a;
                int b:16;
                unsigned int p:9;
            } t;

            unsigned int foo() {
                return t.p;
            }

            int main() {
                t.p = 8;
                if (foo(t) != 8)
                    return 1;
                return 0;
            }
        ''', optimize=False)
        assert ret == 0

    def test_prior_prototype_conflicts_with_empty_parameter_list_definition(self):
        pcc = CEvaluator()
        with self.assertRaisesRegex(Exception, "conflicting types for function 'blapp'"):
            pcc.evaluate('''
                void blapp(int);
                void blapp() { }
                int main(){ return 0; }
            ''', optimize=False)


class TestLocalExternBindings(unittest.TestCase):
    def test_block_scope_extern_reuses_file_scope_definition(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int v = 3;
            int main(){
                int v = 4;
                {
                    extern int v;
                    if (v != 3) return 1;
                }
                return 0;
            }
        ''', optimize=False)
        assert ret == 0


class TestOffsetofLikeFieldAddress(unittest.TestCase):
    def test_null_struct_pointer_field_address_lowers_to_field_offset(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            struct S {
                int i[18];
                char f;
                char b[2];
            };
            int main(){
                return (int)(unsigned long)&((struct S *)0)->b;
            }
        ''', optimize=False)
        assert ret == 73

    def test_non_null_struct_pointer_field_address_preserves_runtime_base(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            struct S {
                int i;
                int j;
            };
            struct R {
                int k;
                struct S a;
            };
            struct R g;
            int main(){
                struct S *b = &((struct R *)&g)->a;
                g.a.i = 0;
                b->i = 3;
                return g.a.i;
            }
        ''', optimize=False)
        assert ret == 3


class TestOversizedArrayRejection(unittest.TestCase):
    def test_global_array_at_clang_limit_is_rejected(self):
        pcc = CEvaluator()
        with self.assertRaisesRegex(Exception, r"array is too large"):
            pcc.evaluate('''
                typedef unsigned long long U;
                char buf[(1ULL << 61)];
                int main(){ return sizeof(buf); }
            ''', optimize=False)


class TestForwardLabelIntoBlock(unittest.TestCase):
    def test_goto_into_block_after_uninitialized_decl(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                goto inner;
                {
                    int b;
                inner:
                    b = 1234;
                    return b;
                }
            }
        ''', optimize=False)
        assert ret == 1234


class TestBindingMetadataIsolation(unittest.TestCase):
    def test_unsigned_binding_tags_do_not_leak_across_functions(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int test2(unsigned int x){
                return -((int)(x >> 31));
            }

            int test3(int x){
                int y;
                y = 31;
                return -(x >> y);
            }

            int main(){
                if (test2((unsigned int)-1) != -1)
                    return 1;
                if (test3(-1) != 1)
                    return 2;
                return 0;
            }
        ''', optimize=False)
        assert ret == 0


class TestPowerFunction(unittest.TestCase):
    def test_power_2_10(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int power(int base, int exp){
                int r = 1;
                int i;
                for(i = 0; i < exp; i++){
                    r *= base;
                }
                return r;
            }
            int main(){ return power(2, 10); }
        ''')
        assert ret == 1024


if __name__ == '__main__':
    unittest.main()
