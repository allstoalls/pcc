"""Execute the owned C string leaves and fixed-C-locale case mapping."""

from tests.owned_c_execution import compile_and_run_owned_c


def test_owned_strcpy_preserves_bytes_terminator_and_destination_identity():
    source = r"""
        #include <string.h>

        int main(void) {
            char source[] = {'a', (char)0x80, (char)0xff, 0};
            char destination[6] = {'x', 'x', 'x', 'x', 'x', 'x'};
            if (strcpy(destination, source) != destination)
                return 1;
            if (destination[0] != 'a' ||
                (unsigned char)destination[1] != 0x80 ||
                (unsigned char)destination[2] != 0xff ||
                destination[3] != 0 || destination[4] != 'x')
                return 2;
            if (strcpy(destination, "") != destination ||
                destination[0] != 0 ||
                (unsigned char)destination[1] != 0x80)
                return 3;
            return 0;
        }
    """

    result = compile_and_run_owned_c(source)
    assert result.returncode == 0, result.stdout + result.stderr


def test_owned_strcat_preserves_prefix_terminator_and_destination_identity():
    source = r"""
        #include <string.h>

        int main(void) {
            char suffix[] = {(char)0x80, (char)0xff, 0};
            char destination[7] = {'a', 0, 'x', 'x', 'x', 'x', 'x'};
            char empty[4] = {0, 'x', 'x', 'x'};
            if (strcat(destination, suffix) != destination)
                return 1;
            if (destination[0] != 'a' ||
                (unsigned char)destination[1] != 0x80 ||
                (unsigned char)destination[2] != 0xff ||
                destination[3] != 0 || destination[4] != 'x')
                return 2;
            if (strcat(destination, "") != destination ||
                destination[3] != 0 || destination[4] != 'x')
                return 3;
            if (strcat(empty, "") != empty ||
                empty[0] != 0 || empty[1] != 'x')
                return 4;
            if (strcat(empty, "b") != empty ||
                empty[0] != 'b' || empty[1] != 0 || empty[2] != 'x')
                return 5;
            return 0;
        }
    """

    result = compile_and_run_owned_c(source)
    assert result.returncode == 0, result.stdout + result.stderr


def test_owned_toupper_preserves_eof_and_maps_every_c_locale_byte():
    source = r"""
        #include <ctype.h>
        #include <stdio.h>

        int main(void) {
            if (toupper(EOF) != EOF)
                return 1;
            for (int value = 0; value <= 255; value++) {
                int expected = value;
                if (value >= 'a' && value <= 'z')
                    expected = value - 'a' + 'A';
                if (toupper(value) != expected)
                    return 2;
            }
            return 0;
        }
    """

    result = compile_and_run_owned_c(source)
    assert result.returncode == 0, result.stdout + result.stderr
