"""Algorithm tests: sieve, binary search, selection sort, matrix multiply."""
import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestSieve(unittest.TestCase):
    def test_primes_up_to_50(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int sieve[51];
                int i; int j;
                for(i = 0; i <= 50; i++) sieve[i] = 1;
                sieve[0] = 0; sieve[1] = 0;
                for(i = 2; i * i <= 50; i++){
                    if(sieve[i]){
                        for(j = i * i; j <= 50; j += i)
                            sieve[j] = 0;
                    }
                }
                int count = 0;
                for(i = 2; i <= 50; i++)
                    if(sieve[i]) count++;
                return count;
            }
        ''', optimize=False)
        assert ret == 15


class TestBinarySearch(unittest.TestCase):
    def test_found(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int bs(int *a, int n, int target){
                int lo = 0; int hi = n - 1;
                while(lo <= hi){
                    int mid = lo + (hi - lo) / 2;
                    if(a[mid] == target) return mid;
                    else if(a[mid] < target) lo = mid + 1;
                    else hi = mid - 1;
                }
                return -1;
            }
            int main(){
                int a[8] = {2, 5, 8, 12, 16, 23, 38, 56};
                return bs(a, 8, 23);
            }
        ''', optimize=False)
        assert ret == 5

    def test_not_found(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int bs(int *a, int n, int target){
                int lo = 0; int hi = n - 1;
                while(lo <= hi){
                    int mid = lo + (hi - lo) / 2;
                    if(a[mid] == target) return mid;
                    else if(a[mid] < target) lo = mid + 1;
                    else hi = mid - 1;
                }
                return -1;
            }
            int main(){
                int a[5] = {1, 3, 5, 7, 9};
                return bs(a, 5, 4);
            }
        ''', optimize=False)
        assert ret == -1


class TestSelectionSort(unittest.TestCase):
    def test_sort(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            void swap(int *a, int *b){ int t = *a; *a = *b; *b = t; }
            void ssort(int *a, int n){
                int i; int j;
                for(i = 0; i < n - 1; i++){
                    int min = i;
                    for(j = i + 1; j < n; j++){
                        if(a[j] < a[min]) min = j;
                    }
                    if(min != i) swap(a + i, a + min);
                }
            }
            int main(){
                int a[5] = {5, 3, 1, 4, 2};
                ssort(a, 5);
                return a[0]*10000 + a[1]*1000 + a[2]*100 + a[3]*10 + a[4];
            }
        ''', optimize=False)
        assert ret == 12345


class TestMatrixMultiply(unittest.TestCase):
    def test_2x2(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a[2][2] = {{1, 2}, {3, 4}};
                int b[2][2] = {{5, 6}, {7, 8}};
                int c[2][2] = {{0, 0}, {0, 0}};
                int i; int j; int k;
                for(i = 0; i < 2; i++)
                    for(j = 0; j < 2; j++)
                        for(k = 0; k < 2; k++)
                            c[i][j] += a[i][k] * b[k][j];
                return c[0][0]*1000 + c[0][1]*100 + c[1][0]*10 + c[1][1];
            }
        ''', optimize=False)
        # [[1,2],[3,4]] * [[5,6],[7,8]] = [[19,22],[43,50]]
        assert ret == 19 * 1000 + 22 * 100 + 43 * 10 + 50


class TestPointerSubscript(unittest.TestCase):
    def test_ptr_bracket_access(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int sum(int *a, int n){
                int s = 0;
                int i;
                for(i = 0; i < n; i++) s += a[i];
                return s;
            }
            int main(){
                int a[5] = {1, 2, 3, 4, 5};
                return sum(a, 5);
            }
        ''', optimize=False)
        assert ret == 15


if __name__ == '__main__':
    unittest.main()
