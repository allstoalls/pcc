import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestInt(unittest.TestCase):
    def test_assignment(self):
        pcc = CEvaluator()

        ret = pcc.evaluate('''
            double main(){
                double a = 3;
                double b = 4;

                return b - a ;
            }
            ''', llvmdump=True)

        assert ret == 1

# TODO  If is complext should finish the basic
if __name__ == '__main__':
    # Evaluate some code.
    unittest.main()
    # This is a good point to self start main
    # print(pcc.evaluate('main()'))
