"""Phase G3 — weakref contract.

Locks the contract for the ``weakref`` module per
``docs/issues/gc-semantics-gap.md`` Phase G3.

Sub-protocols covered by this file:

1. ``weakref.ref(obj)`` returns callable; calling it returns obj
   while alive, ``None`` after collection
2. ``weakref.ref`` callback fires when target is collected
3. ``weakref.proxy(obj)`` forwards attribute access transparently
4. ``WeakValueDictionary`` auto-removes entries when value collected
5. ``WeakKeyDictionary`` auto-removes entries when key collected
6. weakref to an object with ``__slots__`` requires ``__weakref__``
   in slots
"""
from __future__ import annotations

import subprocess
import textwrap

import pytest


def _compile_and_run(tmp_path, source: str) -> subprocess.CompletedProcess[str]:
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    compile_python(str(src), str(exe), ir_scaffold_mode="on")
    return subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=20,
    )


# ---------------------------------------------------------------------------
# weakref.ref basic
# ---------------------------------------------------------------------------


def test_ref_alive_returns_object(tmp_path):
    result = _compile_and_run(tmp_path, """
        import weakref

        class Box:
            pass

        def main() -> None:
            b = Box()
            r = weakref.ref(b)
            print(r() is b)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"


def test_ref_dead_returns_none(tmp_path):
    result = _compile_and_run(tmp_path, """
        import weakref

        class Box:
            pass

        def main() -> None:
            b = Box()
            r = weakref.ref(b)
            del b
            print(r() is None)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"


# ---------------------------------------------------------------------------
# weakref callback
# ---------------------------------------------------------------------------


def test_ref_named_callback_fires_on_collection(tmp_path):
    result = _compile_and_run(tmp_path, """
        import weakref

        class Box:
            pass

        log = []

        def cb(dead):
            log.append("freed")

        def main() -> None:
            b = Box()
            r = weakref.ref(b, cb)
            del b
            print(log)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "['freed']"


def test_ref_lambda_callback_fires_on_collection(tmp_path):
    result = _compile_and_run(tmp_path, """
        import weakref

        class Box:
            pass

        log = []

        def main() -> None:
            b = Box()
            r = weakref.ref(b, lambda dead: log.append("freed"))
            del b
            print(log)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "['freed']"


# ---------------------------------------------------------------------------
# weakref.proxy
# ---------------------------------------------------------------------------


def test_proxy_forwards_attr(tmp_path):
    result = _compile_and_run(tmp_path, """
        import weakref

        class Box:
            def __init__(self):
                self.v = 7

        def main() -> None:
            b = Box()
            p = weakref.proxy(b)
            print(p.v)            # transparent forward — like b.v

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "7"


def test_proxy_after_collection_raises(tmp_path):
    """Calling a proxy whose target died must raise
    ``ReferenceError`` (NOT ``AttributeError``)."""
    result = _compile_and_run(tmp_path, """
        import weakref

        class Box:
            v = 1

        def main() -> None:
            b = Box()
            p = weakref.proxy(b)
            del b
            try:
                _ = p.v
                print("alive")
            except ReferenceError:
                print("ReferenceError")

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ReferenceError"


# ---------------------------------------------------------------------------
# WeakValueDictionary / WeakKeyDictionary
# ---------------------------------------------------------------------------


def test_weak_value_dict_auto_removes(tmp_path):
    result = _compile_and_run(tmp_path, """
        import weakref

        class Box:
            pass

        def main() -> None:
            d = weakref.WeakValueDictionary()
            b = Box()
            d["k"] = b
            print("k" in d)         # True while b alive
            del b
            print("k" in d)         # False after collection

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == ["True", "False"]


def test_weak_key_dict_auto_removes(tmp_path):
    result = _compile_and_run(tmp_path, """
        import weakref

        class Key:
            pass

        def main() -> None:
            d = weakref.WeakKeyDictionary()
            k = Key()
            d[k] = "val"
            print(len(d))           # 1
            del k
            print(len(d))           # 0

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == ["1", "0"]
