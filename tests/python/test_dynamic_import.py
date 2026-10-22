"""Phase D8 — dynamic import contract.

Locks the contract for ``importlib`` / ``__import__`` / dynamic
module loading per ``docs/issues/python-data-model-gaps.md`` Phase D8.

pcc today resolves all imports at compile time (closed-world) — D8
opens the door to runtime-resolved imports for compatibility with
plugin systems, lazy stdlib loading, and tools like ``unittest`` that
discover test modules by name.

Sub-protocols covered:

1. ``importlib.import_module(name)`` resolves a known module
2. ``__import__(name)`` low-level entry point
3. ``importlib.reload(mod)`` re-executes module body
4. Module ``__dict__`` introspection
5. ``hasattr`` / ``getattr`` on a dynamically-loaded module
6. ``sys.modules`` cache reflects loaded modules
7. ``ImportError`` on missing module name
"""
from __future__ import annotations

import subprocess
import textwrap

def _compile_and_run(tmp_path, source: str) -> subprocess.CompletedProcess[str]:
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    # importlib.import_module / runtime getattr legitimately need the
    # libpython fallback today; the strict ``off`` default refuses the
    # build.  Tests in this file specifically exercise the dynamic
    # CPython import path, so opt in to ``auto`` mode.
    compile_python(
        str(src), str(exe),
        ir_scaffold_mode="on", libpython_mode="auto",
    )
    return subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=20,
    )


# ---------------------------------------------------------------------------
# importlib.import_module
# ---------------------------------------------------------------------------


def test_importlib_import_module_known(tmp_path):
    """Dynamic import of a stdlib module resolved at runtime."""
    result = _compile_and_run(tmp_path, """
        import importlib

        def main() -> None:
            m = importlib.import_module("math")
            print(m.pi)            # ~3.14159...

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().startswith("3.14")


def test_importlib_missing_raises_import_error(tmp_path):
    result = _compile_and_run(tmp_path, """
        import importlib

        def main() -> None:
            try:
                importlib.import_module("definitely_not_a_real_module_xyz")
                print("loaded")
            except ImportError:
                print("ImportError")

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ImportError"


# ---------------------------------------------------------------------------
# __import__ low-level
# ---------------------------------------------------------------------------


def test_dunder_import_builtin(tmp_path):
    """``__import__("math")`` returns the math module — same as
    ``import math``, but resolved at runtime."""
    result = _compile_and_run(tmp_path, """
        def main() -> None:
            m = __import__("math")
            print(m.pi)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().startswith("3.14")


# ---------------------------------------------------------------------------
# importlib.reload
# ---------------------------------------------------------------------------


def test_importlib_reload_returns_same_module(tmp_path):
    """``reload(m)`` re-executes module init but returns the same
    module object."""
    result = _compile_and_run(tmp_path, """
        import importlib
        import math

        def main() -> None:
            m2 = importlib.reload(math)
            print(m2 is math)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"


# ---------------------------------------------------------------------------
# Module introspection
# ---------------------------------------------------------------------------


def test_getattr_on_dynamic_module(tmp_path):
    result = _compile_and_run(tmp_path, """
        import importlib

        def main() -> None:
            m = importlib.import_module("math")
            print(hasattr(m, "pi"))
            print(hasattr(m, "no_such_attr"))
            v = getattr(m, "pi")
            print(round(v, 2))

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == ["True", "False", "3.14"]


def test_module_dict_introspection(tmp_path):
    result = _compile_and_run(tmp_path, """
        import importlib

        def main() -> None:
            m = importlib.import_module("math")
            d = m.__dict__
            print("pi" in d)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"


# ---------------------------------------------------------------------------
# sys.modules cache
# ---------------------------------------------------------------------------


def test_sys_modules_reflects_loaded(tmp_path):
    """After ``import math``, ``sys.modules['math']`` returns the
    same module object."""
    result = _compile_and_run(tmp_path, """
        import sys
        import math

        def main() -> None:
            print(sys.modules["math"] is math)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"
