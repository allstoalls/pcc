import os
import sys
import pytest

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestInt(unittest.TestCase):
    def test_assignment(self):
        pcc = CEvaluator()

        ret = pcc.evaluate('''
            int main(){
                double a = 3.0;
                int b = 4;
                int c = (int) a;
                return b - c ;
            }
            ''', llvmdump=True)

        assert ret == 1


def test_invalid_struct_scalar_casts_raise_value_error():
    pcc = CEvaluator()

    source = """
        struct foo {
            int a;
        };

        int main(void) {
            struct foo xxx;
            int i;

            xxx = (struct foo)1;
            i = (int)xxx;
            return i;
        }
    """

    with pytest.raises(ValueError, match="invalid cast"):
        pcc.evaluate(source)

# TODO  If is complext should finish the basic
if __name__ == '__main__':
    # Evaluate some code.
    unittest.main()
    # This is a good point to self start main
    # print(pcc.evaluate('main()'))
