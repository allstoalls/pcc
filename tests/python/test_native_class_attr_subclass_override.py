"""Subclass class-attr override wins for ``self.<attr>`` / hinted ``inst.<attr>``.

A base-class method (or property) reading ``self.<class_attr>`` used to lower to
a *static* load of the defining class's per-class global, returning the base
value even when the instance's actual subclass overrode that attribute. CPython
resolves a class attribute via ``type(inst).__mro__``, so the override must win.

Fix: ``ClassLowering.class_attr_overridden_by_subclass`` gates the static
fast path; when any subclass redeclares the attribute the access falls through
to the runtime ``py_obj_getattr`` MRO lookup. The no-override case keeps the
static path (unchanged IR). Frontend-only; verified under ``--python-libpython=off``.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap


def test_class_attr_subclass_override_matches_cpython(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "clsattr.py"
    exe = tmp_path / "clsattr.out"
    src.write_text(textwrap.dedent("""
        class Base:
            kind = "base"
            def via_method(self):
                return self.kind
            @property
            def via_prop(self):
                return self.kind

        class Sub(Base):
            kind = "sub"

        class Plain:
            only = "x"
            def get_only(self):
                return self.only

        def use_base(b: Base) -> str:
            return b.kind

        def main() -> None:
            s = Sub()
            print("direct:", s.kind)
            print("method:", s.via_method())
            print("prop:", s.via_prop)
            b = Base()
            print("base method:", b.via_method())
            print("hinted-local:", use_base(s))
            print("plain:", Plain().get_only())

        main()
        """).lstrip(), encoding="utf-8")
    compile_python(
        str(src), str(exe),
        ir_scaffold_mode="on", libpython_mode="off", backend="self",
    )
    cpython = subprocess.run(
        [sys.executable, str(src)], capture_output=True, text=True, timeout=30,
    ).stdout
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == cpython
    assert "method: sub" in result.stdout
