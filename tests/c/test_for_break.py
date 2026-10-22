import sys
sys.path.insert(0, "../pcc")
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator

import unittest

class TestWhile(unittest.TestCase):


    # def _assert_body(self, toplevel, expected):
    #     """Assert the flattened body of the given toplevel function"""
    #     self.assertIsInstance(toplevel, FunctionAST)
    #     self.assertEqual(self._flatten(toplevel.body), expected)

    def test_simple(self):
        # Evaluate some code.
        pcc = CEvaluator()

        # kalei.evaluate('def binary: 1 (x y) y')
        # Can't have comment
        # no continue here TODO
        ret = pcc.evaluate('''
            int main(){

                int len = 100;
                int sum =  0 ;
                int i = 1;

                for(i=1; i <= len; i++){
                    sum += i;
                    if (i == 10) {
                        break;
                    }
                    else{
                        continue;
                    }

                }
                return sum ;
            }
            ''', llvmdump=True)
        print(ret)
        assert(ret == 55)


#TODO  If is complext should finish the basic
if __name__ == '__main__':

    # This is a good point to self start main
    # print(pcc.evaluate('main()'))
    unittest.main()
