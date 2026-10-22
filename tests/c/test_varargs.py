import os
import sys
import tempfile

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)

from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
from pcc.driver.project import TranslationUnit

from tests.owned_c_execution import compile_and_run_owned_c


def test_stdarg_pointer_int_double_roundtrip():
    source = r"""
        #include <stdarg.h>

        int check(const char *fmt, ...) {
            va_list ap;
            va_start(ap, fmt);
            char *s = va_arg(ap, char *);
            int i = va_arg(ap, int);
            double d = va_arg(ap, double);
            void *p = va_arg(ap, void *);
            va_end(ap);
            return (s[0] == 'h' && s[1] == 'i' &&
                    i == 42 &&
                    d > 2.4 && d < 2.6 &&
                    p == s) ? 0 : 1;
        }

        int main() {
            char *s = "hi";
            return check("", s, 42, 2.5, s);
        }
    """

    r = compile_and_run_owned_c(source)
    assert r.returncode == 0, r.stderr


def test_stdarg_helper_accepts_va_list_parameter():
    source = r"""
        #include <stdarg.h>

        int collect_from_list(va_list ap) {
            char *s = va_arg(ap, char *);
            int i = va_arg(ap, int);
            double d = va_arg(ap, double);
            void *p = va_arg(ap, void *);
            return (s[0] == 'o' && s[1] == 'k' &&
                    i == 7 &&
                    d > 1.2 && d < 1.3 &&
                    p == s) ? 0 : 1;
        }

        int check(const char *fmt, ...) {
            va_list ap;
            int rc;
            va_start(ap, fmt);
            rc = collect_from_list(ap);
            va_end(ap);
            return rc;
        }

        int main() {
            char *s = "ok";
            return check("", s, 7, 1.25, s);
        }
    """

    r = compile_and_run_owned_c(source)
    assert r.returncode == 0, r.stderr


def test_stdarg_copy_from_parameter_preserves_original_cursor():
    source = r"""
        #include <stdarg.h>

        int copy_sum(va_list incoming) {
            va_list copy;
            va_copy(copy, incoming);
            int first = va_arg(copy, int);
            int second = va_arg(copy, int);
            va_end(copy);
            return first + second;
        }

        int check(int tag, ...) {
            va_list ap;
            va_start(ap, tag);
            int sum = copy_sum(ap);
            int original_first = va_arg(ap, int);
            va_end(ap);
            return sum == 12 && original_first == 5 ? 0 : 1;
        }

        int main(void) { return check(0, 5, 7); }
    """
    result = compile_and_run_owned_c(source)
    assert result.returncode == 0, result.stderr


def test_stdarg_address_expression_is_evaluated_once():
    source = r"""
        #include <stdarg.h>

        static int calls;
        va_list *next_list(va_list *ap) {
            calls++;
            return ap;
        }

        int check(int tag, ...) {
            va_list ap;
            va_start(ap, tag);
            int first = va_arg(*next_list(&ap), int);
            int second = va_arg(*next_list(&ap), int);
            va_end(ap);
            return calls == 2 && first == 5 && second == 7 ? 0 : 1;
        }

        int main(void) { return check(0, 5, 7); }
    """
    result = compile_and_run_owned_c(source)
    assert result.returncode == 0, result.stderr


def test_variadic_string_literal_argument_decays_to_pointer():
    source = r"""
        #include <stdarg.h>

        int check(const char *fmt, ...) {
            va_list ap;
            char *first;
            char *second;
            va_start(ap, fmt);
            first = va_arg(ap, char *);
            second = va_arg(ap, char *);
            va_end(ap);
            return (first[0] == 'o' && first[1] == 'k' &&
                    second[0] == 'x' && second[1] == 'y' &&
                    second[2] == 0) ? 0 : 1;
        }

        int main() {
            char *s = "ok";
            return check("%s%s", s, "xy");
        }
    """

    r = compile_and_run_owned_c(source)
    assert r.returncode == 0, r.stderr


def test_direct_builtin_va_start_and_va_copy_work_with_system_style_va_list():
    code = r'''
        typedef char *__builtin_va_list;
        typedef __builtin_va_list va_list;

        int vsnprintf(char *s, unsigned long n, const char *format, va_list ap);
        int strcmp(const char *a, const char *b);

        int check(const char *fmt, ...) {
            char buf[32];
            int n;
            va_list args;
            va_list copy;

            __builtin_va_start(args, fmt);
            __builtin_va_copy(copy, args);
            n = vsnprintf(buf, 32, fmt, copy);
            __builtin_va_end(copy);
            __builtin_va_end(args);

            return (n == 6 && strcmp(buf, "num=42") == 0) ? 0 : 1;
        }

        int main(void) {
            return check("num=%d", 42);
        }
    '''

    unit = TranslationUnit(
        name="direct_builtin_va_start.c",
        path=os.path.join(parent_dir, "direct_builtin_va_start.c"),
        source=code,
    )

    result = CEvaluator().run_translation_units_with_system_cc(
        [unit],
        optimize=True,
        base_dir=parent_dir,
        jobs=1,
        include_dirs=[],
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_old_style_strlen_call_inside_vararg_function_uses_real_call_abi():
    code = r'''
        #include <stdarg.h>

        extern __SIZE_TYPE__ strlen ();
        extern void abort(void);

        void check(int tag, char *fmt, ...) {
            va_list ap;
            va_start(ap, fmt);
            if (strlen(fmt) != 15)
                abort();
            va_end(ap);
        }

        int main(void) {
            char *text = "0123456789abcdef";
            check(0, text + 1, 1, 2, 3);
            return 0;
        }
    '''

    unit = TranslationUnit(
        name="old_style_vararg_strlen.c",
        path=os.path.join(parent_dir, "old_style_vararg_strlen.c"),
        source=code,
    )

    result = CEvaluator().run_translation_units_with_system_cc(
        [unit],
        optimize=False,
        base_dir=parent_dir,
        jobs=1,
        include_dirs=[],
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_cast_variadic_function_pointer_keeps_varargs_and_promotions():
    pcc = CEvaluator()
    ret = pcc.evaluate(
        r'''
        #include <stdarg.h>

        typedef long (*generic_fn)(void);

        long collect(int tag, ...) {
            va_list ap;
            int promoted_char;
            double promoted_float;
            char *text;
            va_start(ap, tag);
            promoted_char = va_arg(ap, int);
            promoted_float = va_arg(ap, double);
            text = va_arg(ap, char *);
            va_end(ap);
            return (promoted_char == 65 &&
                    promoted_float > 2.4 &&
                    promoted_float < 2.6 &&
                    text[0] == 'o' &&
                    text[1] == 'k' &&
                    text[2] == 0) ? 0 : 1;
        }

        int main(void) {
            generic_fn raw = (generic_fn)collect;
            char ch = 'A';
            float f = 2.5f;
            char *text = "ok";
            return ((long (*)(int, ...))raw)(0, ch, f, text);
        }
        ''',
        optimize=False,
    )
    assert ret == 0


def test_sqlite_style_variadic_function_pointer_table_passes_pointer_args():
    code = r'''
        #include <fcntl.h>
        #include <unistd.h>
        #include <errno.h>

        typedef void (*syscall_ptr)(void);

        struct Entry {
            const char *name;
            syscall_ptr current;
        };

        static struct Entry table[] = {
            { "open", (syscall_ptr)open },
            { "close", (syscall_ptr)close },
            { "fcntl", (syscall_ptr)fcntl },
        };

        #define osOpen ((int(*)(const char*,int,int))table[0].current)
        #define osClose ((int(*)(int))table[1].current)
        #define osFcntl ((int(*)(int,int,...))table[2].current)

        int main(int argc, char **argv) {
            int fd = osOpen(argv[1], O_RDWR | O_CREAT, 0600);
            struct flock lock;
            if (fd < 0) {
                return errno ? errno : 100;
            }
            lock.l_start = 0;
            lock.l_len = 1;
            lock.l_pid = 0;
            lock.l_type = F_RDLCK;
            lock.l_whence = 0;
            if (osFcntl(fd, F_GETLK, &lock) != 0) {
                int err = errno;
                osClose(fd);
                return err ? err : 101;
            }
            osClose(fd);
            return 0;
        }
    '''

    unit = TranslationUnit(
        name="sqlite_style_variadic_fp.c",
        path=os.path.join(parent_dir, "sqlite_style_variadic_fp.c"),
        source=code,
    )

    fd, path = tempfile.mkstemp(prefix="pcc_sqlite_style_", suffix=".db")
    os.close(fd)
    os.unlink(path)
    try:
        result = CEvaluator().run_translation_units_with_system_cc(
            [unit],
            optimize=True,
            base_dir=parent_dir,
            prog_args=[path],
            include_dirs=[],
        )
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        if os.path.exists(path):
            os.unlink(path)
