"""Finite real ELF image/error/lifetime differentials for normal existing CI.

No compiler or executable invocation, and no new timing experiment. The frozen
oracle includes current classification/progress behavior before application
projection removal. Host images do not establish native Stage1 performance.
"""

from dataclasses import replace
import struct
import weakref

from pcc.backend import elf_x86_64 as elf
from pcc.backend.owned_elf_inputs import ElfInputStore
from tests.elf_packed_application_preimage import original_linker
from tests.python.test_elf_packed_link_classification import (
    BASE, TARGET, _exception_chain, _fixture, _outcome, _pack, _packed_object,
)


class _Timing:
    phase = ""
    def begin(self, phase):
        self.phase = phase
        return 0
    def complete(self, phase, started):
        pass
    def elf_classification_skips(self, *values):
        return True
    def elf_relocation_progress(self, *values):
        return True


def _single(kind, *, addend=0, value=None, tls=False, word=0x94000000):
    if value is None:
        value = BASE + 0x1000
    sections = [elf.ElfSection(".text", 1, 6, 16,
        struct.pack("<II", word, 0xD65F03C0),
        relocations=(elf.ElfRelocation(0, 2, kind, addend),))]
    if tls:
        sections.append(elf.ElfSection(".tdata", 1, 3 | elf.SHF_TLS, 16, bytes(16)))
    return elf.ElfObject(tuple(sections), (
        elf.ElfSymbol.null(), elf.ElfSymbol("_start", 1, 0, 8, 1, elf.STT_FUNC),
        elf.ElfSymbol("target", 2 if tls else elf.SHN_ABS, 0 if tls else value,
                      0, 1, elf.STT_TLS if tls else elf.STT_FUNC)), elf.EM_AARCH64)


def _packed(obj):
    for section in obj.sections:
        object.__setattr__(section, "relocations", elf._PackedElfRelocations(_pack(tuple(
            (row.offset, row.symbol_index, row.type, row.addend) for row in section.relocations))))
    return obj


def test_packed_application_all_arm_families_match_tuple_parse_and_stage(tmp_path):
    kinds = (0, 257, 258, 260, 261, 274, 275, 277, 278, 282, 283,
             284, 285, 286, 299, 311, 312, 541, 542, 549, 551)
    assert len(kinds) == 21
    for kind in kinds:
        obj = _single(kind, tls=kind in (541, 542, 549, 551))
        encoded = elf.emit_relocatable(obj)
        path = tmp_path / (str(kind) + ".o")
        path.write_bytes(encoded)
        store = ElfInputStore(TARGET)
        staged = store.stage(str(path))
        compact = elf.parse_relocatable(encoded, compact_relocations=True)
        expected = _outcome(original_linker(elf), obj)
        assert expected[0] == "image", (kind, expected)
        for owner, options in ((obj, {}), (compact, {}), (staged, {"_input_store": store})):
            assert _outcome(original_linker(elf), owner, **options) == expected
            assert _outcome(elf._link_static_executable_image, owner, **options) == expected
        assert elf.parse_static_executable(expected[1])["tls_segments"] == int(kind in (541, 542, 549, 551))
        assert path.read_bytes() == encoded
        store.verify()


def test_packed_application_removes_exactly_one_projection_per_visited_row(tmp_path, monkeypatch):
    obj, text_rows, data_rows = _fixture()
    path = tmp_path / "input.o"
    path.write_bytes(elf.emit_relocatable(obj))
    store = ElfInputStore(TARGET)
    staged = store.stage(str(path))
    rows = len(text_rows) + len(data_rows)
    assert rows == 6 and sum(section.relocation_count for section in staged.sections) == rows
    constructor = elf.ElfRelocation.__init__
    calls = []
    def counted(self, *args, **kwargs):
        calls.append(args)
        constructor(self, *args, **kwargs)
    monkeypatch.setattr(elf.ElfRelocation, "__init__", counted)
    expected = _outcome(original_linker(elf), staged, _input_store=store)
    prior = len(calls)
    calls.clear()
    actual = _outcome(elf._link_static_executable_image, staged, _input_store=store)
    assert actual == expected and actual[0] == "image"
    assert (prior, len(calls)) == (6, 0)
    # This finite staged domain includes NONE. It is not a census of the
    # interrupted production input's packed or actually applied row counts.
    store.verify()


def test_packed_application_range_alignment_opcode_and_first_errors_match():
    cases = (
        _single(257, addend=-(1 << 63), value=0),
        _single(258, value=1 << 32),
        _single(260, addend=-(1 << 63), value=0),
        _single(261, value=1 << 40),
        _single(274, value=1 << 40),
        _single(275, value=1 << 48),
        _single(282, value=1 << 40),
        _single(283, value=1 << 40, word=0x14000000),
        _single(283, value=BASE + 0x1002),
        _single(286, value=BASE + 0x1001),
        _single(549, addend=1 << 24, tls=True),
        _single(551, addend=-100, tls=True),
    )
    for obj in cases:
        owner = _packed(obj)
        expected = _outcome(original_linker(elf), owner)
        assert expected[0] != "image"
        assert _outcome(elf._link_static_executable_image, owner) == expected
    for rows in (((0, 2, 283, 2), (4, 99, 283, 0)),
                 ((0, 99, 283, 0), (4, 2, 283, 2)),
                 ((0, 99, 311, 0), (4, 2, 283, 2)),
                 ((1, 2, 274, 0),), ((1 << 32, 2, 257, 0),),
                 ((0, 2, 999, 0),)):
        owner = _packed_object(rows)
        expected = _outcome(original_linker(elf), owner)
        assert expected[0] != "image"
        assert _outcome(elf._link_static_executable_image, owner) == expected


def test_packed_application_symbol_and_tls_boundaries_match():
    # Distinct records with equal names/addresses must retain their own input
    # identities. Include local, defined global, weak-zero, ABS, and boundary.
    for section_index, binding, name, symbol_type in (
        (1, elf.STB_LOCAL, "local", elf.STT_FUNC),
        (1, elf.STB_GLOBAL, "target", elf.STT_FUNC),
        (elf.SHN_UNDEF, elf.STB_WEAK, "optional", elf.STT_NOTYPE),
        (elf.SHN_ABS, elf.STB_GLOBAL, "target", elf.STT_FUNC),
        (elf.SHN_UNDEF, elf.STB_GLOBAL, "__init_array_start", elf.STT_NOTYPE),
    ):
        obj = _single(257, addend=0)
        target = elf.ElfSymbol(name, section_index, 0, 0, binding, symbol_type)
        symbols = ((elf.ElfSymbol.null(), target, obj.symbols[1]) if binding == elf.STB_LOCAL
                   else (elf.ElfSymbol.null(), obj.symbols[1], target))
        index = 1 if binding == elf.STB_LOCAL else 2
        section = replace(obj.sections[0], relocations=(elf.ElfRelocation(0, index, 257, 0),))
        owner = _packed(elf.ElfObject((section,), symbols, elf.EM_AARCH64))
        expected = _outcome(original_linker(elf), owner)
        assert expected[0] == "image", expected
        assert _outcome(elf._link_static_executable_image, owner) == expected
    # Undefined global references resolve through the selected definition;
    # defined weak/global symbols with the same name still resolve themselves.
    for defined in (False, True):
        obj = _single(257)
        reference = replace(obj.symbols[2], section_index=elf.SHN_ABS if defined else elf.SHN_UNDEF,
                            value=17 if defined else 0, binding=elf.STB_WEAK if defined else elf.STB_GLOBAL)
        object.__setattr__(obj, "symbols", obj.symbols[:2] + (reference,))
        other = elf.ElfObject((), (elf.ElfSymbol.null(),
            elf.ElfSymbol("target", elf.SHN_ABS, 23, 0, elf.STB_GLOBAL, elf.STT_FUNC)), elf.EM_AARCH64)
        owner = _packed(obj)
        expected = bytes(original_linker(elf)([owner, other], base_address=BASE))
        assert bytes(elf._link_static_executable_image([owner, other], base_address=BASE)) == expected
    for kind, tls, target_type in ((311, True, elf.STT_TLS), (541, False, elf.STT_FUNC),
                                  (549, False, elf.STT_FUNC), (549, False, elf.STT_TLS)):
        obj = _single(kind, tls=tls)
        object.__setattr__(obj, "symbols", obj.symbols[:2] + (replace(obj.symbols[2], type=target_type),))
        owner = _packed(obj)
        expected = _outcome(original_linker(elf), owner)
        assert expected[0] != "image"
        assert _outcome(elf._link_static_executable_image, owner) == expected


def test_packed_application_fallback_reads_and_row_destructors_match():
    def run(link, empty):
        obj, text, data = _fixture()
        events = []
        class Row:
            def __init__(self, values):
                self.values = dict(zip(("offset", "symbol_index", "type", "addend"), values))
            def __getattr__(self, name):
                events.append(("field", name))
                return self.values[name]
            def __del__(self):
                events.append(("row-release",))
        class Cursor:
            def __init__(self):
                self.index = 0
            def __iter__(self):
                raise AssertionError("unexpected second iterator call")
            def __next__(self):
                events.append(("next",))
                if self.index == len(text):
                    raise StopIteration
                value = Row(text[self.index])
                self.index += 1
                return value
        class Fresh:
            def __iter__(self):
                events.append(("iter",))
                return Cursor()
            def __del__(self):
                events.append(("container-release",))
        class Section:
            def __init__(self, source):
                self.source = source
            def __getattr__(self, name):
                if name == "relocations":
                    events.append(("get",))
                    return Fresh()
                return getattr(self.source, name)
        second = replace(obj.sections[1], relocations=elf._PackedElfRelocations(_pack(() if empty else data)))
        object.__setattr__(obj, "sections", (Section(obj.sections[0]), second))
        outcome = _outcome(link, obj)
        return outcome, events
    for empty in (False, True):
        expected = run(original_linker(elf), empty)
        assert expected[0][0] == "image"
        assert run(elf._link_static_executable_image, empty) == expected


def test_packed_application_rereads_payload_after_resolution_callback(monkeypatch):
    identity = elf._symbol_identity
    def run(reference):
        obj = _packed(_single(257))
        view = obj.sections[0].relocations
        rows = ((0, 2, 257, 0), (0, 2, 257, 1))
        object.__setattr__(view, "payload", _pack(rows))
        object.__setattr__(view, "size", 48)
        timer = _Timing()
        events = []
        def changed(*args):
            if timer.phase == "elf_image_resolve_relocate":
                events.append(args[3])
                if len(events) == 1:
                    object.__setattr__(view, "payload", _pack((rows[0], (0, 2, 257, -7))))
                    object.__setattr__(view, "size", 0)
            return identity(*args)
        with monkeypatch.context() as patch:
            patch.setattr(elf, "_symbol_identity", changed)
            link = original_linker(elf) if reference else elf._link_static_executable_image
            outcome = _outcome(link, obj, coordinator_timing=timer)
        return outcome, events
    expected = run(True)
    assert expected[0][0] == "image" and expected[1] == [2, 2]
    assert run(False) == expected


def test_packed_application_error_tracebacks_release_fresh_views():
    def run(link, generic):
        obj = _single(283, value=BASE + 0x1002)
        timer = _Timing()
        views = []
        class Cursor:
            def __init__(self):
                self.done = False
            def __iter__(self):
                return self
            def __next__(self):
                if self.done:
                    raise StopIteration
                self.done = True
                return elf.ElfRelocation(0, 2, 283, 0)
        class Fresh:
            def __iter__(self):
                cursor = Cursor()
                views.append(weakref.ref(cursor))
                return cursor
        class Section:
            def __getattr__(self, name):
                if name == "relocations":
                    view = Fresh() if generic else elf._PackedElfRelocations(_pack(((0, 2, 283, 0),)))
                    views.append(weakref.ref(view))
                    return view
                return getattr(source, name)
        source = obj.sections[0]
        object.__setattr__(obj, "sections", (Section(),))
        saved = None
        try:
            link([obj], base_address=BASE, coordinator_timing=timer)
        except Exception as error:
            saved = error
        assert saved is not None and saved.__traceback__ is not None
        return type(saved), str(saved), _exception_chain(saved), tuple(view() is not None for view in views)
    for generic in (False, True):
        expected = run(original_linker(elf), generic)
        assert expected[0] is elf.ElfError and not any(expected[3])
        assert run(elf._link_static_executable_image, generic) == expected


def test_packed_application_subclass_and_scalar_fallbacks_keep_projection_calls(monkeypatch):
    class PackedSubclass(elf._PackedElfRelocations):
        pass
    class Integer(int):
        pass
    class Bytes(bytes):
        pass
    constructor = elf.ElfRelocation.__init__
    for shape in ("subclass", "offset", "size", "payload", "machine"):
        owner = _packed(_single(257))
        view = owner.sections[0].relocations
        if shape == "subclass":
            object.__setattr__(owner.sections[0], "relocations", PackedSubclass(view.payload))
        elif shape in ("offset", "size"):
            object.__setattr__(view, shape, Integer(getattr(view, shape)))
        elif shape == "payload":
            object.__setattr__(view, "payload", Bytes(view.payload))
        else:
            object.__setattr__(owner, "machine", Integer(owner.machine))
        calls = []
        def counted(self, *args, **kwargs):
            calls.append(1)
            constructor(self, *args, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(elf.ElfRelocation, "__init__", counted)
            expected = _outcome(original_linker(elf), owner)
            prior = len(calls)
            calls.clear()
            actual = _outcome(elf._link_static_executable_image, owner)
        assert actual == expected and actual[0] == "image"
        assert len(calls) == prior and prior in (1, 2)
