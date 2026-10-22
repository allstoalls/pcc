from __future__ import annotations

import re

from pcc.frontends.python.pipeline import compile_python


def test_str_join_pins_sequence_across_allocating_runtime_call(tmp_path):
    src = tmp_path / "str_join_root.py"
    out = tmp_path / "str_join_root.ll"
    src.write_text(
        "def join_parts(sep: str, parts: list[str]) -> str:\n"
        "    return sep.join(parts)\n",
        encoding="utf-8",
    )

    compile_python(
        str(src),
        str(out),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
        emit_llvm_only=True,
    )
    ir_text = out.read_text(encoding="utf-8")
    match = re.search(
        r"define[^@]*@user_str_join_root_join_parts\([^)]*\) \{(?P<body>.*?)\n\}",
        ir_text,
        re.DOTALL,
    )
    assert match is not None, ir_text
    body = match.group("body")

    join_pos = body.index("@py_str_join")
    arg_pin_pos = body.rindex("@pcc_gc_pin", 0, join_pos)
    result_pin_pos = body.index("@pcc_gc_pin", join_pos)
    arg_unpin_pos = body.index("@pcc_gc_unpin", result_pin_pos)
    result_unpin_pos = body.index("@pcc_gc_unpin", arg_unpin_pos + 1)

    assert arg_pin_pos < join_pos < result_pin_pos < arg_unpin_pos < result_unpin_pos
