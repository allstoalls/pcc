"""Tests for the C preprocessor: #include, #define, #ifdef."""

import os
import sys
import tempfile
import pytest

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
from pcc.frontends.c.preprocessor import preprocess
import unittest


class TestIncludeSystem(unittest.TestCase):
    def test_include_stdio(self):
        pcc = CEvaluator()
        ret = pcc.evaluate("""
            #include <stdio.h>
            int main(){ return 0; }
        """)
        assert ret == 0

    def test_include_multiple(self):
        pcc = CEvaluator()
        ret = pcc.evaluate("""
            #include <stdio.h>
            #include <stdlib.h>
            #include <string.h>
            #include <math.h>
            int main(){ return (int)sqrt(16.0) + strlen("hi"); }
        """)
        assert ret == 6  # 4 + 2

    def test_include_with_libc_calls(self):
        pcc = CEvaluator()
        ret = pcc.evaluate("""
            #include <stdlib.h>
            int main(){ return abs(-99); }
        """)
        assert ret == 99


class TestIncludeUser(unittest.TestCase):
    def test_include_user_header(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Write a header file
            header = os.path.join(tmpdir, "mylib.h")
            with open(header, "w") as f:
                f.write("int add(int a, int b){ return a + b; }\n")

            # Write the main file
            pcc = CEvaluator()
            ret = pcc.evaluate(
                """
                #include "mylib.h"
                int main(){ return add(3, 4); }
            """,
                base_dir=tmpdir,
            )
            assert ret == 7

    def test_missing_user_header_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Owned and system preprocessing both name the missing header.
            with pytest.raises(RuntimeError, match=r"missing_header\.h"):
                CEvaluator().evaluate(
                    """
                    #include "missing_header.h"
                    int main(){ return 0; }
                    """,
                    base_dir=tmpdir,
                )

    def test_cpp11_style_attributes_are_ignored(self):
        ret = CEvaluator().evaluate(
            """
            int main(void) {
                int a[4];
                [[clang::code_align(8)]]
                for (int i = 0; i < 4; ++i) {
                    a[i] = i;
                }
                return a[3] == 3 ? 0 : 1;
            }
            """,
            use_system_cpp=False,
        )
        assert ret == 0

    def test_clang_pragma_lines_are_ignored(self):
        ret = CEvaluator().evaluate(
            """
            #define foo 7
            #pragma clang deprecated(foo)
            int main(void) { return foo; }
            """
        )
        assert ret == 7

    def test_embed_directive_is_rewritten_to_zero_byte(self):
        ret = CEvaluator().evaluate(
            """
            int main(void) {
                const unsigned char data[] = {
                    #embed "ignored.bin"
                };
                return data[0];
            }
            """
        )
        assert ret == 0

    def test_missing_clang_fixture_headers_are_rewritten(self):
        ret = CEvaluator().evaluate(
            """
            #include "Inputs/system-header-simulator.h"
            #include "../Inputs/system-header-simulator-for-malloc.h"

            int main(void) {
                char env[] = "NAME=value";
                void *p = malloc(4);
                if (p == NULL) return 1;
                if (strlen(env) != 10) return 2;
                if (scanf == 0) return 3;
                free(p);
                return 0;
            }
            """
        )
        assert ret == 0


class TestDefine(unittest.TestCase):
    def test_simple_define(self):
        pcc = CEvaluator()
        ret = pcc.evaluate("""
            #define SIZE 10
            int main(){ return SIZE; }
        """)
        assert ret == 10

    def test_define_in_expression(self):
        pcc = CEvaluator()
        ret = pcc.evaluate("""
            #define WIDTH 4
            #define HEIGHT 5
            int main(){ return WIDTH * HEIGHT; }
        """)
        assert ret == 20

    def test_null_pointer(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            #include <stddef.h>
            int main(){
                int *p = NULL;
                if (!p) return 1;
                return 0;
            }
        """,
            optimize=False,
        )
        assert ret == 1

    def test_eof(self):
        pcc = CEvaluator()
        ret = pcc.evaluate("""
            #include <stdio.h>
            int main(){ return -(EOF); }
        """)
        assert ret == 1

    def test_object_macro_preserves_hex_escape_strings(self):
        processed = preprocess(r"""
            #define LUA_SIGNATURE "\x1bLua"
            const char *sig = LUA_SIGNATURE;
        """)

        assert 'const char *sig = "\\x1bLua";' in processed

    def test_function_macro_preserves_string_arg_escapes(self):
        processed = preprocess(r"""
            #define LUA_WRITESTRINGERROR(s,p) (fprintf(stderr, (s), (p)), fflush(stderr))
            int main(){
                LUA_WRITESTRINGERROR("%s\n", foo);
                return 0;
            }
        """)

        lines = [
            line.strip() for line in processed.splitlines() if "fprintf(stderr" in line
        ]
        assert lines == ['(fprintf(stderr, ("%s\\n"), (foo)), fflush(stderr));']


class TestIfdef(unittest.TestCase):
    def test_ifdef_defined(self):
        pcc = CEvaluator()
        ret = pcc.evaluate("""
            #define DEBUG
            #ifdef DEBUG
            int g = 1;
            #endif
            int main(){ return g; }
        """)
        assert ret == 1

    def test_ifdef_not_defined(self):
        pcc = CEvaluator()
        ret = pcc.evaluate("""
            #ifdef RELEASE
            int g = 99;
            #endif
            int g = 0;
            int main(){ return g; }
        """)
        assert ret == 0

    def test_ifndef(self):
        pcc = CEvaluator()
        ret = pcc.evaluate("""
            #ifndef UNDEFINED
            int g = 42;
            #endif
            int main(){ return g; }
        """)
        assert ret == 42

    def test_ifdef_else(self):
        pcc = CEvaluator()
        ret = pcc.evaluate("""
            #ifdef NOPE
            int mode = 1;
            #else
            int mode = 2;
            #endif
            int main(){ return mode; }
        """)
        assert ret == 2

    def test_nested_ifdef(self):
        pcc = CEvaluator()
        ret = pcc.evaluate("""
            #define A
            #define B
            #ifdef A
            #ifdef B
            int val = 12;
            #else
            int val = 10;
            #endif
            #else
            int val = 0;
            #endif
            int main(){ return val; }
        """)
        assert ret == 12


class TestRealisticProgram(unittest.TestCase):
    def test_standard_c_program(self):
        """A program that looks like real C with includes and defines."""
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            #include <stdio.h>
            #include <stdlib.h>
            #include <string.h>

            #define MAX_LEN 100
            #define SUCCESS 0

            int main(){
                char *buf = malloc(MAX_LEN);
                strcpy(buf, "hello");
                int len = strlen(buf);
                free(buf);
                if (len == 5) return SUCCESS;
                return 1;
            }
        """,
            optimize=False,
        )
        assert ret == 0


if __name__ == "__main__":
    unittest.main()
