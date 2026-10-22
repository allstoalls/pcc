import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestMemsetFree(unittest.TestCase):
    def test_memset_and_free(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int *p = malloc(8);
                memset(p, 0, 8);
                *p = 99;
                int val = *p;
                free(p);
                return val;
            }
            ''', llvmdump=True)
        assert ret == 99

    def test_memset_zeroes(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int *p = malloc(8);
                *p = 12345;
                memset(p, 0, 8);
                int val = *p;
                free(p);
                return val;
            }
            ''', llvmdump=True)
        assert ret == 0


if __name__ == '__main__':
    unittest.main()
