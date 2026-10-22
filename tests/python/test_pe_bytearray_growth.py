"""PE growth ownership on the host and in native lowering, without a bootstrap."""

import ast
from pathlib import Path
import re

import pytest

from pcc.backend import coff_x86_64 as coff
from pcc.backend import pe_host_budget as host
from pcc.backend import pe_x86_64 as pe
from pcc.backend.precise_stackmap import PreciseStackMapError
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.pipeline_context import build_closed_world_context
from pcc.frontends.python.type_infer import infer_module
from tests.owned_ir_validation import verify_ir_text


def _budget():
    return host.PEHostBudget(
        1_000_000, 100, set_header_bytes=10, set_entry_bytes=2,
        integer_bytes=3, metadata_entry_bytes=7, allocation_quantum=1,
    )


@pytest.mark.parametrize("helper,argument,expected", (
    ("_append", b"cd", b"abcd"),
    ("_append", b"", b"ab"),
    ("_zeros", 3, b"ab\0\0\0"),
    ("_zeros", 0, b"ab"),
))
@pytest.mark.parametrize("budgeted", (False, True))
def test_growth_returns_the_current_buffer(helper, argument, expected, budgeted):
    payload = bytearray(b"ab")
    alias = payload
    budget = _budget() if budgeted else None
    result = getattr(pe, helper)(payload, argument, budget)
    assert result is payload is alias
    assert bytes(result) == expected
    if budgeted:
        assert budget._buffers[id(result)][0] is result
        assert budget.retained_bytes == budget._buffers[id(result)][1]


@pytest.mark.parametrize("helper,argument", (
    ("_append", b"cd"), ("_append", b""), ("_zeros", 3), ("_zeros", 0),
))
def test_growth_budget_still_rejects_before_mutation(helper, argument):
    payload = bytearray(b"ab")
    budget = _budget()
    budget.retain(payload)
    budget.limit_bytes = budget.retained_bytes
    retained = budget.retained_bytes
    with pytest.raises(PreciseStackMapError):
        getattr(pe, helper)(payload, argument, budget)
    assert payload == b"ab"
    assert budget.retained_bytes == retained
    assert budget._buffers[id(payload)][0] is payload


@pytest.mark.parametrize("data", (None, 0, False))
def test_empty_shortcut_does_not_hide_invalid_extend_inputs(data):
    payload = bytearray(b"ab")
    with pytest.raises(TypeError):
        pe._append(payload, data, None)
    assert payload == b"ab"


def test_empty_shortcut_does_not_call_custom_truthiness():
    class Data(bytes):
        def __bool__(self):
            raise AssertionError("extend does not truth-test its argument")

    class Count:
        def __bool__(self):
            raise AssertionError("byte repetition uses __index__, not truthiness")

        def __index__(self):
            return 2

    payload = pe._append(bytearray(b"ab"), Data(b"cd"), None)
    assert pe._zeros(payload, Count(), None) == b"abcd\0\0"


def _objects():
    return [coff.CoffObject((
        coff.CoffSection(".text$a", b"\xc3", 0x60000020, 1),
        coff.CoffSection(".text$z", b"\x90", 0x60000020, 16),
        coff.CoffSection(".data", b"\0" * 12, 0xC0000040, 8,
                         (coff.CoffRelocation(0, 0, coff.ADDR64),)),
        coff.CoffSection(".rdata", b"constant", 0x40000040, 8),
        coff.CoffSection(".tls", b"tls", 0xC0000040, 4),
    ), (
        coff.CoffSymbol("pcc_windows_start", 1, function=True),
        coff.CoffSymbol("ExitProcess", 0),
        coff.CoffSymbol("_tls_index", 3, 8),
    ))]


def _link(objects):
    definitions, imports = pe._symbols(objects)
    boundaries = {
        "__init_array_start", "__init_array_end",
        "__fini_array_start", "__fini_array_end",
    }
    return pe._link_selected_objects(
        objects, definitions, imports, boundaries,
        "pcc_windows_start", 0x140000000, None,
    )


def test_all_growth_callers_keep_replacement_buffers(monkeypatch):
    objects = _objects()
    original_objects = [coff.emit_object(obj) for obj in objects]
    expected = _link(objects)
    original_append, original_zeros = pe._append, pe._zeros
    retired = []

    def replaced(helper, payload, argument, budget):
        # Model the native runtime's replacement allocation. Keep old owners
        # alive and unchanged, so a discarded return cannot pass accidentally.
        assert budget is None
        old = bytes(payload)
        result = helper(bytearray(payload), argument, budget)
        retired.append((payload, old))
        assert result is not payload
        return result

    monkeypatch.setattr(pe, "_append", lambda payload, data, budget:
                        replaced(original_append, payload, data, budget))
    monkeypatch.setattr(pe, "_zeros", lambda payload, count, budget:
                        replaced(original_zeros, payload, count, budget))
    assert _link(objects) == expected
    # Import tables still grow one buffer; unbudgeted section and relocation
    # payloads are joined pieces (below).
    assert len(retired) >= 5
    assert all(bytes(owner) == original for owner, original in retired)
    assert [coff.emit_object(obj) for obj in objects] == original_objects


def test_unbudgeted_pieces_join_once_without_growth(monkeypatch):
    def refuse(*_args):
        raise AssertionError("unbudgeted pieces never grow a bytearray")

    monkeypatch.setattr(pe, "_append", refuse)
    monkeypatch.setattr(pe, "_zeros", refuse)
    pieces = pe._Pieces(None)
    pieces.append(b"ab")
    pieces.zeros(3)
    pieces.append(b"")
    pieces.zeros(0)
    pieces.append(bytearray(b"cd"))
    assert pieces.size == 7
    payload = pieces.finish()
    assert type(payload) is bytearray and payload == b"ab\0\0\0cd"
    assert pieces.finish() is payload
    assert type(pe._Pieces(None).finish()) is bytearray


def test_budgeted_pieces_grow_one_retained_buffer():
    budget = _budget()
    pieces = pe._Pieces(budget)
    first = pieces.payload
    budget.retain(first)
    pieces.append(b"ab")
    pieces.zeros(2)
    pieces.append(b"")
    assert pieces.size == 4
    payload = pieces.finish()
    assert payload is first and payload == b"ab\0\0"
    assert budget._buffers[id(payload)][0] is payload
    assert budget.retained_bytes == budget._buffers[id(payload)][1]


def _provider_source():
    source = Path(pe.__file__).read_text(encoding="utf-8")
    selected = [ast.get_source_segment(source, node)
                for node in ast.parse(source).body
                if isinstance(node, ast.FunctionDef)
                and node.name in ("_append", "_zeros")]
    assert len(selected) == 2
    return "\n\n".join(selected)


def test_exact_growth_helpers_lower_to_owned_bytearray_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "0")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "0")
    provider_name = "pcc.backend.pe_x86_64"
    consumer_name = "pcc.backend.pe_growth_probe"
    provider, consumer = tmp_path / "provider.py", tmp_path / "consumer.py"
    provider.write_text(_provider_source(), encoding="utf-8")
    consumer.write_text(
        "from pcc.backend.pe_x86_64 import _append, _zeros\n"
        "def probe():\n"
        "    payload = bytearray(b'ab')\n"
        "    payload = _append(payload, b'cd', None)\n"
        "    payload = _zeros(payload, 2, None)\n"
        "    payload = _append(payload, b'', None)\n"
        "    payload = _zeros(payload, 0, None)\n"
        "    return bytes(payload) == b'abcd\\0\\0'\n",
        encoding="utf-8",
    )
    modules, exports, derived = build_closed_world_context(
        [str(provider), str(consumer)], [provider_name, consumer_name],
    )
    for module in modules:
        external = {name: value for name, value in exports.items() if name != module.name}
        typed = infer_module(module, external_exports=external, derived_class_map=derived)
        codegen = L1CodeGen(typed, emit_cpy_main_exitcode=False, ir_scaffold_mode="on")
        codegen._strict_no_libpython = True
        codegen._prefer_native_callable_values = True
        codegen._native_module_exports = exports
        codegen._sibling_module_inits = tuple(external)
        text = str(codegen.generate(typed))
        (tmp_path / (module.name + ".ll")).write_text(text, encoding="utf-8")
        verify_ir_text(text)
        assert "strict.nolib.stub" not in text
        assert not re.search(r"\bcall [^\n]*@py_cpy_", text)
        if module.name == provider_name:
            assert len(re.findall(r"\bcall [^\n]*@py_bytearray_extend\(", text)) == 2
            assert "dyn.extend.recv" not in text
            for helper in ("_append", "_zeros"):
                symbol = "user_pcc_backend_pe_x86_64_" + helper
                body = re.search(r"^define [^\n]*@" + symbol + r"\([^\n]*\) \{\n(.*?)^\}",
                                 text, re.M | re.S).group(1)
                # Replacement returns transfer the owned local; unchanged
                # empty/budgeted returns retain only the borrowed input.
                assert re.search(r"store i1 1, ptr %result\.owned\.", body)
                assert re.search(r"ret\.local\.owned\.[^=]*= load i1, ptr %result\.owned\.", body)
                assert re.search(r"ret\.local\.retain\.[^=]*= call ptr .*@pcc_gc_retain\(", body)
