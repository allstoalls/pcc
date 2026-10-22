"""Tests for libc functions: auto-declared from LIBC_FUNCTIONS registry."""
import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestStdlib(unittest.TestCase):
    def test_abs(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){ return abs(-42); }
        ''')
        assert ret == 42

    def test_calloc(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int *p = calloc(3, 8);
                *(p + 0) = 10;
                *(p + 1) = 20;
                *(p + 2) = 30;
                int s = *p + *(p+1) + *(p+2);
                free(p);
                return s;
            }
        ''', optimize=False)
        assert ret == 60

    def test_calloc_zeroed(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int *p = calloc(1, 8);
                int v = *p;
                free(p);
                return v;
            }
        ''', optimize=False)
        assert ret == 0


class TestString(unittest.TestCase):
    def test_strcat(self):
        """strcat concatenates strings in pre-allocated buffer."""
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                char *buf = malloc(20);
                strcpy(buf, "hello");
                strcat(buf, "world");
                int len = strlen(buf);
                free(buf);
                return len;
            }
        ''', optimize=False)
        assert ret == 10

    def test_strncmp(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                return strncmp("hello", "help", 3);
            }
        ''', optimize=False)
        assert ret == 0  # first 3 chars "hel" == "hel"

    def test_memcpy(self):
        # Note: original test passed size 24 which is UB — the arrays are
        # only 12 bytes each, so memcpy would overrun. Clang with stack
        # checks aborts (exit 133); undefined behavior means the pcc result
        # is equally valid but different. Use the correct size 12.
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int src[3] = {10, 20, 30};
                int dst[3] = {0, 0, 0};
                memcpy(dst, src, 12);
                return dst[0] + dst[1] + dst[2];
            }
        ''', optimize=False)
        assert ret == 60

    def test_memcmp(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a[2] = {1, 2};
                int b[2] = {1, 2};
                return memcmp(a, b, sizeof(a));
            }
        ''', optimize=False)
        assert ret == 0

    def test_strchr(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                char *s = "hello";
                char *p = strchr(s, 108);
                if (p) return *p;
                return 0;
            }
        ''', optimize=False)
        assert ret == 108  # 'l'

    def test_setenv_and_getenv(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                if (setenv("PCC_TEST_ENV", "ok", 1) != 0) return 1;
                return getenv("PCC_TEST_ENV")[0] == 'o' ? 0 : 2;
            }
        ''', optimize=False)
        assert ret == 0


class TestCtype(unittest.TestCase):
    def test_toupper(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                return toupper(97);
            }
        ''')
        assert ret == 65  # 'a' -> 'A'

    def test_tolower(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                return tolower(65);
            }
        ''')
        assert ret == 97  # 'A' -> 'a'

    def test_isdigit(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int d = isdigit(53);
                int a = isdigit(65);
                if (d && !a) return 1;
                return 0;
            }
        ''')
        assert ret == 1  # '5' is digit, 'A' is not

    def test_wide_char_functions_are_declared(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            #include <wchar.h>
            #include <wctype.h>

            int main(){
                wchar_t upper = 65;
                if (!iswupper(upper)) return 1;
                if (towlower(upper) != 97) return 2;
                return wcwidth(upper) == 1 ? 0 : 3;
            }
        ''')
        assert ret == 0

    def test_multibyte_limit_macros_are_available(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            #include <limits.h>

            int main(){
                return (MB_CUR_MAX >= 1 && MB_LEN_MAX >= 1) ? 0 : 1;
            }
        ''')
        assert ret == 0

    def test_basic_integer_limit_macros_are_available(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            #include <limits.h>

            int main(){
                if (CHAR_BIT != 8) return 1;
                if (SCHAR_MAX != 127 || SCHAR_MIN != -128) return 2;
                if (SHRT_MAX != 32767 || SHRT_MIN != -32768) return 3;
                if (INT_MAX != 2147483647 || INT_MIN != (-2147483647 - 1)) return 4;
                if (UINT_MAX != 4294967295U) return 5;
                return 0;
            }
        ''')
        assert ret == 0


class TestPuts(unittest.TestCase):
    def test_puts_compiles(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                puts("hello");
                return 0;
            }
        ''')
        assert ret == 0


if __name__ == '__main__':
    unittest.main()
