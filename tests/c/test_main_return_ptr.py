import os
import sys
import ctypes

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)

from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestMainReturnPtr(unittest.TestCase):
    def test_simple(self):
        pcc = CEvaluator()

        ret = pcc.evaluate('''

            int a = 50;
            int b = 4;
            int* swap(int *x, int *y){
                int tmp;
                tmp = *x;
                *x = *y;
                *y = tmp;
                return x;
            }

            int* main(){
                swap(&a, &b);
                return &a ;
            }
            ''', llvmdump=True)

        # ret_value = ret.contents
        # so the global var
        assert ret.contents.value == 4

if __name__ == "__main__":
    unittest.main()