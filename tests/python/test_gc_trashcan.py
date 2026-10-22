"""Trashcan / deep dealloc — releasing N-deep linked structures
without overflowing the C call stack.

Modeled after CPython's trashcan mechanism (`Py_TRASHCAN_BEGIN /
Py_TRASHCAN_END` + tests in `Lib/test/test_gc.py`). The contract:
recursive deallocation of a 100k-element linked list MUST NOT
overflow the C stack.

CPython's trashcan defers nested deallocations onto a thread-local
queue, processing them iteratively. Without this, a 100k linked list
would stack-overflow when its head is dropped.

pcc today: dealloc is iterative-by-construction (the type-tag switch
in `py_decref` doesn't recurse for leaf types) — but containers like
list / dict / instance fields might recursively decref children, which
is the trashcan's actual concern.
"""
from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).absolute().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"


def _compile_and_run(
    tmp_path, source: str, *, env=None
) -> subprocess.CompletedProcess[str]:
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    compile_python(str(src), str(exe), ir_scaffold_mode="on")
    return subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=120, env=env,
    )


# ---------------------------------------------------------------------------
# Linked list of class instances
# ---------------------------------------------------------------------------


def test_linked_list_100k_instances_no_overflow(tmp_path):
    """Build a 100k-deep linked list, drop the head. Each node's
    dealloc decrefs `next` → cascading dealloc. Without trashcan
    this stack-overflows around 5-10k depth."""
    result = _compile_and_run(tmp_path, """
        class Node:
            def __init__(self):
                self.next = None
                self.payload = "x"

        def main() -> None:
            n: int = 100_000
            head = Node()
            cur = head
            i: int = 1
            while i < n:
                nxt = Node()
                cur.next = nxt
                cur = nxt
                i = i + 1
            cur = None
            head = None     # cascade dealloc of 100k nodes
            print("released")

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "released"


# ---------------------------------------------------------------------------
# Nested lists
# ---------------------------------------------------------------------------


def test_nested_list_chain_no_overflow(tmp_path):
    """`xs = [[[[...]]]]` 50k deep. Dropping outermost cascades
    through every list."""
    result = _compile_and_run(tmp_path, """
        def main() -> None:
            n: int = 50_000
            xs = []
            cur = xs
            i: int = 0
            while i < n:
                inner = []
                cur.append(inner)
                cur = inner
                i = i + 1
            cur = None
            xs = None      # cascade through 50k nested lists
            print("released")

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "released"


# ---------------------------------------------------------------------------
# Nested tuples (immutable, but still cascading dealloc)
# ---------------------------------------------------------------------------


def test_nested_tuple_chain_no_overflow(tmp_path):
    """Tuples are immutable but still need cascading dealloc through
    their elements."""
    result = _compile_and_run(tmp_path, """
        def main() -> None:
            n: int = 30_000
            t = (None,)
            i: int = 0
            while i < n:
                t = (t,)        # rebuild outer tuple each time
                i = i + 1
            t = None            # cascade through 30k nested tuples
            print("released")

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "released"


# ---------------------------------------------------------------------------
# Nested dicts
# ---------------------------------------------------------------------------


def test_nested_dict_chain_no_overflow(tmp_path):
    """Dict chain `{'k': {'k': {'k': ...}}}` 30k deep."""
    result = _compile_and_run(tmp_path, """
        def main() -> None:
            n: int = 30_000
            d = {}
            cur = d
            i: int = 0
            while i < n:
                inner = {}
                cur["next"] = inner
                cur = inner
                i = i + 1
            cur = None
            d = None         # cascade through 30k dicts
            print("released")

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "released"


# ---------------------------------------------------------------------------
# Mixed container chain
# ---------------------------------------------------------------------------


def test_mixed_container_chain_no_overflow(tmp_path):
    """20k levels with each level holding both a class instance and a
    nested list. Stresses cascading dealloc through inhomogeneous
    container types (list-of-instance-of-list)."""
    result = _compile_and_run(tmp_path, """
        class N:
            def __init__(self):
                self.children = []

        def main() -> None:
            n: int = 20_000
            root = N()
            cur = root
            i: int = 0
            while i < n:
                child = N()
                cur.children.append(child)
                cur = child
                i = i + 1
            cur = None
            root = None         # cascade through 20k N->list->N->list...
            print("released")

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "released"


# ---------------------------------------------------------------------------
# Deep frame stack vs deep heap (separate concerns)
# ---------------------------------------------------------------------------


def test_recursive_call_depth_does_not_break_dealloc(tmp_path):
    """Even when the active call stack is deep at the time of dealloc,
    the cascading release path must remain bounded. Build a chain
    inside a recursion."""
    result = _compile_and_run(tmp_path, """
        class N:
            def __init__(self):
                self.next = None

        def make_chain(n):
            if n == 0:
                return N()
            tail = make_chain(n - 1)
            head = N()
            head.next = tail
            return head

        def main() -> None:
            head = make_chain(5000)   # built via 5k-deep recursion
            head = None               # released cleanly
            print("released")

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "released"


# ---------------------------------------------------------------------------
# Million-entry list freed at once
# ---------------------------------------------------------------------------


def test_million_entry_list_release(tmp_path):
    """Single 1M-element list released — each element decref'd in a
    loop inside `py_dealloc_list`. Should be linear, not pathological."""
    result = _compile_and_run(tmp_path, """
        class Tiny:
            pass

        def main() -> None:
            n: int = 1_000_000
            xs = []
            i: int = 0
            while i < n:
                xs.append(Tiny())
                i = i + 1
            xs = None    # 1M Tiny instances released
            print("released")

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "released"


# ---------------------------------------------------------------------------
# Trashcan during __del__
# ---------------------------------------------------------------------------


def test_trashcan_with_del_no_overflow(tmp_path):
    """100k chain where every node has `__del__`. Trashcan must
    iteratively schedule each `__del__` without recursing the C
    stack."""
    result = _compile_and_run(tmp_path, """
        counter = [0]

        class N:
            def __init__(self):
                self.next = None
            def __del__(self):
                counter[0] = counter[0] + 1

        def main() -> None:
            n: int = 100_000
            head = N()
            cur = head
            i: int = 1
            while i < n:
                cur.next = N()
                cur = cur.next
                i = i + 1
            cur = None
            head = None
            print(counter[0] == n)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"


# ---------------------------------------------------------------------------
# Several finalizable instances cleared together
# ---------------------------------------------------------------------------


def test_list_of_finalizable_instances_cleared(tmp_path):
    """A list holding >= 2 ``__del__`` instances, then cleared.

    Regression for a switch fall-through in the C runtime's
    ``pcc_trash_should_defer``: every ``case`` (LIST/DICT/INSTANCE/...)
    fell through to ``default: return type_tag >= PY_TYPE_USER``, so
    those container/instance tags were dealloc'd recursively in cc mode
    instead of deferred onto the trash queue like the pcc-Python port.
    A list/instance-dict cascade then re-entered the drain and
    double-freed a trash node (``del`` lines printed, then SIGTRAP in
    ``pcc_trash_drain`` -> ``free``). One instance never triggered it;
    two-or-more did. The port tier passed throughout, so the default-mode
    trashcan tests above never covered this."""
    result = _compile_and_run(
        tmp_path,
        """
        class T:
            def __init__(self, v: int):
                self.v = v
            def __del__(self):
                print("del", self.v)

        def main() -> None:
            items = [T(0), T(1), T(2)]
            items = []
            print("end")

        if __name__ == "__main__":
            main()
        """,
    )
    assert result.returncode == 0, result.stderr
    lines = [l for l in result.stdout.splitlines() if l]
    assert sorted(lines[:-1]) == ["del 0", "del 1", "del 2"], result.stdout
    assert lines[-1] == "end", result.stdout


def test_trash_defer_helpers_match_port_and_exclude_cext_tags_source():
    """Lock the deferral family before C-extension dynamic tags reach it.

    Dynamic pcc-native C-extension type tags are above PY_TYPE_USER, but they
    are not pcc user instances and must not be admitted by the generic user-tag
    catch-all. The split C dealloc helper also needs explicit per-case returns;
    otherwise container tags fall through to the default expression.
    """
    py_obj_dealloc_py = (RUNTIME_DIR / "py" / "py_obj_dealloc.py").read_text(
        encoding="utf-8"
    )

    port_start = py_obj_dealloc_py.index("def _dealloc_should_defer(tag: int) -> bool:")
    port_body = py_obj_dealloc_py[
        port_start:py_obj_dealloc_py.index("def _debug_check_dict_dealloc_slot", port_start)
    ]
    assert "pcc_capi_is_cext_type_tag = extern(" in py_obj_dealloc_py
    assert "pcc_capi_is_cext_type_tag(tag) != 0" in port_body
    assert port_body.index("pcc_capi_is_cext_type_tag(tag) != 0") < port_body.index("if tag >= PY_TYPE_USER_CLASS_START")
