import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestPreIncDec(unittest.TestCase):
    def test_pre_increment(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a = 5;
                int b = ++a;
                return b;
            }
            ''', llvmdump=True)
        assert ret == 6

    def test_pre_increment_var_updated(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a = 5;
                ++a;
                return a;
            }
            ''', llvmdump=True)
        assert ret == 6

    def test_pre_decrement(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a = 5;
                int b = --a;
                return b;
            }
            ''', llvmdump=True)
        assert ret == 4

    def test_post_increment_returns_old(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a = 5;
                int b = a++;
                return b;
            }
            ''', llvmdump=True)
        assert ret == 5

    def test_post_increment_updates_var(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a = 5;
                a++;
                return a;
            }
            ''', llvmdump=True)
        assert ret == 6

    def test_post_decrement_returns_old(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a = 5;
                int b = a--;
                return b;
            }
            ''', llvmdump=True)
        assert ret == 5


if __name__ == '__main__':
    unittest.main()
