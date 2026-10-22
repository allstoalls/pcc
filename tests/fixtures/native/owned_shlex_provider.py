"""The same quoting and callable-alias contract on CPython and native PCC."""
import gc
import shlex
import shlex as shell_words
import sys
from shlex import join as join_words, quote as quote_word, split as split_words

from pcc.extern import c_int64, extern


observed_backend = extern("pcc_gc_backend", (), c_int64)


def check_words():
    values = ["", "a b", "a'b", 'a"b', "$value", "`command`", "é", "a@b+c,d"]
    expected = ["''", "'a b'", "'a'\"'\"'b'", '\'a"b\'', "'$value'", "'`command`'", "'é'", "a@b+c,d"]
    saved_quote = shlex.quote
    saved_join = shell_words.join
    for index in range(len(values)):
        value = values[index]
        assert shlex.quote(value) == expected[index]
        assert shell_words.quote(value) == expected[index]
        assert quote_word(value) == expected[index]
        assert saved_quote(value) == expected[index]
    joined = saved_join(values)
    gc.collect()
    assert joined == join_words(values)
    assert split_words(joined) == values
    assert shlex.split(joined, posix=True) == values
    assert shell_words.join([]) == ""


def main():
    check_words()
    if sys.implementation.name == "pcc":
        print("OWNED_SHLEX_OK", observed_backend())
    else:
        print("OWNED_SHLEX_REFERENCE_OK")


main()
