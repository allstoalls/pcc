"""Tests for extra libc functions: more string ops, stdlib, ctype, time, POSIX."""
import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestStringExtra(unittest.TestCase):
    def test_strrchr(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                char *s = "hello world";
                char *p = strrchr(s, 111);
                return *p;
            }
        ''', optimize=False)
        assert ret == 111  # last 'o' in "hello world"

    def test_strspn(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                return strspn("123abc", "0123456789");
            }
        ''', optimize=False)
        assert ret == 3  # first 3 chars are digits

    def test_strcspn(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                return strcspn("hello world", " ");
            }
        ''', optimize=False)
        assert ret == 5  # 5 chars before first space

    def test_memchr(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                char *s = "abcdef";
                char *p = memchr(s, 100, 6);
                if (p) return *p;
                return 0;
            }
        ''', optimize=False)
        assert ret == 100  # found 'd'

    def test_memmove(self):
        """memmove handles overlapping regions."""
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a[5] = {1, 2, 3, 4, 5};
                memmove(a + 1, a, 3 * sizeof(int));
                return a[1];
            }
        ''', optimize=False)
        assert ret == 1  # a[0] was 1, moved to a[1]


class TestStdlibExtra(unittest.TestCase):
    def test_atoi(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){ return atoi("12345"); }
        ''')
        assert ret == 12345

    def test_atol(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){ return atol("99999"); }
        ''')
        assert ret == 99999

    def test_labs(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){ return labs(-100); }
        ''')
        assert ret == 100

    def test_realloc(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int *p = malloc(8);
                *p = 42;
                p = realloc(p, 16);
                int v = *p;
                free(p);
                return v;
            }
        ''', optimize=False)
        assert ret == 42


class TestDlfcnExtra(unittest.TestCase):
    def test_rtld_lazy_macro_is_available(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            '''
            #include <dlfcn.h>

            int main(){ return RTLD_LAZY; }
            ''',
            optimize=False,
        )
        assert ret == 1


class TestCtypeExtra(unittest.TestCase):
    def test_isupper_islower(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int u = isupper(65);
                int l = islower(97);
                if (u && l) return 1;
                return 0;
            }
        ''')
        assert ret == 1  # 'A' is upper, 'a' is lower

    def test_isprint(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int p = isprint(65);
                int np = isprint(7);
                if (p && !np) return 1;
                return 0;
            }
        ''')
        assert ret == 1  # 'A' is printable, BEL is not

    def test_isxdigit(self):
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int a = isxdigit(65);
                int g = isxdigit(71);
                if (a && !g) return 1;
                return 0;
            }
        ''')
        assert ret == 1  # 'A' is hex digit, 'G' is not


class TestTimeGetpid(unittest.TestCase):
    def test_clock(self):
        """clock() should return a non-negative value."""
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int c = clock();
                return c >= 0;
            }
        ''')
        assert ret == 1

    def test_getpid(self):
        """getpid() should return a positive value."""
        pcc = CEvaluator()
        ret = pcc.evaluate('''
            int main(){
                int pid = getpid();
                return pid > 0;
            }
        ''')
        assert ret == 1

    def test_localtime_r_with_time_t(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            '''
            #include <time.h>

            int main(){
                time_t t = time(0);
                struct tm out;
                return localtime_r(&t, &out) != 0 &&
                       out.tm_year >= 100 &&
                       out.tm_mon >= 0 &&
                       out.tm_mon < 12;
            }
            ''',
            optimize=False,
        )
        assert ret == 1


class TestLocaleExtra(unittest.TestCase):
    def test_localeconv_decimal_point(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            '''
            #include <locale.h>

            int main(){
                setlocale(LC_ALL, "C");
                return localeconv()->decimal_point[0] == '.';
            }
            ''',
            optimize=False,
        )
        assert ret == 1

    def test_pwd_header_declares_struct_passwd_fields(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            '''
            #include <pwd.h>

            int main(){
                return sizeof(((struct passwd *)0)->pw_dir) == sizeof(char *) &&
                       sizeof(((struct passwd *)0)->pw_uid) == sizeof(uid_t);
            }
            ''',
            optimize=False,
        )
        assert ret == 1


if __name__ == '__main__':
    unittest.main()
