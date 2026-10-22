"""Tests for union, typedef pointer, array of pointers, sizeof(char)."""
import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
from pcc.frontends.c.codegen.c_codegen import CCodeGenerator
from pcc.frontends.c.parse.c_parser import CParser
import unittest


class TestUnion(unittest.TestCase):
    def test_union_int(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                union { int i; double d; } u;
                u.i = 42;
                return u.i;
            }
        ''', optimize=False)
        assert ret == 42

    def test_union_write_read(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                union { int a; int b; } u;
                u.a = 99;
                return u.b;
            }
        ''', optimize=False)
        assert ret == 99  # same memory


class TestTypedefPointer(unittest.TestCase):
    def test_typedef_intptr(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            typedef int* intptr;
            int main(){
                int x = 42;
                intptr p = &x;
                return *p;
            }
        ''', optimize=False)
        assert ret == 42


class TestArrayOfPointers(unittest.TestCase):
    def test_ptr_array(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a = 1; int b = 2; int c = 3;
                int *arr[3];
                arr[0] = &a;
                arr[1] = &b;
                arr[2] = &c;
                return *arr[0] + *arr[1] + *arr[2];
            }
        ''', optimize=False)
        assert ret == 6

    def test_multidim_ptr_array_ir_keeps_all_dimensions(self):
        cg = CCodeGenerator()
        ast = CParser().parse(
            '''
            int a = 11;
            int b = 22;
            int *grid[2][2] = {{&a, &b}, {&b, &a}};

            int main(void) {
                return 0;
            }
            '''
        )
        cg.generate_code(ast)
        ir_str = str(cg.module)
        assert "grid" in ir_str
        assert "[2 x [2 x i32*]]" in ir_str or "[2 x [2 x ptr]]" in ir_str

    def test_multidim_ptr_array_runtime(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            '''
            int a = 11;
            int b = 22;
            int *grid[2][2] = {{&a, &b}, {&b, &a}};

            int main(void) {
                return *grid[0][0] + *grid[0][1] + *grid[1][0] + *grid[1][1];
            }
            '''
        )
        assert ret == 66


class TestSizeofChar(unittest.TestCase):
    def test_sizeof_char(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('int main(){ return sizeof(char); }')
        assert ret == 1

    def test_sizeof_int(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('int main(){ return sizeof(int); }')
        assert ret == 4  # int is i32 (standard C)


if __name__ == '__main__':
    unittest.main()
