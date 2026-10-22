"""Terminal backend-owner retirement contracts; draft execution UNRUN."""

import gc
import weakref

import pytest

from pcc.backend import self_backend_ir as backend_ir
from pcc.backend import self_backend_parse as parser
from pcc.backend import self_backend_x86_64_linux as x86
from pcc.backend import self_backend_aarch64_darwin as arm
from pcc.backend.self_backend_module_symbols import PreparedModuleSymbols


def _symbols(context):
    return PreparedModuleSymbols(
        internal_prefix=".batch_", defined_symbols=frozenset(("deferred",)),
        internal_symbols=frozenset(("deferred",)), thread_local_symbols=frozenset(),
        type_context=context,
    )


@pytest.mark.parametrize("target", ("x86", "arm"))
def test_target_retirement_releases_context_owner_without_mutating_it(monkeypatch, target):
    backend = x86 if target == "x86" else arm
    active = "_X86_EMISSION_ACTIVE" if target == "x86" else "_AARCH64_EMISSION_ACTIVE"
    retire = backend.retire_x86_module_state if target == "x86" else backend.retire_aarch64_module_state
    monkeypatch.setattr(backend, active, False)
    if target == "arm":
        monkeypatch.setattr(backend, "require_direct_instruction_capture_idle", lambda: None)
    context = backend_ir.TypeParseContext(named_type_bodies={"T": "{ i64 }"})
    symbols = _symbols(context)
    monkeypatch.setattr(backend, "_MODULE_SYMBOLS", symbols)
    if target == "x86":
        monkeypatch.setattr(backend, "_TLS_GLOBALS", {"retained": context})
        monkeypatch.setattr(backend, "_VARARG_FUNCTIONS", frozenset(("vararg",)))
        # This is the callback deferred stack-map encoding still needs.
        assert backend._asm_symbol("deferred") == ".batch_deferred"
    retire()
    assert backend._MODULE_SYMBOLS.type_context is None
    assert context.named_type_bodies == {"T": "{ i64 }"}
    assert symbols.type_context is context  # External aliases were not mutated.
    if target == "x86":
        assert backend._TLS_GLOBALS == {}
        assert backend._VARARG_FUNCTIONS == frozenset()
    reference = weakref.ref(context)
    del symbols
    del context
    gc.collect()
    assert reference() is None


@pytest.mark.parametrize("target", ("x86", "arm"))
def test_target_retirement_rejects_live_emission_before_replacing_owners(monkeypatch, target):
    backend = x86 if target == "x86" else arm
    active = "_X86_EMISSION_ACTIVE" if target == "x86" else "_AARCH64_EMISSION_ACTIVE"
    retire = backend.retire_x86_module_state if target == "x86" else backend.retire_aarch64_module_state
    marker = _symbols(backend_ir.TypeParseContext())
    monkeypatch.setattr(backend, "_MODULE_SYMBOLS", marker)
    monkeypatch.setattr(backend, active, True)
    with pytest.raises(Exception, match="cannot retire active"):
        retire()
    assert backend._MODULE_SYMBOLS is marker


def test_arm_retirement_requires_direct_capture_idle(monkeypatch):
    marker = _symbols(backend_ir.TypeParseContext())
    monkeypatch.setattr(arm, "_MODULE_SYMBOLS", marker)
    monkeypatch.setattr(arm, "_AARCH64_EMISSION_ACTIVE", False)

    def busy():
        raise ValueError("capture still active")

    monkeypatch.setattr(arm, "require_direct_instruction_capture_idle", busy)
    with pytest.raises(ValueError, match="capture still active"):
        arm.retire_aarch64_module_state()
    assert arm._MODULE_SYMBOLS is marker


def test_lookup_cache_retirement_drops_indexes_without_mutating_mapping(monkeypatch):
    mapping = {"value": object()}
    monkeypatch.setattr(backend_ir, "_TEXT_KEY_INDEX_CACHE", {id(mapping): [mapping]})
    monkeypatch.setattr(backend_ir, "_OPERAND_INTERN", {"value": "value"})
    monkeypatch.setattr(parser, "_NUMERIC_SSA_NAME_CACHE", {1: "1"})
    monkeypatch.setattr(parser, "_DOT_NUMERIC_SSA_NAME_CACHE", {1: ".1"})
    parser.retire_module_parse_caches()
    backend_ir.retire_module_text_key_cache()
    assert backend_ir._TEXT_KEY_INDEX_CACHE == {}
    assert backend_ir._OPERAND_INTERN == {}
    assert parser._NUMERIC_SSA_NAME_CACHE == {}
    assert parser._DOT_NUMERIC_SSA_NAME_CACHE == {}
    assert set(mapping) == {"value"}
