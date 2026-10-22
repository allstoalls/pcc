import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestArrow(unittest.TestCase):
    def test_arrow_read_write(self):
        """Test ptr->field access via malloc'd struct."""
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                struct { int x; int y; } *p = malloc(16);
                p->x = 10;
                p->y = 20;
                int result = p->x + p->y;
                free(p);
                return result;
            }
            ''', llvmdump=True)
        assert ret == 30


if __name__ == '__main__':
    unittest.main()
