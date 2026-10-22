"""repr() of a user exception subclass instance uses BaseException.__repr__.

str() of these instances already followed BaseException.__str__ through
``args``, but repr() fell through to object.__repr__ and printed
``<__main__.AppError object at 0x...>`` where CPython prints
``AppError('bad')``: the class name, then repr(args[0]) in parentheses for
one argument, else repr(args). Underneath, an instance built by calling
the class (not raised) never received ``args`` at all, so str() was empty and
``.args`` raised AttributeError: BaseException.__new__ keeps the constructor
arguments as ``args`` before any __init__ runs.
"""

from __future__ import annotations

import os
import subprocess
import sys

_PROGRAM = '''
class AppError(ValueError):
    pass


class Pair(Exception):
    def __init__(self, left, right):
        super().__init__(left, right)


class Quiet(Exception):
    def __init__(self):
        super().__init__()


class Shown(Exception):
    def __str__(self):
        return "shown"


class Custom(Exception):
    def __repr__(self):
        return "custom!"


class MissingKey(KeyError):
    pass


def main() -> None:
    made = AppError("bad")
    print(str(made), made.args, str(MissingKey("k")), Shown("x").args, Pair(1, 2).args)
    print(repr(AppError("bad")), repr(AppError()), repr(AppError("a", 1)))
    print(repr(Pair(1, "two")), repr(Quiet()), repr(Shown("x")), str(Shown("x")))
    print(repr(Custom("y")), repr(MissingKey("k")), str(MissingKey("k")))
    caught = []
    for error in (AppError("e1"), Pair(3, 4)):
        try:
            raise error
        except Exception as exc:
            caught.append(repr(exc))
    print(caught, [repr(e) for e in [AppError(i) for i in range(3)]])


main()
'''


def test_user_exception_repr_matches_cpython_under_five_collectors(tmp_path, pcc_runtime_archive,
                                                                   python_program_compiler):
    source = tmp_path / "exception_repr.py"
    source.write_text(_PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    output = tmp_path / "exception_repr"
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
