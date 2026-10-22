"""Real-world program tests: Hanoi, Collatz, custom strlen, hashtable, fib iterative."""
import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestTowerOfHanoi(unittest.TestCase):
    def test_5_disks(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int moves = 0;
            void hanoi(int n, int from, int to, int aux){
                if(n == 1){ moves++; return; }
                hanoi(n - 1, from, aux, to);
                moves++;
                hanoi(n - 1, aux, to, from);
            }
            int main(){
                hanoi(5, 1, 3, 2);
                return moves;
            }
        ''', optimize=False)
        assert ret == 31  # 2^5 - 1


class TestCollatz(unittest.TestCase):
    def test_27(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int collatz(int n){
                int steps = 0;
                while(n != 1){
                    if(n % 2 == 0) n = n / 2;
                    else n = 3 * n + 1;
                    steps++;
                }
                return steps;
            }
            int main(){ return collatz(27); }
        ''')
        assert ret == 111


class TestCustomStrlen(unittest.TestCase):
    def test_custom_strlen(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int my_strlen(char *s){
                int n = 0;
                while(*(s + n) != 0) n++;
                return n;
            }
            int main(){ return my_strlen("hello world"); }
        ''', optimize=False)
        assert ret == 11


class TestFibIterative(unittest.TestCase):
    def test_fib_20(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int fib(int n){
                if(n <= 1) return n;
                int a = 0; int b = 1;
                int i;
                for(i = 2; i <= n; i++){
                    int t = a + b;
                    a = b;
                    b = t;
                }
                return b;
            }
            int main(){ return fib(20); }
        ''')
        assert ret == 6765


class TestHashTable(unittest.TestCase):
    def test_simple_hash(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int hash(int key, int size){ return key % size; }
            int main(){
                int table[16];
                int i;
                for(i = 0; i < 16; i++) table[i] = -1;
                table[hash(42, 16)] = 42;
                table[hash(99, 16)] = 99;
                return table[hash(42, 16)] + table[hash(99, 16)];
            }
        ''', optimize=False)
        assert ret == 141


class TestMixedTypeArithmetic(unittest.TestCase):
    def test_pi_approx(self):
        """Compute pi approximation with mixed types."""
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a = 3;
                double b = 0.14159;
                return (int)((a + b) * 100000);
            }
        ''', optimize=False)
        assert ret == 314159


if __name__ == '__main__':
    unittest.main()
