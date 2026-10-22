import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestElseIf(unittest.TestCase):
    def test_else_if_chain(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int x = 15;
                if (x > 20) return 3;
                else if (x > 10) return 2;
                else return 1;
            }
        ''')
        assert ret == 2

    def test_else_if_first_branch(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int x = 25;
                if (x > 20) return 3;
                else if (x > 10) return 2;
                else return 1;
            }
        ''')
        assert ret == 3

    def test_else_if_last_branch(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int x = 5;
                if (x > 20) return 3;
                else if (x > 10) return 2;
                else return 1;
            }
        ''')
        assert ret == 1


if __name__ == '__main__':
    unittest.main()
