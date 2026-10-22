import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestHexOctal(unittest.TestCase):
    def test_hex_literal(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('int main(){ return 0xFF; }')
        assert ret == 255

    def test_hex_lowercase(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('int main(){ return 0x1a; }')
        assert ret == 26

    def test_hex_in_expression(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('int main(){ return 0x10 + 0x20; }')
        assert ret == 48

    def test_octal_literal(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('int main(){ return 077; }')
        assert ret == 63

    def test_octal_in_expression(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('int main(){ return 010 + 1; }')
        assert ret == 9

    def test_hex_enum(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            enum { FLAG_A = 0x01, FLAG_B = 0x02, FLAG_C = 0x04 };
            int main(){ return FLAG_A | FLAG_B | FLAG_C; }
        ''')
        assert ret == 7


if __name__ == '__main__':
    unittest.main()
