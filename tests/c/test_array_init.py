import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestArrayInit(unittest.TestCase):
    def test_array_init_list(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a[3] = {10, 20, 30};
                return a[1];
            }
            ''', llvmdump=True)
        assert ret == 20

    def test_array_init_sum(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a[4] = {1, 2, 3, 4};
                return a[0] + a[1] + a[2] + a[3];
            }
            ''', llvmdump=True)
        assert ret == 10


if __name__ == '__main__':
    unittest.main()
