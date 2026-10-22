"""Independent bounded host parity for windowed PCO preparation.

These compare complete prepared state and final Mach-O bytes with the eager
owned route. They neither execute arm64 output nor qualify a native Stage1.
"""

import copy
import json
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import weakref

import pytest

from pcc.backend import (
    macho_exec,
    macho_host_validation as validation_boundary,
    macho_host_prepare as host,
    macho_link,
    macho_obj as obj,
    macho_spec as spec,
    native_object as native,
    precise_stackmap as maps,
)
from pcc.backend.ar_writer import write_archive
from tests.python.test_macho_archive_symbol_state import _unit
from tests.python.test_macho_section_placement import _inputs as _placement_inputs


def _budget():
    # Deliberately sufficient on each route: capacity belongs to the separate
    # windowed-preparation fixture, not this semantic differential.
    word = struct.calcsize("P")
    return maps.CompactStackMapBudget(
        8_000_000, 100_000, set_header_bytes=sys.getsizeof(set()),
        set_entry_bytes=2 * word, integer_bytes=sys.getsizeof((1 << 64) - 1),
        metadata_entry_bytes=64 * word + sys.getsizeof((0, 0, 0))
        + 3 * sys.getsizeof((1 << 64) - 1) + sys.getsizeof(b""),
        allocation_quantum=2 * word,
    )


def _write_pcos(tmp_path, objects):
    paths = []
    for index, value in enumerate(objects):
        path = tmp_path / (str(index) + ".pco")
        path.write_bytes(native.encode_native_object(value))
        paths.append(str(path))
    return paths


def _prepared_state(view):
    assert isinstance(view, native.OwnedMergedSourceView)
    return (
        view.sections(), view.symbols(),
        tuple(bytes(view.section_data(section)) for section in view.sections()),
        tuple(view.compact_relocation_rows(section) for section in view.sections()),
        tuple(tuple(view.iter_relocations(section, ordered=False))
              for section in view.sections()),
        tuple(tuple(view.iter_relocations(section)) for section in view.sections()),
    )


def _prepare_both(tmp_path, objects, *, archives=(), archive_mode="bytes"):
    paths = _write_pcos(tmp_path, objects)
    eager_budget, window_budget = _budget(), _budget()
    eager_inputs = [native.decode_packed_native_object(Path(path).read_bytes())
                    for path in paths]
    eager = macho_exec.prepare_executable_object(
        eager_inputs, archives=archives, _consume_inputs=True,
        _source_view=True, compact_budget=eager_budget,
    )
    assert eager_inputs == []
    archive_args = {"archives": archives}
    if archive_mode == "paths":
        archive_paths = []
        for index, data in enumerate(archives):
            path = tmp_path / (str(index) + ".a")
            path.write_bytes(data)
            archive_paths.append(str(path))
        archive_args = {"archive_paths": archive_paths}
    windowed = host.prepare_executable_from_paths(
        paths, compact_budget=window_budget, **archive_args,
    )
    eager_state, window_state = _prepared_state(eager), _prepared_state(windowed)
    assert window_state == eager_state
    assert eager_budget.retained_bytes == window_budget.retained_bytes == 0
    return eager, windowed, eager_budget, window_budget, eager_state


def _assert_image_parity(tmp_path, objects, **kwargs):
    eager, windowed, eager_budget, window_budget, state = _prepare_both(
        tmp_path, objects, **kwargs,
    )
    expected = macho_exec.link_prepared_executable(eager, compact_budget=eager_budget)
    actual = macho_exec.link_prepared_executable(windowed, compact_budget=window_budget)
    assert actual == expected
    assert spec.parse_object(actual).header["filetype"] == spec.MH_EXECUTE
    assert eager_budget.retained_bytes == window_budget.retained_bytes == 0
    return state, actual


@pytest.mark.parametrize("archive_mode", ("bytes", "paths"))
@pytest.mark.parametrize("reverse_explicit", (False, True))
def test_archive_backreferences_unused_members_and_weak_refs_match_eager(
    tmp_path, archive_mode, reverse_explicit,
):
    # Reuse the established archive-state fixture constructors. The earlier
    # member becomes needed only after a later member has been selected.
    inputs = [_unit("_main", ("_late", "_optional"), weak=("_optional",)),
              _unit("_explicit", ("_main",))]
    if reverse_explicit:
        inputs.reverse()
    archives = [write_archive([
        ("early.o", _unit("_early", ("_main",)).to_macho()),
        ("late.o", _unit("_late", ("_early", "_next")).to_macho()),
        ("duplicate.o", _unit("_main").to_macho()),
        ("optional.o", _unit("_optional", ("_unprovided",)).to_macho()),
        ("unused.o", _unit("_unused").to_macho()),
    ]), write_archive([
        ("duplicate-early.o", _unit("_early").to_macho()),
        ("next.o", _unit("_next", ("_early",)).to_macho()),
    ])]
    state, _image = _assert_image_parity(
        tmp_path, inputs, archives=archives, archive_mode=archive_mode,
    )
    symbols = {row["name"]: row for row in state[1]}
    assert {"_early", "_late", "_next"}.issubset(symbols)
    assert not {"_unprovided", "_unused"}.intersection(symbols)
    assert symbols["_optional"]["n_type"] & spec.N_TYPE == spec.N_UNDF
    assert symbols["_optional"]["n_desc"] & spec.N_WEAK_REF


def _local_collision_inputs():
    inputs = [
        _unit("_main", extra_symbols=(
            obj.TextSymbol("_shared", 0, external=False),
            obj.TextSymbol("Lrepeat", 0, external=False),
        )),
        _unit("_shared", extra_symbols=(
            obj.TextSymbol("Lrepeat", 0, external=False),
            obj.TextSymbol("_shared$link0", 0),
        )),
        _unit("_consumer", ("_shared", "_main", "_shared$link0")),
    ]
    for index, names in enumerate((("_shared", "Lrepeat"), ("Lrepeat",))):
        sections, undefined = inputs[index].to_sections()
        sections.append(obj.Section(
            "__data", "__DATA", data=b"\0" * (8 * len(names)), align_log2=3,
            relocations=tuple(obj.Relocation(
                offset * 8, name, spec.ARM64_RELOC_UNSIGNED, False, 3,
            ) for offset, name in enumerate(names)),
        ))
        inputs[index] = native.NativeObject.from_sections(sections, undefined=undefined)
    return inputs


@pytest.mark.parametrize("order", ((0, 1, 2), (2, 0, 1), (1, 2, 0)))
def test_local_collisions_external_reservations_and_input_order_match_eager(
    tmp_path, order,
):
    inputs = _local_collision_inputs()
    state, _image = _assert_image_parity(tmp_path, [inputs[index] for index in order])
    symbols = state[1]
    names = [row["name"] for row in symbols]
    assert len(names) == len(set(names))
    assert {"_shared", "_shared$link0"}.issubset(names)
    assert len([row for row in symbols if not row["n_type"] & spec.N_EXT]) == 3
    assert not any(row["n_type"] & spec.N_TYPE == spec.N_UNDF for row in symbols)


@pytest.mark.parametrize("chunk_limit", (1, 8, 64))
def test_inline_data_alignment_zerofill_and_text_chunks_match_eager(
    tmp_path, monkeypatch, chunk_limit,
):
    monkeypatch.setattr(macho_link, "_TEXT_CHUNK_BYTES", chunk_limit)
    state, _image = _assert_image_parity(tmp_path, _placement_inputs("native"))
    text = [section for section in state[0] if section["segname_str"] == "__TEXT"]
    assert [section["sectname_str"] for section in text] == (
        ["__text"] if chunk_limit == 64 else ["__text", "__text1"]
    )
    if chunk_limit == 64:
        payload = state[2][text[0]["_pcc_source_index"]]
        assert payload[8:16] == b"\x7f" + b"\0" * 7
    bss = next(section for section in state[0] if section["sectname_str"] == "__bss")
    assert bss["size"] == 14


def _stackmap_section(name):
    payload = maps.encode_stack_map(maps.PreciseStackMap(
        arch=maps.ARCH_AARCH64,
        functions=(maps.FunctionStackMap(
            function_id=maps.function_id(name), function_address=0,
            code_size=16, frame_size=32,
            records=(maps.SafepointRecord(
                safepoint_id=maps.safepoint_id(name, 0, maps.SAFEPOINT_ENTRY),
                instruction_offset=4,
                kind=maps.SAFEPOINT_ENTRY, locations=(),
            ),),
        ),),
    ))
    offset, = maps.function_address_offsets(payload)
    return obj.Section(
        "__pcc_stackmaps", "__DATA", data=payload,
        flags=obj.PCC_STACKMAP_SECTION_FLAGS, align_log2=3,
        relocations=(obj.Relocation(offset, name, spec.ARM64_RELOC_UNSIGNED, False, 3),),
    )


def _companion_inputs(*, live_subtractor=False):
    text = obj.Section(
        "__text", "__TEXT", data=struct.pack("<IIII", 0x90000009, 0x91000129,
                                             0x94000000, 0xD65F03C0),
        align_log2=2, flags=obj.TEXT_SECTION_FLAGS,
        symbols=(obj.TextSymbol("_main", 0), obj.TextSymbol("Ltail", 12, external=False)),
        # Storage order is intentionally neither ascending nor descending.
        relocations=(
            obj.Relocation(4, "_table", spec.ARM64_RELOC_PAGEOFF12, False, 2, 8),
            obj.Relocation(8, "_helper", spec.ARM64_RELOC_BRANCH26, True, 2),
            obj.Relocation(0, "_table", spec.ARM64_RELOC_PAGE21, True, 2, 8),
        ),
    )
    pointers = obj.Section(
        "__data", "__DATA", data=b"\0" * 32, align_log2=3,
        relocations=(
            obj.Relocation(24, "", spec.ARM64_RELOC_UNSIGNED, False, 3,
                           section=("__TEXT", "__text"), target_offset=12),
            obj.Relocation(0, "_helper", spec.ARM64_RELOC_UNSIGNED, False, 3),
            obj.Relocation(16, "Ltail", spec.ARM64_RELOC_UNSIGNED, False, 3),
        ),
    )
    differences = obj.Section(
        "__difference" if live_subtractor else "__eh_frame",
        "__DATA" if live_subtractor else "__TEXT",
        data=struct.pack("<qii", 7, 3, 0), align_log2=3,
        flags=obj.DATA_SECTION_FLAGS if live_subtractor else obj.EH_FRAME_SECTION_FLAGS,
        relocations=(
            obj.Relocation(8, "_main", spec.ARM64_RELOC_SUBTRACTOR, False, 2,
                           minuend="Ltail"),
            obj.Relocation(0, "_main", spec.ARM64_RELOC_SUBTRACTOR, False, 3,
                           minuend="_helper"),
        ),
    )
    helper = native.NativeObject.from_sections([
        obj.Section("__text", "__TEXT", data=b"\xc0\x03\x5f\xd6" * 4,
                    align_log2=4, flags=obj.TEXT_SECTION_FLAGS,
                    symbols=(obj.TextSymbol("_helper", 0),)),
        obj.Section("__const", "__TEXT", data=b"\x11" * 24,
                    symbols=(obj.TextSymbol("_table", 0),)),
        _stackmap_section("_helper"),
    ])
    main = native.NativeObject.from_sections(
        [text, pointers, differences, _stackmap_section("_main")],
        undefined=("_helper", "_table"),
    )
    return [main, helper]


def test_companion_rows_section_targets_and_stackmaps_match_eager(tmp_path):
    inputs = _companion_inputs()
    packed = native.decode_packed_native_object(native.encode_native_object(inputs[0]))
    assert packed.sections[0].relocation_order == 0
    assert [row[0] for row in native._packed_relocations_in_storage_order(
        packed, packed.sections[0],
    )] == [4, 8, 0]
    state, image = _assert_image_parity(tmp_path, inputs)
    text_index = next(index for index, section in enumerate(state[0])
                      if section["sectname_str"] == "__text")
    rows = list(native._FINAL_LINK_RELOCATION.iter_unpack(state[3][text_index]))
    assert [row[0] for row in rows] == [8, 4, 4, 0, 0]
    assert [row[-1] for row in rows] == [
        spec.ARM64_RELOC_BRANCH26, spec.ARM64_RELOC_ADDEND,
        spec.ARM64_RELOC_PAGEOFF12, spec.ARM64_RELOC_ADDEND, spec.ARM64_RELOC_PAGE21,
    ]
    difference_index = next(index for index, section in enumerate(state[0])
                            if section["sectname_str"] == "__eh_frame")
    differences = list(native._FINAL_LINK_RELOCATION.iter_unpack(state[3][difference_index]))
    assert [row[0] for row in differences] == [8, 8, 0, 0]
    assert [row[-1] for row in differences] == [
        spec.ARM64_RELOC_SUBTRACTOR, spec.ARM64_RELOC_UNSIGNED,
        spec.ARM64_RELOC_SUBTRACTOR, spec.ARM64_RELOC_UNSIGNED,
    ]
    assert all(row["r_extern"] for rows in state[4] for row in rows
               if row["r_type"] == spec.ARM64_RELOC_UNSIGNED)
    assert "__eh_frame" not in {section["sectname_str"]
                                for section in spec.parse_object(image).sections()}


def test_runtime_stack_map_format_matches_eager(tmp_path, monkeypatch):
    # Both final-link routes transcode the merged table to the version 3
    # runtime form when PCC_PRECISE_STACKMAP_FORMAT=3 selects it.
    monkeypatch.setenv(maps.PRECISE_STACKMAP_FORMAT_ENV, "3")
    _state, image = _assert_image_parity(tmp_path, _companion_inputs())
    table = next(
        image[section["offset"]:section["offset"] + section["size"]]
        for section in spec.parse_object(image).sections()
        if section["sectname_str"] == "__pcc_stackmaps"
    )
    assert int.from_bytes(table[8:10], "little") == maps.RUNTIME_TABLE_VERSION
    functions = maps.decode_runtime_table(table, expected_arch=maps.ARCH_AARCH64)
    assert sorted(function[0] for function in functions) == sorted(
        maps.function_id(name) for name in ("_main", "_helper")
    )
    assert all(function[1] != 0 for function in functions)


def test_live_subtractor_keeps_existing_final_link_rejection(tmp_path):
    eager, windowed, eager_budget, window_budget, _state = _prepare_both(
        tmp_path, _companion_inputs(live_subtractor=True),
    )
    errors = []
    for prepared, budget in ((eager, eager_budget), (windowed, window_budget)):
        with pytest.raises(macho_link.LinkError, match="relocation type 1 not applied") as error:
            macho_exec.link_prepared_executable(prepared, compact_budget=budget)
        errors.append(str(error.value))
    assert errors[0] == errors[1]


@pytest.mark.parametrize("symbol_at_target", (False, True))
def test_stackmap_growth_before_interior_zerofill_target_matches_eager(
    tmp_path, symbol_at_target,
):
    value = native.NativeObject.from_sections([
        obj.Section("__text", "__TEXT", data=b"\xc0\x03\x5f\xd6" * 4,
                    align_log2=2, flags=obj.TEXT_SECTION_FLAGS,
                    symbols=(obj.TextSymbol("_main", 0),)),
        obj.Section("__data", "__DATA", data=b"\0" * 8, align_log2=3,
                    relocations=(obj.Relocation(
                        0, "", spec.ARM64_RELOC_UNSIGNED, False, 3,
                        section=("__DATA", "__bss"), target_offset=8,
                    ),)),
        _stackmap_section("_main"),
        obj.Section("__bss", "__DATA", flags=obj.ZEROFILL_SECTION_FLAGS,
                    align_log2=3, zerofill_size=16,
                    symbols=(obj.TextSymbol("_interior", 8),) if symbol_at_target else ()),
    ])
    eager, windowed, eager_budget, window_budget, state = _prepare_both(tmp_path, [value])
    sections = {section["sectname_str"]: section for section in state[0]}
    assert sections["__bss"]["addr"] >= (
        sections["__pcc_stackmaps"]["addr"] + sections["__pcc_stackmaps"]["size"]
    )
    data = state[2][sections["__data"]["_pcc_source_index"]]
    pointer, = struct.unpack("<Q", data)
    assert pointer == (0 if symbol_at_target else sections["__bss"]["addr"] + 8)
    if symbol_at_target:
        expected = macho_exec.link_prepared_executable(eager, compact_budget=eager_budget)
        assert macho_exec.link_prepared_executable(windowed, compact_budget=window_budget) == expected
    else:
        # Preserve the existing final-link boundary without moving rejection
        # earlier into valid, reusable prepared-state construction.
        errors = []
        for prepared, budget in ((eager, eager_budget), (windowed, window_budget)):
            with pytest.raises(macho_link.LinkError, match="must be normalized to a defined symbol") as error:
                macho_exec.link_prepared_executable(prepared, compact_budget=budget)
            errors.append(str(error.value))
        assert errors[0] == errors[1]


@pytest.mark.parametrize("manifest_input", (False, True))
@pytest.mark.parametrize("runtime_archive", (False, True))
def test_budgeted_all_pco_pipeline_uses_windowed_route_and_preserves_image(
    tmp_path, monkeypatch, manifest_input, runtime_archive,
):
    from pcc.backend import stackmap_host_budget
    from pcc.frontends.python import pipeline_self_backend_link as pipeline

    inputs = [_unit("_helper"), _unit("_main", ("_helper",))]
    archives = []
    archive_path = None
    if runtime_archive:
        inputs[1] = _unit("_main", ("_helper", "_runtime"))
        archives = [write_archive([("runtime.o", _unit("_runtime").to_macho())])]
        archive_path = tmp_path / "runtime.a"
        archive_path.write_bytes(archives[0])
    paths = _write_pcos(tmp_path, inputs)
    expected = macho_exec.link_executable(inputs, archives=archives)
    manifest = tmp_path / "inputs.txt"
    manifest.write_text("pcc.macho-internal-inputs.v1\n2\n"
                        + "".join("PCO\t" + path + "\n" for path in paths))
    output, profile = tmp_path / "program", tmp_path / "profile.json"
    calls = []
    prepare = host.prepare_executable_from_paths

    def checked_prepare(selected_paths, **kwargs):
        assert list(selected_paths) == paths
        assert kwargs["compact_budget"] is not None
        assert list(kwargs.get("archive_paths", ())) == (
            [str(archive_path)] if runtime_archive else []
        )
        assert not kwargs.get("archives")
        calls.append(True)
        return prepare(selected_paths, **kwargs)

    def forbidden(*_args, **_kwargs):
        pytest.fail("eligible all-PCO pipeline used eager preparation or external delegation")

    monkeypatch.setattr(stackmap_host_budget, "host_compact_stackmap_budget", _budget)
    monkeypatch.setattr(host, "prepare_executable_from_paths", checked_prepare)
    monkeypatch.setattr(macho_exec, "prepare_executable_object", forbidden)
    monkeypatch.setattr(pipeline.subprocess, "run", forbidden)
    pipeline._owned_macho_link_in_process(
        asm_path=None, pcc_asm_inputs=(),
        pcc_native_object_inputs=() if manifest_input else tuple(paths),
        pcc_internal_input_manifest=str(manifest) if manifest_input else None,
        runtime_archive=str(archive_path) if runtime_archive else None,
        extra_link_inputs=(), tmp_out_path=str(output), link_profile_path=str(profile),
    )
    assert calls == [True]
    assert output.read_bytes() == expected
    receipt = json.loads(profile.read_text())
    assert receipt["inputs"] == {
        "asm": 0, "native_object": 2, "object": 0, "archive": int(runtime_archive),
    }


@pytest.mark.parametrize("alias", ("source", "payload", "snapshot"))
def test_owned_output_hot_calls_keep_exact_reference_boundaries(
    tmp_path, monkeypatch, alias,
):
    from pcc.backend import macho_host_output as output

    value = native.NativeObject.from_sections([
        obj.Section("__text", "__TEXT", data=b"\xc0\x03\x5f\xd6" * 4,
                    align_log2=2, flags=obj.TEXT_SECTION_FLAGS,
                    symbols=(obj.TextSymbol("_main", 0),)),
        _stackmap_section("_main"),
    ])
    paths = _write_pcos(tmp_path, [value])
    expected = macho_exec.link_executable([value])
    validate = validation_boundary.validate_stack_map_payload
    escaped = []
    escape_snapshot = False

    def checked_validate(payload, **kwargs):
        result = validate(payload, **kwargs)
        if escape_snapshot and kwargs.get("final_image"):
            escaped.append(payload)
        return result

    def reject_retained_source(source, budget):
        before = copy.deepcopy(_prepared_state(source))
        with pytest.raises(macho_link.LinkError, match="exclusively transferred"):
            output.link_owned_executable(source, compact_budget=budget)
        # Keep the caller's parameter live after the call: the callee may
        # borrow it, but must still detect this external owner before mutation.
        assert _prepared_state(source) == before

    monkeypatch.setattr(validation_boundary, "validate_stack_map_payload", checked_validate)
    # Exercise the same sites before and after adaptive specialization. In
    # CPython 3.14+ LOAD_FAST_BORROW must not turn an external owner into an
    # apparently exclusive source, or reject a legitimately transferred one.
    for _ in range(64):
        budget = _budget()
        pending = [host.prepare_executable_from_paths(paths, compact_budget=budget)]
        source_ref = weakref.ref(pending[0])
        # Keep the consuming call outside assertion rewriting, which would
        # retain pending.pop() in a diagnostic temporary across the call.
        actual = output.link_owned_executable(pending.pop(), compact_budget=budget)
        assert actual == expected
        assert source_ref() is None
        assert not escaped

        budget = _budget()
        pending = [host.prepare_executable_from_paths(paths, compact_budget=budget)]
        if alias == "source":
            reject_retained_source(pending.pop(), budget)
        elif alias == "payload":
            payload = pending[0]._payloads[0]
            before = bytes(memoryview(payload))
            with pytest.raises(macho_link.LinkError, match="escaped bytes alias"):
                output.link_owned_executable(pending.pop(), compact_budget=budget)
            assert payload == before
            payload = None
        else:
            escape_snapshot = True
            try:
                with pytest.raises(macho_link.LinkError, match="snapshot alias"):
                    output.link_owned_executable(pending.pop(), compact_budget=budget)
            finally:
                escape_snapshot = False
            assert len(escaped) == 1
            validate(escaped[0], final_image=True, compact_budget=_budget())
            escaped.clear()


@pytest.mark.parametrize("surface", ("no-budget", "native", "asm", "mixed", "extras"))
def test_ineligible_pipeline_keeps_existing_owned_route(tmp_path, monkeypatch, surface):
    from pcc.backend import stackmap_host_budget
    from pcc.frontends.python import pipeline_self_backend_link as pipeline

    paths = _write_pcos(tmp_path, [_unit("_main")])
    assembly = tmp_path / "helper.s"
    assembly.write_text(".section __TEXT,__text,regular,pure_instructions\n"
                        ".globl _helper\n_helper:\n ret\n")
    extra = tmp_path / "extra.o"
    extra.write_bytes(_unit("_extra").to_macho())
    manifest = tmp_path / "mixed.txt"
    manifest.write_text("pcc.macho-internal-inputs.v1\n2\nPCO\t" + paths[0]
                        + "\nASM\t" + str(assembly) + "\n")
    if surface == "asm":
        assembly.write_text(assembly.read_text().replace("_helper", "_main"))
    expected_objects = [_unit("_main")]
    if surface == "mixed":
        expected_objects.append(_unit("_helper"))
    elif surface == "extras":
        expected_objects.append(_unit("_extra"))
    expected = macho_exec.link_executable(expected_objects)
    output = tmp_path / "program"
    calls = []
    eager = macho_exec.prepare_executable_object

    def checked_eager(objects, **kwargs):
        assert kwargs.get("_consume_inputs") is True
        assert kwargs.get("_source_view") is True
        result = eager(objects, **kwargs)
        assert objects == []
        calls.append(True)
        return result

    def forbidden(*_args, **_kwargs):
        pytest.fail("ineligible pipeline entered the CPython-only windowed route")

    monkeypatch.setattr(stackmap_host_budget, "host_compact_stackmap_budget",
                        (lambda: None) if surface == "no-budget" else _budget)
    monkeypatch.setattr(host, "prepare_executable_from_paths", forbidden)
    monkeypatch.setattr(macho_exec, "prepare_executable_object", checked_eager)
    with monkeypatch.context() as patch:
        if surface == "native":
            patch.setattr(sys, "implementation", SimpleNamespace(
                **(vars(sys.implementation) | {"name": "pcc"}),
            ))
        pipeline._owned_macho_link_in_process(
            asm_path=str(assembly) if surface == "asm" else None,
            pcc_asm_inputs=(),
            pcc_native_object_inputs=() if surface in ("asm", "mixed") else tuple(paths),
            pcc_internal_input_manifest=str(manifest) if surface == "mixed" else None,
            runtime_archive=None,
            extra_link_inputs=(str(extra),) if surface == "extras" else (),
            tmp_out_path=str(output),
        )
    assert calls == [True]
    assert output.read_bytes() == expected


@pytest.mark.parametrize("module_name", (
    "pcc.backend.native_object", "pcc.frontends.python.pipeline_self_backend_link",
))
def test_windowed_adapter_stays_out_of_native_import_closure(module_name):
    from importlib import import_module
    from pcc.frontends.python.pipeline_dependency_closure import (
        _package_import_targets,
    )
    from pcc.frontends.python.pipeline_import_scan import (
        _without_inactive_implementation_imports,
    )

    module = import_module(module_name)
    path = Path(module.__file__)
    source = path.read_text()
    marker = "from .macho_host_prepare import" if module is native else (
        "from pcc.backend.macho_host_prepare import"
    )
    assert marker in source
    assert marker not in _without_inactive_implementation_imports(source, implementation="pcc")
    assert marker in _without_inactive_implementation_imports(source, implementation="cpython")
    root = Path(__file__).resolve().parents[2]
    dependencies = {name for _path, name in _package_import_targets(
        str(path), module_name, root_dir=str(root),
    )}
    assert "pcc.backend.macho_host_prepare" not in dependencies


def test_compact_factory_rejects_negative_alignment_before_layout():
    section = obj.Section("__text", "__TEXT", data=b"\xc0\x03\x5f\xd6",
                          align_log2=-1, flags=obj.TEXT_SECTION_FLAGS,
                          symbols=(obj.TextSymbol("_main", 0),))
    with pytest.raises(native.NativeObjectError, match="align"):
        native.OwnedMergedSourceView([section], _compact_rows=[b""], compact_budget=_budget())


def test_compact_factory_rejects_pcrel_subtractor_first_companion():
    sections = [
        obj.Section("__text", "__TEXT", data=b"\xc0\x03\x5f\xd6" * 2,
                    align_log2=2, flags=obj.TEXT_SECTION_FLAGS,
                    symbols=(obj.TextSymbol("_main", 0), obj.TextSymbol("_tail", 4))),
        obj.Section("__data", "__DATA", data=b"\0" * 8),
    ]
    rows = b"".join(native._FINAL_LINK_RELOCATION.pack(*row) for row in (
        (0, 0, 1, 3, 1, spec.ARM64_RELOC_SUBTRACTOR),
        (0, 1, 0, 3, 1, spec.ARM64_RELOC_UNSIGNED),
    ))
    with pytest.raises((native.NativeObjectError, macho_link.LinkError), match="SUBTRACTOR|pcrel"):
        native.OwnedMergedSourceView(sections, _compact_rows=[b"", rows], compact_budget=_budget())
