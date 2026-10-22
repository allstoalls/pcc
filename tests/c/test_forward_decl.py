import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestForwardDecl(unittest.TestCase):
    def test_forward_declaration(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int foo(int x);
            int main() { return foo(5); }
            int foo(int x) { return x * 2; }
        ''')
        assert ret == 10

    def test_mutual_reference(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int is_even(int n);
            int is_odd(int n);
            int is_even(int n) {
                if (n == 0) return 1;
                return is_odd(n - 1);
            }
            int is_odd(int n) {
                if (n == 0) return 0;
                return is_even(n - 1);
            }
            int main() { return is_even(4); }
        ''')
        assert ret == 1

    def test_static_forward_declaration_without_definition_is_allowed(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            static int helper(void);
            int main(void) { return 0; }
        ''')
        assert ret == 0


if __name__ == '__main__':
    unittest.main()
