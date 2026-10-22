import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestCastIntToDouble(unittest.TestCase):
    def test_int_to_double_cast(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a = 7;
                double b = (double)a;
                int result = (int)(b + 0.5);
                return result;
            }
            ''', llvmdump=True)
        assert ret == 7


if __name__ == '__main__':
    unittest.main()
