from __future__ import annotations

import subprocess
import textwrap


def test_typeddict_total_keyword_is_noop_self_backend(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "typeddict_total.py"
    src.write_text(textwrap.dedent(
        """
        class TypedDict:
            pass

        class Base(TypedDict):
            name: str

        class Child(Base, total=False):
            value: int

        print("ok")
        """
    ), encoding="utf-8")
    exe = tmp_path / "typeddict_total.out"
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "ok\n"


def test_typing_supports_index_alias_and_typeddict_are_compile_time_only(
    tmp_path,
):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "typing_metadata.py"
    src.write_text(
        textwrap.dedent(
            """
            from collections.abc import Sequence
            from typing import SupportsIndex, TypeAlias, TypedDict

            ShapeLike: TypeAlias = SupportsIndex | Sequence[SupportsIndex]

            class Base(TypedDict):
                names: Sequence[str]

            class Child(Base, total=False):
                offsets: Sequence[int]

            print("typing metadata")
            """
        ),
        encoding="utf-8",
    )
    exe = tmp_path / "typing_metadata.out"
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "typing metadata\n"
