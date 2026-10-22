import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestTernary(unittest.TestCase):
    def test_ternary_true(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a = 5;
                int b = 3;
                int max = (a > b) ? a : b;
                return max;
            }
            ''', llvmdump=True)
        assert ret == 5

    def test_ternary_false(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a = 2;
                int b = 7;
                int max = (a > b) ? a : b;
                return max;
            }
            ''', llvmdump=True)
        assert ret == 7

    def test_ternary_nested(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a = 5;
                int b = 5;
                int result = (a > b) ? 1 : ((a == b) ? 0 : -1);
                return result;
            }
            ''', llvmdump=True)
        assert ret == 0

    def test_ternary_condition_with_local_variable_is_not_constant_folded(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int x = 0;
                int y = 1;
                if (x ? 1 : 0)
                    return 1;
                if (y ? 0 : 1)
                    return 2;
                return 0;
            }
            ''', llvmdump=True)
        assert ret == 0


if __name__ == '__main__':
    unittest.main()
