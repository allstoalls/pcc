"""Showcase tests: complex algorithms proving the compiler's capabilities."""
import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestQuicksort(unittest.TestCase):
    def test_sort_8_elements(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            void swap(int *a, int *b){ int t = *a; *a = *b; *b = t; }
            int partition(int *a, int lo, int hi){
                int pivot = a[hi]; int i = lo - 1; int j;
                for(j = lo; j < hi; j++){
                    if(a[j] <= pivot){ i++; swap(a + i, a + j); }
                }
                swap(a + i + 1, a + hi);
                return i + 1;
            }
            void qs(int *a, int lo, int hi){
                if(lo < hi){
                    int p = partition(a, lo, hi);
                    qs(a, lo, p - 1);
                    qs(a, p + 1, hi);
                }
            }
            int main(){
                int a[8] = {38, 27, 43, 3, 9, 82, 10, 1};
                qs(a, 0, 7);

                return a[0] + a[7];
            }
        ''', optimize=False)
        assert ret == 83  # 1 + 82


class TestAckermann(unittest.TestCase):
    def test_3_3(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int ack(int m, int n){
                if(m == 0) return n + 1;
                if(n == 0) return ack(m - 1, 1);
                return ack(m - 1, ack(m, n - 1));
            }
            int main(){ return ack(3, 3); }
        ''')
        assert ret == 61


class TestROT13(unittest.TestCase):
    def test_uppercase(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int rot13(int c){
                if(c >= 65 && c <= 90) return (c - 65 + 13) % 26 + 65;
                if(c >= 97 && c <= 122) return (c - 97 + 13) % 26 + 97;
                return c;
            }
            int main(){

                return rot13(rot13(65));
            }
        ''')
        assert ret == 65  # ROT13 applied twice = identity


class TestGameOfLife(unittest.TestCase):
    def test_blinker(self):
        """Blinker oscillator: vertical line becomes horizontal after one step."""
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int get(int *g, int r, int c, int n){
                if(r < 0 || r >= n || c < 0 || c >= n) return 0;
                return g[r * n + c];
            }
            int neighbors(int *g, int r, int c, int n){
                int s = 0; int dr; int dc;
                for(dr = -1; dr <= 1; dr++)
                    for(dc = -1; dc <= 1; dc++)
                        if(dr != 0 || dc != 0)
                            s += get(g, r + dr, c + dc, n);
                return s;
            }
            int main(){
                int g[25] = {0,0,0,0,0,
                             0,0,1,0,0,
                             0,0,1,0,0,
                             0,0,1,0,0,
                             0,0,0,0,0};
                int next[25];
                int r; int c; int n = 5;
                for(r = 0; r < n; r++){
                    for(c = 0; c < n; c++){
                        int nb = neighbors(g, r, c, n);
                        int alive = g[r * n + c];
                        if(alive && (nb == 2 || nb == 3)) next[r*n+c] = 1;
                        else if(!alive && nb == 3) next[r*n+c] = 1;
                        else next[r*n+c] = 0;
                    }
                }

                return next[2*5+1] + next[2*5+2] + next[2*5+3];
            }
        ''', optimize=False)
        assert ret == 3  # all three cells in row 2 are alive


class TestGCDSubtraction(unittest.TestCase):
    def test_euclidean_subtraction(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int gcd(int a, int b){
                while(a != b){
                    if(a > b) a -= b;
                    else b -= a;
                }
                return a;
            }
            int main(){ return gcd(252, 105); }
        ''')
        assert ret == 21


if __name__ == '__main__':
    unittest.main()
