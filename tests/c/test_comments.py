import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestComments(unittest.TestCase):
    def test_block_comment(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('int main(){ /* comment */ return 42; }')
        assert ret == 42

    def test_line_comment(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('int main(){ // comment\n return 42; }')
        assert ret == 42

    def test_multiline_block_comment(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                /* this is
                   a multiline
                   comment */
                return 1;
            }
        ''')
        assert ret == 1

    def test_comment_in_expression(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int x = 5; /* x value */
                int y = 3; // y value
                return x + y;
            }
        ''')
        assert ret == 8


class TestMorePatterns(unittest.TestCase):
    def test_popcount(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int popcount(int n){
                int c = 0;
                while(n){ c += n & 1; n = n >> 1; }
                return c;
            }
            int main(){ return popcount(0xFF); }
        ''')
        assert ret == 8

    def test_palindrome(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int is_pal(int *a, int lo, int hi){
                if(lo >= hi) return 1;
                if(a[lo] != a[hi]) return 0;
                return is_pal(a, lo + 1, hi - 1);
            }
            int main(){
                int a[5] = {1, 2, 3, 2, 1};
                return is_pal(a, 0, 4);
            }
        ''', optimize=False)
        assert ret == 1

    def test_power_of_2(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int is_pow2(int n){
                return n > 0 && (n & (n - 1)) == 0;
            }
            int main(){
                return is_pow2(64) * 10 + is_pow2(65);
            }
        ''')
        assert ret == 10

    def test_large_array_sum(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a[100]; int i;
                for(i = 0; i < 100; i++) a[i] = i;
                int s = 0;
                for(i = 0; i < 100; i++) s += a[i];
                return s;
            }
        ''', optimize=False)
        assert ret == 4950


if __name__ == '__main__':
    unittest.main()
