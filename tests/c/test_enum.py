import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestEnum(unittest.TestCase):
    def test_enum_basic(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            enum Color { RED, GREEN, BLUE };
            int main(){
                int c = GREEN;
                return c;
            }
            ''', llvmdump=True)
        assert ret == 1  # RED=0, GREEN=1, BLUE=2

    def test_enum_explicit_values(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            enum Status { OK = 0, ERR = 10, WARN = 20 };
            int main(){
                return WARN;
            }
            ''', llvmdump=True)
        assert ret == 20

    def test_enum_auto_increment(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            enum Nums { A = 5, B, C };
            int main(){
                return C;
            }
            ''', llvmdump=True)
        assert ret == 7  # A=5, B=6, C=7


if __name__ == '__main__':
    unittest.main()
