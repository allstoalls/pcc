import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestShortCircuit(unittest.TestCase):
    def test_and_short_circuit(self):
        """&& should not evaluate rhs if lhs is false."""
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int g = 0;
            void set_g() { g = 1; }
            int main(){
                int x = 0;
                if (x && g) {
                    return 99;
                }
                return g;
            }
            ''', llvmdump=True)
        assert ret == 0  # g should stay 0, rhs not evaluated

    def test_or_short_circuit(self):
        """|| should not evaluate rhs if lhs is true."""
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int x = 1;
                int y = 0;
                if (x || y) {
                    return 1;
                }
                return 0;
            }
            ''', llvmdump=True)
        assert ret == 1

    def test_and_both_true(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a = 3;
                int b = 5;
                if (a && b) return 1;
                return 0;
            }
            ''', llvmdump=True)
        assert ret == 1

    def test_or_both_false(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a = 0;
                int b = 0;
                if (a || b) return 1;
                return 0;
            }
            ''', llvmdump=True)
        assert ret == 0


if __name__ == '__main__':
    unittest.main()
