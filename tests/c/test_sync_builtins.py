from pcc.frontends.c.evaluator.c_evaluator import CEvaluator


def test_sync_fetch_and_add_returns_previous_value():
    src = r"""
        int main(void) {
            int value = 3;
            int old = __sync_fetch_and_add(&value, 4);
            if (old != 3) return 1;
            if (value != 7) return 2;
            return 0;
        }
    """

    assert CEvaluator().evaluate(src) == 0


def test_sync_bool_compare_and_swap_updates_on_match():
    src = r"""
        int main(void) {
            int value = 5;
            int ok1 = __sync_bool_compare_and_swap(&value, 5, 9);
            int ok2 = __sync_bool_compare_and_swap(&value, 5, 11);
            if (ok1 != 1) return 1;
            if (ok2 != 0) return 2;
            if (value != 9) return 3;
            return 0;
        }
    """

    assert CEvaluator().evaluate(src) == 0
