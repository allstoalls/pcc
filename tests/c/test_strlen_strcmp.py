import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestStrlen(unittest.TestCase):
    def test_strlen(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(r'''
            int main(){
                return strlen("hello");
            }
            ''', llvmdump=True)
        assert ret == 5

    def test_strcmp_equal(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(r'''
            int main(){
                return strcmp("abc", "abc");
            }
            ''', llvmdump=True)
        assert ret == 0

    def test_strcmp_different(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(r'''
            int main(){
                int r = strcmp("abc", "abd");
                if (r < 0) return 1;
                return 0;
            }
            ''', llvmdump=True)
        assert ret == 1  # "abc" < "abd"


if __name__ == '__main__':
    unittest.main()
