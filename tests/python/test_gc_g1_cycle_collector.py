"""Phase G1 — tricolor cycle collector contract.

Each test exercises one cycle shape that used to be beyond the
refcount-only runtime. The core shapes now pass under the default
collector. See
``docs/issues/gc-semantics-gap.md`` Phase G1 for the implementation
plan.

When G1 lands for a backend, the marker should be disabled for that backend
so its verdict command passes green. The acceptance gate "pcc2 / pcc3 still
byte-equal across stages" is verified separately by ``scripts/bootstrap.py``,
not here.

Container shapes covered (each must support a ``traverse`` callback
that yields outgoing references for the collector to walk):

- ``list`` (mutable, indexed)
- ``tuple`` (immutable, but can be cyclic via mutable container nested)
- ``dict`` (key + value pairs)
- ``set`` (member-only, no values; but can hold mutable containers)
- class instance (``__dict__`` + ``__slots__``)
- function closure cells
- exception ``__traceback__`` chain (cycles via ``__cause__`` /
  ``__context__``)
"""
from __future__ import annotations

import subprocess
import textwrap


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
# Cycle shapes.
# ---------------------------------------------------------------------------


def test_cycle_class_instance_self_loop(tmp_path):
    """Class instance referencing itself via attribute."""
    result = _compile_and_run(tmp_path, """
        import gc

        class Marker:
            triggered = 0
            def __del__(self):
                Marker.triggered = Marker.triggered + 1

        def make():
            a = Marker()
            a.peer = a  # self-cycle
            return None

        def main() -> None:
            for _ in range(5):
                make()
            gc.collect()
            print(Marker.triggered)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "5"


def test_cycle_two_instances_mutual(tmp_path):
    """Two instances referencing each other (A → B → A)."""
    result = _compile_and_run(tmp_path, """
        import gc

        class Marker:
            triggered = 0
            def __del__(self):
                Marker.triggered = Marker.triggered + 1

        def make():
            a = Marker()
            b = Marker()
            a.peer = b
            b.peer = a
            return None

        def main() -> None:
            make()
            gc.collect()
            print(Marker.triggered)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "2"


def test_cycle_list_self_reference(tmp_path):
    """List containing itself: ``xs.append(xs)``."""
    result = _compile_and_run(tmp_path, """
        import gc

        class Sentinel:
            triggered = 0
            def __del__(self):
                Sentinel.triggered = Sentinel.triggered + 1

        def make():
            xs = [Sentinel()]
            xs.append(xs)  # list self-reference
            return None

        def main() -> None:
            make()
            gc.collect()
            print(Sentinel.triggered)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1"


def test_cycle_dict_value_to_self(tmp_path):
    """Dict whose value is the dict itself: ``d['k'] = d``."""
    result = _compile_and_run(tmp_path, """
        import gc

        class Sentinel:
            triggered = 0
            def __del__(self):
                Sentinel.triggered = Sentinel.triggered + 1

        def make():
            d = {}
            d["self"] = d
            d["payload"] = Sentinel()
            return None

        def main() -> None:
            make()
            gc.collect()
            print(Sentinel.triggered)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1"


def test_cycle_closure_capture(tmp_path):
    """Closure cell capturing the function it lives in (factory pattern)."""
    result = _compile_and_run(tmp_path, """
        import gc

        class Sentinel:
            triggered = 0
            def __del__(self):
                Sentinel.triggered = Sentinel.triggered + 1

        def make():
            payload = [Sentinel()]
            def inner():
                return payload  # closure refs list payload
            payload.append(inner)  # list -> func -> captures -> list
            return None

        def main() -> None:
            make()
            gc.collect()
            print(Sentinel.triggered)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1"


def test_cycle_chain_long(tmp_path):
    """Long chain: A → B → C → D → A (4-node cycle)."""
    result = _compile_and_run(tmp_path, """
        import gc

        class Marker:
            triggered = 0
            def __del__(self):
                Marker.triggered = Marker.triggered + 1

        def make():
            a = Marker()
            b = Marker()
            c = Marker()
            d = Marker()
            a.next = b
            b.next = c
            c.next = d
            d.next = a  # closes the loop
            return None

        def main() -> None:
            make()
            gc.collect()
            print(Marker.triggered)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "4"


def test_gc_collect_returns_count(tmp_path):
    """``gc.collect()`` returns the number of objects collected."""
    result = _compile_and_run(tmp_path, """
        import gc

        class M:
            pass

        def make():
            a = M()
            b = M()
            a.peer = b
            b.peer = a
            return None

        def main() -> None:
            make()
            n = gc.collect()
            # CPython returns "number of unreachable objects" — for our
            # 2-node cycle that's 2.
            print(n >= 2)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"


def test_gc_disable_enable(tmp_path):
    """``gc.disable()`` halts auto-collection; ``gc.collect()`` still works."""
    result = _compile_and_run(tmp_path, """
        import gc

        class M:
            pass

        def main() -> None:
            gc.disable()
            # Build many cycles; auto-collection must NOT fire
            for _ in range(100):
                a = M(); b = M()
                a.peer = b; b.peer = a
            n = gc.collect()
            gc.enable()
            print(n >= 200)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"
