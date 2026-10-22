import sys
import os
this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator

import unittest

class TestFor(unittest.TestCase):

    def test_simple(self):
        # Evaluate some code.
        pcc = CEvaluator()

        #kalei.evaluate('def binary: 1 (x y) y')
        ret = pcc.evaluate('''
            int len = 100;
            int len2 = 10;    
            
            int main(){
                int i = 1;
                int j = 1;

                int sum =  0 ;

                for(i=1; i <= len ; i++){
                    sum += i ;

                    for(j=1; j<= len2; j++){
                        sum += j;
                    }
                }

                return sum;
            }
            ''', llvmdump=False)

        assert (ret == 10550)
    # This is a good point to self start main
    # print(pcc.evaluate('main()'))

if __name__ == "__main__":
    unittest.main()