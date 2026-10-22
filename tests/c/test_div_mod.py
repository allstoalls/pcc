import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestDivMod(unittest.TestCase):
    def test_int_division(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a = 10;
                int b = 3;
                return a / b;
            }
            ''', llvmdump=True)
        assert ret == 3

    def test_int_modulo(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a = 10;
                int b = 3;
                return a % b;
            }
            ''', llvmdump=True)
        assert ret == 1

    def test_division_negative(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a = -7;
                int b = 2;
                return a / b;
            }
            ''', llvmdump=True)
        assert ret == -3

    def test_modulo_combined(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a = 17;
                int b = 5;
                return (a / b) * b + (a % b);
            }
            ''', llvmdump=True)
        assert ret == 17


if __name__ == '__main__':
    unittest.main()
