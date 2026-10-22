import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)

from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest

from tests.owned_c_execution import compile_and_run_owned_c


def test_printf():
    pcc = CEvaluator()

    ret = pcc.evaluate(
        """
        int main(){
            printf("helloworld");
            return 0;
        }
        """,
        llvmdump=True,
    )
    # printf output goes to native stdout, not capturable by Python
    # Just verify the program compiles and returns 0
    assert ret == 0


def test_stdio_globals_link_and_run():
    source = """
        int main(){
            fwrite("x", 1, 1, stdout);
            fflush(stdout);
            return 0;
        }
        """
    r = compile_and_run_owned_c(source)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "x"


def test_owned_printf_preserves_mixed_varargs_and_return_count():
    source = r"""
        #include <stdio.h>

        const char *select_format(int argc) {
            return argc > 0 ? "%s:%d:%.2f\n" : "%d";
        }

        int main(int argc, char **argv) {
            const char *format = select_format(argc);
            int count = printf(format, "owned", -7, 3.5);
            FILE *saved_stdout = stdout;
            stdout = stderr;
            int redirected = printf(format, "redirected", 8, 2.25);
            stdout = saved_stdout;
            return count == 14 && redirected == 18 ? 0 : 1;
        }
    """

    result = compile_and_run_owned_c(source)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "owned:-7:3.50\n"
    assert result.stderr == "redirected:8:2.25\n"


if __name__ == "__main__":
    unittest.main()
