from __future__ import annotations

from pcc.frontends.python import parser
from pcc.frontends.python.py_ast import Import, ImportFrom


def test_stdlib_pathlib_base64_hashlib_string_time_import_shapes():
    mod = parser.parse(
        "import base64\n"
        "import hashlib\n"
        "import string\n"
        "import time\n"
        "from pathlib import Path, PurePath\n",
        "stdlib_more_probe.py",
    )
    assert isinstance(mod.body[0], Import)
    assert isinstance(mod.body[1], Import)
    assert isinstance(mod.body[2], Import)
    assert isinstance(mod.body[3], Import)
    assert isinstance(mod.body[4], ImportFrom)
    assert mod.body[4].module == "pathlib"
