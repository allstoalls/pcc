from __future__ import annotations

import copy

import pytest

from pcc.backend import macho_spec as spec
from pcc.backend.macho_obj import DataInCodeRegion, PCC_STACKMAP_SECTION_FLAGS
from pcc.backend.macho_semantic_layout import (
    FrontendSemanticFunction,
    FrontendSemanticLayoutPolicy,
    SemanticAtom,
    SemanticLayoutError,
    SemanticLayoutManifest,
    apply_semantic_layout,
    frontend_policy_from_payload,
    manifest_from_payload,
    materialize_frontend_manifest,
    native_object_sha256,
)
from pcc.backend.native_object import (
    NativeObject,
    NativeRelocation,
    NativeSection,
    NativeSymbol,
)
from pcc.backend.precise_stackmap import (
    ARCH_AARCH64,
    FunctionStackMap,
    PreciseStackMap,
    decode_stack_map,
    encode_stack_map,
    function_address_offsets,
    function_id,
)


def _object(*, section_target=False):
    symbols = (
        NativeSymbol("_main", 1, 0, True),
        NativeSymbol("_hot", 1, 8, True, True),
        NativeSymbol("_dead", 1, 16, True, True),
        NativeSymbol("_cold", 1, 24, True, True),
    )
    relocations = (
        NativeRelocation(
            0,
            None if section_target else 1,
            spec.ARM64_RELOC_BRANCH26 if not section_target else spec.ARM64_RELOC_UNSIGNED,
            not section_target,
            2 if not section_target else 3,
            target_section_index=1 if section_target else None,
            target_offset=8 if section_target else None,
        ),
        NativeRelocation(4, 3, spec.ARM64_RELOC_BRANCH26, True, 2),
    )
    return NativeObject(
        (
            NativeSection(
                "__TEXT",
                "__text",
                spec.S_REGULAR | spec.S_ATTR_PURE_INSTRUCTIONS,
                2,
                b"M" * 8 + b"H" * 8 + b"D" * 8 + b"C" * 8,
                relocations=relocations,
                data_in_code=(DataInCodeRegion(16, 4, spec.DICE_KIND_DATA),),
            ),
        ),
        symbols,
    )


def _manifest(obj, **updates):
    values = {
        "object_sha256": native_object_sha256(obj),
        "entry": "_main",
        "roots": (),
        "atoms": (
            SemanticAtom.create(
                name="_main", segname="__TEXT", sectname="__text",
                offset=0, size=8, align_log2=2, temperature="hot",
            ),
            SemanticAtom.create(
                name="_hot", segname="__TEXT", sectname="__text",
                offset=8, size=8, align_log2=2, temperature="hot",
                eliminable=True,
            ),
            SemanticAtom.create(
                name="_dead", segname="__TEXT", sectname="__text",
                offset=16, size=8, align_log2=2, temperature="normal",
                eliminable=True,
            ),
            SemanticAtom.create(
                name="_cold", segname="__TEXT", sectname="__text",
                offset=24, size=8, align_log2=2, temperature="cold",
                eliminable=True,
            ),
        ),
    }
    values.update(updates)
    return SemanticLayoutManifest.create(**values)


def test_semantic_layout_drops_only_unreachable_atom_and_rewrites_all_offsets():
    obj = _object()
    result = apply_semantic_layout(obj, _manifest(obj))
    out = result.native_object

    assert result.plan.kept_atoms == ("_main", "_hot", "_cold")
    assert result.plan.dropped_atoms == ("_dead",)
    assert result.plan.output_order == ("_main", "_hot", "_cold")
    assert result.plan.input_bytes == 32
    assert result.plan.output_bytes == 24
    assert out.sections[0].data == b"M" * 8 + b"H" * 8 + b"C" * 8
    assert [symbol.name for symbol in out.symbols] == ["_main", "_hot", "_cold"]
    assert [symbol.offset for symbol in out.symbols] == [0, 8, 16]
    assert [reloc.offset for reloc in out.sections[0].relocations] == [0, 4]
    assert out.sections[0].data_in_code == ()
    assert out.symbols[out.sections[0].relocations[1].symbol_index].name == "_cold"


def test_hot_cold_order_is_stable_and_independent_of_manifest_input_order():
    obj = _object()
    manifest = _manifest(obj, roots=("_dead",))
    payload = manifest.payload()
    payload["atoms"] = list(reversed(payload["atoms"]))
    rebuilt = manifest_from_payload(payload)
    result = apply_semantic_layout(obj, rebuilt)

    assert result.plan.output_order == ("_main", "_hot", "_dead", "_cold")
    assert result.native_object.sections[0].data == (
        b"M" * 8 + b"H" * 8 + b"D" * 8 + b"C" * 8
    )


def test_manifest_digest_and_unknown_fields_fail_closed():
    obj = _object()
    manifest = _manifest(obj)
    with pytest.raises(SemanticLayoutError, match="digest mismatch"):
        apply_semantic_layout(
            obj,
            _manifest(obj, object_sha256="f" * 64),
        )

    payload = copy.deepcopy(manifest.payload())
    payload["guess_dead_code"] = True
    with pytest.raises(SemanticLayoutError, match="fields"):
        manifest_from_payload(payload)


def test_unowned_bytes_symbol_and_section_target_relocation_fail_closed():
    obj = _object()
    atoms = list(_manifest(obj).atoms)
    atoms[0] = SemanticAtom.create(
        name="_main", segname="__TEXT", sectname="__text",
        offset=0, size=4, align_log2=2, temperature="hot",
    )
    with pytest.raises(SemanticLayoutError, match="unowned nonzero bytes"):
        apply_semantic_layout(obj, _manifest(obj, atoms=atoms))

    section_target = _object(section_target=True)
    with pytest.raises(SemanticLayoutError, match="section-target"):
        apply_semantic_layout(section_target, _manifest(section_target))


def test_reachable_atom_cannot_be_dropped_even_when_marked_eliminable():
    obj = _object()
    result = apply_semantic_layout(obj, _manifest(obj))

    assert "_hot" in result.plan.kept_atoms
    assert "_cold" in result.plan.kept_atoms
    assert "_dead" not in result.plan.kept_atoms


def test_frontend_policy_materializes_exact_atoms_and_keeps_unknown_runtime():
    from pcc.frontends.python import pipeline_ir_split, pipeline_ir_text
    from pcc.frontends.python.pipeline_semantic_layout import (
        build_frontend_semantic_layout_policy,
    )

    ir_text = '''
target triple = "arm64-apple-darwin23.6.0"

define i32 @main() #0 {
entry:
  ret i32 0
}

define internal i32 @live() #1 {
entry:
  ret i32 1
}

define internal i32 @dead() {
entry:
  ret i32 2
}

attributes #0 = { hot }
attributes #1 = { cold }
'''.strip()
    payload = build_frontend_semantic_layout_policy(
        [ir_text],
        defined_function_name_from_line=(
            pipeline_ir_text.defined_function_name_from_line
        ),
        global_name_from_definition_line=(
            pipeline_ir_text.global_name_from_definition_line
        ),
        ir_global_definition_line=pipeline_ir_split.ir_global_definition_line,
        line_has_internal_linkage=pipeline_ir_text.line_has_internal_linkage,
    )
    policy = frontend_policy_from_payload(payload)
    by_temperature = {
        function.temperature: function.symbol for function in policy.functions
    }
    main_symbol = by_temperature["hot"]
    live_symbol = by_temperature["cold"]
    dead_symbol = next(
        function.symbol
        for function in policy.functions
        if function.symbol not in (main_symbol, live_symbol)
    )
    symbols = (
        NativeSymbol(live_symbol, 1, 4, False),
        NativeSymbol(dead_symbol, 1, 8, False),
        NativeSymbol(main_symbol, 1, 0, True),
        NativeSymbol("_runtime_owner", 1, 12, True),
    )
    obj = NativeObject(
        (
            NativeSection(
                "__TEXT",
                "__text",
                spec.S_REGULAR | spec.S_ATTR_PURE_INSTRUCTIONS,
                2,
                b"M" * 4 + b"L" * 4 + b"D" * 4 + b"R" * 4,
                relocations=(
                    NativeRelocation(
                        0,
                        0,
                        spec.ARM64_RELOC_BRANCH26,
                        True,
                        2,
                    ),
                ),
            ),
        ),
        symbols,
    )

    manifest = materialize_frontend_manifest(obj, policy)
    result = apply_semantic_layout(obj, manifest)

    assert manifest.object_sha256 == native_object_sha256(obj)
    assert result.plan.dropped_atoms == (dead_symbol,)
    assert result.plan.output_order == (
        main_symbol,
        "_runtime_owner",
        live_symbol,
    )
    assert result.native_object.sections[0].data == b"M" * 4 + b"R" * 4 + b"L" * 4
    assert "_runtime_owner" in result.plan.kept_atoms


def test_frontend_policy_rejects_missing_merged_function_and_schema_drift():
    obj = _object()
    policy = FrontendSemanticLayoutPolicy.create(
        entry="_main",
        roots=(),
        functions=(
            FrontendSemanticFunction.create(
                symbol="_missing", temperature="normal", eliminable=True
            ),
            FrontendSemanticFunction.create(
                symbol="_main", temperature="hot", eliminable=False
            ),
        ),
    )
    with pytest.raises(SemanticLayoutError, match="absent from merged text"):
        materialize_frontend_manifest(obj, policy)

    with pytest.raises(SemanticLayoutError, match="fields"):
        frontend_policy_from_payload(
            {
                "entry": "_main",
                "functions": [],
                "roots": [],
                "schema": "pcc.frontend-macho-semantic-layout.v1",
                "guess": True,
            }
        )


def test_frontend_policy_internal_namespace_includes_module_global_symbols():
    from pcc.backend.self_backend_module_symbols import prepare_module_symbols
    from pcc.backend.self_backend_parse import parse_self_backend_module
    from pcc.frontends.python import pipeline_ir_split, pipeline_ir_text
    from pcc.frontends.python.pipeline_semantic_layout import (
        build_frontend_semantic_layout_policy,
    )

    ir_text = '''
target triple = "arm64-apple-darwin23.6.0"
@public_seed = global i32 0
@private_seed = internal global i32 1

define i32 @main() {
entry:
  ret i32 0
}

define internal i32 @helper() {
entry:
  ret i32 1
}
'''.strip()
    parsed = parse_self_backend_module(ir_text)
    module_symbols = prepare_module_symbols(
        ir_text, list(parsed.globals_), list(parsed.functions)
    )
    payload = build_frontend_semantic_layout_policy(
        [ir_text],
        defined_function_name_from_line=(
            pipeline_ir_text.defined_function_name_from_line
        ),
        global_name_from_definition_line=(
            pipeline_ir_text.global_name_from_definition_line
        ),
        ir_global_definition_line=pipeline_ir_split.ir_global_definition_line,
        line_has_internal_linkage=pipeline_ir_text.line_has_internal_linkage,
    )
    symbols = {item["symbol"] for item in payload["functions"]}

    assert "_main" in symbols
    assert "_" + module_symbols.internal_prefix + "helper" in symbols


def test_semantic_layout_packs_stackmap_entries_with_kept_functions():
    function_targets = sorted(
        (
            (FunctionStackMap(function_id("_main"), 0, 8, 0, ()), 1),
            (FunctionStackMap(function_id("_dead"), 0, 8, 0, ()), 0),
        ),
        key=lambda item: item[0].function_id,
    )
    stackmap = PreciseStackMap(
        ARCH_AARCH64,
        tuple(item[0] for item in function_targets),
    )
    stackmap_data = encode_stack_map(stackmap)
    address_offsets = function_address_offsets(stackmap_data)
    obj = NativeObject(
        (
            NativeSection(
                "__TEXT",
                "__text",
                spec.S_REGULAR | spec.S_ATTR_PURE_INSTRUCTIONS,
                2,
                b"M" * 8 + b"D" * 8,
            ),
            NativeSection(
                "__DATA",
                "__pcc_stackmaps",
                PCC_STACKMAP_SECTION_FLAGS,
                3,
                stackmap_data,
                relocations=tuple(
                    NativeRelocation(
                        address_offsets[index],
                        target[1],
                        spec.ARM64_RELOC_UNSIGNED,
                        False,
                        3,
                    )
                    for index, target in enumerate(function_targets)
                ),
            ),
        ),
        (
            NativeSymbol("_dead", 1, 8, False),
            NativeSymbol("_main", 1, 0, True),
        ),
    )
    manifest = SemanticLayoutManifest.create(
        object_sha256=native_object_sha256(obj),
        entry="_main",
        roots=(),
        atoms=(
            SemanticAtom.create(
                name="_main",
                segname="__TEXT",
                sectname="__text",
                offset=0,
                size=8,
                align_log2=2,
                temperature="hot",
            ),
            SemanticAtom.create(
                name="_dead",
                segname="__TEXT",
                sectname="__text",
                offset=8,
                size=8,
                align_log2=2,
                temperature="cold",
                eliminable=True,
            ),
        ),
    )

    result = apply_semantic_layout(obj, manifest)
    packed_section = result.native_object.sections[1]
    packed = decode_stack_map(packed_section.data, expected_arch=ARCH_AARCH64)

    assert result.plan.dropped_atoms == ("_dead",)
    assert result.plan.packed_runtime_tables == ("__DATA,__pcc_stackmaps",)
    assert [item.function_id for item in packed.functions] == [
        function_id("_main")
    ]
    assert len(packed_section.relocations) == 1
    target = result.native_object.symbols[
        packed_section.relocations[0].symbol_index
    ]
    assert target.name == "_main"


def test_semantic_layout_refuses_a_version_3_runtime_table(monkeypatch):
    # Semantic layout re-encodes a version 2 table; a compact runtime table
    # is a final-link product it cannot rebuild, so both entry points refuse.
    from pcc.backend.macho_exec import prepare_executable_object
    from pcc.backend.macho_link import LinkError
    from pcc.backend.precise_stackmap import (
        PRECISE_STACKMAP_FORMAT_ENV,
        transcode_runtime_table,
    )

    stackmap_data = encode_stack_map(PreciseStackMap(
        ARCH_AARCH64, (FunctionStackMap(function_id("_main"), 0, 8, 0, ()),),
    ))
    runtime, remapped = transcode_runtime_table(
        stackmap_data,
        ((function_id("_main"), function_address_offsets(stackmap_data)[0]),),
    )
    obj = NativeObject(
        (
            NativeSection(
                "__TEXT", "__text", spec.S_REGULAR | spec.S_ATTR_PURE_INSTRUCTIONS,
                2, b"M" * 8,
            ),
            NativeSection(
                "__DATA", "__pcc_stackmaps", PCC_STACKMAP_SECTION_FLAGS, 3, runtime,
                relocations=(NativeRelocation(remapped[0][1], 0, spec.ARM64_RELOC_UNSIGNED, False, 3),),
            ),
        ),
        (NativeSymbol("_main", 1, 0, True),),
    )
    manifest = SemanticLayoutManifest.create(
        object_sha256=native_object_sha256(obj),
        entry="_main",
        roots=(),
        atoms=(SemanticAtom.create(
            name="_main", segname="__TEXT", sectname="__text", offset=0, size=8,
            align_log2=2, temperature="hot",
        ),),
    )
    with pytest.raises(SemanticLayoutError, match="version 3 runtime stack-map table"):
        apply_semantic_layout(obj, manifest)
    monkeypatch.setenv(PRECISE_STACKMAP_FORMAT_ENV, "3")
    with pytest.raises(LinkError, match="semantic layout cannot emit a version 3"):
        prepare_executable_object([obj], semantic_manifest=manifest)
