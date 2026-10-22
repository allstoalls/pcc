import sys
sys.path.insert(0, "../pcc")
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator

import unittest

class TestAndOperator(unittest.TestCase):

    def test_and_operator(self):
        pcc = CEvaluator()

        ret = pcc.evaluate('''
            int main() {
                int a = 5;
                int b = 10;
                int c = 15;
                int result;

                if (a == 5 && b < c) {
                    result = 1;
                } else {
                    result = 0;
                }

                return result;
            }
            ''', llvmdump=True)
        print(ret)
        self.assertEqual(ret, 1)

if __name__ == '__main__':
    unittest.main()
