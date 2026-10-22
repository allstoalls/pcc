"""Static pointer initializers accept integer constants converted to pointers.

GCC and Clang accept ``(void *)(long)5`` as an address constant in a static
initializer.  The constant builder handled only the null pointer constant and
rejected every other value with "pointer initializer is not a compile-time
constant".
"""

import pytest

from pcc.frontends.c.evaluator.c_evaluator import CEvaluator, TranslationUnit
from tests.owned_c_corpus import run_owned_c_corpus


SOURCE = r"""
#include <stdio.h>

struct entry {
    const char *name;
    void *tag;
};

static struct entry table[] = {
    { "five", (void *)(long)5 },
    { "null", (void *)0 },
    { "ones", (void *)(unsigned long)-1 },
};

static void *direct = (void *)(long)(3 * 7);

int main(void) {
    printf("%ld %d %d %ld\n", (long)table[0].tag, table[1].tag == 0,
           table[2].tag == (void *)(unsigned long)-1, (long)direct);
    return 0;
}
"""


@pytest.mark.parametrize("backend", [pytest.param(None, id="default-self"), pytest.param("self", id="explicit-self")])
def test_integer_constant_pointer_initializers(tmp_path, backend):
    unit = TranslationUnit(
        name="main.c", path=str(tmp_path / "main.c"), source=SOURCE
    )
    if backend == "self":
        evaluator = CEvaluator(backend="self", allow_unimplemented_backend=True)
    else:
        evaluator = CEvaluator()
    result = run_owned_c_corpus(
        evaluator, [unit], timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "5 1 1 21\n"
