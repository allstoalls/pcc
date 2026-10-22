"""Condition.notify_all has its own owned broadcast entry."""

from __future__ import annotations

import re
import textwrap


def _function_body(text: str, suffix: str) -> str:
    match = re.search(
        r"^define [^\n]*@[^ (\n]*" + re.escape(suffix) + r"\([^\n]*\n(.*?)^}",
        text, re.MULTILINE | re.DOTALL,
    )
    assert match is not None, suffix
    return match.group(1)


def test_condition_notify_and_notify_all_select_distinct_owned_runtime_calls(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / "condition_notify_variants.py"
    output = tmp_path / "condition_notify_variants.ll"
    source.write_text(textwrap.dedent('''
        import threading

        condition = threading.Condition()

        def wake_one() -> None:
            condition.notify()

        def wake_all() -> None:
            condition.notify_all()

        def main() -> None:
            condition.acquire()
            wake_one()
            wake_all()
            condition.release()

        main()
    ''').lstrip(), encoding="utf-8")
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", emit_llvm_only=True)
    text = output.read_text(encoding="utf-8")
    for suffix, expected, rejected in (
        ("wake_one", "py_threading_condition_notify", "py_threading_condition_notify_all"),
        ("wake_all", "py_threading_condition_notify_all", "py_threading_condition_notify"),
    ):
        body = _function_body(text, suffix)
        callees = re.findall(r"\bcall [^\n]*@([^ (\n]+)\(", body)
        assert expected in callees, body
        assert rejected not in callees, body
        assert not any(name.startswith("py_cpy_") for name in callees), body


def test_owned_condition_provider_routes_broadcast_independently(monkeypatch):
    import pcc.stdlib.threading as owned

    pointer = object()
    calls = []
    monkeypatch.setattr(owned, "_condition_new", lambda lock: pointer)
    monkeypatch.setattr(owned, "_condition_notify", lambda p: calls.append(("one", p)))
    monkeypatch.setattr(owned, "_condition_notify_all", lambda p: calls.append(("all", p)))
    condition = owned.Condition()
    assert condition.notify() is None
    assert condition.notify_all() is None
    assert calls == [("one", pointer), ("all", pointer)]
