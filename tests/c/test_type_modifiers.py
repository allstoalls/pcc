"""Tests for unsigned/long/short/const/size_t type modifiers."""
import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestTypeModifiers(unittest.TestCase):
    def test_unsigned_int(self):
        pcc = CEvaluator()
        assert pcc.evaluate('int main(){ unsigned int x = 42; return x; }') == 42

    def test_long_int(self):
        pcc = CEvaluator()
        assert pcc.evaluate('int main(){ long int y = 100; return y; }') == 100

    def test_short_int(self):
        pcc = CEvaluator()
        assert pcc.evaluate('int main(){ short int s = 100; return s; }') == 100

    def test_long_long(self):
        pcc = CEvaluator()
        assert pcc.evaluate('int main(){ long long x = 123; return x; }') == 123

    def test_unsigned_long(self):
        pcc = CEvaluator()
        assert pcc.evaluate('int main(){ unsigned long x = 42; return x; }') == 42

    def test_const_int(self):
        pcc = CEvaluator()
        assert pcc.evaluate('int main(){ const int x = 99; return x; }') == 99

    def test_signed_int(self):
        pcc = CEvaluator()
        assert pcc.evaluate('int main(){ signed int x = -5; return -x; }') == 5

    def test_size_t(self):
        pcc = CEvaluator()
        assert pcc.evaluate('#include <stddef.h>\nint main(){ size_t n = 42; return n; }') == 42

    def test_size_t_in_function(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            #include <stddef.h>
            int count(int *a, size_t n) {
                int s = 0;
                size_t i;
                for (i = 0; i < n; i++) s += a[i];
                return s;
            }
            int main(){
                int a[3] = {10, 20, 30};
                return count(a, 3);
            }
        ''', optimize=False)
        assert ret == 60

    def test_unsigned_name_does_not_leak_across_scopes(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int u(unsigned idx) { return idx > 0; }
            int f(int idx) {
                if (!((idx) <= (-(32767/2 + 1000))))
                    return 1;
                else if (idx == (-(32767/2 + 1000)))
                    return 2;
                return 3;
            }
            int main() {
                return u(1) + f(-(32767/2 + 1000)) * 10;
            }
        ''', optimize=False)
        assert ret == 21


class TestFuncMacro(unittest.TestCase):
    def test_max_macro(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            #define MAX(a,b) ((a)>(b)?(a):(b))
            int main(){ return MAX(3, 7); }
        ''')
        assert ret == 7

    def test_min_macro(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            #define MIN(a,b) ((a)<(b)?(a):(b))
            int main(){ return MIN(3, 7); }
        ''')
        assert ret == 3

    def test_square_macro(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            #define SQ(x) ((x)*(x))
            int main(){ return SQ(5); }
        ''')
        assert ret == 25

    def test_nested_func_macro(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            #define SQ(x) ((x)*(x))
            int main(){ return SQ(SQ(2)); }
        ''')
        assert ret == 16  # SQ(4) = 16

    def test_multi_param_macro(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            #define CLAMP(x,lo,hi) ((x)<(lo)?(lo):(x)>(hi)?(hi):(x))
            int main(){ return CLAMP(15, 0, 10); }
        ''')
        assert ret == 10


class TestIfDirective(unittest.TestCase):
    def test_if_expression(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            #define VERSION 3
            #if VERSION >= 3
            int g = 1;
            #else
            int g = 0;
            #endif
            int main(){ return g; }
        ''')
        assert ret == 1

    def test_if_defined(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            #define FOO
            #if defined(FOO)
            int g = 1;
            #else
            int g = 0;
            #endif
            int main(){ return g; }
        ''')
        assert ret == 1

    def test_elif_chain(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            #define MODE 2
            #if MODE == 1
            int val = 10;
            #elif MODE == 2
            int val = 20;
            #elif MODE == 3
            int val = 30;
            #else
            int val = 0;
            #endif
            int main(){ return val; }
        ''')
        assert ret == 20

    def test_if_and_or(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            #define A 1
            #define B 0
            #if defined(A) && !defined(C)
            int g = 42;
            #else
            int g = 0;
            #endif
            int main(){ return g; }
        ''')
        assert ret == 42


if __name__ == '__main__':
    unittest.main()
