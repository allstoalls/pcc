import os
import sys
from pathlib import Path

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
from tests.c_testsuite_cases import run_pcc
import unittest


class TestGoto(unittest.TestCase):
    def test_goto_forward(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            int main(){
                int x = 0;
                goto skip;
                x = 99;
            skip:
                x = 42;
                return x;
            }
            """,
            llvmdump=True,
        )
        assert ret == 42

    def test_goto_backward(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            int main(){
                int x = 0;
            loop:
                x = x + 1;
                if (x < 5) {
                    goto loop;
                }
                return x;
            }
            """,
            llvmdump=True,
        )
        assert ret == 5

    def test_while_with_labels_and_continue_reaches_afterloop(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            int main() {
                int value = 1;
                int *p = &value;
                int *curr;
                while ((curr = p) != 0) {
                    if (*curr)
                        goto remove;
                    else if (*curr == 2) {
                        goto remain;
                    }
                    else {
                        goto remove;
                    }
                remove:
                    p = 0;
                    continue;
                remain:
                    p = 0;
                    continue;
                }
                return p == 0;
            }
            """,
            llvmdump=True,
        )
        assert ret == 1

    def test_goto_into_nested_block_after_declaration(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            int main() {
                goto inner;
                {
                    int b;
                inner:
                    b = 1234;
                    return b == 1234 ? 0 : 1;
                }
            }
            """,
            llvmdump=True,
        )
        assert ret == 0

    def test_goto_into_nested_block_with_constant_size_arrays(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            int main() {
                goto start;
                {
                    int a[1 && 1];
                    int b[1 || 1];
                    int c[1 ? 1 : 1];
                start:
                    a[0] = 0;
                    b[0] = 0;
                    c[0] = 0;
                }
                return 0;
            }
            """,
            llvmdump=True,
        )
        assert ret == 0

    def test_goto_into_nested_block_with_vla(self):
        case_path = Path(parent_dir) / "projects" / "c-testsuite" / "00207.c"
        result = run_pcc(case_path, Path(parent_dir))
        assert result.returncode == 0

    def test_computed_goto_through_static_label_table(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            int main(void) {
                static void *table[] = { &&done };
                goto *table[0];
                return 1;
            done:
                return 0;
            }
            """,
            llvmdump=True,
        )
        assert ret == 0


if __name__ == "__main__":
    unittest.main()
