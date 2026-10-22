import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestForInfinite(unittest.TestCase):
    def test_for_infinite_with_break(self):
        """Test for(;;) with break."""
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int i = 0;
                for(;;){
                    i++;
                    if (i == 10) {
                        break;
                    }
                }
                return i;
            }
            ''', llvmdump=True)
        assert ret == 10

    def test_while_one(self):
        """Test while(1) with integer condition."""
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int i = 0;
                while(1){
                    i++;
                    if (i >= 5) {
                        break;
                    }
                }
                return i;
            }
            ''', llvmdump=True)
        assert ret == 5

    def test_for_empty_init_and_next(self):
        """Test for loop with only condition."""
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int i = 0;
                for(; i < 5;){
                    i++;
                }
                return i;
            }
            ''', llvmdump=True)
        assert ret == 5


if __name__ == '__main__':
    unittest.main()
