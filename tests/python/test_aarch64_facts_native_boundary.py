"""Real PCC pruning/typing source controls for normal CI; no native execution."""

import ast
import re
from pathlib import Path

import pytest


SOURCE = Path(__file__).resolve().parents[2] / "pcc/backend/self_backend_aarch64_darwin_regalloc.py"


# This is a deliberately narrow contract for the generated probe below, not
# an IR optimizer. Current entry-local inventory emits empty roots even for
# a statically inactive suite. Those roots have real native bookkeeping cost.
_OBSERVER_LOCALS = ("observer", "sample", "query", "more")
_OWNED_PROBE_CALLS = {
    "py_recursion_enter", "py_recursion_leave", "pcc_gc_frame_enter",
    "pcc_gc_frame_leave", "pcc_gc_frame_enter_lifo", "pcc_gc_frame_leave_lifo",
    "pcc_gc_store_root", "pcc_gc_load_ptr", "pcc_gc_retain", "pcc_gc_safepoint",
    "pcc_thread_safepoint_gate", "pcc_gc_frame_enter_mapped",
    "pcc_gc_frame_leave_mapped", "pcc_gc_frame_enter_lifo_mapped",
    "pcc_gc_frame_leave_lifo_mapped",
    "py_exc_new", "py_raise", "py_current_exception", "py_exc_append_frame_source",
}
_SSA = r"%[-a-zA-Z0-9_.$]+"


def _assert_native_observer_pruned(body):
    """Allow inert null roots; reject observer objects, operations and escapes."""
    assert all(token not in body for token in (
        "_HOST_FACTS_STRUCTURE_OBSERVER", "host_assembly_profile", "cProfile", "py_cpy_",
    ))
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    definitions = {}
    for line in lines:
        match = re.fullmatch(r"(" + _SSA + r") = (.+)", line)
        if match:
            assert match[1] not in definitions
            definitions[match[1]] = match[2]
        if re.search(r"\b(call|invoke|callbr)\b", line):
            call = re.search(r"\bcall\s+[^%@\n]*@([-a-zA-Z0-9_.$]+)\(", line)
            assert call is not None and call[1] in _OWNED_PROBE_CALLS
            assert not re.search(r"\b(invoke|callbr)\b", line)
            # Ordinary application polls and mapped frames are bookkeeping,
            # not observer work. Admit only their concrete runtime ABI.
            if call[1] == "pcc_thread_safepoint_gate":
                assert line == "call void () @pcc_thread_safepoint_gate()"
            elif call[1] in ("pcc_gc_frame_enter_mapped", "pcc_gc_frame_enter_lifo_mapped"):
                assert re.fullmatch(r"call void \(ptr, ptr\) @" + call[1]
                                    + r"\(ptr (?:" + _SSA + r"|@[-a-zA-Z0-9_.$]+), ptr "
                                    + _SSA + r"\)", line)
            elif call[1] in ("pcc_gc_frame_leave_mapped", "pcc_gc_frame_leave_lifo_mapped"):
                assert re.fullmatch(r"call void \(ptr\) @" + call[1]
                                    + r"\(ptr " + _SSA + r"\)", line)
    slots, flags = set(), set()
    for role in _OBSERVER_LOCALS:
        prefix = "_facts_" + role
        addresses = [name for name, value in definitions.items()
                     if re.fullmatch(r"%" + prefix + r"\.addr(?:\.\d+)+", name)]
        assert len(addresses) == 1 and definitions[addresses[0]] == "alloca ptr"
        assert "store ptr null, ptr " + addresses[0] in lines
        slots.add(addresses[0])
        for name_prefix in ("%" + prefix, "%.bound." + prefix):
            matches = [name for name in definitions if re.fullmatch(
                re.escape(name_prefix) + r"\.owned(?:\.\d+)+", name)]
            assert len(matches) == 1 and definitions[matches[0]] == "alloca i1"
            flags.add(matches[0])
    enable = [name for name in definitions if re.fullmatch(
        r"%facts_observe\.addr(?:\.\d+)+", name)]
    assert len(enable) == 1 and definitions[enable[0]] == "alloca i1"
    # The ordinary enable value stays false. Its separate lexical boundness
    # flag may become true after that initialization.
    flags.add(enable[0])
    for flag in flags:
        assert "store i1 0, ptr " + flag in lines
        for line in lines:
            lhs, _, rhs = line.partition(" = ")
            if lhs == flag:
                continue
            if flag not in re.findall(_SSA, rhs if rhs else line):
                continue
            assert line == "store i1 0, ptr " + flag or re.fullmatch(
                _SSA + r" = load i1, ptr " + re.escape(flag), line)

    def null_address_chain(address):
        # A select's other operand must resolve through bitcasts to a null
        # scratch slot. Its complete use set is checked with the roots below.
        chain = set()
        while address not in chain:
            chain.add(address)
            value = definitions.get(address, "")
            if value == "alloca ptr":
                assert "store ptr null, ptr " + address in lines
                return chain
            cast = re.fullmatch(r"bitcast ptr (" + _SSA + r") to ptr", value)
            assert cast is not None
            address = cast[1]
        raise AssertionError("cyclic null-address aliases")

    addresses = set(slots)
    # Only two address-producing operations are permitted. A finite closure
    # over existing SSA definitions exposes aliases without executing IR.
    changed = True
    while changed:
        previous = len(addresses)
        for name, value in definitions.items():
            cast = re.fullmatch(r"bitcast ptr (" + _SSA + r") to ptr", value)
            if cast and cast[1] in addresses:
                addresses.add(name)
            select = re.fullmatch(r"select i1 " + _SSA + r", ptr (" + _SSA
                                  + r"), ptr (" + _SSA + r")", value)
            if select and (select[1] in addresses or select[2] in addresses):
                for operand in (select[1], select[2]):
                    if operand not in addresses:
                        addresses.update(null_address_chain(operand))
                addresses.add(name)
        changed = len(addresses) != previous
    scalar_loads = {name for name, value in definitions.items()
                    if any(value == "load i1, ptr " + flag for flag in flags)}
    known = addresses | flags | scalar_loads
    assert all(token in known for token in re.findall(_SSA, body) if "_facts_" in token)
    for line in lines:
        lhs, separator, rhs = line.partition(" = ")
        operands = set(re.findall(_SSA, rhs if separator else line))
        used = operands & addresses
        if not used:
            continue
        if separator and lhs in addresses:
            assert (re.fullmatch(r"bitcast ptr " + _SSA + r" to ptr", rhs)
                    or re.fullmatch(r"select i1 " + _SSA + r", ptr " + _SSA
                                    + r", ptr " + _SSA, rhs))
            continue
        # The frame map is a global, passed directly or through an alias.
        enter = re.fullmatch(r"call void \(ptr, ptr\) @pcc_gc_frame_enter(?:_mapped)?\(ptr (" + _SSA
                             + r"|@[-a-zA-Z0-9_.$]+), ptr (" + _SSA + r")\)", line)
        assert any(line == allowed for address in used for allowed in (
            "store ptr null, ptr " + address,
            "call void (ptr) @pcc_gc_frame_leave(ptr " + address + ")",
            "call void (ptr) @pcc_gc_frame_leave_mapped(ptr " + address + ")",
            "call void (ptr, ptr) @pcc_gc_store_root(ptr " + address + ", ptr null)",
        )) or (enter is not None and enter[1] not in addresses and used == {enter[2]})
    assert "inttoptr i64 35 to ptr" in body


def test_current_facts_host_references_are_pruned_by_real_import_scanner():
    from pcc.frontends.python import pipeline_import_scan as scan
    source = SOURCE.read_text()
    native = scan._without_inactive_implementation_imports(source)
    host = scan._without_inactive_implementation_imports(source, implementation="cpython")
    assert "_HOST_FACTS_STRUCTURE_OBSERVER" not in native
    assert "_facts_observer" not in native and "_facts_sample" not in native
    assert "_HOST_FACTS_STRUCTURE_OBSERVER" in host
    assert "_facts_observer.row" in host
    assert "cProfile" not in native and "host_assembly_profile" not in native


def test_current_guarded_suites_type_and_prune_in_real_native_ir(monkeypatch):
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.type_infer import infer_module
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    # This AArch64 Darwin boundary uses the ordinary application defaults,
    # including thread gates and mapped roots, independent of the test host.
    monkeypatch.delenv("PCC_IMPLICIT_THREAD_SAFEPOINTS", raising=False)
    monkeypatch.delenv("PCC_PRECISE_STACKMAP_TABLES", raising=False)
    source = SOURCE.read_text()
    tree = ast.parse(source)
    facts = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name == "_function_level_facts")
    # Use the exact current guarded suites, including the observed business
    # loop and dynamic host callbacks; do not replace them with a toy guard.
    guarded = []
    for node in facts.body:
        if (isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "facts_observe" for target in node.targets)):
            guarded.append(node)
        elif (isinstance(node, ast.If)
                and ast.unparse(node.test) == "sys.implementation.name == 'cpython'"):
            guarded.append(ast.Import(names=[ast.alias(name="sys")]))
            guarded.append(node)
        elif isinstance(node, ast.For):
            guarded.extend(child for child in node.body if isinstance(child, ast.If)
                           and isinstance(child.test, ast.Name) and child.test.id == "facts_observe")
    probe = ast.parse("def native_probe():\n    return 17\n")
    probe.body[0].body = guarded + probe.body[0].body
    text = ast.unparse(ast.fix_missing_locations(probe))
    module = infer_module(parse_and_lift(text, "facts_native_boundary.py", "facts_native_boundary"))
    generator = L1CodeGen(module, emit_cpy_main_exitcode=False, ir_scaffold_mode="on")
    generator._target_triple = "aarch64-apple-darwin"
    generator._strict_no_libpython = True
    result = str(generator.generate())
    match = re.search(r"^define[^\n]*@user_facts_native_boundary_native_probe\([^\n]*\) \{", result, re.MULTILINE)
    assert match is not None
    body = result[match.start():].split("\n}\n", 1)[0]
    assert "call void () @pcc_thread_safepoint_gate()" in body
    assert "@pcc_gc_frame_enter_mapped(" in body
    assert "@pcc_gc_frame_leave_mapped(" in body
    _assert_native_observer_pruned(body)


def _inert_observer_probe_ir(*, mapped=False):
    """Small hand-written IR control, not a saved compiler transcript."""
    lines = ["define ptr @native_probe() {", "entry:",
             "  %facts_observe.addr.90 = alloca i1",
             "  store i1 0, ptr %facts_observe.addr.90",
             "  %gc.map = bitcast ptr @root_map to ptr"]
    for index, role in enumerate(_OBSERVER_LOCALS, 1):
        slot = "%_facts_" + role + ".addr." + str(index)
        flag = "%_facts_" + role + ".owned." + str(index)
        bound = "%.bound._facts_" + role + ".owned." + str(index)
        alias, empty, empty_alias, owned, active = ("%" + role + suffix for suffix in (
            ".root", ".empty", ".empty.alias", ".is_owned", ".active"))
        lines.extend([
            "  " + slot + " = alloca ptr", "  store ptr null, ptr " + slot,
            "  " + flag + " = alloca i1", "  store i1 0, ptr " + flag,
            "  " + bound + " = alloca i1", "  store i1 0, ptr " + bound,
            "  " + alias + " = bitcast ptr " + slot + " to ptr",
            "  call void (ptr, ptr) @pcc_gc_frame_enter(ptr %gc.map, ptr " + alias + ")",
            "  " + empty + " = alloca ptr", "  store ptr null, ptr " + empty,
            "  " + empty_alias + " = bitcast ptr " + empty + " to ptr",
            "  " + owned + " = load i1, ptr " + flag,
            "  " + active + " = select i1 " + owned + ", ptr " + alias + ", ptr " + empty_alias,
            "  call void (ptr, ptr) @pcc_gc_store_root(ptr " + active + ", ptr null)",
            "  call void (ptr) @pcc_gc_frame_leave(ptr " + alias + ")",
        ])
    lines.extend(["  call void () @pcc_thread_safepoint_gate()",
                  "  %seventeen = inttoptr i64 35 to ptr", "  ret ptr %seventeen", "}"])
    body = "\n".join(lines)
    if mapped:
        body = body.replace("@pcc_gc_frame_enter(", "@pcc_gc_frame_enter_mapped(")
        body = body.replace("@pcc_gc_frame_leave(", "@pcc_gc_frame_leave_mapped(")
    return body


def test_native_observer_ir_contract_accepts_only_inert_null_roots():
    for mapped in (False, True):
        body = _inert_observer_probe_ir(mapped=mapped)
        assert "_facts_observer" in body
        _assert_native_observer_pruned(body)


@pytest.mark.parametrize("violation", [
    "callback", "indirect", "hook_capture", "non_null_store", "payload_read",
    "root_escape", "owned_true",
])
def test_native_observer_ir_contract_rejects_active_or_escaping_state(violation):
    insertions = {
        "callback": (
            "  call void () @observer_callback()",
            "  %boxed = call ptr (i64) @py_int_from_i64(i64 17)",
            "  call void (ptr) @pcc_thread_safepoint_gate(ptr null)",
            "  %gate = call ptr () @pcc_thread_safepoint_gate()",
            "  call void () @pcc_gc_frame_leave_mapped()",
            "  call void (ptr) @pcc_gc_frame_enter_mapped(ptr %gc.map)",
        ),
        "indirect": ("  call void () %callback()",),
        "hook_capture": (
            "  %captured = load ptr, ptr @_HOST_FACTS_STRUCTURE_OBSERVER",
            "  %_facts_observer.shadow = inttoptr i64 35 to ptr\n"
            "  store ptr %_facts_observer.shadow, ptr @escaped_root",
        ),
        "non_null_store": (
            "  store ptr inttoptr (i64 35 to ptr), ptr %observer.active",
            "  store ptr inttoptr (i64 35 to ptr), ptr %observer.empty",
        ),
        "payload_read": ("  %captured = load ptr, ptr %observer.root",),
        # Whitelisted runtime targets cannot consume root addresses in other
        # argument positions; both select arms remain subject to the contract.
        "root_escape": (
            "  %escaped = call ptr (ptr) @pcc_gc_retain(ptr %observer.root)",
            "  call void (ptr, ptr) @pcc_gc_frame_enter(ptr %observer.root, ptr %observer.root)",
            "  call void (ptr, ptr) @pcc_gc_frame_enter_mapped(ptr %observer.root, ptr %observer.root)",
            "  call void (ptr, ptr) @pcc_gc_frame_enter_lifo_mapped(ptr %gc.map, ptr %observer.root)",
            "  store ptr %observer.root, ptr @escaped_root",
        ),
        "owned_true": (
            "  store i1 1, ptr %_facts_observer.owned.1",
            "  store i1 1, ptr %.bound._facts_observer.owned.1",
            "  %flag.alias = bitcast ptr %_facts_observer.owned.1 to ptr\n"
            "  store i1 1, ptr %flag.alias",
            "  %dynamic = trunc i8 1 to i1\n"
            "  store i1 %dynamic, ptr %facts_observe.addr.90",
            "  %enable.alias = bitcast ptr %facts_observe.addr.90 to ptr\n"
            "  store i1 1, ptr %enable.alias",
        ),
    }
    for mapped in (False, True):
        body = _inert_observer_probe_ir(mapped=mapped)
        for insertion in insertions[violation]:
            mutated = body.replace("  %seventeen =", insertion + "\n  %seventeen =", 1)
            with pytest.raises(AssertionError):
                _assert_native_observer_pruned(mutated)
