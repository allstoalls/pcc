import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestVoid(unittest.TestCase):
    def test_void_function_call(self):
        """Test calling a void function that modifies a pointer."""
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            void set_val(int *p, int v) {
                *p = v;
                return;
            }
            int main(){
                int x = 0;
                set_val(&x, 42);
                return x;
            }
            ''', llvmdump=True)
        assert ret == 42

    def test_void_function_no_return(self):
        """Test void function without explicit return."""
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int g = 0;
            void set_global() {
                g = 99;
            }
            int main(){
                set_global();
                return g;
            }
            ''', llvmdump=True)
        assert ret == 99


if __name__ == '__main__':
    unittest.main()
