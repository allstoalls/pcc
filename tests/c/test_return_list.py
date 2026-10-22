import sys
import  os
this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)

from pcc.frontends.c.evaluator.c_evaluator import CEvaluator


import unittest


class TestReturnArray(unittest.TestCase):
    """C cannot return an array; these are the three ways programs do it.

    Filling and summing a local array is test_array.py; here the array
    always crosses a function return.
    """

    def test_return_decayed_caller_buffer(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int *fill(int *a, int n){
                int i;
                for (i = 0; i < n; i++) {
                    a[i] = i + 1;
                }
                return a;
            }

            int main(){
                int buf[100];
                int *p = fill(buf, 100);
                int sum = 0;
                int i;
                for (i = 0; i < 100; i++) {
                    sum += p[i];
                }
                return (p == buf) * 10000 + sum;
            }
            ''', llvmdump=True)

        assert (ret == 15050)

    def test_return_struct_wrapping_array_by_value(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            struct Arr { int v[4]; };

            struct Arr make(int base){
                struct Arr r;
                int i;
                for (i = 0; i < 4; i++) {
                    r.v[i] = base + i;
                }
                return r;
            }

            int main(){
                struct Arr a = make(10);
                struct Arr b = make(1);
                a.v[0] = a.v[0] + b.v[3];
                return a.v[0] + a.v[1] * 10 + a.v[2] * 100 + a.v[3] * 1000;
            }
            ''', llvmdump=True)

        # b is a separate copy: writing through a must not disturb it, and
        # b.v[3] == 4 lands in a.v[0].
        assert (ret == 14324)

    def test_return_static_table(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int *squares(void){
                static int table[5];
                int i;
                for (i = 0; i < 5; i++) {
                    table[i] = i * i;
                }
                return table;
            }

            int main(){
                int *t = squares();
                int *u = squares();
                return (t == u) * 100 + t[4] + t[3];
            }
            ''', llvmdump=True)

        assert (ret == 125)

if __name__ == '__main__':
    unittest.main()
