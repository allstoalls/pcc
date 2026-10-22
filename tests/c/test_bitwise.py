import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestBitwise(unittest.TestCase):
    def test_and(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a = 12;
                int b = 10;
                return a & b;
            }
            ''', llvmdump=True)
        assert ret == 8  # 1100 & 1010 = 1000

    def test_or(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a = 12;
                int b = 10;
                return a | b;
            }
            ''', llvmdump=True)
        assert ret == 14  # 1100 | 1010 = 1110

    def test_xor(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a = 12;
                int b = 10;
                return a ^ b;
            }
            ''', llvmdump=True)
        assert ret == 6  # 1100 ^ 1010 = 0110

    def test_left_shift(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a = 3;
                return a << 2;
            }
            ''', llvmdump=True)
        assert ret == 12

    def test_right_shift(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a = 20;
                return a >> 2;
            }
            ''', llvmdump=True)
        assert ret == 5


if __name__ == '__main__':
    unittest.main()
