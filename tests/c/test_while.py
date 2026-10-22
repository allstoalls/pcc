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


                while( len != 0 ){
                    sum += len;
                    if (len == 2) {
                        break;
                    }
                    else{
                        len--;
                        continue;
                    }

                }

                return sum ;
            }
            ''', llvmdump=True)
        print(ret)
        assert(ret == 5049)

    def test_while_statement_nested_in_for_body_preserves_statement_contract(self):
        ret = CEvaluator().evaluate(
            """
            int main(void) {
                for (; 0;) while (0) {}
                return 1;
            }
            """,
            optimize=False,
            use_compile_cache=False,
        )
        assert ret == 1


#TODO  If is complext should finish the basic
if __name__ == '__main__':

    # This is a good point to self start main
    # print(pcc.evaluate('main()'))
    unittest.main()
