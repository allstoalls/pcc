from __future__ import annotations

import re
import textwrap
from pathlib import Path


def test_unique_external_class_annotation_dispatches_method_natively(tmp_path):
    from pcc.frontends.python.pipeline import compile_python_multi

    owner = tmp_path / "owner.py"
    owner.write_text(
        textwrap.dedent(
            """
            class Parent:
                def ping(self) -> int:
                    return 7
            """
        ).lstrip()
    , encoding="utf-8")
    user = tmp_path / "user.py"
    user.write_text(
        textwrap.dedent(
            """
            from __future__ import annotations

            def run(parent: Parent) -> int:
                return parent.ping()
            """
        ).lstrip()
    , encoding="utf-8")
    out = tmp_path / "combined.ll"
    compile_python_multi(
        [str(user), str(owner)],
        str(out),
        emit_llvm_only=True,
        entry_module="user",
        module_names=["user", "owner"],
    )
    ir_text = out.read_text(encoding="utf-8")
    m = re.search(
        r"define[^\n]+@user_user_run[^{]+\{(.+?)\n\}",
        ir_text,
        re.DOTALL,
    )
    assert m is not None, ir_text[:500]
    body = m.group(1)
    assert "user_owner_Parent_ping" in body
    assert "py_cpy_getattr" not in body
    assert "py_cpy_call" not in body
