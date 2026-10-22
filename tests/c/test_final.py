"""Final batch: return type coercion, pointer comparison, reusable evaluator, etc."""
import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestReturnTypeCoercion(unittest.TestCase):
    def test_return_char_from_int_func(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('int main(){ char c = 65; return c; }')
        assert ret == 65

    def test_return_comparison_result(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('int main(){ return 3 > 2; }')
        assert ret == 1


class TestPointerComparison(unittest.TestCase):
    def test_ptr_less_than(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a[3] = {1, 2, 3};
                int *p = &a[0];
                int *q = &a[2];
                return p < q;
            }
        ''', optimize=False)
        assert ret == 1

    def test_ptr_equal(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int x = 42;
                int *p = &x;
                int *q = &x;
                return p == q;
            }
        ''', optimize=False)
        assert ret == 1


class TestEvaluatorReuse(unittest.TestCase):
    def test_multiple_evaluations(self):
        """Evaluator should work for multiple programs."""
        pcc = CEvaluator()
        assert pcc.evaluate('int main(){ return 1; }') == 1
        assert pcc.evaluate('int main(){ return 2; }') == 2
        assert pcc.evaluate('int main(){ return 3; }') == 3


class TestBitmask(unittest.TestCase):
    def test_build_mask(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int mask = 0;
                int i;
                for(i = 0; i < 8; i++) mask |= (1 << i);
                return mask;
            }
        ''')
        assert ret == 255


class TestBin2Dec(unittest.TestCase):
    def test_recursive_conversion(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int bin2dec(int b){
                if(b == 0) return 0;
                return b % 10 + 2 * bin2dec(b / 10);
            }
            int main(){ return bin2dec(1010); }
        ''')
        assert ret == 10


class TestAbsTernary(unittest.TestCase):
    def test_abs(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int myabs(int x){ return x >= 0 ? x : -x; }
            int main(){ return myabs(-42) + myabs(7); }
        ''')
        assert ret == 49


class TestNestedDoWhile(unittest.TestCase):
    def test_nested(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int s = 0; int i = 0;
                do {
                    int j = 0;
                    do { s++; j++; } while(j < 3);
                    i++;
                } while(i < 3);
                return s;
            }
        ''')
        assert ret == 9


class TestVoidReturnInLoop(unittest.TestCase):
    def test_early_return(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int g = 0;
            void count_to(int n){
                int i;
                for(i = 0; i < n; i++){
                    if(i == 3) return;
                    g++;
                }
            }
            int main(){ count_to(10); return g; }
        ''', optimize=False)
        assert ret == 3


if __name__ == '__main__':
    unittest.main()
