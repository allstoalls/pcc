"""NEW-ref ownership contracts for exact-container subscripts."""

from __future__ import annotations

import re

import pytest

_BLOCK_RE = re.compile(
    r'(?ms)^(?P<label>(?:"[^"]+"|[-.$A-Za-z0-9_]+)):\n'
    r'.*?(?=^(?:"[^"]+"|[-.$A-Za-z0-9_]+):\n|\Z)'
)


def _generate_ir(
    source: str,
    *,
    module_name: str = "pcc.subscript_ownership_probe",
) -> str:
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python import type_infer
    from pcc.frontends.python.codegen.layer1 import L1CodeGen

    module = parse_and_lift(source, "<test>", module_name)
    typed = type_infer.infer_module(module)
    codegen = L1CodeGen(typed, ir_scaffold_mode="on")
    return str(codegen.generate(codegen.ast_module))


def _probe_body(
    ir_text: str,
    *,
    module_name: str = "pcc.subscript_ownership_probe",
) -> str:
    lowered_module_name = re.escape(module_name.replace(".", "_"))
    match = re.search(
        rf"define external [^{{\n]+ @user_{lowered_module_name}_probe"
        r"\([^\n]*\) \{(.*?)\n\}",
        ir_text,
        re.DOTALL,
    )
    assert match is not None, ir_text
    return match.group(1)


def _call_result(body: str, runtime_symbol: str) -> str:
    match = re.search(
        rf"(?P<value>%[-.A-Za-z0-9]+) = call [^\n]+ @{runtime_symbol}\(",
        body,
    )
    assert match is not None, body
    return match.group("value")


def _basic_block_at(body: str, offset: int):
    blocks = [match for match in _BLOCK_RE.finditer(body) if match.start() <= offset]
    containing = [match for match in blocks if offset < match.end()]
    assert len(containing) == 1, body
    return containing[0]


def _root_lifecycle(body: str, incoming: str, *, require_entry: bool = True):
    root_store = re.search(
        rf"@pcc_gc_store_root\(ptr (?P<root>%[-.A-Za-z0-9]+), "
        rf"ptr {re.escape(incoming)}\)",
        body,
    )
    assert root_store is not None, body
    root = root_store.group("root")
    root_definition = list(
        re.finditer(
            rf"{re.escape(root)} = bitcast ptr (?P<slot>%[-.A-Za-z0-9]+) to ptr",
            body[: root_store.start()],
        )
    )
    assert root_definition, body
    slot = root_definition[-1].group("slot")
    slot_alloca = re.search(
        rf"^  {re.escape(slot)} = alloca ptr$",
        body[: root_store.start()],
        re.MULTILINE,
    )
    assert slot_alloca is not None, body
    slot_block_start = body.rfind("\n\n", 0, slot_alloca.start()) + 2
    if require_entry:
        assert body[slot_block_start:].startswith("entry:\n")
    slot_init = re.search(
        rf"^  store ptr null, ptr {re.escape(slot)}$",
        body[slot_alloca.end() : root_store.start()],
        re.MULTILINE,
    )
    assert slot_init is not None, body
    tail = body[root_store.end() :]
    slots_pointer = re.search(
        rf"(?P<pointer>%[-.A-Za-z0-9]+) = bitcast ptr {re.escape(slot)} to ptr",
        tail,
    )
    assert slots_pointer is not None, body
    enter = re.search(
        rf"@pcc_gc_frame_enter_lifo\([^\n]*"
        rf"{re.escape(slots_pointer.group('pointer'))}\)",
        tail[slots_pointer.end() :],
    )
    assert enter is not None, body
    loads = list(
        re.finditer(
            rf"(?P<value>%[-.A-Za-z0-9]+) = call ptr (?:\([^)]*\) )?"
            rf"@pcc_gc_load_ptr\(ptr null, ptr {re.escape(root)}\)",
            tail,
        )
    )
    assert loads, body
    clear = re.search(
        rf"@pcc_gc_store_root\(ptr {re.escape(root)}, ptr null\)",
        tail,
    )
    assert clear is not None, body
    assert all(load.start() < clear.start() for load in loads)
    leave_pointer = re.search(
        rf"(?P<pointer>%[-.A-Za-z0-9]+) = bitcast ptr {re.escape(slot)} to ptr",
        tail[clear.end() :],
    )
    assert leave_pointer is not None, body
    leave = re.search(
        rf"@pcc_gc_frame_leave_lifo\(ptr "
        rf"{re.escape(leave_pointer.group('pointer'))}\)",
        tail[clear.end() + leave_pointer.end() :],
    )
    assert leave is not None, body
    enter_position = root_store.end() + slots_pointer.end() + enter.start()
    load_positions = [root_store.end() + load.start() for load in loads]
    clear_position = root_store.end() + clear.start()
    leave_position = (
        root_store.end() + clear.end() + leave_pointer.end() + leave.start()
    )
    assert root_store.start() < enter_position < load_positions[0]
    assert load_positions[-1] < clear_position < leave_position
    lifecycle_positions = [
        root_store.start(),
        enter_position,
        *load_positions,
        clear_position,
        leave_position,
    ]
    lifecycle_blocks = {
        _basic_block_at(body, position).group("label")
        for position in lifecycle_positions
    }
    assert len(lifecycle_blocks) == 1, body
    return (
        [load.group("value") for load in loads],
        enter_position,
        clear_position,
        leave_position,
    )


def _assert_owned_local_transfer(body: str, runtime_symbol: str) -> None:
    got = _call_result(body, runtime_symbol)
    loads, _, _, result_root_leave = _root_lifecycle(body, got)
    assert len(loads) == 1, body
    current = loads[0]
    resolved_match = re.search(
        rf"(?P<resolved>%[-.A-Za-z0-9]+) = call ptr (?:\([^)]*\) )?"
        rf"@pcc_gc_resolve_owned_ptr\(ptr {re.escape(current)}\)",
        body,
    )
    assert resolved_match is not None, body
    assert result_root_leave < resolved_match.start()
    resolved = resolved_match.group("resolved")
    local_store = re.search(
        rf"store ptr {re.escape(resolved)}, ptr (?P<slot>%[-.A-Za-z0-9]+)",
        body,
    )
    assert local_store is not None, body
    slot = local_store.group("slot")
    tail = body[local_store.end() :]
    owned_guard = re.search(
        r"(?P<guard>%[-.A-Za-z0-9]+) = load i1, ptr "
        r"(?P<flag>%item\.owned[-.A-Za-z0-9]*)\n\s+br i1 (?P=guard), "
        r"label %(?P<release>[-.A-Za-z0-9]+),",
        tail,
    )
    assert owned_guard is not None, body
    owned_true = re.search(
        rf"store i1 1, ptr {re.escape(owned_guard.group('flag'))}",
        tail[: owned_guard.start()],
    )
    assert owned_true is not None, body
    local_block = _basic_block_at(body, local_store.start()).group("label")
    assert (
        _basic_block_at(body, local_store.end() + owned_true.start()).group("label")
        == local_block
    )
    assert (
        _basic_block_at(body, local_store.end() + owned_guard.start()).group("label")
        == local_block
    )
    slot_pointer = re.search(
        rf"(?P<pointer>%[-.A-Za-z0-9]+) = bitcast ptr {re.escape(slot)} to ptr",
        tail,
    )
    assert slot_pointer is not None, body
    exit_load = re.search(
        rf"(?P<value>%[-.A-Za-z0-9]+) = call ptr (?:\([^)]*\) )?"
        rf"@pcc_gc_load_ptr\(ptr null, ptr "
        rf"{re.escape(slot_pointer.group('pointer'))}\)",
        tail[slot_pointer.end() :],
    )
    assert exit_load is not None, body
    release_call = f"@pcc_gc_release(ptr {exit_load.group('value')})"
    assert body.count(release_call) == 1
    release_block = re.search(
        rf"\n{re.escape(owned_guard.group('release'))}:\n(?P<body>.*?)(?:\n\n|$)",
        body,
        re.DOTALL,
    )
    assert release_block is not None, body
    assert release_call in release_block.group("body")
    exit_load_position = local_store.end() + slot_pointer.end() + exit_load.start()
    assert _basic_block_at(body, exit_load_position).group(
        "label"
    ) == owned_guard.group("release")
    for value in (got, current, resolved):
        assert f"@pcc_gc_release(ptr {value})" not in body


def test_unannotated_exact_container_element_is_inferred_dynamic():
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python import type_infer
    from pcc.frontends.python.py_ast import Assign, DynType, FuncDef, Subscript

    module = parse_and_lift(
        "def probe(values: list) -> None:\n    item = values[0]\n",
        "<test>",
        "pcc.subscript_ownership_probe",
    )
    typed = type_infer.infer_module(module)
    probe = next(stmt for stmt in typed.body if isinstance(stmt, FuncDef))
    assignment = probe.body[0]
    assert isinstance(assignment, Assign)
    assert isinstance(assignment.value, Subscript)
    assert isinstance(assignment.value.ty, DynType)


@pytest.mark.parametrize(
    ("annotation", "index", "runtime_symbol"),
    (
        ("tuple", "0", "py_tuple_getitem"),
        ("list", "0", "py_list_getitem"),
        ("dict", '"key"', "py_dict_getitem"),
    ),
)
def test_raw_scaffold_exact_container_dyn_subscript_is_owned(
    annotation,
    index,
    runtime_symbol,
):
    body = _probe_body(_generate_ir(f"""
def probe(values: {annotation}) -> None:
    item = values[{index}]
""".lstrip()))

    assert f"@{runtime_symbol}(" in body
    assert "item.owned" in body
    _assert_owned_local_transfer(body, runtime_symbol)
    assert "@py_obj_getitem_i64(" not in body
    assert "@py_cpy_getitem(" not in body


@pytest.mark.parametrize(
    ("annotation", "index", "runtime_symbol", "unbox_symbol"),
    (
        ("tuple[int, ...]", "0", "py_tuple_getitem", "py_int_to_i64"),
        ("tuple[float, ...]", "0", "py_tuple_getitem", "py_float_to_f64"),
        ("tuple[bool, ...]", "0", "py_tuple_getitem", "py_obj_truthy"),
        ("list[int]", "0", "py_list_getitem", "py_int_to_i64"),
        ("list[float]", "0", "py_list_getitem", "py_float_to_f64"),
        ("list[bool]", "0", "py_list_getitem", "py_obj_truthy"),
        ("dict[str, int]", '"key"', "py_dict_getitem", "py_int_to_i64"),
        ("dict[str, float]", '"key"', "py_dict_getitem", "py_float_to_f64"),
        ("dict[str, bool]", '"key"', "py_dict_getitem", "py_obj_truthy"),
    ),
)
def test_raw_scaffold_exact_container_scalar_subscript_releases_new_ref(
    annotation,
    index,
    runtime_symbol,
    unbox_symbol,
):
    body = _probe_body(_generate_ir(f"""
def probe(values: {annotation}) -> None:
    item = values[{index}]
""".lstrip()))

    item = _call_result(body, runtime_symbol)
    loads, _, clear, leave = _root_lifecycle(body, item)
    assert len(loads) == 2, body
    rooted_value, release_value = loads
    unbox = body.index(f"@{unbox_symbol}(")
    unbox_line_end = body.find("\n", unbox)
    release = body.index(f"@pcc_gc_release(ptr {release_value})")
    assert rooted_value in body[unbox:unbox_line_end]
    first_load = body.index(f"{rooted_value} = call ptr")
    second_load = body.index(f"{release_value} = call ptr")
    assert first_load < unbox < second_load < clear < leave < release
    normal_block_match = _basic_block_at(body, unbox)
    normal_block_name = normal_block_match.group("label")
    normal_block = normal_block_match.group(0)
    for position in (first_load, second_load, clear, leave, release):
        assert _basic_block_at(body, position).group("label") == normal_block_name
    for value in (item, rooted_value):
        assert f"@pcc_gc_release(ptr {value})" not in body
    assert "@pcc_gc_pin(" not in body
    assert "@pcc_gc_unpin(" not in body
    assert body.count(f"@pcc_gc_release(ptr {release_value})") == 1
    assert normal_block.count(f"@pcc_gc_release(ptr {release_value})") == 1
    assert "item.owned" not in body
    assert "@py_obj_getitem_i64(" not in body
    assert "@py_cpy_getitem(" not in body


def test_owned_receiver_cleanup_happens_after_result_rooting():
    body = _probe_body(_generate_ir("""
def make_values() -> list[float]:
    return [1.0]

def probe() -> None:
    item = make_values()[0]
""".lstrip()))

    got = _call_result(body, "py_list_getitem")
    _, result_enter, clear, _ = _root_lifecycle(body, got)
    root_store = body.index(f", ptr {got})")
    getitem = re.search(
        rf"{re.escape(got)} = call ptr \(ptr, i64\) "
        r"@py_list_getitem\(ptr (?P<receiver>%[-.A-Za-z0-9]+), i64 0\)",
        body,
    )
    assert getitem is not None, body
    receiver_release = body.index(
        f"@pcc_gc_release(ptr {getitem.group('receiver')})",
        root_store,
    )
    unbox = body.index("@py_float_to_f64(")
    result_release = body.rindex("@pcc_gc_release(")
    assert root_store < result_enter < receiver_release < unbox < clear < result_release


@pytest.mark.parametrize(
    ("annotation", "index", "runtime_symbol"),
    (
        ("tuple[str, ...]", "0", "py_tuple_getitem"),
        ("list[str]", "0", "py_list_getitem"),
        ("dict[str, str]", '"key"', "py_dict_getitem"),
    ),
)
def test_raw_scaffold_exact_container_object_subscript_transfers_new_ref(
    annotation,
    index,
    runtime_symbol,
):
    body = _probe_body(_generate_ir(f"""
def probe(values: {annotation}) -> None:
    item = values[{index}]
""".lstrip()))

    assert "item.owned" in body
    _assert_owned_local_transfer(body, runtime_symbol)


def test_boxed_exact_int_subscript_transfers_new_ref_to_return_root():
    module_name = "user.subscript_ownership_probe"
    ir_text = _generate_ir(
        """
def probe(values: tuple[int, ...]) -> int:
    return values[0]
""".lstrip(),
        module_name=module_name,
    )
    body = _probe_body(
        ir_text,
        module_name=module_name,
    )

    item = _call_result(body, "py_tuple_getitem")
    assert re.search(
        r"define external ptr @user_user_subscript_ownership_probe_probe",
        ir_text,
    )
    assert not re.search(rf"@py_int_to_i64\([^\n]*{re.escape(item)}", body)
    assert f"@pcc_gc_release(ptr {item})" not in body
    shared_loads, _, _, _ = _root_lifecycle(body, item)
    assert len(shared_loads) == 1, body
    return_loads, _, _, return_root_leave = _root_lifecycle(
        body,
        shared_loads[0],
        require_entry=False,
    )
    assert len(return_loads) == 1, body
    for value in (item, shared_loads[0], return_loads[0]):
        assert f"@pcc_gc_release(ptr {value})" not in body
    returned = re.search(r"ret ptr (?P<value>%[-.A-Za-z0-9]+)", body)
    assert returned is not None, body
    assert returned.group("value") == return_loads[0]
    assert return_root_leave < returned.start()
    assert _basic_block_at(body, return_root_leave).group("label") == _basic_block_at(
        body, returned.start()
    ).group("label")


def test_c_abi_module_exact_container_dyn_subscript_keeps_owned_result():
    ir_text = _generate_ir("""
from pcc.extern import c_abi_export

@c_abi_export("ownership_anchor")
def anchor() -> int:
    return 0

def probe(values: list) -> None:
    item = values[0]
""".lstrip())
    body = _probe_body(ir_text)

    assert "define external i64 @ownership_anchor()" in ir_text
    assert "item.owned" in body
    _assert_owned_local_transfer(body, "py_list_getitem")
