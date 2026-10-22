"""Phase D7 — pickle / copy protocol contract.

Locks the contract for ``copy.copy`` / ``copy.deepcopy`` / pickle
hooks (``__reduce__``, ``__getstate__``, ``__setstate__``,
``__copy__``, ``__deepcopy__``) per
``docs/issues/python-data-model-gaps.md`` Phase D7.

Sub-protocols covered:

1. ``copy.copy`` of a built-in (list/dict/set) does a shallow copy
2. ``copy.deepcopy`` recurses
3. ``copy.copy`` / ``deepcopy`` cycle handling (memo)
4. ``__copy__`` / ``__deepcopy__`` user hooks
5. ``__reduce__`` / ``__reduce_ex__`` for pickling
6. ``__getstate__`` / ``__setstate__`` round-trip
7. pickle round-trip of basic class instances
"""
from __future__ import annotations

import subprocess
import textwrap

def _compile_and_run(
    tmp_path, source: str, *, backend: str = "self"
) -> subprocess.CompletedProcess[str]:
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend=backend,
    )
    return subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=20,
    )


# ---------------------------------------------------------------------------
# copy.copy / copy.deepcopy on built-ins
# ---------------------------------------------------------------------------


def test_copy_list_shallow(tmp_path):
    result = _compile_and_run(tmp_path, """
        import copy

        def main() -> None:
            a = [1, 2, 3]
            b = copy.copy(a)
            print(a is b)            # False — different object
            print(a == b)            # True — same content
            b.append(4)
            print(a)                 # unchanged
            print(b)                 # has 4

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == [
        "False", "True", "[1, 2, 3]", "[1, 2, 3, 4]",
    ]


def test_deepcopy_recurses(tmp_path):
    """``deepcopy`` makes nested mutable elements independent of source."""
    result = _compile_and_run(tmp_path, """
        import copy

        def main() -> None:
            a = [[1, 2], [3, 4]]
            b = copy.deepcopy(a)
            b[0].append(99)
            print(a)                 # unchanged: [[1, 2], [3, 4]]
            print(b)                 # has 99 in inner

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == [
        "[[1, 2], [3, 4]]", "[[1, 2, 99], [3, 4]]",
    ]


def test_deepcopy_handles_cycle(tmp_path):
    """``deepcopy`` of a self-referential structure must terminate
    (memo dict prevents infinite recursion)."""
    result = _compile_and_run(tmp_path, """
        import copy

        def main() -> None:
            a = []
            a.append(a)              # a = [a, ...]
            b = copy.deepcopy(a)
            print(b is a)            # False
            print(b[0] is b)         # True — cycle preserved structurally

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == ["False", "True"]


# ---------------------------------------------------------------------------
# __copy__ / __deepcopy__ user hooks
# ---------------------------------------------------------------------------


def test_user_copy_hook(tmp_path):
    result = _compile_and_run(tmp_path, """
        import copy

        class Box:
            def __init__(self, v):
                self.v = v
                self.copied = False
            def __copy__(self):
                out = Box(self.v)
                out.copied = True
                return out

        def main() -> None:
            a = Box(7)
            b = copy.copy(a)
            print(a.copied)
            print(b.copied)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == ["False", "True"]


# ---------------------------------------------------------------------------
# __getstate__ / __setstate__ round-trip
# ---------------------------------------------------------------------------


def test_getstate_setstate_round_trip(tmp_path):
    """``copy`` round-trips through __getstate__/__setstate__ when
    they're defined."""
    result = _compile_and_run(tmp_path, """
        import copy

        class Box:
            def __init__(self):
                self.v = 0
            def __getstate__(self):
                return {"v": self.v + 100}    # mutate on serialize
            def __setstate__(self, state):
                self.v = state["v"]

        def main() -> None:
            a = Box()
            a.v = 7
            b = copy.copy(a)
            print(b.v)                        # 107 (round-tripped)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "107"


# ---------------------------------------------------------------------------
# pickle round-trip
# ---------------------------------------------------------------------------


def test_pickle_dumps_loads_basic(tmp_path):
    result = _compile_and_run(tmp_path, """
        import pickle

        class Box:
            def __init__(self, v):
                self.v = v

        def main() -> None:
            a = Box(42)
            data = pickle.dumps(a)
            b = pickle.loads(data)
            print(b.v)
            print(a is b)        # False — different object

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == ["42", "False"]


def test_reduce_hook(tmp_path):
    """``__reduce__`` returns ``(callable, args)`` — pickle uses it
    to reconstruct the object."""
    result = _compile_and_run(tmp_path, """
        import pickle

        class Tag:
            def __init__(self, name):
                self.name = name
            def __reduce__(self):
                return (Tag, (self.name,))

        def main() -> None:
            t = Tag("hello")
            data = pickle.dumps(t)
            t2 = pickle.loads(data)
            print(t2.name)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "hello"


def test_copy_pickle_pcc_python_owner_self_backend(tmp_path):
    result = _compile_and_run(
        tmp_path,
        """
        import copy
        import pickle

        class Tag:
            def __init__(self, name):
                self.name = name
            def __copy__(self):
                return Tag(self.name + ":copy")
            def __reduce__(self):
                return (Tag, (self.name,))

        def main() -> None:
            cycle = []
            cycle.append(cycle)
            cloned = copy.deepcopy(cycle)
            print(cloned is cycle)
            print(cloned[0] is cloned)
            print(copy.copy(Tag("x")).name)
            restored = pickle.loads(pickle.dumps(Tag("wire")))
            print(restored.name)

        if __name__ == "__main__":
            main()
        """,
        backend="self",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == [
        "False",
        "True",
        "x:copy",
        "wire",
    ]
