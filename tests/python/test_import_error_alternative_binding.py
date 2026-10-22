"""``except ImportError`` alternatives bind what the program actually imported.

``try: from a import X`` / ``except ImportError: from b import X`` executes the
handler only when the try import raised. With both modules compiled, a static
binding to the textually last import made ``class Derived(X)`` read ``b``'s
never-initialized class object: a NULL+0x28 load in ``py_class_new``. It hit
tarfile -> gzip/bz2/lzma whenever ``pcc.stdlib._compression_stream`` resolved
alongside ``_compression_stream`` (any program inside a pcc source tree).
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from textwrap import dedent

from pcc.frontends.python.import_alternatives import import_fallbacks, import_statement_key
from pcc.frontends.python.pipeline import compile_python
from pcc.frontends.python.py_lift import lift_module
from pcc.frontends.python.py_parse import parse


_PROVIDER = """
class Base:
    def __init__(self, value):
        self.value = value

    def origin(self):
        return "{name}"
"""

_ENTRY = """
try:
    from {primary} import Base
except ImportError:
    from alt_b import Base


class Derived(Base):
    def doubled(self):
        return self.value * 2


def main():
    item = Derived(21)
    print(item.origin(), item.doubled(), Base.__module__)


main()
"""


def _build_and_run(tmp_path: Path, primary: str) -> str:
    for name in ("alt_a", "alt_b"):
        (tmp_path / (name + ".py")).write_text(dedent(_PROVIDER.format(name=name)), encoding="utf-8")
    source = tmp_path / "alt_main.py"
    source.write_text(dedent(_ENTRY.format(primary=primary)), encoding="utf-8")
    exe = tmp_path / "alt_main"
    compile_python(str(source), str(exe), libpython_mode="off", ir_scaffold_mode="on", backend="self")
    proc = subprocess.run([str(exe)], text=True, capture_output=True, check=True, timeout=30)
    return proc.stdout.strip()


def test_compiled_try_import_owns_the_static_class_binding(tmp_path):
    assert _build_and_run(tmp_path, "alt_a") == "alt_a 42 alt_a"


def test_failing_try_import_still_binds_the_handler_alternative(tmp_path):
    assert _build_and_run(tmp_path, "alt_missing") == "alt_b 42 alt_b"


def test_only_import_error_handlers_reimporting_try_names_are_fallbacks():
    source = dedent("""
        try:
            from a import X, Y as Z
        except ImportError:
            from b import X, W
        try:
            from a import P
        except (KeyError, ModuleNotFoundError):
            from b import P
        try:
            from a import Q
        except ValueError:
            from b import Q
        def f():
            try:
                from a import R
            except:
                from b import R
    """)
    module = lift_module(parse(source, "probe.py"), "probe.py", "probe")
    fallbacks = import_fallbacks(module.body)
    by_line = {key[1]: {name: (primary.module, attr) for name, (primary, attr) in names.items()}
               for key, names in fallbacks.items()}
    assert by_line == {
        5: {"X": ("a", "X")},
        9: {"P": ("a", "P")},
        18: {"R": ("a", "R")},
    }
    handler_import = module.body[0].handlers[0].body[0]
    assert import_statement_key(handler_import) in fallbacks
