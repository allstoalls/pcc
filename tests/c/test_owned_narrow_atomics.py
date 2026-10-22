"""Execute narrow C atomics through owned preprocessing, emission and linking."""

import pytest

from tests.owned_c_execution import compile_and_run_owned_c


@pytest.mark.parametrize(
    "ctype, width, unsigned",
    (
        ("signed char", 8, False),
        ("unsigned char", 8, True),
        ("short", 16, False),
        ("unsigned short", 16, True),
    ),
    ids=("i8", "u8", "i16", "u16"),
)
def test_owned_narrow_atomic_family_preserves_values_and_neighbors(
    ctype, width, unsigned,
):
    initial = (1 << width) - 5 if unsigned else -5
    quotient = initial // 2 if unsigned else -2
    shifted = initial >> 1
    source = f"typedef {ctype} atomic_word;\n" + r"""
        struct atomic_box {
            atomic_word before;
            atomic_word value;
            atomic_word after;
        };

        static struct atomic_box global_box = {0x55, 0, 0x2a};

        int exercise(struct atomic_box *box) {
            __atomic_store_n(&box->value, (atomic_word)-5, __ATOMIC_RELEASE);
            if (__atomic_load_n(&box->value, __ATOMIC_RELAXED) !=
                    (atomic_word)-5) return 1;
            if (__atomic_load_n(&box->value, __ATOMIC_ACQUIRE) !=
                    (atomic_word)-5) return 2;
            if (__atomic_load_n(&box->value, __ATOMIC_SEQ_CST) !=
                    (atomic_word)-5) return 3;

            /* Promotion must preserve the atomic object's signedness. */
            if (__atomic_load_n(&box->value, __ATOMIC_ACQUIRE) / 2 !=
                    EXPECTED_QUOTIENT) return 4;
            if ((__atomic_load_n(&box->value, __ATOMIC_ACQUIRE) >> 1) !=
                    EXPECTED_SHIFTED) return 5;
            if ((__atomic_load_n(&box->value, __ATOMIC_ACQUIRE) < 0) !=
                    EXPECTED_NEGATIVE) return 6;
            if ((unsigned int)__atomic_load_n(
                    &box->value, __ATOMIC_ACQUIRE) !=
                    (unsigned int)EXPECTED_INITIAL) return 7;

            if (__atomic_fetch_add(&box->value, 7, __ATOMIC_ACQ_REL) !=
                    (atomic_word)-5) return 8;
            if (__atomic_fetch_sub(&box->value, 3, __ATOMIC_RELEASE) !=
                    (atomic_word)2) return 9;
            if (__atomic_load_n(&box->value, __ATOMIC_ACQUIRE) !=
                    (atomic_word)-1) return 10;
            if (__atomic_fetch_and(&box->value, 0x36, __ATOMIC_SEQ_CST) !=
                    (atomic_word)-1) return 11;
            if (__atomic_fetch_or(&box->value, 0x41, __ATOMIC_RELAXED) !=
                    (atomic_word)0x36) return 12;
            if (__atomic_exchange_n(&box->value, (atomic_word)-2,
                    __ATOMIC_ACQUIRE) != (atomic_word)0x77) return 13;

            atomic_word expected = (atomic_word)-2;
            if (!__atomic_compare_exchange_n(
                    &box->value, &expected, (atomic_word)-7, 0,
                    __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) return 14;
            if (expected != (atomic_word)-2) return 15;
            if (__atomic_load_n(&box->value, __ATOMIC_ACQUIRE) !=
                    (atomic_word)-7) return 16;
            expected = (atomic_word)-2;
            if (__atomic_compare_exchange_n(
                    &box->value, &expected, 9, 0,
                    __ATOMIC_SEQ_CST, __ATOMIC_RELAXED)) return 17;
            if (expected != (atomic_word)-7) return 18;
            if (__atomic_load_n(&box->value, __ATOMIC_ACQUIRE) !=
                    (atomic_word)-7) return 19;

            __atomic_store_n(&box->value, (atomic_word)-5, __ATOMIC_SEQ_CST);
            if (__atomic_add_fetch(&box->value, 7, __ATOMIC_SEQ_CST) !=
                    (atomic_word)2) return 20;
            if (__atomic_sub_fetch(&box->value, 3, __ATOMIC_RELAXED) !=
                    (atomic_word)-1) return 21;
            if (__atomic_and_fetch(&box->value, 0x36, __ATOMIC_ACQUIRE) !=
                    (atomic_word)0x36) return 22;
            if (__atomic_or_fetch(&box->value, 0x41, __ATOMIC_RELEASE) !=
                    (atomic_word)0x77) return 23;

            __atomic_store_n(&box->value, (atomic_word)0x12345,
                    __ATOMIC_RELAXED);
            if (__atomic_load_n(&box->value, __ATOMIC_ACQUIRE) !=
                    (atomic_word)0x12345) return 24;
            if (box->before != 0x55 || box->after != 0x2a) return 25;
            return 0;
        }

        int main(void) {
            struct atomic_box local_box = {0x55, 0, 0x2a};
            int result = exercise(&local_box);
            if (result) return result;
            result = exercise(&global_box);
            if (result) return 30 + result;
            return 0;
        }
    """
    source = (
        source.replace("EXPECTED_QUOTIENT", str(quotient))
        .replace("EXPECTED_SHIFTED", str(shifted))
        .replace("EXPECTED_NEGATIVE", str(int(not unsigned)))
        .replace("EXPECTED_INITIAL", str(initial))
    )

    result = compile_and_run_owned_c(source)
    assert result.returncode == 0, (
        f"{ctype} atomic step {result.returncode}:\n"
        + result.stdout
        + result.stderr
    )
