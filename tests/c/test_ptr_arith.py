import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestPtrArith(unittest.TestCase):
    def test_ptr_plus_int(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int *p = malloc(24);
                *p = 10;
                *(p + 1) = 20;
                *(p + 2) = 30;
                int v = *(p + 1);
                free(p);
                return v;
            }
        ''', optimize=False)
        assert ret == 20

    def test_ptr_increment(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a[3] = {10, 20, 30};
                int *p = &a[0];
                p++;
                return *p;
            }
        ''', optimize=False)
        assert ret == 20

    def test_ptr_walk_array(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a[4] = {1, 2, 3, 4};
                int *p = &a[0];
                int sum = 0;
                int i;
                for (i = 0; i < 4; i++) {
                    sum += *p;
                    p++;
                }
                return sum;
            }
        ''', optimize=False)
        assert ret == 10


if __name__ == '__main__':
    unittest.main()
