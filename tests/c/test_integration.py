"""Integration tests: complex C programs that exercise multiple features together."""
import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestFibonacci(unittest.TestCase):
    def test_fib_10(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int fib(int n) {
                if (n <= 1) return n;
                return fib(n - 1) + fib(n - 2);
            }
            int main() { return fib(10); }
        ''')
        assert ret == 55


class TestFactorial(unittest.TestCase):
    def test_fact_5(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int fact(int n) {
                if (n <= 1) return 1;
                return n * fact(n - 1);
            }
            int main() { return fact(5); }
        ''')
        assert ret == 120


class TestGCD(unittest.TestCase):
    def test_gcd(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int gcd(int a, int b) {
                if (b == 0) return a;
                return gcd(b, a % b);
            }
            int main() { return gcd(48, 18); }
        ''')
        assert ret == 6


class TestBubbleSort(unittest.TestCase):
    def test_sort(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main() {
                int a[5] = {5, 3, 1, 4, 2};
                int i; int j; int tmp;
                for (i = 0; i < 5; i++) {
                    for (j = 0; j < 4 - i; j++) {
                        if (a[j] > a[j + 1]) {
                            tmp = a[j];
                            a[j] = a[j + 1];
                            a[j + 1] = tmp;
                        }
                    }
                }
                return a[0]*10000 + a[1]*1000 + a[2]*100 + a[3]*10 + a[4];
            }
        ''')
        assert ret == 12345


class TestEnumSwitch(unittest.TestCase):
    def test_enum_in_switch(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            enum Dir { UP = 0, DOWN = 1, LEFT = 2, RIGHT = 3 };
            int main() {
                int d = RIGHT;
                int r = 0;
                switch (d) {
                    case 0: r = 10; break;
                    case 1: r = 20; break;
                    case 2: r = 30; break;
                    case 3: r = 40; break;
                }
                return r;
            }
        ''')
        assert ret == 40


class TestStructFunction(unittest.TestCase):
    def test_init_struct_via_ptr(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            void init_point(int *px, int *py, int x, int y) {
                *px = x;
                *py = y;
            }
            int main() {
                struct { int x; int y; } p;
                init_point(&p.x, &p.y, 3, 4);
                return p.x * p.x + p.y * p.y;
            }
        ''')
        assert ret == 25


class TestDoWhileAccumulate(unittest.TestCase):
    def test_sum_1_to_10(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main() {
                int i = 10;
                int s = 0;
                do {
                    s += i;
                    i--;
                } while (i > 0);
                return s;
            }
        ''')
        assert ret == 55


class TestMultiMalloc(unittest.TestCase):
    def test_alloc_and_free(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main() {
                int *a = malloc(8);
                int *b = malloc(8);
                *a = 10;
                *b = 20;
                int sum = *a + *b;
                free(a);
                free(b);
                return sum;
            }
        ''')
        assert ret == 30


class TestComplexExpression(unittest.TestCase):
    def test_nested_ternary_sign(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int sign(int x) {
                return (x > 0) ? 1 : ((x == 0) ? 0 : -1);
            }
            int main() {
                return sign(-5) + sign(0) * 10 + sign(3) * 100;
            }
        ''')
        assert ret == 99  # -1 + 0 + 100

    def test_comparison_returns_int(self):
        """Comparisons should return int, usable directly."""
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main() {
                int x = (3 > 2);
                int y = (1 == 1);
                int z = (5 < 3);
                return x + y + z;
            }
        ''')
        assert ret == 2  # 1 + 1 + 0


class TestForVariants(unittest.TestCase):
    def test_for_empty_body(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main() {
                int i;
                for (i = 0; i < 10; i++);
                return i;
            }
        ''')
        assert ret == 10

    def test_for_infinite_break(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main() {
                int sum = 0;
                int i = 1;
                for (;;) {
                    sum += i;
                    i++;
                    if (i > 100) break;
                }
                return sum;
            }
        ''')
        assert ret == 5050


if __name__ == '__main__':
    unittest.main()
