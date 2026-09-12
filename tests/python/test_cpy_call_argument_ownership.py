from __future__ import annotations

import re

import pytest


def _compile_to_ir(tmp_path, source: str) -> str:
    from pcc.py_frontend.pipeline import compile_python

    src = tmp_path / "cpy_call_argument_ownership.py"
    out = tmp_path / "cpy_call_argument_ownership.ll"
    src.write_text(source, encoding="utf-8")
    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        libpython_mode="auto",
    )
    return out.read_text(encoding="utf-8")


def _function_body(ir_text: str, name: str) -> str:
    match = re.search(
        rf"define\s+[^\n]*?@[A-Za-z0-9_]*{re.escape(name)}\s*"
        r"\([^)]*\)[^{]*\{(.+?)\n\}",
        ir_text,
        re.DOTALL,
    )
    assert match is not None, ir_text
    return match.group(1)


def _block_containing(body: str, needle: str) -> str:
    for match in re.finditer(
        r"(?ms)^[A-Za-z0-9_.]+:.*?(?=^[A-Za-z0-9_.]+:|\Z)",
        body,
    ):
        if needle in match.group(0):
            return match.group(0)
    raise AssertionError(body)


def _cleanup_chain(body: str, start_label: str) -> str:
    """Every block reachable from ``start_label`` through unconditional edges.

    Operand cleanup is a chain: each block releases what it owns and branches
    to the next one, which releases the outer operand, and so on to the
    function's error exit.  Asserting a release lands in the block a failing
    site branches to *directly* pins one link of that chain, so an extra link
    -- a container temp root, say -- moves the release one block along and the
    assertion fails even though the error path still releases it exactly once.
    Walking the chain checks the path instead of the link.
    """
    blocks = {}
    for match in re.finditer(
        r"(?ms)^([A-Za-z0-9_.]+):(.*?)(?=^[A-Za-z0-9_.]+:|\Z)",
        body,
    ):
        blocks[match.group(1)] = match.group(0)
    seen = []
    work = [start_label]
    while work:
        label = work.pop()
        if label in seen or label not in blocks:
            continue
        seen.append(label)
        for nxt in re.findall(r"br label %([A-Za-z0-9_.]+)", blocks[label]):
            work.append(nxt)
        for nxt in re.findall(
            r"br i1 [^,]+, label %([A-Za-z0-9_.]+), label %([A-Za-z0-9_.]+)",
            blocks[label],
        ):
            work.extend(nxt)
    return "".join(blocks[label] for label in seen)


def _blocks_containing(body: str, needle: str) -> list[str]:
    return [
        match.group(0)
        for match in re.finditer(
            r"(?ms)^[A-Za-z0-9_.]+:.*?(?=^[A-Za-z0-9_.]+:|\Z)",
            body,
        )
        if needle in match.group(0)
    ]


def test_immortal_string_literals_skip_container_operand_pins(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
def stable_list() -> list[str]:
    return ["a", "b"]

def stable_tuple() -> tuple[str, str]:
    return ("c", "d")

def stable_dict() -> dict[str, str]:
    return {"e": "f"}

def stable_singletons() -> tuple[object, object]:
    return (None, True)
""",
    )
    literal_pin = re.compile(
        r'call void @pcc_gc_pin\(ptr @"?\.pystr\.obj\.'
    )
    literal_unpin = re.compile(
        r'call void @pcc_gc_unpin\(ptr @"?\.pystr\.obj\.'
    )
    for name in ("stable_list", "stable_tuple", "stable_dict"):
        body = _function_body(ir_text, name)
        assert literal_pin.search(body) is None, body
        assert literal_unpin.search(body) is None, body
    singleton_body = _function_body(ir_text, "stable_singletons")
    assert not re.search(
        r"call void @pcc_gc_(?:pin|unpin)\(ptr %(?:none|m\.bool_box)",
        singleton_body,
    ), singleton_body


def test_dynamic_string_dict_operands_remain_pinned(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
def dynamic_dict(key: str, value: str) -> dict[str, str]:
    return {key: value}
""",
    )
    body = _function_body(ir_text, "dynamic_dict")
    set_call = re.search(
        r"call void @py_dict_set\(ptr [^,]+, ptr "
        r"(?P<key>%[-A-Za-z0-9._]+), ptr "
        r"(?P<value>%[-A-Za-z0-9._]+)\)",
        body,
    )
    assert set_call is not None, body
    for operand in (set_call.group("key"), set_call.group("value")):
        pin = f"call void @pcc_gc_pin(ptr {operand})"
        unpin = f"call void @pcc_gc_unpin(ptr {operand})"
        assert pin in body, body
        assert unpin in body, body


def test_freestanding_raw_pointer_call_args_do_not_emit_managed_pins(tmp_path):
    from pcc.py_frontend.pipeline import compile_python

    src = tmp_path / "freestanding_raw_pointer_call.py"
    out = tmp_path / "freestanding_raw_pointer_call.ll"
    src.write_text(
        "__pcc_freestanding__ = True\n"
        "from pcc.extern import c_abi_export\n"
        "from pcc.unsafe import ptr_add, stack_alloc, store_i64\n"
        "\n"
        "@c_abi_export('raw_write')\n"
        "def raw_write(pointer) -> None:\n"
        "    store_i64(pointer, 0, 41)\n"
        "\n"
        "@c_abi_export('raw_call')\n"
        "def raw_call() -> None:\n"
        "    memory = stack_alloc(16)\n"
        "    raw_write(ptr_add(memory, 8))\n",
        encoding="utf-8",
    )
    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    llvm_ir = out.read_text(encoding="utf-8")
    body = _function_body(llvm_ir, "raw_call")
    assert "@pcc_gc_pin" not in body
    assert "@pcc_gc_unpin" not in body


def test_c_abi_library_raw_pointer_call_args_do_not_emit_managed_pins(tmp_path):
    from pcc.py_frontend.pipeline import compile_python

    src = tmp_path / "c_abi_raw_pointer_call.py"
    out = tmp_path / "c_abi_raw_pointer_call.ll"
    src.write_text(
        "from pcc.extern import c_abi_export\n"
        "from pcc.unsafe import ptr_add, stack_alloc, store_i64\n"
        "\n"
        "def raw_write(pointer) -> None:\n"
        "    store_i64(pointer, 0, 41)\n"
        "\n"
        "@c_abi_export('raw_call')\n"
        "def raw_call() -> None:\n"
        "    memory = stack_alloc(16)\n"
        "    raw_write(ptr_add(memory, 8))\n",
        encoding="utf-8",
    )
    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    llvm_ir = out.read_text(encoding="utf-8")
    body = _function_body(llvm_ir, "raw_call")
    assert "@pcc_gc_pin" not in body
    assert "@pcc_gc_unpin" not in body


def test_owned_one_arg_is_released_after_cpython_call(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Decimal

def owned_one() -> object:
    return Decimal(41)
""",
    )
    body = _function_body(ir_text, "owned_one")
    boxed = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr '
        r'@py_cpy_from_(?:i64|pcc_obj)\(',
        body,
    )
    assert boxed is not None, body
    value = re.escape(boxed.group("value"))
    call = re.search(
        rf"call ptr @py_cpy_call1\(ptr [^,]+, ptr {value}\)",
        body,
    )
    release = re.search(
        rf"call void @py_cpy_decref\(ptr {value}\)",
        body,
    )
    assert call is not None, body
    assert release is not None, body
    assert call.start() < release.start(), body


def test_cpython_arg_bridge_consumes_only_fresh_native_source(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context

def fresh_native_arg() -> object:
    return Context(str(1))

def borrowed_native_arg(value: str) -> object:
    return Context(value)
""",
    )
    fresh_body = _function_body(ir_text, "fresh_native_arg")
    fresh_bridge = re.search(
        r"(?P<cpy>%[A-Za-z0-9_.]+) = call ptr @py_cpy_from_pccstr\(ptr "
        r"(?P<source>%[A-Za-z0-9_.]+)\)",
        fresh_body,
    )
    assert fresh_bridge is not None, fresh_body
    fresh_source = fresh_bridge.group("source")
    assert f"call void @pcc_gc_pin(ptr {fresh_source})" in fresh_body, fresh_body
    source_release_blocks = _blocks_containing(
        fresh_body,
        f"call void @pcc_gc_release(ptr {fresh_source})",
    )
    assert any(block.startswith("cpy.arg.err.cleanup") for block in source_release_blocks)
    assert any(block.startswith("cpy.arg.cont") for block in source_release_blocks)

    borrowed_body = _function_body(ir_text, "borrowed_native_arg")
    borrowed_bridge = re.search(
        r"call ptr @py_cpy_from_pccstr\(ptr (?P<source>%[A-Za-z0-9_.]+)\)",
        borrowed_body,
    )
    assert borrowed_bridge is not None, borrowed_body
    borrowed_source = borrowed_bridge.group("source")
    assert f"call void @pcc_gc_pin(ptr {borrowed_source})" in borrowed_body
    assert f"call void @pcc_gc_unpin(ptr {borrowed_source})" in borrowed_body
    assert f"call void @pcc_gc_release(ptr {borrowed_source})" not in borrowed_body


def test_exact_int_cpython_arg_preserves_precision_and_source_ownership(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context

def fresh_exact_arg() -> object:
    return Context(2 ** 100)

def borrowed_exact_arg(value: int) -> object:
    return Context(value)
""",
    )
    fresh_body = _function_body(ir_text, "fresh_exact_arg")
    fresh_bridge = re.search(
        r"call ptr @py_cpy_from_pcc_obj\(ptr "
        r"(?P<source>%[A-Za-z0-9_.]+)\)",
        fresh_body,
    )
    assert fresh_bridge is not None, fresh_body
    fresh_source = fresh_bridge.group("source")
    assert "call i64 @py_int_to_i64" not in fresh_body, fresh_body
    assert f"call void @pcc_gc_pin(ptr {fresh_source})" in fresh_body, fresh_body
    assert f"call void @pcc_gc_release(ptr {fresh_source})" in fresh_body, fresh_body

    borrowed_body = _function_body(ir_text, "borrowed_exact_arg")
    borrowed_bridge = re.search(
        r"call ptr @py_cpy_from_pcc_obj\(ptr "
        r"(?P<source>%[A-Za-z0-9_.]+)\)",
        borrowed_body,
    )
    assert borrowed_bridge is not None, borrowed_body
    borrowed_source = borrowed_bridge.group("source")
    assert "call i64 @py_int_to_i64" not in borrowed_body, borrowed_body
    assert f"call void @pcc_gc_pin(ptr {borrowed_source})" in borrowed_body
    assert f"call void @pcc_gc_unpin(ptr {borrowed_source})" in borrowed_body
    assert f"call void @pcc_gc_release(ptr {borrowed_source})" not in borrowed_body


@pytest.mark.parametrize(("name", "expression"), (
    ("fresh_left", "make_values() < Decimal(2)"),
    ("fresh_right", "Decimal(2) < make_values()"),
))
def test_cpython_compare_bridge_consumes_fresh_native_source(
    name,
    expression,
    tmp_path,
):
    ir_text = _compile_to_ir(
        tmp_path,
        f"""
from decimal import Decimal

def make_values() -> list[int]:
    return [1]

def {name}() -> bool:
    return {expression}
""",
    )
    body = _function_body(ir_text, name)
    bridge = re.search(
        r"(?P<cpy>%cpy\.from_pcc_list[A-Za-z0-9_.]*) = call ptr "
        r"@py_cpy_from_pcc_obj\(ptr "
        r"(?P<source>%[A-Za-z0-9_.]+)\)",
        body,
    )
    assert bridge is not None, body
    source = bridge.group("source")
    assert f"call void @pcc_gc_pin(ptr {source})" in body, body
    assert f"call void @pcc_gc_release(ptr {source})" in body, body


def test_cpython_compare_bridge_keeps_borrowed_native_source(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Decimal

def borrowed_left(values: list[int]) -> bool:
    return values < Decimal(2)
""",
    )
    body = _function_body(ir_text, "borrowed_left")
    bridge = re.search(
        r"(?P<cpy>%cpy\.from_pcc_list[A-Za-z0-9_.]*) = call ptr "
        r"@py_cpy_from_pcc_obj\(ptr "
        r"(?P<source>%[A-Za-z0-9_.]+)\)",
        body,
    )
    assert bridge is not None, body
    source = bridge.group("source")
    assert f"call void @pcc_gc_pin(ptr {source})" in body, body
    assert f"call void @pcc_gc_unpin(ptr {source})" in body, body
    assert f"call void @pcc_gc_release(ptr {source})" not in body, body


def test_cpython_binop_keeps_fresh_native_source_live_and_cleans_failures(
    tmp_path,
):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context, Decimal

def fresh_binop() -> object:
    return Context(str(1) + Decimal(2))

def failing_rhs_binop() -> object:
    return Context(str(1) + Decimal("bad"))

def borrowed_binop(value: str) -> object:
    return Context(value + Decimal(2))
""",
    )
    for name in ("fresh_binop", "failing_rhs_binop"):
        body = _function_body(ir_text, name)
        bridge = re.search(
            r"call ptr @py_cpy_from_pccstr\(ptr "
            r"(?P<source>%[A-Za-z0-9_.]+)\)",
            body,
        )
        assert bridge is not None, body
        source = bridge.group("source")
        assert f"call void @pcc_gc_pin(ptr {source})" in body, body
        cleanup_blocks = _blocks_containing(
            body,
            f"call void @pcc_gc_release(ptr {source})",
        )
        assert any(
            block.startswith(("cpy.operand.", "cpy.arg.err.cleanup"))
            and f"call void @pcc_gc_unpin(ptr {source})" in block
            for block in cleanup_blocks
        ), body
        assert any(
            "call ptr @py_cpy_binop" in body
            and not block.startswith(("cpy.operand.", "cpy.arg.err.cleanup"))
            for block in cleanup_blocks
        ), body

    borrowed_body = _function_body(ir_text, "borrowed_binop")
    borrowed_bridge = re.search(
        r"call ptr @py_cpy_from_pccstr\(ptr "
        r"(?P<source>%[A-Za-z0-9_.]+)\)",
        borrowed_body,
    )
    assert borrowed_bridge is not None, borrowed_body
    borrowed_source = borrowed_bridge.group("source")
    assert f"call void @pcc_gc_pin(ptr {borrowed_source})" in borrowed_body
    assert f"call void @pcc_gc_unpin(ptr {borrowed_source})" in borrowed_body
    assert f"call void @pcc_gc_release(ptr {borrowed_source})" not in borrowed_body


@pytest.mark.parametrize(("name", "expression"), (
    ("conditional_value", "Decimal(1) if flag else make_values()"),
    ("boolean_value", "Decimal(0) or make_values()"),
))
def test_cpython_value_phi_consumes_fresh_native_branch(
    name,
    expression,
    tmp_path,
):
    ir_text = _compile_to_ir(
        tmp_path,
        f"""
from decimal import Context, Decimal

def make_values() -> list[int]:
    return [1]

def {name}(flag: bool) -> object:
    return Context({expression})
""",
    )
    body = _function_body(ir_text, name)
    bridges = list(re.finditer(
        r"call ptr @py_cpy_from_pcc_obj\(ptr "
        r"(?P<source>%[A-Za-z0-9_.]+)\)",
        body,
    ))
    assert bridges, body
    assert any(
        f"call void @pcc_gc_pin(ptr {bridge.group('source')})" in body
        and f"call void @pcc_gc_release(ptr {bridge.group('source')})" in body
        for bridge in bridges
    ), body


def test_cpython_method_bridge_consumes_only_fresh_native_receiver(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
def make_text() -> str:
    return "value"

def fresh_receiver() -> object:
    return make_text().unknown_fallback_method()

def borrowed_receiver(value: str) -> object:
    return value.unknown_fallback_method()
""",
    )
    fresh_body = _function_body(ir_text, "fresh_receiver")
    fresh_bridge = re.search(
        r"call ptr @py_cpy_from_pccstr\(ptr "
        r"(?P<source>%[A-Za-z0-9_.]+)\)",
        fresh_body,
    )
    assert fresh_bridge is not None, fresh_body
    fresh_source = fresh_bridge.group("source")
    assert f"call void @pcc_gc_pin(ptr {fresh_source})" in fresh_body
    assert f"call void @pcc_gc_release(ptr {fresh_source})" in fresh_body

    borrowed_body = _function_body(ir_text, "borrowed_receiver")
    borrowed_bridge = re.search(
        r"call ptr @py_cpy_from_pccstr\(ptr "
        r"(?P<source>%[A-Za-z0-9_.]+)\)",
        borrowed_body,
    )
    assert borrowed_bridge is not None, borrowed_body
    borrowed_source = borrowed_bridge.group("source")
    assert f"call void @pcc_gc_pin(ptr {borrowed_source})" in borrowed_body
    assert f"call void @pcc_gc_unpin(ptr {borrowed_source})" in borrowed_body
    assert f"call void @pcc_gc_release(ptr {borrowed_source})" not in borrowed_body


def test_numeric_cpython_method_preserves_exact_receiver_ownership(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
def fresh_exact_receiver() -> object:
    return (2 ** 100).conjugate()

def borrowed_exact_receiver(value: int) -> object:
    return value.conjugate()
""",
    )
    fresh_body = _function_body(ir_text, "fresh_exact_receiver")
    fresh_bridge = re.search(
        r"call ptr @py_cpy_from_pcc_obj\(ptr "
        r"(?P<source>%[A-Za-z0-9_.]+)\)",
        fresh_body,
    )
    assert fresh_bridge is not None, fresh_body
    fresh_source = fresh_bridge.group("source")
    assert "call i64 @py_int_to_i64" not in fresh_body, fresh_body
    assert f"call void @pcc_gc_pin(ptr {fresh_source})" in fresh_body
    assert f"call void @pcc_gc_release(ptr {fresh_source})" in fresh_body

    borrowed_body = _function_body(ir_text, "borrowed_exact_receiver")
    borrowed_bridge = re.search(
        r"call ptr @py_cpy_from_pcc_obj\(ptr "
        r"(?P<source>%[A-Za-z0-9_.]+)\)",
        borrowed_body,
    )
    assert borrowed_bridge is not None, borrowed_body
    borrowed_source = borrowed_bridge.group("source")
    assert "call i64 @py_int_to_i64" not in borrowed_body, borrowed_body
    assert f"call void @pcc_gc_pin(ptr {borrowed_source})" in borrowed_body
    assert f"call void @pcc_gc_unpin(ptr {borrowed_source})" in borrowed_body
    assert f"call void @pcc_gc_release(ptr {borrowed_source})" not in borrowed_body


def test_mixed_four_args_promote_only_borrowed_refs_before_stealing_call(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context, Decimal

def mixed_four() -> object:
    existing = Decimal(2)
    return Context(1, existing, 3, existing)
""",
    )
    body = _function_body(ir_text, "mixed_four")
    borrowed = re.findall(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call1\(',
        body,
    )
    assert len(borrowed) == 1, body
    outer_call = body.find("call ptr @py_cpy_call_argv")
    assert outer_call >= 0, body
    outer_block = _block_containing(body, "call ptr @py_cpy_call_argv")
    for value in borrowed:
        incref = f"call void @py_cpy_incref(ptr {value})"
        assert body.count(incref) == 2, body
        assert body.find(incref) < outer_call, body

    transferred_owned = re.findall(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr '
        r'@py_cpy_from_(?:i64|pcc_obj)\(',
        body,
    )
    assert len(transferred_owned) == 3, body
    # One box feeds the local-producing one-arg call and is released there.
    # The other two are transferred directly to the stealing argv helper: they
    # must be neither promoted nor released by the caller.
    direct_owned = [
        value
        for value in transferred_owned
        if f"ptr {value})" not in "\n".join(
            line for line in body.splitlines() if "@py_cpy_call1" in line
        )
    ]
    assert len(direct_owned) == 2, body
    for value in direct_owned:
        assert f"@py_cpy_incref(ptr {value})" not in body
        assert f"@py_cpy_decref(ptr {value})" not in outer_block


def test_kwargs_distinguish_stolen_positional_from_borrowed_values(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context, Decimal

def mixed_kwargs() -> object:
    existing_pos = Decimal(2)
    existing_kw = Context(4, 5)
    return Context(1, existing_pos, capitals=3, clamp=existing_kw)
""",
    )
    body = _function_body(ir_text, "mixed_kwargs")
    outer_call = body.find("call ptr @py_cpy_call_kw")
    assert outer_call >= 0, body
    outer_block = _block_containing(body, "call ptr @py_cpy_call_kw")

    borrowed_pos_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call1\(',
        body,
    )
    borrowed_kw_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call2\(',
        body,
    )
    assert borrowed_pos_match is not None, body
    assert borrowed_kw_match is not None, body
    borrowed_pos = borrowed_pos_match.group("value")
    borrowed_kw = borrowed_kw_match.group("value")

    pos_values = re.findall(
        r"store ptr (?P<value>%[A-Za-z0-9_.]+), ptr %pos\.[0-9]+\.",
        body,
    )
    kw_values = re.findall(
        r"store ptr (?P<value>%[A-Za-z0-9_.]+), ptr %kwv\.[0-9]+\.",
        body,
    )
    assert len(pos_values) == 2, body
    assert len(kw_values) == 2, body
    assert borrowed_pos in pos_values, body
    assert borrowed_kw in kw_values, body

    incref = body.find(f"call void @py_cpy_incref(ptr {borrowed_pos})")
    assert 0 <= incref < outer_call, body
    assert f"@py_cpy_decref(ptr {borrowed_pos})" not in outer_block
    assert f"@py_cpy_incref(ptr {borrowed_kw})" not in body
    assert f"@py_cpy_decref(ptr {borrowed_kw})" not in body

    callable_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = load ptr, ptr @\.cpy\.modref\.Context',
        body,
    )
    assert callable_match is not None, body
    callable_value = callable_match.group("value")
    assert f"call void @py_cpy_decref(ptr {callable_value})" not in body

    owned_pos = next(value for value in pos_values if value != borrowed_pos)
    owned_kw = next(value for value in kw_values if value != borrowed_kw)
    assert f"@py_cpy_incref(ptr {owned_pos})" not in body
    assert f"@py_cpy_decref(ptr {owned_pos})" not in outer_block
    assert f"call void @py_cpy_decref(ptr {owned_kw})" in outer_block


def test_kwdict_call_promotes_borrowed_positional_and_releases_owned_mapping(
    tmp_path,
):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context, Decimal

def mixed_kwdict(options: dict[str, int]) -> object:
    existing = Decimal(2)
    return Context(1, existing, **options)
""",
    )
    body = _function_body(ir_text, "mixed_kwdict")
    borrowed_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call1\(',
        body,
    )
    assert borrowed_match is not None, body
    borrowed = borrowed_match.group("value")
    pos_values = re.findall(
        r"store ptr (?P<value>%[A-Za-z0-9_.]+), ptr %pos\.[0-9]+\.",
        body,
    )
    assert len(pos_values) == 2, body
    assert borrowed in pos_values, body

    outer_call = body.find("call ptr @py_cpy_call_kwdict")
    outer_block = _block_containing(body, "call ptr @py_cpy_call_kwdict")
    incref = body.find(f"call void @py_cpy_incref(ptr {borrowed})")
    assert 0 <= incref < outer_call, body
    assert f"@py_cpy_decref(ptr {borrowed})" not in outer_block
    borrowed_cleanup_blocks = _blocks_containing(
        body,
        f"call void @py_cpy_decref(ptr {borrowed})",
    )
    assert any(
        block.startswith("cpy.arg.err.cleanup")
        for block in borrowed_cleanup_blocks
    ), body

    owned_pos = next(value for value in pos_values if value != borrowed)
    assert f"@py_cpy_incref(ptr {owned_pos})" not in body
    assert f"@py_cpy_decref(ptr {owned_pos})" not in outer_block

    mapping_match = re.search(
        r'(?P<value>%cpy\.from_pcc_dict[A-Za-z0-9_.]*) = call ptr '
        r'@py_cpy_from_pcc_obj\(',
        body,
    )
    assert mapping_match is not None, body
    mapping = mapping_match.group("value")
    assert f"call void @py_cpy_decref(ptr {mapping})" in outer_block

    callable_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = load ptr, ptr @\.cpy\.modref\.Context',
        body,
    )
    assert callable_match is not None, body
    callable_value = callable_match.group("value")
    assert f"call void @py_cpy_decref(ptr {callable_value})" not in body


def test_kwdict_plus_preserves_mixed_positional_and_keyword_ownership(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Decimal

def mixed_kwdict_plus(options: dict[str, int]) -> object:
    receiver = Decimal(0)
    existing = Decimal(2)
    return receiver.quantize(1, existing, rounding=3, **options)
""",
    )
    body = _function_body(ir_text, "mixed_kwdict_plus")
    pos_values = re.findall(
        r"store ptr (?P<value>%[A-Za-z0-9_.]+), ptr %posmix\.[0-9]+\.",
        body,
    )
    explicit_kw_values = re.findall(
        r"store ptr (?P<value>%[A-Za-z0-9_.]+), ptr %mixv\.[0-9]+\.",
        body,
    )
    nested_results = set(
        re.findall(
            r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call1\(',
            body,
        )
    )
    assert len(pos_values) == 2, body
    assert len(explicit_kw_values) == 1, body
    borrowed_pos_values = [value for value in pos_values if value in nested_results]
    assert len(borrowed_pos_values) == 1, body
    borrowed_pos = borrowed_pos_values[0]
    owned_pos = next(value for value in pos_values if value != borrowed_pos)
    owned_kw = explicit_kw_values[0]

    outer_call = body.find("call ptr @py_cpy_call_kwdict_plus")
    incref = body.find(f"call void @py_cpy_incref(ptr {borrowed_pos})")
    assert 0 <= incref < outer_call, body
    success_block = _block_containing(body, "call ptr @py_cpy_call_kwdict_plus")
    # Pre-call NULL guards release this promoted bump on their error edges;
    # the success path transfers it to the stealing helper and must not release
    # it afterwards.
    assert f"@py_cpy_decref(ptr {borrowed_pos})" not in success_block
    assert f"@py_cpy_incref(ptr {owned_pos})" not in body
    assert f"@py_cpy_decref(ptr {owned_pos})" not in success_block
    assert f"call void @py_cpy_decref(ptr {owned_kw})" in success_block

    mapping_match = re.search(
        r'(?P<value>%cpy\.from_pcc_dict[A-Za-z0-9_.]*) = call ptr '
        r'@py_cpy_from_pcc_obj\(',
        body,
    )
    assert mapping_match is not None, body
    mapping = mapping_match.group("value")
    assert f"call void @py_cpy_decref(ptr {mapping})" in success_block

    callable_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_getattr\(',
        body,
    )
    assert callable_match is not None, body
    callable_value = callable_match.group("value")
    assert f"call void @py_cpy_decref(ptr {callable_value})" in success_block


def test_kwargs_call_does_not_release_borrowed_imported_callable(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Decimal

def borrowed_callable_kwargs() -> object:
    return Decimal(value=1)
""",
    )
    body = _function_body(ir_text, "borrowed_callable_kwargs")
    callable_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = load ptr, ptr @\.cpy\.modref\.Decimal',
        body,
    )
    assert callable_match is not None, body
    callable_value = callable_match.group("value")
    assert "call ptr @py_cpy_call_kw" in body, body
    assert f"call void @py_cpy_decref(ptr {callable_value})" not in body


def test_kwargs_call_releases_fresh_cpython_callable(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from operator import methodcaller

def fresh_callable_kwargs() -> object:
    return methodcaller("as_tuple")(obj=1)
""",
    )
    body = _function_body(ir_text, "fresh_callable_kwargs")
    callable_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call1\(',
        body,
    )
    assert callable_match is not None, body
    callable_value = callable_match.group("value")
    outer_call = body.rfind("call ptr @py_cpy_call_kw")
    assert outer_call >= 0, body
    outer_block = _block_containing(body, "call ptr @py_cpy_call_kw")
    assert outer_block.count(
        f"call void @py_cpy_decref(ptr {callable_value})"
    ) == 1


def test_nonkwargs_builtin_call_releases_module_and_fresh_callable(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
def call_builtin() -> object:
    return dir(1)
""",
    )
    body = _function_body(ir_text, "call_builtin")
    module_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_import\(',
        body,
    )
    callable_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_getattr\(',
        body,
    )
    assert module_match is not None, body
    assert callable_match is not None, body
    module_value = module_match.group("value")
    callable_value = callable_match.group("value")
    getattr_call = callable_match.end()
    outer_call = body.find("call ptr @py_cpy_call1", getattr_call)
    module_release = body.find(f"call void @py_cpy_decref(ptr {module_value})")
    assert getattr_call < module_release < outer_call, body
    assert body.count(f"call void @py_cpy_decref(ptr {module_value})") == 1
    outer_block = _block_containing(body, "call ptr @py_cpy_call1")
    assert outer_block.count(
        f"call void @py_cpy_decref(ptr {callable_value})"
    ) == 1
    assert outer_block.find("call ptr @py_cpy_call1") < outer_block.find(
        f"call void @py_cpy_decref(ptr {callable_value})"
    )


def test_nested_fresh_cpython_result_is_released_after_borrowing_call(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context, Decimal

def nested_fresh_result() -> object:
    return Context(Decimal(1))
""",
    )
    body = _function_body(ir_text, "nested_fresh_result")
    calls = re.findall(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call1\(',
        body,
    )
    assert len(calls) == 2, body
    nested_value = calls[0]
    outer_call = body.rfind("call ptr @py_cpy_call1")
    release = body.find(f"call void @py_cpy_decref(ptr {nested_value})")
    assert outer_call < release, body
    assert body.count(f"call void @py_cpy_decref(ptr {nested_value})") == 1


def test_nested_fresh_results_transfer_to_stealing_argv_call(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context, Decimal

def nested_fresh_argv() -> object:
    return Context(1, Decimal(2), 3, Decimal(4))
""",
    )
    body = _function_body(ir_text, "nested_fresh_argv")
    nested_values = re.findall(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call1\(',
        body,
    )
    assert len(nested_values) == 2, body
    outer_call = body.find("call ptr @py_cpy_call_argv")
    assert outer_call >= 0, body
    outer_block = _block_containing(body, "call ptr @py_cpy_call_argv")
    for value in nested_values:
        assert f"call void @py_cpy_incref(ptr {value})" not in body
        assert f"call void @py_cpy_decref(ptr {value})" not in outer_block


def test_method_call_consumes_owned_receiver_but_not_borrowed_receiver(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Decimal

def owned_receiver() -> object:
    return Decimal(1).as_tuple()

def borrowed_receiver() -> object:
    value = Decimal(1)
    return value.as_tuple()
""",
    )
    owned_body = _function_body(ir_text, "owned_receiver")
    receiver_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call1\(',
        owned_body,
    )
    assert receiver_match is not None, owned_body
    receiver = receiver_match.group("value")
    getattr_at = owned_body.find(
        f"call ptr @py_cpy_getattr(ptr {receiver},"
    )
    before_getattr = owned_body[receiver_match.end() : getattr_at]
    assert re.search(
        rf"icmp eq ptr {re.escape(receiver)}, null",
        before_getattr,
    ), owned_body
    assert "br i1" in before_getattr, owned_body
    receiver_release_at = owned_body.find(
        f"call void @py_cpy_decref(ptr {receiver})"
    )
    outer_call_at = owned_body.find("call ptr @py_cpy_call_noargs")
    assert 0 <= getattr_at < receiver_release_at < outer_call_at, owned_body

    borrowed_body = _function_body(ir_text, "borrowed_receiver")
    getattr_match = re.search(
        r"call ptr @py_cpy_getattr\(ptr (?P<value>%[A-Za-z0-9_.]+),",
        borrowed_body,
    )
    assert getattr_match is not None, borrowed_body
    borrowed_receiver = getattr_match.group("value")
    assert (
        f"call void @py_cpy_decref(ptr {borrowed_receiver})"
        not in borrowed_body
    )


def test_marshaled_numeric_method_receiver_is_consumed_once(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
def boxed_receiver() -> object:
    return True.conjugate()
""",
    )
    body = _function_body(ir_text, "boxed_receiver")
    receiver_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr '
        r'@py_cpy_from_(?:pcc_obj|i64)\(',
        body,
    )
    assert receiver_match is not None, body
    receiver = receiver_match.group("value")
    getattr_at = body.find(f"call ptr @py_cpy_getattr(ptr {receiver},")
    release_at = body.find(f"call void @py_cpy_decref(ptr {receiver})")
    outer_call_at = body.find("call ptr @py_cpy_call_noargs")
    assert 0 <= getattr_at < release_at < outer_call_at, body
    assert body.count(f"call void @py_cpy_decref(ptr {receiver})") == 1


def test_marshaled_owned_compare_receiver_and_result_are_each_consumed_once(
    tmp_path,
):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Decimal

def fresh_compare() -> bool:
    return Decimal(1) < Decimal(2)
""",
    )
    body = _function_body(ir_text, "fresh_compare")
    call_results = re.findall(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call1\(',
        body,
    )
    assert len(call_results) == 3, body
    receiver, _other_operand, comparison_result = call_results
    receiver_cleanup_blocks = _blocks_containing(
        body,
        f"call void @py_cpy_decref(ptr {receiver})",
    )
    assert any(
        "call ptr @py_cpy_getattr" in block
        for block in receiver_cleanup_blocks
    ), body
    assert all(
        block.count(f"call void @py_cpy_decref(ptr {receiver})") == 1
        for block in receiver_cleanup_blocks
    ), body
    truth_at = body.find(
        f"call i32 @py_cpy_truthy(ptr {comparison_result})"
    )
    result_release_at = body.find(
        f"call void @py_cpy_decref(ptr {comparison_result})"
    )
    assert 0 <= truth_at < result_release_at, body
    normal_result_blocks = _blocks_containing(
        body,
        f"call void @py_cpy_decref(ptr {comparison_result})",
    )
    assert any(block.startswith("cpy.status.cont") for block in normal_result_blocks)
    assert any(
        block.startswith("cpy.status.err.cleanup")
        for block in normal_result_blocks
    )


def test_ephemeral_builtin_module_method_consumes_module_receiver(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
import builtins as b

def module_dir() -> object:
    return b.__dir__()
""",
    )
    body = _function_body(ir_text, "module_dir")
    module_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_import\(',
        body,
    )
    assert module_match is not None, body
    module_value = module_match.group("value")
    method_match = re.search(
        rf'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_getattr\('
        rf'ptr {re.escape(module_value)}, ptr %[A-Za-z0-9_.]+\)',
        body,
    )
    assert method_match is not None, body
    method_value = method_match.group("value")
    assert body.count(f"call void @py_cpy_decref(ptr {module_value})") == 1, body
    assert body.count(f"call void @py_cpy_decref(ptr {method_value})") == 1, body


def test_print_does_not_release_borrowed_cpython_values(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Decimal

def borrowed_prints() -> object:
    value = Decimal(1)
    print(value)
    print(value, value)
    return value
""",
    )
    body = _function_body(ir_text, "borrowed_prints")
    value_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call1\(',
        body,
    )
    assert value_match is not None, body
    value = value_match.group("value")
    assert body.count(f"call void @py_cpy_decref(ptr {value})") == 0, body


def test_fallback_print_releases_ignored_cpython_none_result(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Decimal

def fallback_print() -> None:
    print("value", flush=Decimal(0))
""",
    )
    body = _function_body(ir_text, "fallback_print")
    result_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call_kw\(',
        body,
    )
    assert result_match is not None, body
    result = result_match.group("value")
    assert body.count(f"call void @py_cpy_decref(ptr {result})") == 1, body


def test_hasattr_releases_cpython_boolean_result_after_truthiness(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Decimal

def cpython_hasattr() -> bool:
    return hasattr(Decimal(1), "real")
""",
    )
    body = _function_body(ir_text, "cpython_hasattr")
    result_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call2\(',
        body,
    )
    assert result_match is not None, body
    result = result_match.group("value")
    truth_at = body.find(f"call i32 @py_cpy_truthy(ptr {result})")
    release_at = body.find(f"call void @py_cpy_decref(ptr {result})")
    assert 0 <= truth_at < release_at, body
    result_cleanup_blocks = _blocks_containing(
        body,
        f"call void @py_cpy_decref(ptr {result})",
    )
    assert any(block.startswith("cpy.status.cont") for block in result_cleanup_blocks)
    assert any(
        block.startswith("cpy.status.err.cleanup")
        for block in result_cleanup_blocks
    )


def test_native_bool_helper_result_is_not_tagged_as_cpython(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
class Native:
    pass

def native_hasattr(value: Native) -> bool:
    return hasattr(value, "marker")

def use_native_hasattr(value: Native) -> int:
    if native_hasattr(value):
        return 1
    return 0
""",
    )
    body = _function_body(ir_text, "use_native_hasattr")
    result_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call i1 (?:\(ptr\) )?'
        r'@user_[^(]*native_hasattr\(',
        body,
    )
    assert result_match is not None, body
    assert "@py_cpy_truthy" not in body
    assert "@py_cpy_decref" not in body


def test_discarded_cpython_call_result_is_released(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Decimal

def discard_result() -> None:
    Decimal(1)
""",
    )
    body = _function_body(ir_text, "discard_result")
    result_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call1\(',
        body,
    )
    assert result_match is not None, body
    result = result_match.group("value")
    assert body.count(f"call void @py_cpy_decref(ptr {result})") == 1, body


def test_user_function_cpython_result_is_owned_by_caller(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Decimal

def make_decimal() -> Decimal:
    return Decimal(1)

def use_decimal() -> object:
    return make_decimal().as_tuple()
""",
    )
    body = _function_body(ir_text, "use_decimal")
    receiver_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @user_[^(]*make_decimal\(',
        body,
    )
    assert receiver_match is not None, body
    receiver = receiver_match.group("value")
    getattr_at = body.find(f"call ptr @py_cpy_getattr(ptr {receiver},")
    release_at = body.find(f"call void @py_cpy_decref(ptr {receiver})")
    assert 0 <= getattr_at < release_at, body
    assert body.count(f"call void @py_cpy_decref(ptr {receiver})") == 1, body


def test_cpython_bridge_releases_only_owned_source_reference(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Decimal

def borrowed_bridge() -> object:
    value = Decimal(1)
    return [0, value]

def owned_bridge() -> object:
    return [0, Decimal(1)]
""",
    )
    borrowed_body = _function_body(ir_text, "borrowed_bridge")
    borrowed_match = re.search(
        r"call ptr @py_cpy_to_pcc_obj\(ptr (?P<value>%[A-Za-z0-9_.]+)\)",
        borrowed_body,
    )
    assert borrowed_match is not None, borrowed_body
    borrowed = borrowed_match.group("value")
    assert f"call void @py_cpy_decref(ptr {borrowed})" not in borrowed_body

    owned_body = _function_body(ir_text, "owned_bridge")
    owned_match = re.search(
        r"(?P<bridge>%[A-Za-z0-9_.]+) = call ptr @py_cpy_to_pcc_obj\(ptr "
        r"(?P<value>%[A-Za-z0-9_.]+)\)",
        owned_body,
    )
    assert owned_match is not None, owned_body
    owned = owned_match.group("value")
    bridge = owned_match.group("bridge")
    append_block = _block_containing(
        owned_body,
        f"call void @py_list_append(ptr %",
    )
    if f"ptr {bridge})" not in append_block:
        append_block = _block_containing(owned_body, f"ptr {bridge})")
    assert "call void @py_list_append" in append_block, owned_body
    assert append_block.count(f"call void @py_cpy_decref(ptr {owned})") == 1
    cleanup_blocks = _blocks_containing(
        owned_body,
        f"call void @py_cpy_decref(ptr {owned})",
    )
    assert any(block.startswith("cpy.arg.err.cleanup") for block in cleanup_blocks)


def test_nested_cpython_binop_result_and_operands_are_consumed(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context, Decimal

def nested_binop() -> object:
    return Context(Decimal(1) + Decimal(2))
""",
    )
    body = _function_body(ir_text, "nested_binop")
    binop_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_binop\(',
        body,
    )
    assert binop_match is not None, body
    operands = re.findall(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call1\(',
        body[: binop_match.start()],
    )
    assert len(operands) == 2, body
    binop = binop_match.group("value")
    binop_call_at = binop_match.start()
    outer_call_at = body.rfind("call ptr @py_cpy_call1")
    binop_block = _block_containing(body, "call ptr @py_cpy_binop")
    binop_call_in_block = binop_block.find("call ptr @py_cpy_binop")
    for operand in operands:
        release_at = binop_block.find(
            f"call void @py_cpy_decref(ptr {operand})"
        )
        assert binop_call_in_block < release_at, body
    assert binop_call_at < outer_call_at, body
    result_release_at = body.find(f"call void @py_cpy_decref(ptr {binop})")
    assert outer_call_at < result_release_at, body


def test_nested_cpython_getitem_result_and_receiver_are_consumed(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context, Decimal

def nested_getitem() -> object:
    return Context(Decimal(1).as_tuple()[0])
""",
    )
    body = _function_body(ir_text, "nested_getitem")
    receiver_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call_noargs\(',
        body,
    )
    getitem_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_getitem\(',
        body,
    )
    assert receiver_match is not None, body
    assert getitem_match is not None, body
    receiver = receiver_match.group("value")
    getitem = getitem_match.group("value")
    getitem_block = _block_containing(
        body,
        f"call ptr @py_cpy_getitem(ptr {receiver},",
    )
    getitem_at = getitem_block.find("call ptr @py_cpy_getitem")
    receiver_release_at = getitem_block.find(
        f"call void @py_cpy_decref(ptr {receiver})"
    )
    outer_call_at = body.rfind("call ptr @py_cpy_call1")
    result_release_at = body.find(f"call void @py_cpy_decref(ptr {getitem})")
    assert 0 <= getitem_at < receiver_release_at, body
    assert getitem_match.start() < outer_call_at, body
    assert outer_call_at < result_release_at, body


def test_nested_cpython_slice_result_and_temporaries_are_consumed(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context, Decimal

def nested_slice() -> object:
    return Context(Decimal(1).as_tuple()[0:1])
""",
    )
    body = _function_body(ir_text, "nested_slice")
    receiver_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call_noargs\(',
        body,
    )
    slice_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call3\(',
        body,
    )
    getitem_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_getitem\(',
        body,
    )
    assert receiver_match is not None, body
    assert slice_match is not None, body
    assert getitem_match is not None, body
    receiver = receiver_match.group("value")
    slice_obj = slice_match.group("value")
    result = getitem_match.group("value")
    getitem_block = _block_containing(
        body,
        f"call ptr @py_cpy_getitem(ptr {receiver}, ptr {slice_obj})",
    )
    getitem_at = getitem_block.find("call ptr @py_cpy_getitem")
    slice_release_at = getitem_block.find(
        f"call void @py_cpy_decref(ptr {slice_obj})"
    )
    receiver_release_at = getitem_block.find(
        f"call void @py_cpy_decref(ptr {receiver})"
    )
    assert 0 <= getitem_at < slice_release_at < receiver_release_at, body
    result_use = f"ptr {result})"
    outer_block = _block_containing(body, result_use)
    if "call ptr @py_cpy_call1" not in outer_block:
        matching_blocks = _blocks_containing(body, result_use)
        outer_block = next(
            block for block in matching_blocks if "call ptr @py_cpy_call1" in block
        )
    assert f"call void @py_cpy_decref(ptr {result})" in outer_block, body


def test_nested_cpython_binop_guards_lhs_before_rhs_and_unwinds_it(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context, Decimal

def nested_binop_short_circuit() -> object:
    return Context(Decimal("bad") + Decimal(2))
""",
    )
    body = _function_body(ir_text, "nested_binop_short_circuit")
    calls = list(
        re.finditer(
            r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call1\('
            r'ptr %cpy.fn.Decimal',
            body,
        )
    )
    assert len(calls) == 2, body
    lhs = calls[0].group("value")
    between = body[calls[0].end() : calls[1].start()]
    assert re.search(rf"icmp eq ptr {re.escape(lhs)}, null", between), body
    cleanup_blocks = _blocks_containing(
        body,
        f"call void @py_cpy_decref(ptr {lhs})",
    )
    assert any(
        block.startswith(("cpy.operand.", "cpy.arg.err.cleanup"))
        for block in cleanup_blocks
    ), body
    binop_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_binop\(',
        body,
    )
    assert binop_match is not None, body
    binop = binop_match.group("value")
    outer_call_at = body.rfind("call ptr @py_cpy_call1")
    release_at = body.find(f"call void @py_cpy_decref(ptr {binop})")
    assert binop_match.start() < outer_call_at < release_at, body


def test_cpython_conditional_phi_normalizes_to_one_owned_result(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context, Decimal

def conditional_decimal(flag: bool) -> object:
    existing = Decimal(0)
    return Context(existing if flag else Decimal(2))
""",
    )
    body = _function_body(ir_text, "conditional_decimal")
    phi_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = phi\s+ptr ',
        body,
    )
    assert phi_match is not None, body
    phi = phi_match.group("value")
    existing_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call1\('
        r'ptr %cpy.fn.Decimal',
        body,
    )
    assert existing_match is not None, body
    existing = existing_match.group("value")
    assert f"call void @py_cpy_incref(ptr {existing})" in body, body
    outer_block = _block_containing(body, f"ptr {phi})")
    assert "call ptr @py_cpy_call1" in outer_block, body
    assert f"call void @py_cpy_decref(ptr {phi})" in outer_block, body


def test_static_cpython_conditional_preserves_owned_result(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context, Decimal

def static_conditional_decimal() -> object:
    return Context(Decimal(1) if True else Decimal(2))
""",
    )
    body = _function_body(ir_text, "static_conditional_decimal")
    inner_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call1\('
        r'ptr %cpy.fn.Decimal',
        body,
    )
    assert inner_match is not None, body
    inner = inner_match.group("value")
    outer_call_at = body.rfind("call ptr @py_cpy_call1")
    release_at = body.find(f"call void @py_cpy_decref(ptr {inner})")
    assert inner_match.start() < outer_call_at < release_at, body


def test_cpython_conditional_condition_is_guarded_and_released(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context, Decimal

def cpython_conditional_condition() -> object:
    return Context(Decimal(1) if Decimal(0) else Decimal(2))
""",
    )
    body = _function_body(ir_text, "cpython_conditional_condition")
    condition_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call1\('
        r'ptr %cpy.fn.Decimal',
        body,
    )
    assert condition_match is not None, body
    condition = condition_match.group("value")
    guard_at = body.find(f"icmp eq ptr {condition}, null")
    truthy_at = body.find(f"call i32 @py_cpy_truthy(ptr {condition})")
    release_at = body.find(f"call void @py_cpy_decref(ptr {condition})")
    branch_at = body.find("label %ternary_true", truthy_at)
    assert condition_match.start() < guard_at < truthy_at < release_at < branch_at, body


@pytest.mark.parametrize("operator", ("and", "or"))
def test_cpython_boolexpr_transfers_selected_operand_to_owned_phi(
    tmp_path,
    operator,
):
    from llvmlite import binding as llvm

    ir_text = _compile_to_ir(
        tmp_path,
        f"""
from decimal import Context, Decimal

def nested_bool_expr() -> object:
    return Context(Decimal(0) {operator} Decimal(1))
""",
    )
    llvm.parse_assembly(ir_text).verify()
    body = _function_body(ir_text, "nested_bool_expr")
    operands = list(
        re.finditer(
            r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call1\('
            r'ptr %cpy.fn.Decimal',
            body,
        )
    )
    assert len(operands) == 2, body
    lhs = operands[0].group("value")
    truthy_at = body.find(f"call i32 @py_cpy_truthy(ptr {lhs})")
    lhs_guard_at = body.find(f"icmp eq ptr {lhs}, null")
    assert operands[0].start() < lhs_guard_at < truthy_at, body
    rhs_blocks = _blocks_containing(body, f"call void @py_cpy_decref(ptr {lhs})")
    assert any(block.startswith("bool.rhs") for block in rhs_blocks), body
    phi_match = re.search(
        rf'(?P<value>%[A-Za-z0-9_.]+) = phi\s+ptr .*{operator}',
        body,
    )
    if phi_match is None:
        phi_match = re.search(
            rf'(?P<value>%{operator}[A-Za-z0-9_.]*) = phi\s+ptr ',
            body,
        )
    assert phi_match is not None, body
    phi = phi_match.group("value")
    outer_blocks = _blocks_containing(body, f"ptr {phi})")
    assert any(
        "call ptr @py_cpy_call1" in block
        and f"call void @py_cpy_decref(ptr {phi})" in block
        for block in outer_blocks
    ), body


def test_cpython_list_literal_stops_after_first_null_element(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context, Decimal

def list_element_short_circuit() -> object:
    return Context([Decimal("bad"), Decimal(2)])
""",
    )
    body = _function_body(ir_text, "list_element_short_circuit")
    element_calls = list(
        re.finditer(
            r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call1\('
            r'ptr %cpy.fn.Decimal',
            body,
        )
    )
    assert len(element_calls) == 2, body
    first = element_calls[0].group("value")
    between = body[element_calls[0].end() : element_calls[1].start()]
    assert re.search(rf"icmp eq ptr {re.escape(first)}, null", between), body
    second = element_calls[1].group("value")
    cleanup_blocks = _blocks_containing(
        body,
        f"call void @py_cpy_decref(ptr {first})",
    )
    assert any(
        block.startswith(("cpy.operand.", "cpy.arg.err.cleanup"))
        and f"{second}" not in block
        for block in cleanup_blocks
    ), body


def test_cpython_list_append_result_is_guarded_with_remaining_refs(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context, Decimal

def list_append_cleanup() -> object:
    return Context([Decimal(1), Decimal(2)])
""",
    )
    body = _function_body(ir_text, "list_append_cleanup")
    element_calls = list(
        re.finditer(
            r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call1\('
            r'ptr %cpy.fn.Decimal',
            body,
        )
    )
    append_calls = list(
        re.finditer(
            r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call1\('
            r'ptr %cpy.get.append',
            body,
        )
    )
    assert len(element_calls) == 2, body
    assert len(append_calls) == 2, body
    first_result = append_calls[0].group("value")
    between = body[append_calls[0].end() : append_calls[1].start()]
    assert re.search(
        rf"icmp eq ptr {re.escape(first_result)}, null",
        between,
    ), body
    remaining = element_calls[1].group("value")
    cleanup_blocks = _blocks_containing(
        body,
        f"call void @py_cpy_decref(ptr {remaining})",
    )
    assert any(
        block.startswith("cpy.arg.err.cleanup")
        for block in cleanup_blocks
    ), body


def test_cpython_dict_setitem_status_is_checked_and_cleans_container(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context, Decimal

def dict_setitem_cleanup() -> object:
    return Context({Decimal(1): 1})
""",
    )
    body = _function_body(ir_text, "dict_setitem_cleanup")
    dict_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call_noargs\('
        r'ptr %cpy.builtin.dict',
        body,
    )
    status_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call i32 @py_cpy_setitem\(',
        body,
    )
    assert dict_match is not None, body
    assert status_match is not None, body
    status = status_match.group("value")
    after_status = body[status_match.end() :]
    assert re.search(rf"icmp slt i32 {re.escape(status)}, 0", after_status), body
    container = dict_match.group("value")
    cleanup_blocks = _blocks_containing(
        body,
        f"call void @py_cpy_decref(ptr {container})",
    )
    assert any(
        block.startswith("cpy.status.err.cleanup")
        for block in cleanup_blocks
    ), body


def test_multi_pair_cpython_key_dict_evaluates_operands_before_inserting(
    tmp_path,
):
    """The pair-count restriction is gone; the ordering it protected is not.

    A CPython-key dict literal used to be refused past one pair, on the
    grounds that a per-pair insertion error had to be visible before later
    operands ran.  The collection loop already evaluates every key and value
    before a single ``_emit_cpython_dict_items`` inserts them, which is what
    CPython's dict display does too (all operands pushed, then one
    ``BUILD_MAP``).  Runtime parity is pinned in
    tests/python/test_py_dict_literal_cpython_key_pairs.py.
    """
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context, Decimal

def later() -> int:
    return 2

def cpython_key_insertion_order() -> object:
    return Context({Decimal("sNaN"): later(), Decimal(3): later()})
""",
    )
    body = _function_body(ir_text, "cpython_key_insertion_order")
    assert body is not None, ir_text
    last_operand = body.rindex("later(")
    first_insert = body.index("@py_cpy_call")
    setitem = [
        m.start()
        for m in re.finditer(r"@py_cpy_(?:setitem|call2|call3)\(", body)
    ]
    assert setitem, body
    # Every operand runs before the first insertion.
    assert last_operand < setitem[-1], body
    assert first_insert >= 0


def test_multi_pair_native_custom_key_dict_inserts_before_later_pair(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context

class BadHash:
    def __hash__(self) -> int:
        return 1

def later() -> int:
    return 2

def native_key_insertion_order() -> object:
    return Context({BadHash(): later(), BadHash(): later()})
""",
    )
    body = _function_body(ir_text, "native_key_insertion_order")
    later_calls = [
        match.start()
        for match in re.finditer(r"call ptr @[^\n]*later\(", body)
    ]
    insert_calls = [
        match.start()
        for match in re.finditer(r"call void @py_dict_set\(", body)
    ]
    assert len(later_calls) == 2, body
    assert len(insert_calls) == 2, body
    assert later_calls[0] < insert_calls[0] < later_calls[1] < insert_calls[1]


def test_native_dyn_dict_key_error_short_circuits_value_operand(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from typing import Any

def later() -> int:
    return 2

def build(mapping: Any) -> dict[object, int]:
    return {mapping["missing"]: later(), "tail": 3}
""",
    )
    body = _function_body(ir_text, "build")
    getitem = body.index("call ptr @py_obj_getitem")
    later = re.search(r"call ptr @[^\n]*later\(", body)
    assert later is not None, body
    assert getitem < later.start()
    assert "@py_err_occurred" in body[getitem : later.start()]


def test_exact_int_list_splat_uses_source_order_boundary(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context

class BadIter:
    def __iter__(self) -> object:
        return self

def exact_int_splat_order() -> object:
    return Context([*BadIter(), 1 << 80])
""",
    )
    body = _function_body(ir_text, "exact_int_splat_order")
    extend_match = re.search(r"call void @py_list_extend\(", body)
    bigint_match = re.search(r"call ptr @py_int_shl\(", body)
    assert extend_match is not None, body
    assert bigint_match is not None, body
    assert extend_match.start() < bigint_match.start(), body


def test_native_scalar_key_dict_cleanup_ir_scales_linearly(tmp_path):
    bodies = []
    for count in (16, 32):
        case_path = tmp_path / str(count)
        case_path.mkdir()
        pairs = ", ".join(
            f'"key_{index}": "value_{index}"' for index in range(count)
        )
        ir_text = _compile_to_ir(
            case_path,
            f"""
def build_table() -> dict[str, str]:
    return {{{pairs}}}
""",
        )
        bodies.append(_function_body(ir_text, "build_table"))

    small, large = bodies
    assert len(large) < len(small) * 3
    assert large.count("call void @pcc_gc_unpin") < (
        small.count("call void @pcc_gc_unpin") * 3
    )


def test_native_str_list_cleanup_ir_scales_linearly(tmp_path):
    bodies = []
    for count in (16, 32):
        case_path = tmp_path / str(count)
        case_path.mkdir()
        values = ", ".join(f'"value_{index}"' for index in range(count))
        ir_text = _compile_to_ir(
            case_path,
            f"""
def build_values() -> list[str]:
    return [{values}]
""",
        )
        bodies.append(_function_body(ir_text, "build_values"))

    small, large = bodies
    assert len(large) < len(small) * 3
    assert large.count("call void @pcc_gc_unpin") < (
        small.count("call void @pcc_gc_unpin") * 3
    )


def test_native_str_tuple_cleanup_ir_scales_linearly(tmp_path):
    bodies = []
    for count in (16, 32):
        case_path = tmp_path / str(count)
        case_path.mkdir()
        values = ", ".join(f'"value_{index}"' for index in range(count))
        ir_text = _compile_to_ir(
            case_path,
            f"""
def build_values() -> tuple:
    return ({values},)
""",
        )
        bodies.append(_function_body(ir_text, "build_values"))

    small, large = bodies
    assert len(large) < len(small) * 3
    assert large.count("call void @pcc_gc_unpin") < (
        small.count("call void @pcc_gc_unpin") * 3
    )


@pytest.mark.parametrize(
    ("literal", "store_name"),
    (
        ("[first(), second()]", "py_list_append"),
        ("(first(), second())", "py_tuple_set_item"),
    ),
)
def test_native_sequence_stores_each_operand_before_evaluating_the_next(
    literal,
    store_name,
    tmp_path,
):
    ir_text = _compile_to_ir(
        tmp_path,
        f"""
def first() -> str:
    return "first"

def second() -> str:
    return "second"

def build_values() -> object:
    return {literal}
""",
    )
    body = _function_body(ir_text, "build_values")
    first_call = re.search(r"call ptr @[^\n(]*first\(", body)
    second_call = re.search(r"call ptr @[^\n(]*second\(", body)
    stores = list(re.finditer(rf"call void @{store_name}\(", body))
    assert first_call is not None, body
    assert second_call is not None, body
    assert len(stores) == 2, body
    assert first_call.start() < stores[0].start() < second_call.start(), body
    assert second_call.start() < stores[1].start(), body


def test_native_function_list_keeps_native_callable_materialization(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
def first() -> None:
    return None

def second() -> None:
    return None

def build_callbacks() -> list:
    return [first, second]
""",
    )
    body = _function_body(ir_text, "build_callbacks")
    assert body.count("call ptr @py_func_new_named(") == 2, body
    assert "@py_cpy_" not in body, body


@pytest.mark.parametrize(
    ("literal", "constructor"),
    (
        ("[1 << 80, 1 // 0]", "py_list_new"),
        ("(1 << 80, 1 // 0)", "py_tuple_new"),
        ("{1: 1 << 80, 2: 1 // 0}", "py_dict_new"),
    ),
)
def test_exact_int_literal_nested_error_releases_rooted_container(
    literal,
    constructor,
    tmp_path,
):
    ir_text = _compile_to_ir(
        tmp_path,
        f"""
from decimal import Context

def exact_literal_cleanup() -> object:
    return Context({literal})
""",
    )
    body = _function_body(ir_text, "exact_literal_cleanup")
    container_match = re.search(
        rf"(?P<value>%[A-Za-z0-9_.]+) = call ptr @{constructor}\(",
        body,
    )
    assert container_match is not None, body
    container = container_match.group("value")
    release_blocks = _blocks_containing(
        body,
        f"call void @pcc_gc_release(ptr {container})",
    )
    assert any(
        block.startswith("cpy.operand.pcc.cleanup")
        for block in release_blocks
    ), body
    assert any(
        block.startswith("call.err.cleanup")
        for block in release_blocks
    ), body


@pytest.mark.parametrize(
    "literal",
    (
        "[2 ** 100, Decimal('bad')]",
        "(2 ** 100, Decimal('bad'))",
        "{1: 2 ** 100, 2: Decimal('bad')}",
    ),
)
def test_literal_later_cpython_failure_releases_prior_exact_int(
    literal,
    tmp_path,
):
    ir_text = _compile_to_ir(
        tmp_path,
        f"""
from decimal import Context, Decimal

def exact_temp_cleanup() -> object:
    return Context({literal})
""",
    )
    body = _function_body(ir_text, "exact_temp_cleanup")
    exact_match = re.search(
        r"(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_int_pow\(",
        body,
    )
    assert exact_match is not None, body
    exact_value = exact_match.group("value")
    cleanup_blocks = _blocks_containing(
        body,
        f"call void @pcc_gc_unpin(ptr {exact_value})",
    )
    assert any(
        block.startswith(("cpy.operand.", "cpy.arg.err.cleanup"))
        and
        f"call void @pcc_gc_release(ptr {exact_value})" in block
        for block in cleanup_blocks
    ), body


@pytest.mark.parametrize(
    ("literal", "store_pattern"),
    (
        (
            "[value, 3 ** 100]",
            r"call void @py_list_append\(ptr [^,]+, ptr "
            r"(?P<source>%[A-Za-z0-9_.]+)\)",
        ),
        (
            "(value, 3 ** 100)",
            r"call void @py_tuple_set_item\(ptr [^,]+, i64 0, ptr "
            r"(?P<source>%[A-Za-z0-9_.]+)\)",
        ),
        (
            "{value: 3 ** 100}",
            r"call void @py_dict_set\(ptr [^,]+, ptr "
            r"(?P<source>%[A-Za-z0-9_.]+),",
        ),
    ),
)
def test_exact_literal_does_not_release_borrowed_name(
    literal,
    store_pattern,
    tmp_path,
):
    ir_text = _compile_to_ir(
        tmp_path,
        f"""
from decimal import Context

def borrowed_exact_literal() -> object:
    value = 2 ** 100
    return Context({literal}, value)
""",
    )
    body = _function_body(ir_text, "borrowed_exact_literal")
    store_match = re.search(store_pattern, body)
    assert store_match is not None, body
    borrowed = store_match.group("source")
    assert f"call void @pcc_gc_pin(ptr {borrowed})" in body, body
    assert f"call void @pcc_gc_unpin(ptr {borrowed})" in body, body
    store_block = _block_containing(body, store_match.group(0))
    assert f"call void @pcc_gc_release(ptr {borrowed})" not in store_block
    fresh_values = re.findall(
        r"(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_int_pow\(",
        body,
    )
    assert len(fresh_values) >= 2, body
    assert any(
        f"call void @pcc_gc_release(ptr {value})" in body
        for value in fresh_values[1:]
    ), body


def test_exact_literal_releases_fresh_nested_integer_operands(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context

def nested_exact_literal() -> object:
    return Context([(2 ** 100) + (3 ** 100)])
""",
    )
    body = _function_body(ir_text, "nested_exact_literal")
    pow_results = re.findall(
        r"(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_int_pow\(",
        body,
    )
    assert len(pow_results) == 2, body
    add_match = re.search(
        r"call ptr @py_int_add\(ptr (?P<lhs>%[A-Za-z0-9_.]+), "
        r"ptr (?P<rhs>%[A-Za-z0-9_.]+)\)",
        body,
    )
    assert add_match is not None, body
    assert [add_match.group("lhs"), add_match.group("rhs")] == pow_results, body
    add_pos = add_match.start()
    for value in pow_results:
        release = body.find(f"call void @pcc_gc_release(ptr {value})", add_pos)
        assert release > add_pos, body


def test_exact_nested_integer_keeps_borrowed_name_and_releases_fresh_rhs(
    tmp_path,
):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context

def borrowed_exact_nested() -> object:
    value = 2 ** 100
    return Context([value + (3 ** 100)], value)
""",
    )
    body = _function_body(ir_text, "borrowed_exact_nested")
    add_match = re.search(
        r"call ptr @py_int_add\(ptr (?P<lhs>%[A-Za-z0-9_.]+), "
        r"ptr (?P<rhs>%[A-Za-z0-9_.]+)\)",
        body,
    )
    assert add_match is not None, body
    borrowed = add_match.group("lhs")
    fresh = add_match.group("rhs")
    add_block = _block_containing(body, add_match.group(0))
    assert f"call void @pcc_gc_release(ptr {borrowed})" not in add_block
    assert f"call void @pcc_gc_release(ptr {fresh})" in body, body


def test_exact_nested_rhs_error_releases_live_lhs(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context

def exact_rhs_error() -> object:
    return Context([(2 ** 100) + (1 // 0)])
""",
    )
    body = _function_body(ir_text, "exact_rhs_error")
    lhs_match = re.search(
        r"(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_int_pow\(",
        body,
    )
    assert lhs_match is not None, body
    lhs = lhs_match.group("value")
    div_error = re.search(
        r"(?ms)^div\.zero\.[^:]+:.*?br label %(?P<cleanup>[A-Za-z0-9_.]+)",
        body,
    )
    assert div_error is not None, body
    cleanup = _cleanup_chain(body, div_error.group("cleanup"))
    assert f"call void @pcc_gc_unpin(ptr {lhs})" in cleanup, cleanup
    assert f"call void @pcc_gc_release(ptr {lhs})" in cleanup, cleanup
    # Exactly once on the error path: a double release would be a use-after-free.
    assert cleanup.count(f"call void @pcc_gc_release(ptr {lhs})") == 1, cleanup


def test_exact_unary_releases_fresh_operand(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context

def exact_unary_literal() -> object:
    return Context([-(2 ** 100)])
""",
    )
    body = _function_body(ir_text, "exact_unary_literal")
    neg_match = re.search(
        r"call ptr @py_int_neg\(ptr (?P<operand>%[A-Za-z0-9_.]+)\)",
        body,
    )
    assert neg_match is not None, body
    operand = neg_match.group("operand")
    neg_pos = neg_match.start()
    release = body.find(f"call void @pcc_gc_release(ptr {operand})", neg_pos)
    assert release > neg_pos, body


@pytest.mark.parametrize(
    ("expression", "call_pattern"),
    (
        (
            "-(2 ** 100)",
            r"call ptr @py_int_neg\(ptr (?P<operand>%[A-Za-z0-9_.]+)\)",
        ),
        (
            "~(2 ** 100)",
            r"call ptr @py_int_xor\(ptr (?P<operand>%[A-Za-z0-9_.]+),",
        ),
    ),
)
def test_direct_boxed_unary_releases_fresh_exact_operand(
    expression,
    call_pattern,
    tmp_path,
):
    ir_text = _compile_to_ir(
        tmp_path,
        f"""
from decimal import Context

def direct_exact_unary() -> object:
    return Context({expression})
""",
    )
    body = _function_body(ir_text, "direct_exact_unary")
    unary_call = re.search(call_pattern, body)
    assert unary_call is not None, body
    operand = unary_call.group("operand")
    call_pos = unary_call.start()
    release = body.find(f"call void @pcc_gc_release(ptr {operand})", call_pos)
    assert release > call_pos, body


def test_boxed_not_preserves_exact_truthiness_and_source_ownership(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context

def fresh_exact_not() -> object:
    return Context(not (2 ** 100))

def borrowed_exact_not() -> object:
    value = 2 ** 100
    return Context(not value, value)
""",
    )
    fresh_body = _function_body(ir_text, "fresh_exact_not")
    truthy = re.search(
        r"call i64 @py_obj_truthy\(ptr (?P<operand>%[A-Za-z0-9_.]+)\)",
        fresh_body,
    )
    assert truthy is not None, fresh_body
    operand = truthy.group("operand")
    assert "call i64 @py_int_to_i64" not in fresh_body, fresh_body
    assert f"call void @pcc_gc_release(ptr {operand})" in fresh_body, fresh_body

    borrowed_body = _function_body(ir_text, "borrowed_exact_not")
    borrowed_truthy = re.search(
        r"call i64 @py_obj_truthy\(ptr (?P<operand>%[A-Za-z0-9_.]+)\)",
        borrowed_body,
    )
    assert borrowed_truthy is not None, borrowed_body
    borrowed = borrowed_truthy.group("operand")
    assert f"call void @pcc_gc_pin(ptr {borrowed})" in borrowed_body
    assert f"call void @pcc_gc_unpin(ptr {borrowed})" in borrowed_body
    truthy_block = _block_containing(
        borrowed_body,
        borrowed_truthy.group(0),
    )
    assert f"call void @pcc_gc_release(ptr {borrowed})" not in truthy_block


@pytest.mark.parametrize(
    ("operator", "dunder"),
    (("-", "__neg__"), ("~", "__invert__")),
)
def test_cpython_unary_uses_protocol_and_consumes_fresh_receiver(
    operator,
    dunder,
    tmp_path,
):
    ir_text = _compile_to_ir(
        tmp_path,
        f"""
import random

def cpython_unary() -> object:
    return {operator}random.randint(1, 10)
""",
    )
    body = _function_body(ir_text, "cpython_unary")
    operand_match = re.search(
        r"(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call2\(",
        body,
    )
    assert operand_match is not None, body
    operand = operand_match.group("value")
    getattr_match = re.search(
        rf"call ptr @py_cpy_getattr\(ptr {re.escape(operand)}, ptr ",
        body,
    )
    assert getattr_match is not None, body
    assert dunder in ir_text, ir_text
    assert "call ptr @py_cpy_call_noargs" in body, body
    assert body.count(f"call void @py_cpy_decref(ptr {operand})") == 1, body
    assert "call i64 @py_cpy_to_i64" not in body, body


def test_exact_compare_releases_fresh_operands_and_keeps_borrowed_name(
    tmp_path,
):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context

def fresh_exact_compare() -> object:
    return Context((2 ** 100) < (3 ** 100))

def borrowed_exact_compare() -> object:
    value = 2 ** 100
    return Context(value < (3 ** 100), value)
""",
    )
    fresh_body = _function_body(ir_text, "fresh_exact_compare")
    fresh_cmp = re.search(
        r"call i32 @py_int_cmp\(ptr (?P<lhs>%[A-Za-z0-9_.]+), "
        r"ptr (?P<rhs>%[A-Za-z0-9_.]+)\)",
        fresh_body,
    )
    assert fresh_cmp is not None, fresh_body
    fresh_cmp_pos = fresh_cmp.start()
    for name in ("lhs", "rhs"):
        value = fresh_cmp.group(name)
        release = fresh_body.find(
            f"call void @pcc_gc_release(ptr {value})",
            fresh_cmp_pos,
        )
        assert release > fresh_cmp_pos, fresh_body

    borrowed_body = _function_body(ir_text, "borrowed_exact_compare")
    borrowed_cmp = re.search(
        r"call i32 @py_int_cmp\(ptr (?P<lhs>%[A-Za-z0-9_.]+), "
        r"ptr (?P<rhs>%[A-Za-z0-9_.]+)\)",
        borrowed_body,
    )
    assert borrowed_cmp is not None, borrowed_body
    borrowed = borrowed_cmp.group("lhs")
    fresh = borrowed_cmp.group("rhs")
    compare_block = _block_containing(borrowed_body, borrowed_cmp.group(0))
    assert f"call void @pcc_gc_release(ptr {borrowed})" not in compare_block
    assert f"call void @pcc_gc_release(ptr {fresh})" in borrowed_body


def test_boxed_compare_releases_dynamic_fresh_lhs_and_keeps_borrowed_rhs(
    tmp_path,
):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context

def dynamic_boxed_compare(a: int, b: int) -> object:
    return Context((a + b) < b)
""",
    )
    body = _function_body(ir_text, "dynamic_boxed_compare")
    cmp_match = re.search(
        r"call i32 @py_int_cmp\(ptr (?P<lhs>%[A-Za-z0-9_.]+), "
        r"ptr (?P<rhs>%[A-Za-z0-9_.]+)\)",
        body,
    )
    assert cmp_match is not None, body
    lhs = cmp_match.group("lhs")
    rhs = cmp_match.group("rhs")
    cmp_pos = cmp_match.start()
    assert body.find(f"call void @pcc_gc_release(ptr {lhs})", cmp_pos) > cmp_pos
    assert f"call void @pcc_gc_release(ptr {rhs})" not in body


@pytest.mark.parametrize(
    ("expression", "error_block_prefix"),
    (
        ("(2 ** 100) << -1", "shift.err."),
        ("(2 ** 100) // 0", "div.zero."),
    ),
)
def test_boxed_binop_error_releases_pinned_fresh_lhs(
    expression,
    error_block_prefix,
    tmp_path,
):
    ir_text = _compile_to_ir(
        tmp_path,
        f"""
from decimal import Context

def boxed_binop_error() -> object:
    return Context({expression})
""",
    )
    body = _function_body(ir_text, "boxed_binop_error")
    lhs_match = re.search(
        r"(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_int_pow\(",
        body,
    )
    assert lhs_match is not None, body
    lhs = lhs_match.group("value")
    error_block = next(
        block
        for block in re.findall(
            r"(?ms)^[A-Za-z0-9_.]+:.*?(?=^[A-Za-z0-9_.]+:|\Z)",
            body,
        )
        if block.startswith(error_block_prefix)
    )
    assert f"call void @pcc_gc_unpin(ptr {lhs})" in error_block, error_block
    assert f"call void @pcc_gc_release(ptr {lhs})" in error_block, error_block


def test_exact_abi_argument_is_evaluated_once_and_released_after_call(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
def side() -> int:
    return 2 ** 100

def take(value: int) -> int:
    return value

def exact_abi_arg() -> int:
    return take(side())
""",
    )
    body = _function_body(ir_text, "exact_abi_arg")
    side_calls = re.findall(
        r"(?P<value>%[A-Za-z0-9_.]+) = call ptr @user_[A-Za-z0-9_]*side\(",
        body,
    )
    assert len(side_calls) == 1, body
    arg_value = side_calls[0]
    call_match = re.search(
        rf"call ptr @user_[A-Za-z0-9_]*take\(ptr {re.escape(arg_value)}\)",
        body,
    )
    assert call_match is not None, body
    assert f"call void @pcc_gc_pin(ptr {arg_value})" in body, body
    release = body.find(
        f"call void @pcc_gc_release(ptr {arg_value})",
        call_match.end(),
    )
    assert release > call_match.end(), body


def test_exact_abi_later_argument_error_cleans_prior_owned_argument(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
def take_two(first: int, second: int) -> int:
    return first

def fail() -> int:
    return 1 // 0

def exact_abi_arg_error() -> int:
    return take_two(2 ** 100, fail())
""",
    )
    body = _function_body(ir_text, "exact_abi_arg_error")
    first_match = re.search(
        r"(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_int_pow\(",
        body,
    )
    assert first_match is not None, body
    first = first_match.group("value")
    cleanup_blocks = _blocks_containing(
        body,
        f"call void @pcc_gc_unpin(ptr {first})",
    )
    assert any(
        block.startswith("abi.arg.pcc.cleanup")
        and f"call void @pcc_gc_release(ptr {first})" in block
        for block in cleanup_blocks
    ), body


def test_nested_type_of_cpython_value_is_owned_and_receiver_is_consumed(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context, Decimal

def nested_type() -> object:
    return Context(type(Decimal(1)))
""",
    )
    body = _function_body(ir_text, "nested_type")
    receiver_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call1\(',
        body,
    )
    type_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_getattr\(',
        body,
    )
    assert receiver_match is not None, body
    assert type_match is not None, body
    receiver = receiver_match.group("value")
    type_value = type_match.group("value")
    type_at = type_match.start()
    receiver_release_at = body.find(
        f"call void @py_cpy_decref(ptr {receiver})"
    )
    outer_call_at = body.rfind("call ptr @py_cpy_call1")
    type_release_at = body.find(
        f"call void @py_cpy_decref(ptr {type_value})"
    )
    assert type_at < receiver_release_at < outer_call_at, body
    assert outer_call_at < type_release_at, body


def test_nested_cpython_attr_consumes_owned_receiver_and_result(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context, Decimal

def nested_attr() -> object:
    return Context(Decimal(1).real)
""",
    )
    body = _function_body(ir_text, "nested_attr")
    receiver_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call1\(',
        body,
    )
    attr_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_getattr\(',
        body,
    )
    assert receiver_match is not None, body
    assert attr_match is not None, body
    receiver = receiver_match.group("value")
    attr_value = attr_match.group("value")
    attr_at = attr_match.start()
    receiver_release_at = body.find(
        f"call void @py_cpy_decref(ptr {receiver})"
    )
    outer_call_at = body.rfind("call ptr @py_cpy_call1")
    attr_release_at = body.find(
        f"call void @py_cpy_decref(ptr {attr_value})"
    )
    assert attr_at < receiver_release_at < outer_call_at, body
    assert outer_call_at < attr_release_at, body


def test_kwdict_plus_evaluates_explicit_keyword_before_mapping_and_short_circuits(
    tmp_path,
):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context, Decimal

def kwdict_plus_order(options: dict[str, int]) -> object:
    receiver = Decimal(0)
    return receiver.quantize(1, rounding=Decimal(2), **options)
""",
    )
    body = _function_body(ir_text, "kwdict_plus_order")
    explicit_matches = list(
        re.finditer(
            r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call1\(',
            body,
        )
    )
    mapping_match = re.search(
        r"%cpy\.from_pcc_dict[A-Za-z0-9_.]* = call ptr "
        r"@py_cpy_from_pcc_obj\(",
        body,
    )
    assert len(explicit_matches) == 2, body
    assert mapping_match is not None, body
    explicit_match = explicit_matches[-1]
    explicit_value = explicit_match.group("value")
    explicit_call = explicit_match.start()
    mapping_call = mapping_match.start()
    outer_call = body.find("call ptr @py_cpy_call_kwdict_plus")
    assert explicit_call < mapping_call < outer_call, body

    between_explicit_and_mapping = body[explicit_match.end() : mapping_call]
    assert re.search(
        rf"icmp eq ptr {re.escape(explicit_value)}, null",
        between_explicit_and_mapping,
    ), body
    assert "br i1" in between_explicit_and_mapping, body


def test_kwdict_plus_evaluates_mapping_before_later_explicit_keyword(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Decimal

def kwdict_plus_reverse_order(options: dict[str, int]) -> object:
    receiver = Decimal(0)
    return receiver.quantize(1, **options, rounding=Decimal(2))
""",
    )
    body = _function_body(ir_text, "kwdict_plus_reverse_order")
    explicit_matches = list(
        re.finditer(
            r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call1\(',
            body,
        )
    )
    mapping_match = re.search(
        r"%cpy\.from_pcc_dict[A-Za-z0-9_.]* = call ptr "
        r"@py_cpy_from_pcc_obj\(",
        body,
    )
    assert len(explicit_matches) == 2, body
    assert mapping_match is not None, body
    explicit_match = explicit_matches[-1]
    outer_call = body.find("call ptr @py_cpy_call_kwdict_plus")
    assert mapping_match.start() < explicit_match.start() < outer_call, body
    mapping_value_match = re.search(
        r"(?P<value>%cpy\.from_pcc_dict[A-Za-z0-9_.]*) = call ptr "
        r"@py_cpy_from_pcc_obj\(",
        body,
    )
    assert mapping_value_match is not None, body
    between_mapping_and_explicit = body[
        mapping_value_match.end() : explicit_match.start()
    ]
    assert re.search(
        rf"icmp eq ptr {re.escape(mapping_value_match.group('value'))}, null",
        between_mapping_and_explicit,
    ), body


def test_kwdict_plus_fails_closed_when_mappings_interleave_explicit_keywords(
    tmp_path,
):
    with pytest.raises(
        NotImplementedError,
        match="multiple \\*\\*mapping operands",
    ):
        _compile_to_ir(
            tmp_path,
            """
from decimal import Decimal

def interleaved_mappings(
    left: dict[str, int], right: dict[str, int]
) -> object:
    receiver = Decimal(0)
    return receiver.quantize(1, **left, rounding=Decimal(2), **right)
""",
        )


def test_kwdict_plus_fails_closed_for_starred_positional_mix(tmp_path):
    with pytest.raises(
        NotImplementedError,
        match="combined \\*args, explicit keywords, and \\*\\*mapping",
    ):
        _compile_to_ir(
            tmp_path,
            """
from decimal import Decimal

def starred_keyword_mix(
    values: list[int], options: dict[str, int]
) -> object:
    receiver = Decimal(0)
    return receiver.quantize(rounding=Decimal(2), *values, **options)
""",
        )


def test_multiple_starstar_mappings_fail_closed_before_duplicate_merge(tmp_path):
    with pytest.raises(
        NotImplementedError,
        match=r"multiple \*\*mapping operands",
    ):
        _compile_to_ir(
            tmp_path,
            """
from decimal import Context

def duplicate_mapping_keys(
    first: dict[str, int], second: dict[str, int]
) -> object:
    return Context(**first, **second)
""",
        )


def test_custom_starstar_mapping_fails_closed_before_delayed_expansion(tmp_path):
    with pytest.raises(
        NotImplementedError,
        match="arbitrary mapping expansion is not yet source-ordered",
    ):
        _compile_to_ir(
            tmp_path,
            """
from decimal import Context

def custom_mapping_order(options: object) -> object:
    return Context(**options, prec=1)
""",
        )


@pytest.mark.parametrize(
    "call_source",
    (
        "receiver.quantize(*values, rounding=Decimal(2))",
        "receiver.quantize(rounding=Decimal(2), *values)",
    ),
)
def test_explicit_keywords_with_starred_args_fail_closed_without_reordering(
    tmp_path,
    call_source,
):
    with pytest.raises(
        NotImplementedError,
        match="combined \\*args and explicit keywords",
    ):
        _compile_to_ir(
            tmp_path,
            f"""
from decimal import Decimal

def starred_keyword_order(values: list[int]) -> object:
    receiver = Decimal(0)
    return {call_source}
""",
        )


def test_imported_function_explicit_keyword_and_mapping_use_kwdict_plus(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context, Decimal

def function_kwdict_plus(options: dict[str, int]) -> object:
    return Context(1, capitals=Decimal(2), **options)
""",
    )
    body = _function_body(ir_text, "function_kwdict_plus")
    explicit_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call1\(',
        body,
    )
    mapping_match = re.search(
        r"%cpy\.from_pcc_dict[A-Za-z0-9_.]* = call ptr "
        r"@py_cpy_from_pcc_obj\(",
        body,
    )
    assert explicit_match is not None, body
    assert mapping_match is not None, body
    outer_call = body.find("call ptr @py_cpy_call_kwdict_plus")
    assert explicit_match.start() < mapping_match.start() < outer_call, body


def test_plain_positional_operands_short_circuit_and_clean_prior_fresh_ref(
    tmp_path,
):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context, Decimal

def positional_short_circuit() -> object:
    return Context(Decimal(1), Decimal(2))
""",
    )
    body = _function_body(ir_text, "positional_short_circuit")
    inner_calls = list(
        re.finditer(
            r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call1\(',
            body,
        )
    )
    assert len(inner_calls) == 2, body
    first_value = inner_calls[0].group("value")
    between = body[inner_calls[0].end() : inner_calls[1].start()]
    assert re.search(
        rf"icmp eq ptr {re.escape(first_value)}, null",
        between,
    ), body
    assert "br i1" in between, body
    cleanup_blocks = _blocks_containing(
        body,
        f"call void @py_cpy_decref(ptr {first_value})",
    )
    assert any(block.startswith("cpy.arg.err.cleanup") for block in cleanup_blocks)


def test_method_positional_operands_cleanup_fresh_callable_and_prior_arg(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Decimal

def method_positional_short_circuit() -> object:
    receiver = Decimal(0)
    return receiver.quantize(Decimal(1), Decimal(2))
""",
    )
    body = _function_body(ir_text, "method_positional_short_circuit")
    inner_calls = list(
        re.finditer(
            r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call1\(',
            body,
        )
    )
    assert len(inner_calls) == 3, body
    first_arg = inner_calls[1].group("value")
    method_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_getattr\(',
        body,
    )
    assert method_match is not None, body
    method_callable = method_match.group("value")
    cleanup_blocks = _blocks_containing(
        body,
        f"call void @py_cpy_decref(ptr {first_arg})",
    )
    assert any(
        block.startswith("cpy.arg.err.cleanup")
        and f"call void @py_cpy_decref(ptr {method_callable})" in block
        for block in cleanup_blocks
    ), body


def test_arglist_operand_is_checked_and_bridged_before_starstar_mapping(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context, Decimal

def arglist_then_mapping(
    values: list[int], options: dict[str, int]
) -> object:
    return Context(Decimal(1), *values, **options)
""",
    )
    body = _function_body(ir_text, "arglist_then_mapping")
    list_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_list_new\(',
        body,
    )
    inner_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call1\(',
        body,
    )
    bridge_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_to_pcc_obj\(',
        body,
    )
    mapping_match = re.search(
        r'(?P<value>%cpy\.from_pcc_dict[A-Za-z0-9_.]*) = call ptr '
        r'@py_cpy_from_pcc_obj\(',
        body,
    )
    assert list_match is not None, body
    assert inner_match is not None, body
    assert bridge_match is not None, body
    assert mapping_match is not None, body
    assert list_match.start() < inner_match.start(), body
    before_inner = body[list_match.end() : inner_match.start()]
    assert "call i64 @py_err_occurred()" in before_inner, body
    assert inner_match.start() < bridge_match.start() < mapping_match.start(), body
    list_value = list_match.group("value")
    bridge_value = bridge_match.group("value")
    list_pin = body.find(f"call void @pcc_gc_pin(ptr {list_value})")
    assert list_match.end() < list_pin < inner_match.start(), body
    before_bridge = body[inner_match.end() : bridge_match.start()]
    assert re.search(
        rf"icmp eq ptr {re.escape(inner_match.group('value'))}, null",
        before_bridge,
    ), body
    before_mapping = body[bridge_match.end() : mapping_match.start()]
    assert re.search(
        rf"icmp eq ptr {re.escape(bridge_match.group('value'))}, null",
        before_mapping,
    ), body
    inner_release = (
        f"call void @py_cpy_decref(ptr {inner_match.group('value')})"
    )
    inner_release_at = body.find(inner_release)
    assert inner_match.end() < inner_release_at < mapping_match.start(), body
    append_block = _block_containing(
        body,
        f"call void @py_list_append(ptr {list_value}, ptr {bridge_value})",
    )
    append_at = append_block.find("call void @py_list_append")
    bridge_release_at = append_block.find(
        f"call void @pcc_gc_release(ptr {bridge_value})"
    )
    assert 0 <= append_at < bridge_release_at, body
    cleanup_blocks = _blocks_containing(body, inner_release)
    assert any(block.startswith("cpy.arg.err.cleanup") for block in cleanup_blocks)
    outer_block = _block_containing(body, "call ptr @py_cpy_call_list_kwdict")
    outer_call = outer_block.find("call ptr @py_cpy_call_list_kwdict")
    list_release = outer_block.find(
        f"call void @pcc_gc_release(ptr {list_value})"
    )
    assert 0 <= outer_call < list_release, body


def test_starred_cpython_iterable_is_bridged_and_released_before_mapping(
    tmp_path,
):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context, Decimal

def cpy_starred_then_mapping(options: dict[str, int]) -> object:
    receiver = Decimal(1)
    return Context(*receiver.as_tuple(), **options)
""",
    )
    body = _function_body(ir_text, "cpy_starred_then_mapping")
    iter_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call_noargs\(',
        body,
    )
    bridge_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_to_pcc_obj\(',
        body,
    )
    mapping_match = re.search(
        r"%cpy\.from_pcc_dict[A-Za-z0-9_.]* = call ptr "
        r"@py_cpy_from_pcc_obj\(",
        body,
    )
    assert iter_match is not None, body
    assert bridge_match is not None, body
    assert mapping_match is not None, body
    iter_value = iter_match.group("value")
    bridge_value = bridge_match.group("value")
    assert iter_match.start() < bridge_match.start() < mapping_match.start(), body
    helper_block = _block_containing(
        body,
        "call ptr @py_cpy_call_list_kwdict",
    )
    helper_at = helper_block.find("call ptr @py_cpy_call_list_kwdict")
    assert f"ptr {bridge_value}," in helper_block, body
    assert f"ptr {iter_value}," not in helper_block, body
    bridge_release_at = helper_block.find(
        f"call void @pcc_gc_release(ptr {bridge_value})"
    )
    assert 0 <= helper_at < bridge_release_at, body
    iter_release_at = body.find(f"call void @py_cpy_decref(ptr {iter_value})")
    assert iter_match.end() < iter_release_at < mapping_match.start(), body


def test_pcc_operand_error_unwinds_prior_cpython_owned_argument(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context, Decimal

def pcc_operand_error_cleanup() -> object:
    return Context(Decimal(1), [1][2])
""",
    )
    body = _function_body(ir_text, "pcc_operand_error_cleanup")
    prior_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call1\(',
        body,
    )
    assert prior_match is not None, body
    prior_value = prior_match.group("value")
    assert "call ptr @py_list_getitem" in body, body
    cleanup_blocks = _blocks_containing(
        body,
        f"call void @py_cpy_decref(ptr {prior_value})",
    )
    assert any(
        block.startswith("cpy.operand.pcc.cleanup")
        for block in cleanup_blocks
    ), body


def test_arglist_pcc_and_mapping_errors_unwind_fresh_callable_and_list(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from operator import methodcaller

def fresh_callable_arglist(
    values: list[int], options: dict[str, int]
) -> object:
    return methodcaller("missing")(0, *values, **options)
""",
    )
    body = _function_body(ir_text, "fresh_callable_arglist")
    callable_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call1\(',
        body,
    )
    list_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_list_new\(',
        body,
    )
    assert callable_match is not None, body
    assert list_match is not None, body
    callable_value = callable_match.group("value")
    list_value = list_match.group("value")
    cleanup_blocks = _blocks_containing(
        body,
        f"call void @pcc_gc_release(ptr {list_value})",
    )
    assert any(
        block.startswith("call.err.cleanup")
        and f"call void @py_cpy_decref(ptr {callable_value})" in block
        for block in cleanup_blocks
    ), body
    assert any(
        block.startswith("cpy.arg.err.cleanup")
        and f"call void @py_cpy_decref(ptr {callable_value})" in block
        for block in cleanup_blocks
    ), body


def test_arglist_operand_internal_pcc_error_unwinds_callable_and_list(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from operator import methodcaller

def failing_starred_operand() -> object:
    return methodcaller("missing")(0, *[1][2])
""",
    )
    body = _function_body(ir_text, "failing_starred_operand")
    callable_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call1\(',
        body,
    )
    args_list_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_list_new\(i64 0\)',
        body,
    )
    assert callable_match is not None, body
    assert args_list_match is not None, body
    assert "call ptr @py_list_getitem" in body, body
    callable_value = callable_match.group("value")
    args_list = args_list_match.group("value")
    cleanup_blocks = _blocks_containing(
        body,
        f"call void @pcc_gc_release(ptr {args_list})",
    )
    assert any(
        block.startswith("cpy.operand.pcc.cleanup")
        and f"call void @py_cpy_decref(ptr {callable_value})" in block
        for block in cleanup_blocks
    ), body


def test_cpython_lambda_keyword_uses_function_local_error_blocks(tmp_path):
    from llvmlite import binding as llvm

    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context, Decimal

def lambda_context_state() -> object:
    return Context(1, key=lambda value: Decimal(value))
""",
    )
    llvm.parse_assembly(ir_text).verify()


def test_threaded_arglist_clears_and_unpins_temp_root_before_release(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
import threading
from operator import methodcaller

def threaded_arglist(values: list[int]) -> object:
    return methodcaller("missing")(0, *values)
""",
    )
    body = _function_body(ir_text, "threaded_arglist")
    args_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_list_new\(i64 0\)',
        body,
    )
    assert args_match is not None, body
    args_list = args_match.group("value")
    root_match = re.search(
        rf"call void @pcc_gc_store_root\(ptr "
        rf"(?P<slot>%[A-Za-z0-9_.]+), ptr {re.escape(args_list)}\)",
        body,
    )
    assert root_match is not None, body
    root_slot = root_match.group("slot")
    outer_block = _block_containing(body, "call ptr @py_cpy_call_list")
    call_at = outer_block.find("call ptr @py_cpy_call_list")
    clear_match = re.search(
        r"call void @pcc_gc_store_root\(ptr %[A-Za-z0-9_.]+, ptr null\)",
        outer_block,
    )
    assert clear_match is not None, body
    clear_at = clear_match.start()
    unpin_at = outer_block.find("call void @pcc_gc_unpin")
    release_at = outer_block.find(
        f"call void @pcc_gc_release(ptr {args_list})"
    )
    assert 0 <= call_at < clear_at < unpin_at < release_at, body


def test_arglist_allocation_error_uses_pcc_try_cleanup(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from operator import methodcaller

def arglist_allocation_try(values: list[int]) -> object:
    try:
        return methodcaller("missing")(0, *values)
    except Exception:
        return None
""",
    )
    body = _function_body(ir_text, "arglist_allocation_try")
    callable_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call1\(',
        body,
    )
    args_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_list_new\(i64 0\)',
        body,
    )
    assert callable_match is not None, body
    assert args_match is not None, body
    callable_value = callable_match.group("value")
    args_list = args_match.group("value")
    pin_at = body.find(f"call void @pcc_gc_pin(ptr {args_list})")
    assert pin_at >= 0, body
    after_alloc = body[args_match.end() : pin_at]
    assert "call i64 @py_err_occurred()" in after_alloc, body
    assert not re.search(
        rf"icmp eq ptr {re.escape(args_list)}, null",
        after_alloc,
    ), body
    cleanup_blocks = _blocks_containing(
        body,
        f"call void @py_cpy_decref(ptr {callable_value})",
    )
    assert any(
        block.startswith("call.err.cleanup")
        and "label %err.exit" not in block
        for block in cleanup_blocks
    ), body


def test_indirect_cpython_return_is_owned_by_caller(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Decimal

def make_decimal() -> Decimal:
    return Decimal(1)

def indirect_decimal() -> object:
    factory = make_decimal
    return factory().as_tuple()
""",
    )
    body = _function_body(ir_text, "indirect_decimal")
    receiver_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_obj_call\(',
        body,
    )
    assert receiver_match is not None, body
    receiver = receiver_match.group("value")
    getattr_at = body.find(f"call ptr @py_cpy_getattr(ptr {receiver},")
    release_at = body.find(f"call void @py_cpy_decref(ptr {receiver})")
    assert 0 <= getattr_at < release_at, body
    assert body.count(f"call void @py_cpy_decref(ptr {receiver})") == 1, body


def test_cpython_lambda_identity_promotes_borrowed_parameter_for_return(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context

def lambda_identity() -> object:
    return Context(1, key=lambda value: value)
""",
    )
    adapter_match = re.search(
        r"define internal ptr @(?P<name>[A-Za-z0-9_]*__lambda_[A-Za-z0-9_]*)\(",
        ir_text,
    )
    assert adapter_match is not None, ir_text
    body = _function_body(ir_text, adapter_match.group("name"))
    ret_match = re.search(r"ret ptr (?P<value>%[A-Za-z0-9_.]+)", body)
    assert ret_match is not None, body
    value = ret_match.group("value")
    assert f"call void @py_cpy_incref(ptr {value})" in body, body


def test_cpython_lambda_consumes_fresh_native_body_result(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context

def lambda_native_body() -> object:
    return Context(1, key=lambda value: [1])
""",
    )
    adapter_match = re.search(
        r"define internal ptr @(?P<name>[A-Za-z0-9_]*__lambda_[A-Za-z0-9_]*)\(",
        ir_text,
    )
    assert adapter_match is not None, ir_text
    body = _function_body(ir_text, adapter_match.group("name"))
    bridge = re.search(
        r"call ptr @py_cpy_from_pcc_obj\(ptr "
        r"(?P<source>%[A-Za-z0-9_.]+)\)",
        body,
    )
    assert bridge is not None, body
    source = bridge.group("source")
    assert f"call void @pcc_gc_pin(ptr {source})" in body, body
    assert f"call void @pcc_gc_release(ptr {source})" in body, body


def test_native_lambda_capture_does_not_release_borrowed_cpython_local(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Decimal

def borrowed_lambda_capture() -> object:
    existing = Decimal(1)
    return lambda value: existing
""",
    )
    body = _function_body(ir_text, "borrowed_lambda_capture")
    bridge_match = re.search(
        r'(?P<bridge>%[A-Za-z0-9_.]+) = call ptr @py_cpy_to_pcc_obj\('
        r'ptr (?P<value>%[A-Za-z0-9_.]+)\)',
        body,
    )
    assert bridge_match is not None, body
    bridge = bridge_match.group("bridge")
    value = bridge_match.group("value")
    assert f"call void @py_cpy_decref(ptr {value})" not in body, body
    assert body.count(f"call void @pcc_gc_release(ptr {bridge})") == 1, body


def test_native_lambda_fresh_default_is_released_once_after_capture(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Decimal

def fresh_lambda_default() -> object:
    return lambda value=Decimal(2): value
""",
    )
    body = _function_body(ir_text, "fresh_lambda_default")
    bridge_match = re.search(
        r'(?P<bridge>%[A-Za-z0-9_.]+) = call ptr @py_cpy_to_pcc_obj\('
        r'ptr (?P<value>%[A-Za-z0-9_.]+)\)',
        body,
    )
    assert bridge_match is not None, body
    bridge = bridge_match.group("bridge")
    value = bridge_match.group("value")
    store_block = _block_containing(
        body,
        f"call void @py_tuple_set_item(ptr ",
    )
    assert f"ptr {bridge})" in store_block, body
    assert store_block.count(f"call void @py_cpy_decref(ptr {value})") == 1, body
    cleanup_blocks = _blocks_containing(
        body,
        f"call void @py_cpy_decref(ptr {value})",
    )
    assert any(block.startswith("cpy.arg.err.cleanup") for block in cleanup_blocks)
    assert body.count(f"call void @pcc_gc_release(ptr {bridge})") == 1, body


def test_cpython_unary_not_guards_truthy_status_and_releases_operand(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context, Decimal

def unary_not_cleanup() -> object:
    return Context(not Decimal("bad"))
""",
    )
    body = _function_body(ir_text, "unary_not_cleanup")
    operand_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call1\('
        r'ptr %cpy.fn.Decimal',
        body,
    )
    assert operand_match is not None, body
    operand = operand_match.group("value")
    truthy_match = re.search(
        rf'(?P<status>%[A-Za-z0-9_.]+) = call i32 @py_cpy_truthy\('
        rf'ptr {re.escape(operand)}\)',
        body,
    )
    assert truthy_match is not None, body
    status = truthy_match.group("status")
    before_truthy = body[operand_match.end() : truthy_match.start()]
    assert re.search(rf"icmp eq ptr {re.escape(operand)}, null", before_truthy), body
    after_truthy = body[truthy_match.end() :]
    assert re.search(rf"icmp slt i32 {re.escape(status)}, 0", after_truthy), body
    normal_block = _block_containing(body, " = xor i1 ")
    assert f"call void @py_cpy_decref(ptr {operand})" in normal_block, body


@pytest.mark.parametrize("operator", ("<", "=="))
def test_cpython_rhs_compare_preserves_lhs_then_rhs_source_order(
    tmp_path,
    operator,
):
    ir_text = _compile_to_ir(
        tmp_path,
        f"""
from decimal import Context, Decimal

def left(values: list[int]) -> int:
    values.append(1)
    return 1

def rhs_compare_order(values: list[int]) -> object:
    return Context(left(values) {operator} Decimal(2))
""",
    )
    body = _function_body(ir_text, "rhs_compare_order")
    lhs_match = re.search(r"call (?:i64|ptr) @[^\n(]*left\(", body)
    assert lhs_match is not None, body
    lhs_at = lhs_match.start()
    rhs_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call1\('
        r'ptr %cpy.fn.Decimal',
        body,
    )
    assert rhs_match is not None, body
    assert lhs_at < rhs_match.start(), body
    lhs_box_match = re.search(
        r"(?P<value>%[A-Za-z0-9_.]+) = call ptr "
        r"@py_cpy_from_(?:i64|pcc_obj)\(",
        body[lhs_at : rhs_match.start()],
    )
    assert lhs_box_match is not None, body
    lhs_box = lhs_box_match.group("value")
    cleanup_blocks = _blocks_containing(
        body,
        f"call void @py_cpy_decref(ptr {lhs_box})",
    )
    assert any(
        block.startswith(("cpy.operand.", "cpy.arg.err.cleanup"))
        for block in cleanup_blocks
    ), body


def test_cpython_membership_preserves_lhs_then_rhs_source_order(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context, Decimal

def left(values: list[int]) -> int:
    values.append(1)
    return 1

def membership_order(values: list[int]) -> object:
    return Context(left(values) in Decimal(2))
""",
    )
    body = _function_body(ir_text, "membership_order")
    lhs_match = re.search(r"call (?:i64|ptr) @[^\n(]*left\(", body)
    rhs_match = re.search(
        r"call ptr @py_cpy_call1\(ptr %cpy.fn.Decimal",
        body,
    )
    contains_getattr = body.find("@py_cpy_getattr", rhs_match.end() if rhs_match else 0)
    assert lhs_match is not None, body
    assert rhs_match is not None, body
    assert lhs_match.start() < rhs_match.start() < contains_getattr, body
    lhs_bridge = re.search(
        r"(?P<value>%[A-Za-z0-9_.]+) = call ptr "
        r"@py_cpy_from_(?:i64|pcc_obj)\(",
        body[lhs_match.start() : rhs_match.start()],
    )
    assert lhs_bridge is not None, body
    lhs_value = lhs_bridge.group("value")
    cleanup_blocks = _blocks_containing(
        body,
        f"call void @py_cpy_decref(ptr {lhs_value})",
    )
    assert any(
        block.startswith(("cpy.operand.", "cpy.arg.err.cleanup"))
        for block in cleanup_blocks
    ), body


def test_cpython_lhs_compare_evaluates_rhs_before_dunder_lookup(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context, Decimal

def right(values: list[int]) -> int:
    values.append(1)
    return 2

def lhs_compare_order(values: list[int]) -> object:
    return Context(Decimal(1) < right(values))
""",
    )
    body = _function_body(ir_text, "lhs_compare_order")
    lhs_match = re.search(
        r'(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_cpy_call1\('
        r'ptr %cpy.fn.Decimal',
        body,
    )
    rhs_match = re.search(r"call (?:i64|ptr) @[^\n(]*right\(", body)
    getattr_match = re.search(
        r"call ptr @py_cpy_getattr\(ptr [^,]+, ptr [^)]+\)",
        body,
    )
    assert lhs_match is not None, body
    assert rhs_match is not None, body
    assert getattr_match is not None, body
    assert lhs_match.start() < rhs_match.start() < getattr_match.start(), body
    lhs = lhs_match.group("value")
    cleanup_blocks = _blocks_containing(
        body,
        f"call void @py_cpy_decref(ptr {lhs})",
    )
    assert any(block.startswith("cpy.operand.") for block in cleanup_blocks), body


@pytest.mark.parametrize(
    "literal",
    (
        "[*Decimal(2), later()]",
        "(*Decimal(2), later())",
    ),
)
def test_cpython_splat_with_following_element_lowers_in_source_order(
    literal, tmp_path
):
    """The position restriction is gone; source order is what it protected.

    Both consumers replay the recorded ops in the order the operands were
    written, so a splat is expanded where it appears.  The native sibling of
    this test is ``test_native_splat_with_following_element_lowers_at_source
    _position``; runtime parity is pinned in
    tests/python/test_py_literal_splat_source_position.py.
    """
    ir_text = _compile_to_ir(
        tmp_path,
        f"""
from decimal import Context, Decimal

def later() -> int:
    return 3

def splat_order_boundary() -> object:
    return Context({literal})
""",
    )
    body = _function_body(ir_text, "splat_order_boundary")
    assert body is not None, ir_text
    splat = body.index("Decimal")
    following = body.index("later(")
    assert splat < following, body


@pytest.mark.parametrize(
    "literal",
    (
        "[*values, later()]",
        "(*values, later())",
    ),
)
def test_native_splat_with_following_element_lowers_at_source_position(
    literal,
    tmp_path,
):
    ir_text = _compile_to_ir(
        tmp_path,
        f"""
from decimal import Context

def later() -> str:
    return "later"

def splat_order_boundary(values: list[str]) -> object:
    return Context({literal})
""",
    )
    body = _function_body(ir_text, "splat_order_boundary")
    extend_match = re.search(r"call void @py_list_extend\(", body)
    later_match = re.search(r"call ptr @[^\n(]*later\(", body)
    assert extend_match is not None, body
    assert later_match is not None, body
    assert extend_match.start() < later_match.start(), body


def test_mixed_list_literal_error_cleanup_unpins_prior_native_temp(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context, Decimal

def mixed_list_cleanup() -> object:
    return Context([str(0), Decimal("bad"), Decimal(2)])
""",
    )
    body = _function_body(ir_text, "mixed_list_cleanup")
    pin_match = re.search(
        r"call void @pcc_gc_pin\(ptr (?P<value>%[A-Za-z0-9_.]+)\)",
        body,
    )
    assert pin_match is not None, body
    pinned = pin_match.group("value")
    cleanup_blocks = _blocks_containing(
        body,
        f"call void @pcc_gc_unpin(ptr {pinned})",
    )
    assert any(
        block.startswith(("cpy.operand.", "cpy.arg.err.cleanup"))
        for block in cleanup_blocks
    ), body


def test_cpython_tuple_bridge_failure_cleans_remaining_refs_and_container(tmp_path):
    from llvmlite import binding as llvm

    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context, Decimal

def tuple_bridge_cleanup() -> object:
    return Context((Decimal(1), Decimal(2)))
""",
    )
    body = _function_body(ir_text, "tuple_bridge_cleanup")
    tuple_match = re.search(
        r"(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_tuple_new\(",
        body,
    )
    bridge_matches = list(
        re.finditer(
            r"(?P<bridge>%[A-Za-z0-9_.]+) = call ptr @py_cpy_to_pcc_obj\(ptr "
            r"(?P<source>%[A-Za-z0-9_.]+)\)",
            body,
        )
    )
    assert tuple_match is not None, body
    assert len(bridge_matches) >= 2, body
    container = tuple_match.group("value")
    later_source = bridge_matches[1].group("source")
    cleanup_blocks = _blocks_containing(
        body,
        f"call void @pcc_gc_release(ptr {container})",
    )
    assert any(
        f"call void @py_cpy_decref(ptr {later_source})" in block
        for block in cleanup_blocks
    ), body
    llvm.parse_assembly(ir_text).verify()


def test_mixed_list_bridge_failure_cleans_later_ref_pin_and_container(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context, Decimal

def mixed_list_bridge_cleanup() -> object:
    return Context([Decimal(1), "native", Decimal(2)])
""",
    )
    body = _function_body(ir_text, "mixed_list_bridge_cleanup")
    container_match = re.search(
        r"(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_list_new\(",
        body,
    )
    bridges = list(
        re.finditer(
            r"(?P<bridge>%[A-Za-z0-9_.]+) = call ptr @py_cpy_to_pcc_obj\(ptr "
            r"(?P<source>%[A-Za-z0-9_.]+)\)",
            body,
        )
    )
    assert container_match is not None, body
    assert len(bridges) >= 2, body
    container = container_match.group("value")
    later_source = bridges[1].group("source")
    cleanup_blocks = _blocks_containing(
        body,
        f"call void @pcc_gc_release(ptr {container})",
    )
    assert any(
        f"call void @py_cpy_decref(ptr {later_source})" in block
        and "call void @pcc_gc_unpin" in block
        for block in cleanup_blocks
    ), body


def test_cpython_value_dict_bridge_failure_cleans_later_ref_and_container(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context, Decimal

def dict_value_bridge_cleanup() -> object:
    return Context({1: Decimal(1), 2: Decimal(2)})
""",
    )
    body = _function_body(ir_text, "dict_value_bridge_cleanup")
    container_match = re.search(
        r"(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_dict_new\(\)",
        body,
    )
    bridges = list(
        re.finditer(
            r"(?P<bridge>%[A-Za-z0-9_.]+) = call ptr @py_cpy_to_pcc_obj\(ptr "
            r"(?P<source>%[A-Za-z0-9_.]+)\)",
            body,
        )
    )
    assert container_match is not None, body
    assert len(bridges) >= 2, body
    container = container_match.group("value")
    later_source = bridges[1].group("source")
    cleanup_blocks = _blocks_containing(
        body,
        f"call void @pcc_gc_release(ptr {container})",
    )
    assert any(
        f"call void @py_cpy_decref(ptr {later_source})" in block
        for block in cleanup_blocks
    ), body


def test_splat_tuple_releases_each_owned_list_get_result(tmp_path):
    ir_text = _compile_to_ir(
        tmp_path,
        """
from decimal import Context, Decimal

def splat_tuple_copy_cleanup() -> object:
    return Context((Decimal(1), Decimal(2), *[str(0)]))
""",
    )
    body = _function_body(ir_text, "splat_tuple_copy_cleanup")
    elem_match = re.search(
        r"(?P<value>%[A-Za-z0-9_.]+) = call ptr @py_list_get\(",
        body,
    )
    assert elem_match is not None, body
    elem = elem_match.group("value")
    cleanup_blocks = _blocks_containing(
        body,
        f"call void @pcc_gc_release(ptr {elem})",
    )
    assert any(block.startswith("call.err.cleanup") for block in cleanup_blocks), body
    assert any(block.startswith("call.cont") for block in cleanup_blocks), body
