"""Edge case tests: chained assignment, negative enum, char index, multi-dim array init."""
import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestChainAssignment(unittest.TestCase):
    def test_chain_assign(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a; int b; int c;
                a = b = c = 5;
                return a + b + c;
            }
        ''')
        assert ret == 15


class TestNegativeEnum(unittest.TestCase):
    def test_negative_enum(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            enum { NEG = -1, ZERO = 0, POS = 1 };
            int main(){
                return NEG + ZERO + POS;
            }
        ''')
        assert ret == 0

    def test_enum_expression(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            enum { A = 1 << 0, B = 1 << 1, C = 1 << 2 };
            int main(){
                return A | B | C;
            }
        ''')
        assert ret == 7


class TestCharArrayIndex(unittest.TestCase):
    def test_char_as_index(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a[3] = {10, 20, 30};
                char idx = 1;
                return a[idx];
            }
        ''', optimize=False)
        assert ret == 20


class TestMultiDimArrayInit(unittest.TestCase):
    def test_2d_array_init(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a[2][3] = {{1, 2, 3}, {4, 5, 6}};
                return a[1][2];
            }
        ''', optimize=False)
        assert ret == 6

    def test_2d_array_sum(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a[2][2] = {{1, 2}, {3, 4}};
                return a[0][0] + a[0][1] + a[1][0] + a[1][1];
            }
        ''', optimize=False)
        assert ret == 10


class TestBitwiseInCondition(unittest.TestCase):
    def test_bitwise_and_in_if(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int flags = 5;
                if (flags & 4) return 1;
                return 0;
            }
        ''')
        assert ret == 1


class TestPreDecInWhile(unittest.TestCase):
    def test_predec_in_while_cond(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int i = 5;
                int s = 0;
                while (--i > 0) {
                    s += i;
                }
                return s;
            }
        ''')
        assert ret == 10  # 4+3+2+1


if __name__ == '__main__':
    unittest.main()
