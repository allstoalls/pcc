import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestTypeConvert(unittest.TestCase):
    def test_char_init_from_int(self):
        """char c = 65; should truncate i64 to i8."""
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                char c = 65;
                int x = c;
                return x;
            }
            ''', llvmdump=True)
        assert ret == 65

    def test_if_int_condition_no_braces(self):
        """if(x) return 1; without braces."""
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int x = 5;
                if(x) return 1;
                return 0;
            }
            ''', llvmdump=True)
        assert ret == 1

    def test_if_zero_condition(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int x = 0;
                if(x) return 1;
                return 0;
            }
            ''', llvmdump=True)
        assert ret == 0

    def test_char_arithmetic(self):
        """char + int should promote correctly."""
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                char c = 10;
                int result = c + 5;
                return result;
            }
            ''', llvmdump=True)
        assert ret == 15


if __name__ == '__main__':
    unittest.main()
