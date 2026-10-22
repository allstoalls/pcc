"""Test string literal pointer and global array features."""

import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestStringPointer(unittest.TestCase):
    def test_char_ptr_from_literal(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            int main(){
                char *s = "hello";
                return strlen(s);
            }
        """,
            optimize=False,
        )
        assert ret == 5

    def test_char_ptr_strcmp(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            int main(){
                char *a = "abc";
                char *b = "abc";
                return strcmp(a, b);
            }
        """,
            optimize=False,
        )
        assert ret == 0

    def test_string_literal_index(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            r"""
            #define LUA_SIGNATURE "\x1bLua"
            int main(){
                return LUA_SIGNATURE[0];
            }
        """,
            optimize=False,
        )
        assert ret == 27

    def test_string_literal_high_bit_hex_escape(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            r"""
            int main(){
                unsigned char *s = "\x93";
                return s[0] & 255;
            }
            """,
            optimize=False,
        )
        assert ret == 0x93

    def test_string_literal_unary_deref_and_char_use(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            r"""
            #include <string.h>
            #define SEP ";"

            int main(){
                if (*SEP != ';')
                    return 1;
                return strchr("a;b", *SEP) ? 0 : 2;
            }
        """,
            optimize=False,
        )
        assert ret == 0

    def test_global_const_char_pointer_init(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            static const char *const key = "_CLIBS";
            int main(){
                return strcmp(key, "_CLIBS");
            }
        """,
            optimize=False,
        )
        assert ret == 0


class TestGlobalArray(unittest.TestCase):
    def test_global_array_init(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            int g[3] = {10, 20, 30};
            int main(){ return g[1]; }
        """,
            optimize=False,
        )
        assert ret == 20

    def test_global_array_sum(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            int data[4] = {1, 2, 3, 4};
            int sum(int *a, int n){
                int s = 0;
                int i;
                for(i = 0; i < n; i++) s += a[i];
                return s;
            }
            int main(){ return sum(data, 4); }
        """,
            optimize=False,
        )
        assert ret == 10


class TestC99ForDecl(unittest.TestCase):
    def test_for_with_decl(self):
        pcc = CEvaluator()
        ret = pcc.evaluate("""
            int main(){
                int s = 0;
                for(int i = 0; i < 5; i++) s += i;
                return s;
            }
        """)
        assert ret == 10


class TestDoWhileContinue(unittest.TestCase):
    def test_continue_in_dowhile(self):
        pcc = CEvaluator()
        ret = pcc.evaluate("""
            int main(){
                int i = 0;
                int s = 0;
                do {
                    i++;
                    if (i % 2 == 0) continue;
                    s += i;
                } while(i < 10);
                return s;
            }
        """)
        assert ret == 25  # 1+3+5+7+9


if __name__ == "__main__":
    unittest.main()
