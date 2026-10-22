from tests.owned_c_execution import compile_and_run_owned_c


def test_array_of_pointer_parameter_decays_to_pointer_to_element():
    source = r"""
        const char *pick(int idx, const char *const opts[]) {
            return opts[idx] ? opts[idx] : "(null)";
        }

        int main() {
            static const char *const catnames[] = {
                "all", "collate", "ctype", "monetary", "numeric", "time", 0
            };
            const char *a = pick(0, catnames);
            const char *b = pick(5, catnames);
            const char *c = pick(6, catnames);
            return (a[0] == 'a' && b[0] == 't' && c[0] == '(') ? 0 : 1;
        }
    """

    r = compile_and_run_owned_c(source)
    assert r.returncode == 0, r.stderr


def test_same_width_signed_cast_drops_unsigned_comparison_semantics():
    source = r"""
        #include <stdio.h>

        size_t getend(long pos, size_t len) {
            if (pos > (long)len)
                return len;
            else if (pos >= 0)
                return (size_t)pos;
            else if (pos < -(long)len)
                return 0;
            else
                return len + (size_t)pos + 1;
        }

        int main() {
            return (getend(-20, 9) == 0 && getend(-4, 9) == 6) ? 0 : 1;
        }
    """

    r = compile_and_run_owned_c(source)
    assert r.returncode == 0, r.stderr
