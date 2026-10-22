import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestNamedStruct(unittest.TestCase):
    def test_named_struct_reuse(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                struct Point { int x; int y; };
                struct Point p;
                p.x = 3;
                p.y = 4;
                return p.x + p.y;
            }
        ''')
        assert ret == 7

    def test_two_instances(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                struct Vec { int a; int b; };
                struct Vec v1;
                struct Vec v2;
                v1.a = 10;
                v2.a = 20;
                return v1.a + v2.a;
            }
        ''')
        assert ret == 30

    def test_struct_tag_and_function_name_use_separate_namespaces(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            struct stat { int size; };
            int stat(void){ return 7; }
            int main(void){
                struct stat value;
                value.size = 3;
                return stat() + value.size;
            }
        ''')
        assert ret == 10

    def test_named_struct_layout_does_not_leak_across_compilations(self):
        first = CEvaluator()
        ret = first.evaluate('''
            struct S { char buf[1024]; };
            int main(void){ return sizeof(((struct S *)0)->buf) == 1024 ? 0 : 1; }
        ''')
        assert ret == 0

        second = CEvaluator()
        ret = second.evaluate('''
            struct S { char buf[16]; };
            int main(void){ return sizeof(((struct S *)0)->buf) == 16 ? 0 : 1; }
        ''')
        assert ret == 0


if __name__ == '__main__':
    unittest.main()
