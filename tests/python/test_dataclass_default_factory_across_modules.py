"""A ``field(default_factory=...)`` default survives a module boundary.

An importing module must be able to omit a dataclass field whose default is
``field(default_factory=list)``.  It could not: the field default is an AST
``Call`` node, the exported class signature is rebuilt from a plain
dictionary, and the rebuild recomputed ``has_default`` from a default that had
not survived, so the field became required.  The caller was then rejected with
``missing required argument``.

The cost of that was not academic.  ``pcc/backend/self_backend_ir.py`` ends
``ParsedFunction`` with ``aarch64_tail_call_ids: list[int] =
field(default_factory=list)``, and one construction site in
``pcc/llvm_capi/direct_indexed_kernel.py`` omits it.  That single omission
failed the stage1 self-host build, so pcc could not produce a pcc1 at all.
Two other construction sites had already been made to pass
``aarch64_tail_call_ids=[]`` explicitly, which is the shape of a workaround
rather than a fix.

The fix carries the factory name across the export as a plain string
(``export_default_factory_name``), so both the ``has_default`` flag and the
factory call can be rebuilt no matter which layer recomputes the signature.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

from pcc.py_frontend.pipeline import compile_python
from pcc.py_frontend.pipeline_exports import export_default_factory_name
from pcc.parse.py_lift import parse_and_lift
from pcc.py_frontend.py_ast import Assign, ClassDef


def _class_body_defaults(source: str, class_name: str) -> dict:
    module = parse_and_lift(textwrap.dedent(source).lstrip(), "m.py", "m")
    out = {}
    for stmt in module.body:
        if not isinstance(stmt, ClassDef) or stmt.name != class_name:
            continue
        for body_stmt in stmt.body:
            if not isinstance(body_stmt, Assign) or len(body_stmt.targets) != 1:
                continue
            target = body_stmt.targets[0]
            name = getattr(target, "ident", None)
            if name is None:
                continue
            out[name] = export_default_factory_name(body_stmt.value)
    return out


def test_the_export_names_the_builtin_factory_and_nothing_else() -> None:
    defaults = _class_body_defaults(
        """
        from dataclasses import dataclass, field


        @dataclass
        class Row:
            name: str
            count: int = 0
            tags: list = field(default_factory=list)
            index: dict = field(default_factory=dict)
            seen: set = field(default_factory=set)
            pair: tuple = field(default_factory=tuple)
            note: str = "x"
            made: list = field(default_factory=make_rows)
        """,
        "Row",
    )
    assert defaults["tags"] == "list"
    assert defaults["index"] == "dict"
    assert defaults["seen"] == "set"
    assert defaults["pair"] == "tuple"
    # A bare annotation, a plain literal default and a factory this marker does
    # not model must all report nothing, so none of them can be mistaken for a
    # builtin factory.
    assert defaults["name"] is None
    assert defaults["count"] is None
    assert defaults["note"] is None
    assert defaults["made"] is None


_MODEL = """
    from dataclasses import dataclass, field


    @dataclass
    class Row:
        name: str
        count: int = 0
        tags: list = field(default_factory=list)
        index: dict = field(default_factory=dict)


    @dataclass
    class Trailing:
        first: int
        rest: list = field(default_factory=list)
    """

_APP = """
    from model import Row, Trailing

    omitted = Row(name="a", count=2)
    print(omitted.name, omitted.count, len(omitted.tags), len(omitted.index))

    supplied = Row(name="b", count=3, tags=[7, 8], index={})
    print(supplied.name, supplied.count, len(supplied.tags))

    omitted.tags.append(1)
    fresh = Row(name="c")
    print(len(omitted.tags), len(fresh.tags))

    trailing = Trailing(first=5)
    print(trailing.first, len(trailing.rest))
    """


def test_an_importing_module_may_omit_a_default_factory_field(tmp_path: Path) -> None:
    """The end-to-end case, compiled and executed.

    ``fresh`` exists to check the factory is called per instance: a default
    that is a shared list would make the third line print ``1 1``.
    """
    (tmp_path / "model.py").write_text(
        textwrap.dedent(_MODEL).lstrip(), encoding="utf-8"
    )
    app = tmp_path / "app.py"
    app.write_text(textwrap.dedent(_APP).lstrip(), encoding="utf-8")
    exe = tmp_path / "app"
    compile_python(
        str(app),
        str(exe),
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
    )
    done = subprocess.run([str(exe)], capture_output=True, text=True, timeout=180)
    assert done.returncode == 0, done.stderr
    assert done.stdout == "a 2 0 0\nb 3 2\n1 0\n5 0\n"


def test_the_arity_error_names_the_signature_it_rejected(tmp_path: Path, capfd) -> None:
    """A genuine caller mistake still fails, and says enough to act on.

    The old message was ``missing required argument 'x'`` with only counts,
    which could not distinguish a caller mistake from a signature that had
    lost a default on the way in.  That ambiguity is what made the self-host
    failure above expensive to locate.

    A plain function is used rather than a dataclass on purpose: pcc treats a
    bare ``x: int`` class-body annotation as carrying a default, deliberately
    and with a recorded reason, so a dataclass cannot express "genuinely
    required" here.
    """
    (tmp_path / "model.py").write_text(
        textwrap.dedent(
            """
            def need(first: int, second: int) -> int:
                return first + second
            """
        ).lstrip(),
        encoding="utf-8",
    )
    app = tmp_path / "app.py"
    app.write_text(
        textwrap.dedent(
            """
            from model import need

            print(need(first=1))
            """
        ).lstrip(),
        encoding="utf-8",
    )
    raised = ""
    try:
        compile_python(
            str(app),
            str(tmp_path / "app"),
            libpython_mode="off",
            ir_scaffold_mode="on",
            backend="self",
        )
    except Exception as exc:  # noqa: BLE001 - the diagnostic is the contract
        raised = str(exc)
    assert raised, "the call should not have compiled"
    # The parallel frontend worker reports the detail on stderr; the exception
    # the caller sees is only "parallel frontend codegen worker failed".  That
    # propagation gap is separate from this contract, so read the diagnostic
    # where the worker actually writes it.
    message = capfd.readouterr().err
    assert "missing required argument 'second'" in message, message
    assert "signature=first<none>,second<none>" in message, message
    assert "supplied=first" in message, message
