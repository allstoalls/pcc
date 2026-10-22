from __future__ import annotations

from pcc.frontends.python import parser
from pcc.frontends.python.py_ast import ImportFrom, ClassDef, FuncDef


def test_dataclasses_functools_itertools_collections_import_shapes():
    mod = parser.parse(
        "from dataclasses import dataclass, field\n"
        "from functools import lru_cache, cached_property\n"
        "from itertools import accumulate, zip_longest\n"
        "from collections import Counter, deque, ChainMap\n"
        "@dataclass\n"
        "class P:\n"
        "    x: int = 1\n"
        "@lru_cache()\n"
        "def f(x):\n"
        "    return x\n",
        "stdlib_util_probe.py",
    )
    assert isinstance(mod.body[0], ImportFrom)
    assert mod.body[0].module == "dataclasses"
    assert isinstance(mod.body[1], ImportFrom)
    assert mod.body[1].module == "functools"
    assert isinstance(mod.body[2], ImportFrom)
    assert mod.body[2].module == "itertools"
    assert isinstance(mod.body[3], ImportFrom)
    assert mod.body[3].module == "collections"
    assert isinstance(mod.body[4], ClassDef)
    assert isinstance(mod.body[5], FuncDef)
