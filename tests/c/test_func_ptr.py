"""Tests for function pointers."""
import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestFuncPtr(unittest.TestCase):
    def test_basic(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int add(int a, int b){ return a + b; }
            int main(){
                int (*fp)(int, int) = add;
                return fp(3, 4);
            }
        ''', optimize=False)
        assert ret == 7

    def test_address_of_function_initializer(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int add(int a, int b){ return a + b; }
            int main(){
                int (*fp)(int, int) = &add;
                return fp(3, 4);
            }
        ''', optimize=False)
        assert ret == 7

    def test_reassign(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int add(int a, int b){ return a + b; }
            int mul(int a, int b){ return a * b; }
            int main(){
                int (*fp)(int, int) = add;
                int r1 = fp(3, 4);
                fp = mul;
                int r2 = fp(3, 4);
                return r1 + r2;
            }
        ''', optimize=False)
        assert ret == 19  # 7 + 12

    def test_as_parameter(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int apply(int (*f)(int), int x){ return f(x); }
            int square(int x){ return x * x; }
            int main(){ return apply(square, 5); }
        ''', optimize=False)
        assert ret == 25

    def test_address_of_function_as_parameter(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int apply(int (*f)(int), int x){ return f(x); }
            int square(int x){ return x * x; }
            int main(){ return apply(&square, 5); }
        ''', optimize=False)
        assert ret == 25

    def test_typedef_func_ptr(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            typedef int (*binop)(int, int);
            int add(int a, int b){ return a + b; }
            int sub(int a, int b){ return a - b; }
            int main(){
                binop f = add;
                int r1 = f(10, 3);
                f = sub;
                int r2 = f(10, 3);
                return r1 + r2;
            }
        ''', optimize=False)
        assert ret == 20  # 13 + 7

    def test_callback_pattern(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            void foreach(int *a, int n, void (*fn)(int *)){
                int i;
                for(i = 0; i < n; i++) fn(a + i);
            }
            void doubler(int *p){ *p = *p * 2; }
            int main(){
                int a[3] = {1, 2, 3};
                foreach(a, 3, doubler);
                return a[0] + a[1] + a[2];
            }
        ''', optimize=False)
        assert ret == 12  # 2+4+6

    def test_local_function_pointer_can_be_initialized_to_null(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            typedef void (*fn_t)(int);
            int main(){
                fn_t fn = 0;
                return fn == 0 ? 0 : 1;
            }
        ''', optimize=False)
        assert ret == 0

    def test_comparator_pattern(self):
        """qsort-style comparator function pointer."""
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            void sort2(int *a, int *b, int (*cmp)(int, int)){
                if (cmp(*a, *b) > 0) {
                    int t = *a; *a = *b; *b = t;
                }
            }
            int ascending(int a, int b){ return a - b; }
            int descending(int a, int b){ return b - a; }
            int main(){
                int x = 5; int y = 3;
                sort2(&x, &y, ascending);
                int r1 = x * 10 + y;  // x=3, y=5 -> 35
                x = 5; y = 3;
                sort2(&x, &y, descending);
                int r2 = x * 10 + y;  // x=5, y=3 -> 53
                return r1 + r2;
            }
        ''', optimize=False)
        assert ret == 88  # 35 + 53

    def test_static_tagged_struct_initializer_keeps_function_pointers(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            typedef struct File File;
            struct Methods;
            struct File { const struct Methods *pMethods; };
            struct Methods {
                int iVersion;
                int (*xClose)(File*);
                int (*xWrite)(File*, const void*, int, long);
            };
            static int closef(File *x){ return x != 0; }
            static int writef(File *x, const void *p, int n, long o){ return 2; }
            static const struct Methods M = { 1, closef, writef };
            int main(void){
                File fobj;
                fobj.pMethods = &M;
                return fobj.pMethods->xWrite(&fobj, 0, 0, 0L) == 2 ? 0 : 1;
            }
        ''', optimize=False)
        assert ret == 0

    def test_global_initializer_keeps_cast_function_pointer(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            '''
            typedef int (*generic_fn)(void);
            static int foo(void){ return 7; }
            static generic_fn fp = (generic_fn)foo;
            int main(void){
                return fp && fp() == 7 ? 0 : 1;
            }
            ''',
            optimize=False,
        )
        assert ret == 0

    def test_static_struct_array_keeps_cast_function_pointers(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            '''
            typedef int (*generic_fn)(void);
            struct Entry {
                const char *name;
                generic_fn current;
                generic_fn fallback;
            };
            static int foo(void){ return 7; }
            static struct Entry table[] = {
                { "a", (generic_fn)foo, 0 },
                { "b", 0, 0 },
                { "c", (generic_fn)foo, 0 },
            };
            int main(void){
                return table[0].current
                    && table[2].current
                    && table[0].current() == 7
                    && table[2].current() == 7
                    ? 0 : 1;
            }
            ''',
            optimize=False,
        )
        assert ret == 0

    def test_global_struct_void_pointer_initializer_keeps_function_symbol(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            '''
            struct Wrap { void *func; };
            int global;
            void inc(void){ global++; }
            struct Wrap global_wrap[] = {
                ((struct Wrap){inc}),
                inc,
            };
            int main(void){
                void (*p)(void);
                p = global_wrap[0].func; p();
                p = global_wrap[1].func; p();
                return global == 2 ? 0 : 1;
            }
            ''',
            optimize=False,
        )
        assert ret == 0


if __name__ == '__main__':
    unittest.main()
