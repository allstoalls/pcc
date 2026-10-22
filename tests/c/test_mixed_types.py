"""Test mixed int/double type promotion in expressions."""
import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestMixedTypes(unittest.TestCase):
    def test_int_times_double(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                double x = 3.5;
                int y = 2;
                return (int)(x * y);
            }
        ''', optimize=False)
        assert ret == 7

    def test_cast_division(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a = 7;
                int b = 2;
                double r = (double)a / (double)b;
                return (int)(r * 10);
            }
        ''', optimize=False)
        assert ret == 35

    def test_mixed_comparison(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a = 5;
                double b = 2.5;
                if (a > b) return 1;
                return 0;
            }
        ''', optimize=False)
        assert ret == 1

    def test_int_plus_double(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a = 3;
                double b = 0.14;
                return (int)((a + b) * 100);
            }
        ''', optimize=False)
        assert ret == 314

    def test_double_modulo_not_needed(self):
        """Verify int arithmetic still works after promotion changes."""
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a = 17;
                int b = 5;
                return a % b;
            }
        ''')
        assert ret == 2


if __name__ == '__main__':
    unittest.main()
