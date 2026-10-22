"""Host-only fixup ownership; complete bytes are checked, not arm64 execution."""

import re
import struct
import sys
import weakref

import pytest

from pcc.backend import (
    macho_codesign as codesign,
    macho_exec,
    macho_host_output as output,
    macho_host_prepare as host,
    macho_obj as obj,
    macho_spec as spec,
    native_object as native,
)
from tests.python.test_macho_owned_output import _tracked_buffers
from tests.python.test_macho_windowed_prepare import _budget


_SITE_LISTS = ("tlv_bind_sites", "data_bind_sites", "rebase_sites")


def _fixup_input(tmp_path, *, count=8, tlv=True, fault=None):
    def relocation(offset, symbol):
        return obj.Relocation(offset, symbol, spec.ARM64_RELOC_UNSIGNED, False, 3)

    sections = [obj.Section(
        "__text", "__TEXT", data=struct.pack("<II", 0x94000000, 0xD65F03C0),
        align_log2=2, flags=obj.TEXT_SECTION_FLAGS,
        symbols=(obj.TextSymbol("_main", 0),),
        relocations=(obj.Relocation(0, "_puts", spec.ARM64_RELOC_BRANCH26, True, 2),),
    )]
    for segment, section in (("__DATA", "__const"), ("__DATA", "__data")):
        data = bytearray(8 * (count + 1))
        struct.pack_into("<q", data, 0, 7 if section == "__const" else 255)
        relocations = [relocation(0, "_puts")]
        relocations.extend(relocation(8 * (index + 1), "_main") for index in range(count))
        if section == "__data":
            if fault == "addend":
                struct.pack_into("<q", data, 0, 256)
            elif fault == "high-rebase":
                struct.pack_into("<q", data, 8, 1 << 36)
            elif fault == "negative-rebase":
                struct.pack_into("<q", data, 8, -(1 << 35))
            elif fault == "unaligned":
                relocations = [relocation(2, "_main")]
            elif fault == "page-crossing":
                data = bytearray(macho_exec.PAGE + 4)
                # Descriptors precede ordinary DATA; place this pointer four
                # bytes before the actual page end after both prefix sections.
                prefix = 8 * (count + 1) + (3 * 24 if tlv else 0)
                relocations = [relocation(macho_exec.PAGE - 4 - prefix, "_main")]
        sections.append(obj.Section(
            section, segment, data=bytes(data), align_log2=3,
            flags=obj.DATA_SECTION_FLAGS, relocations=tuple(relocations),
        ))
    if tlv:
        sections.extend([
            obj.Section(
                "__thread_data", "__DATA", data=b"\x05" + b"\0" * 7,
                align_log2=3, flags=0x11,
                symbols=(obj.TextSymbol("_thread_value", 0),),
            ),
            obj.Section(
                "__thread_vars", "__DATA", data=b"\0" * (3 * 24),
                align_log2=3, flags=0x13,
                relocations=tuple(
                    relocation(index * 24 + offset, name)
                    for index in range(3)
                    for offset, name in ((0, "__tlv_bootstrap"), (16, "_thread_value"))
                ),
            ),
        ])
    imports = ("_puts", "__tlv_bootstrap") if tlv else ("_puts",)
    # Weak references exercise the real bind encoding on any host without
    # claiming that Linux can verify or execute Darwin's libSystem exports.
    value = native.NativeObject.from_sections(
        sections, undefined=imports, weak_undefined=imports,
    )
    path = tmp_path / "fixups.pco"
    path.write_bytes(native.encode_native_object(value))
    return [str(path)]


def _link(paths, mode, budget=None, fault=None):
    budget = _budget(100_000_000) if budget is None else budget
    pending = [host.prepare_executable_from_paths(paths, compact_budget=budget)]
    if fault and fault.startswith("duplicate-"):
        # The authoring API rejects duplicate requests. Inject a private final
        # row to exercise the final chained-site defense and its error order.
        _duplicate_row(pending[0], fault)
    if mode == "public":
        return macho_exec.link_prepared_executable(pending.pop(), compact_budget=budget)
    return output.link_owned_executable(pending.pop(), compact_budget=budget)


def _duplicate_row(source, fault):
    section = next(section for section in source.sections()
                   if section["sectname_str"] == "__data")
    index = section["_pcc_source_index"]
    rows = source._raw_relocations[index]
    offset = 0 if fault == "duplicate-bind" else 8
    row = next(rows[start:start + 12] for start in range(0, len(rows), 12)
               if struct.unpack_from("<I", rows, start)[0] == offset)
    if fault == "duplicate-mixed":
        row = struct.pack("<I", 0) + row[4:]
    source._raw_relocations[index] = rows + row


@pytest.mark.parametrize("tlv", (False, True))
@pytest.mark.parametrize("count", (8, 4096))
def test_dense_fixup_bytes_signature_and_tlv_flag_match_public(tmp_path, tlv, count):
    paths = _fixup_input(tmp_path, count=count, tlv=tlv)
    expected = _link(paths, "public")
    actual = _link(paths, "owned")
    assert actual == expected
    parsed = spec.parse_object(actual)
    assert bool(parsed.header["flags"] & 0x00800000) is tlv
    signature = codesign.parse_signature(actual)
    assert codesign.build_signature(
        actual[:signature.dataoff], identifier=signature.identifier,
        exec_seg_base=signature.exec_seg_base,
        exec_seg_limit=signature.exec_seg_limit,
        exec_seg_flags=signature.exec_seg_flags,
    ) == actual[signature.dataoff:]
    if count == 4096:
        for section in parsed.sections():
            if section["sectname_str"] in ("__data", "__const"):
                assert section["size"] > 2 * macho_exec.PAGE


def _observe_conversions(monkeypatch, callback):
    getattribute = macho_exec._Out.__getattribute__

    def observed(self, name):
        if name == "addr":
            frame = sys._getframe(1)
            if frame.f_code is macho_exec._prepare_executable_image.__code__:
                values = frame.f_locals
                if (values.get("_owned_output") is not None
                        and "has_tlv_bind_sites" in values and "seg_vm" not in values):
                    for site_name in _SITE_LISTS:
                        sites = values[site_name]
                        if sites:
                            index = values["index"]
                            callback(site_name, sites, index)
                            break
        return getattribute(self, name)

    monkeypatch.setattr(macho_exec._Out, "__getattribute__", observed)


def test_old_slots_retire_during_conversion_and_segments_before_later_work(tmp_path, monkeypatch):
    paths = _fixup_input(tmp_path, count=8)
    expected = _link(paths, "public")
    conversions = {name: [] for name in _SITE_LISTS}
    segments, final = [], []

    def conversion(name, sites, index):
        assert all(site is None for site in sites[:index])
        assert all(type(site) is tuple for site in sites[index:])
        # The old tuple has no retained entry/enumerate alias after unpacking.
        references = sys.getrefcount([sites[index]].pop())
        assert references == 2
        # All earlier source slots were retired before this next replacement
        # is made. Thus appending one replacement can retain only N + 1 site
        # tuples, not both complete N-sized source and segment graphs.
        values = sys._getframe(1).f_locals["values"]
        state = values["_owned_output"]
        lists = [values[key] for key in _SITE_LISTS]
        lists.extend(values["seg_fixups"].values())
        live = [site for group in lists for site in group if site is not None]
        assert len(live) == len({id(site) for site in live}) == state.fixup_count
        integers = {id(value): value for site in live for value in site
                    if type(value) is int}
        assert len(integers) <= 3 * state.fixup_count
        measured = sum(state.budget._rounded(sys.getsizeof(value))
                       for value in [*lists, *live, *integers.values()])
        assert measured < state.fixup_raw
        conversions[name].append(index)

    _observe_conversions(monkeypatch, conversion)
    pack = macho_exec.struct.pack

    def packed(format, *values):
        frame = sys._getframe(1)
        if format == "<IHHQIH" and frame.f_code.co_name == "_build_seg_info":
            parent = frame.f_back.f_locals
            assert all(parent[name] == [] for name in _SITE_LISTS)
            assert all(parent["seg_fixups"][key] == [] for key in parent["seg_infos"])
            assert parent["fixups"]
            segments.append(parent["seg_index"])
        elif format == "<7I" and frame.f_code is macho_exec._prepare_executable_image.__code__:
            parent = frame.f_locals
            assert parent["seg_fixups"] == {}
            assert parent["fixups"] is None
            final.append(True)
        return pack(format, *values)

    monkeypatch.setattr(macho_exec.struct, "pack", packed)
    actual = _link(paths, "owned")
    assert actual == expected
    assert conversions == {
        "tlv_bind_sites": list(range(3)),
        "data_bind_sites": list(range(2)),
        "rebase_sites": list(range(16)),
    }
    assert segments == [2, 3]
    assert final == [True]


@pytest.mark.parametrize("fault, message", (
    ("addend", "does not fit a chained bind"),
    ("high-rebase", "exceeds 36 bits"),
    ("negative-rebase", "format requires"),
    ("duplicate-bind", "duplicate chained-fixup site"),
    ("duplicate-rebase", "duplicate chained-fixup site"),
    ("duplicate-mixed", "duplicate chained-fixup site"),
    ("unaligned", "four-byte-aligned pointer"),
    ("page-crossing", "crosses a 0x4000-byte page boundary"),
))
def test_fixup_error_text_and_duplicate_semantics_match_public(tmp_path, fault, message):
    paths = _fixup_input(tmp_path, fault=fault)
    errors = []
    for mode in ("public", "owned"):
        error = struct.error if fault == "negative-rebase" else macho_exec.LinkError
        with pytest.raises(error, match=message) as caught:
            _link(paths, mode, fault=fault)
        errors.append((type(caught.value), str(caught.value)))
    assert errors[0] == errors[1]


@pytest.mark.parametrize("failure", (*_SITE_LISTS, "first-segment", "second-segment"))
def test_retained_fixup_exceptions_release_sources_and_backings(tmp_path, monkeypatch, failure):
    paths = _fixup_input(tmp_path)
    budget = _budget(100_000_000)
    budget.retained_bytes = 137
    pending = [host.prepare_executable_from_paths(paths, compact_budget=budget)]
    source = weakref.ref(pending[0])
    owners = _tracked_buffers(monkeypatch)
    observed, published = [], []

    def conversion(name, sites, index):
        if name == failure and index == 1:
            assert sites[0] is None
            assert type(sites[index]) is tuple
            observed.append(name)
            raise ValueError("deliberate fixup conversion failure")

    _observe_conversions(monkeypatch, conversion)
    pack = macho_exec.struct.pack
    segment_count = 0

    def packed(format, *values):
        nonlocal segment_count
        if format == "<IHHQIH" and sys._getframe(1).f_code.co_name == "_build_seg_info":
            segment_count += 1
            if failure == ("first-segment" if segment_count == 1 else "second-segment"):
                observed.append(failure)
                raise ValueError("deliberate fixup segment failure")
        return pack(format, *values)

    monkeypatch.setattr(macho_exec.struct, "pack", packed)
    with pytest.raises(ValueError, match="deliberate fixup") as retained:
        output.link_owned_executable(
            pending.pop(), compact_budget=budget, phase_callback=published.append,
        )
    assert retained.value is not None
    assert observed == [failure]
    assert published == []
    assert source() is None
    assert owners and all(owner.closed for owner in owners)
    assert budget.retained_bytes == 137


def test_empty_fixup_tables_keep_public_image(tmp_path):
    value = native.NativeObject.from_sections([obj.Section(
        "__text", "__TEXT", data=struct.pack("<I", 0xD65F03C0),
        align_log2=2, flags=obj.TEXT_SECTION_FLAGS,
        symbols=(obj.TextSymbol("_main", 0),),
    )])
    path = tmp_path / "empty.pco"
    path.write_bytes(native.encode_native_object(value))
    paths = [str(path)]
    assert _link(paths, "owned") == _link(paths, "public")


def test_invalid_import_ordinal_keeps_public_validation_order(tmp_path, monkeypatch):
    paths = _fixup_input(tmp_path)
    getattribute = macho_exec._Out.__getattribute__

    def invalid_ordinal(self, name):
        if name == "addr":
            frame = sys._getframe(1)
            if frame.f_code is macho_exec._prepare_executable_image.__code__:
                values = frame.f_locals
                if "import_ordinal" in values and "seg_fixups" not in values:
                    values["import_ordinal"]["_puts"] = 1 << 24
        return getattribute(self, name)

    monkeypatch.setattr(macho_exec._Out, "__getattribute__", invalid_ordinal)
    errors = []
    for mode in ("public", "owned"):
        with pytest.raises(macho_exec.LinkError, match="chained-import ordinal 16777216 exceeds 24 bits") as caught:
            _link(paths, mode)
        errors.append(str(caught.value))
    assert errors[0] == errors[1]


def test_native_ir_omits_host_only_none_slots_before_tuple_iteration():
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.type_infer import infer_module
    from pcc.frontends.python.codegen.layer1 import L1CodeGen

    source = '''def run(owner):
    sites: list[tuple] = [(7, 11, 13), (17, 19, 23)]
    has_sites = bool(sites)
    import sys as execution_runtime
    if execution_runtime.implementation.name == "cpython":
        if owner is not None:
            from unavailable_host_adapter import _host_fixup_tuple_retired
            for index in range(len(sites)):
                first, second, third = sites[index]
                _host_fixup_tuple_retired(first, second, third)
                sites[index] = None
            sites.clear()
    total = 0
    for first, second, third in sites:
        total += first + second + third
    if has_sites:
        return total
    return 0
'''
    module = infer_module(parse_and_lift(source, "fixup_boundary.py", "fixup_boundary"))
    generator = L1CodeGen(module, emit_cpy_main_exitcode=False, ir_scaffold_mode="on")
    generator._strict_no_libpython = True
    result = str(generator.generate())
    assert "unavailable_host_adapter" not in result
    definition = re.search(r"^define[^\n]*@user_fixup_boundary_run\([^\n]*\) \{", result, re.MULTILINE)
    assert definition is not None
    body = result[definition.start():].split("\n}\n", 1)[0]
    assert not re.search(r"\bcall[^\n]*_host_fixup_tuple_retired", body)
    assert "@py_cpy_" not in body
    assert "@py_list_setitem" not in body
    assert "@py_list_clear" not in body
    assert "@py_list_len" in body
    assert "@py_list_get" in body
    assert "@py_obj_add" in body
    assert "@py_obj_truthy" in body


def test_native_ir_omits_private_segment_encoder_and_host_only_continue():
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.type_infer import infer_module
    from pcc.frontends.python.codegen.layer1 import L1CodeGen

    source = '''def ordinary(sites):
    return sites[0] + 1

def run(owner):
    total = 0
    for index in range(2):
        sites: list[int] = [index + 2]
        import sys as execution_runtime
        if execution_runtime.implementation.name == "cpython":
            if owner is not None:
                total += owner._build_seg_info(sites, 5, 7, 11)
                sites.clear()
                continue
        total += ordinary(sites)
    return total
'''
    module = infer_module(parse_and_lift(source, "segment_boundary.py", "segment_boundary"))
    generator = L1CodeGen(module, emit_cpy_main_exitcode=False, ir_scaffold_mode="on")
    generator._strict_no_libpython = True
    result = str(generator.generate())
    definition = re.search(r"^define[^\n]*@user_segment_boundary_run\([^\n]*\) \{", result, re.MULTILINE)
    assert definition is not None
    body = result[definition.start():].split("\n}\n", 1)[0]
    assert "_build_seg_info" not in result
    assert "@py_list_clear" not in body
    assert "@py_cpy_" not in body
    assert re.search(r"\bcall[^\n]*@user_segment_boundary_ordinary\(", body)
