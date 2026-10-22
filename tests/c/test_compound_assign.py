import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestCompoundAssign(unittest.TestCase):
    def test_mul_assign(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a = 5;
                a *= 3;
                return a;
            }
            ''', llvmdump=True)
        assert ret == 15

    def test_div_assign(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a = 20;
                a /= 4;
                return a;
            }
            ''', llvmdump=True)
        assert ret == 5

    def test_mod_assign(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a = 17;
                a %= 5;
                return a;
            }
            ''', llvmdump=True)
        assert ret == 2

    def test_shl_assign(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a = 3;
                a <<= 2;
                return a;
            }
            ''', llvmdump=True)
        assert ret == 12

    def test_shr_assign(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a = 20;
                a >>= 2;
                return a;
            }
            ''', llvmdump=True)
        assert ret == 5

    def test_and_assign(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a = 12;
                a &= 10;
                return a;
            }
            ''', llvmdump=True)
        assert ret == 8

    def test_or_assign(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a = 12;
                a |= 3;
                return a;
            }
            ''', llvmdump=True)
        assert ret == 15

    def test_xor_assign(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a = 12;
                a ^= 10;
                return a;
            }
            ''', llvmdump=True)
        assert ret == 6


if __name__ == '__main__':
    unittest.main()
