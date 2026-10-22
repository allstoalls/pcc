import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestStaticLocal(unittest.TestCase):
    def test_static_counter(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int counter(){
                static int n = 0;
                n++;
                return n;
            }
            int main(){
                counter();
                counter();
                return counter();
            }
        ''', optimize=False)
        assert ret == 3

    def test_static_accumulator(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int acc(int x){
                static int sum = 0;
                sum += x;
                return sum;
            }
            int main(){
                acc(10);
                acc(20);
                return acc(30);
            }
        ''', optimize=False)
        assert ret == 60

    def test_separate_statics(self):
        """Each function has its own static variable."""
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int fa(){ static int n = 0; n += 1; return n; }
            int fb(){ static int n = 0; n += 10; return n; }
            int main(){
                fa(); fa();
                fb();
                return fa() + fb();
            }
        ''', optimize=False)
        assert ret == 23  # fa returns 3, fb returns 20

    def test_static_incomplete_char_array_from_string_literal(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            '''
            const char *version(void) {
                static const char my_version[] = "1.3.1";
                return my_version;
            }
            int main(void) {
                const char *p = version();
                return (p[0] == '1' && p[4] == '1' && p[5] == '\\0') ? 0 : 1;
            }
            ''',
            optimize=False,
        )
        assert ret == 0

    def test_static_incomplete_int_array_from_init_list(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            '''
            int read_table(int idx) {
                static const int table[] = {7, 11, 13};
                return table[idx];
            }
            int main(void) {
                return (read_table(0) == 7 && read_table(2) == 13) ? 0 : 1;
            }
            ''',
            optimize=False,
        )
        assert ret == 0

    def test_global_pointer_to_array_element_initializer(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            '''
            static unsigned char table[8] = {0, 1, 2, 3, 4, 5, 6, 7};
            static const unsigned char *ptr = &table[3];
            int main(void) {
                return (ptr[0] == 3 && ptr[2] == 5) ? 0 : 1;
            }
            ''',
            optimize=False,
        )
        assert ret == 0


if __name__ == '__main__':
    unittest.main()
