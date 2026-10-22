import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestTypedef(unittest.TestCase):
    def test_typedef_int(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            typedef int myint;
            int main(){
                myint x = 42;
                return x;
            }
        ''')
        assert ret == 42

    def test_typedef_double(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            typedef double real;
            int main(){
                real x = 3.5;
                int r = (int)(x + 0.5);
                return r;
            }
        ''')
        assert ret == 4


if __name__ == '__main__':
    unittest.main()
