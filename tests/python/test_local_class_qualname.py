"""A class defined in a function keeps CPython's __name__ and __qualname__.

Function-local classes are hoisted to module level for declaration, and the
hoisted class reported its bare name as ``__qualname__`` ("Local" instead of
"make.<locals>.Local"); a second local class with the same name was renamed
on hoisting and reported the internal name ("Local_nest1") as ``__name__``.
"""

from __future__ import annotations

import os
import subprocess
import sys

_PROGRAM = '''
def make():
    class Local:
        def where(self):
            return type(self).__qualname__
    return Local


def again():
    class Local:
        pass
    return Local


class Holder:
    def build(self):
        class Inner:
            pass
        return Inner


def main() -> None:
    local = make()
    print(local.__name__, local.__qualname__, local().where(), repr(local))
    other = again()
    print(other.__name__, other.__qualname__, other is local)
    inner = Holder().build()
    print(inner.__name__, inner.__qualname__, repr(inner))


main()
'''


def test_local_class_names_match_cpython_under_five_collectors(tmp_path, pcc_runtime_archive,
                                                               python_program_compiler):
    source = tmp_path / "local_class.py"
    source.write_text(_PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    output = tmp_path / "local_class"
    python_program_compiler(
        str(source), str(output), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for gc in range(5):
        ran = subprocess.run(
            [str(output)], capture_output=True, text=True, timeout=60,
            env=dict(os.environ, PCC_GC_BACKEND=str(gc)),
        )
        assert ran.returncode == 0, f"GC{gc}: {ran.stdout}{ran.stderr}"
        assert ran.stdout == expected.stdout, f"GC{gc}: {ran.stdout}"
