import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
import unittest


class TestSwitch(unittest.TestCase):
    def test_switch_case(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            int main(){
                int x = 2;
                int result = 0;
                switch(x){
                    case 1:
                        result = 10;
                        break;
                    case 2:
                        result = 20;
                        break;
                    case 3:
                        result = 30;
                        break;
                }
                return result;
            }
            """,
            llvmdump=True,
        )
        assert ret == 20

    def test_switch_default(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            int main(){
                int x = 99;
                int result = 0;
                switch(x){
                    case 1:
                        result = 10;
                        break;
                    case 2:
                        result = 20;
                        break;
                    default:
                        result = 42;
                        break;
                }
                return result;
            }
            """,
            llvmdump=True,
        )
        assert ret == 42

    def test_switch_first_case(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            int main(){
                int x = 1;
                int result = 0;
                switch(x){
                    case 1:
                        result = 100;
                        break;
                    case 2:
                        result = 200;
                        break;
                }
                return result;
            }
            """,
            llvmdump=True,
        )
        assert ret == 100

    def test_switch_grouped_cases_share_body(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            int main(){
                int x = 2;
                int result = 0;
                switch(x){
                    case 1:
                    case 2:
                    case 3:
                        result = 42;
                        break;
                    default:
                        result = 99;
                        break;
                }
                return result;
            }
            """,
            llvmdump=True,
        )
        assert ret == 42

    def test_switch_fallthrough(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            int main(){
                int x = 1;
                int result = 0;
                switch(x){
                    case 1:
                        result += 10;
                    case 2:
                        result += 20;
                        break;
                    default:
                        result = 99;
                        break;
                }
                return result;
            }
            """,
            llvmdump=True,
        )
        assert ret == 30

    def test_switch_grouped_escaped_char_cases(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            r"""
            int main(){
                int c = '\n';
                switch(c){
                    case '\n':
                    case '\r':
                        return 0;
                    default:
                        return 1;
                }
            }
            """,
            llvmdump=True,
        )
        assert ret == 0

    def test_switch_scope_decl_before_first_case_is_visible_in_cases(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            typedef union {
                int x;
            } U;

            int main(){
                switch(1){
                    U u;
                    case 1:
                        u.x = 3;
                        return u.x == 3 ? 0 : 1;
                    default:
                        return 2;
                }
            }
            """,
            llvmdump=True,
        )
        assert ret == 0

    def test_switch_nested_case_labels_inside_case_block_direct_entry(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            int main(){
                int x = 2;
                switch(x){
                    case 1: {
                        int shared;
                        shared = 7;
                        if (shared == 99) return 9;
                    case 2:
                    case 3:
                        shared = 42;
                        return shared == 42 ? 0 : 1;
                    }
                    default:
                        return 2;
                }
            }
            """,
            llvmdump=True,
        )
        assert ret == 0

    def test_switch_nested_case_labels_inside_case_block_fallthrough(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            int main(){
                int x = 1;
                switch(x){
                    case 1: {
                        int shared;
                        shared = 7;
                    case 2:
                        return shared == 7 ? 0 : 1;
                    }
                    default:
                        return 2;
                }
            }
            """,
            llvmdump=True,
        )
        assert ret == 0

    def test_switch_if_guarded_case_label_respects_false_condition(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            int main(void){
                int x = 0;
                switch(x){
                    case 0:
                        if (0)
                    case 1:
                        return 1;
                        break;
                }
                return 0;
            }
            """,
            llvmdump=True,
        )
        assert ret == 0

    def test_switch_if_guarded_default_break_does_not_fallthrough(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            int main(void){
                int x = 41;
                switch(x){
                    if (0) {
                        return 9;
                    default:
                        return 0;
                        break;
                    case 42:
                        return 1;
                    }
                }
                return 2;
            }
            """,
            llvmdump=True,
        )
        assert ret == 0

    def test_switch_case_labels_nested_inside_if_block(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            int main(){
                int i = 41;
                switch(i){
                    if (0) {
                        return 9;
                    case 42:
                        return 8;
                    case 41:
                        return 0;
                    }
                }
                return 7;
            }
            """,
            llvmdump=True,
        )
        assert ret == 0

    def test_switch_default_label_nested_inside_if_block(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            int main(){
                int i = 41;
                switch(i){
                    if (0) {
                        return 9;
                    default:
                        return 0;
                    case 42:
                        return 8;
                    }
                }
                return 7;
            }
            """,
            llvmdump=True,
        )
        assert ret == 0

    def test_switch_goto_label_after_terminated_path(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            int main(){
                int x = 1;
                int result = 7;
                switch(x){
                    case 1:
                        if (result == 7)
                            goto done;
                        return 3;
                    done:
                        break;
                    default:
                        return 4;
                }
                return result == 7 ? 0 : 1;
            }
            """,
            llvmdump=True,
        )
        assert ret == 0

    def test_switch_decl_before_first_case_inside_loop(self):
        pcc = CEvaluator()
        ret = pcc.evaluate(
            """
            int main(){
                int op = 0;
                int hits = 0;
                for(;;){
                    switch(op){
                        int nField;
                        int p2;
                        int iDb;
                        int wrFlag;
                        int res;
                        long long iKey;
                        case 0:
                            hits = 1;
                            break;
                        default:
                            return 3;
                    }
                    op++;
                    if (hits) break;
                }
                return op == 1 && hits == 1 ? 0 : 1;
            }
            """,
            llvmdump=True,
        )
        assert ret == 0


if __name__ == "__main__":
    unittest.main()
