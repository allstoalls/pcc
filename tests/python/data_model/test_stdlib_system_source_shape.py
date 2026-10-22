from __future__ import annotations

from pcc.frontends.python import parser
from pcc.frontends.python.py_ast import Import


def test_system_stdlib_import_shapes():
    mod = parser.parse(
        "import sys\n"
        "import platform\n"
        "import tempfile\n"
        "import shutil\n"
        "import subprocess\n"
        "import shlex\n",
        "stdlib_system_probe.py",
    )
    for stmt in mod.body:
        assert isinstance(stmt, Import)
