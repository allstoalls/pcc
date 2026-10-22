import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestIf(unittest.TestCase):

    def test_nest_if_else(self):
        pcc = CEvaluator()

        # kalei.evaluate('def binary: 1 (x y) y')
        ret = pcc.evaluate('''
        
            int main(){
                int a = 1;
                int b = 4;

                if (b < a){
                    b += 3;
                    return a;
                }
                else{
                    b -= 3;
                    return b;
                }

            }
            ''', llvmdump=True)

        assert int(ret) == 1


# TODO  If is complext should finish the basic
if __name__ == '__main__':
    # Evaluate some code.
    unittest.main()
    # This is a good point to self start main
    # print(pcc.evaluate('main()'))
