import sys
import os
this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator


import unittest

class TestArray(unittest.TestCase):


    # def _assert_body(self, toplevel, expected):
    #     """Assert the flattened body of the given toplevel function"""
    #     self.assertIsInstance(toplevel, FunctionAST)
    #     self.assertEqual(self._flatten(toplevel.body), expected)

    def test_array(self):
        # Evaluate some code.
        pcc = CEvaluator()

        #kalei.evaluate('def binary: 1 (x y) y')
        ret = pcc.evaluate('''
            int main(){
                int i = 1;
                int j = 1;
                int a[100][20];
                int len = 100;
                int len2 = 20;
                int sum =  0 ;

                for(i = 0; i < len ; i++){
                    for(j=0; j<len2; j++){
                        a[i][j] =  2;
                    }

                }

                for(i = 0; i < len ; i++){
                    for(j=0; j<len2; j++){
                        sum += a[i][j];
                    }

                }

                return sum ;
            }
            ''', llvmdump=True)

        assert (ret == 4000)

if __name__ == '__main__':
    unittest.main()

