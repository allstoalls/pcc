"""Host-only x86 classification controls; no emitted program or compiler runs."""

import struct
import weakref

import pytest

from pcc.backend import elf_x86_64 as elf
from pcc.backend.owned_elf_inputs import ElfInputStore
from tests.elf_x86_packed_classification_reference import original_linker


TARGET = "x86_64-unknown-linux-gnu"
BASE = 0x40000000


def _pack(rows):
    return b"".join(struct.pack("<QQq", offset, symbol << 32 | kind, addend)
                    for offset, symbol, kind, addend in rows)


def _fixture():
    rows = (
        (0, 3, elf.R_X86_64_GOTPCREL, -4),
        (4, 3, elf.R_X86_64_GOTPCRELX, -4),
        (8, 3, elf.R_X86_64_REX_GOTPCRELX, -4),
        (12, 4, elf.R_X86_64_GOTPCREL, -4),
        (16, 5, elf.R_X86_64_GOTTPOFF, -4),
        (20, 6, elf.R_X86_64_GOTPCREL, -4),
        (24, 1, elf.R_X86_64_GOTPCREL, -4),
        (32, 3, elf.R_X86_64_64, 1),
    )
    data_rows = ((0, 6, elf.R_X86_64_64, -5),)
    obj = elf.ElfObject(
        (elf.ElfSection(".text", elf.SHT_PROGBITS, elf.SHF_ALLOC | elf.SHF_EXECINSTR,
                        16, bytes(48), relocations=tuple(elf.ElfRelocation(*row) for row in rows)),
         elf.ElfSection(".tdata", elf.SHT_PROGBITS, elf.SHF_ALLOC | elf.SHF_WRITE | elf.SHF_TLS,
                        8, bytes(8)),
         elf.ElfSection(".data", elf.SHT_PROGBITS, elf.SHF_ALLOC | elf.SHF_WRITE,
                        8, bytes(8), relocations=tuple(elf.ElfRelocation(*row) for row in data_rows))),
        (elf.ElfSymbol.null(),
         elf.ElfSymbol("local", 1, 0, 0, elf.STB_LOCAL, elf.STT_NOTYPE),
         elf.ElfSymbol("_start", 1, 0, 48, elf.STB_GLOBAL, elf.STT_FUNC),
         elf.ElfSymbol("target", elf.SHN_ABS, 0x1234, 0, elf.STB_GLOBAL, elf.STT_NOTYPE),
         elf.ElfSymbol("weak_zero", elf.SHN_UNDEF, 0, 0, elf.STB_WEAK, elf.STT_NOTYPE),
         elf.ElfSymbol("tls", 2, 0, 8, elf.STB_GLOBAL, elf.STT_TLS),
         elf.ElfSymbol("resolved", elf.SHN_UNDEF, 0, 0, elf.STB_GLOBAL, elf.STT_NOTYPE)),
        elf.EM_X86_64,
    )
    provider = elf.ElfObject((), (elf.ElfSymbol.null(),
        elf.ElfSymbol("resolved", elf.SHN_ABS, 0x5678, 0, elf.STB_GLOBAL, elf.STT_NOTYPE)),
        elf.EM_X86_64)
    return [obj, provider], rows, data_rows


def _packed_objects(rows=None):
    objects, text_rows, data_rows = _fixture()
    if rows is not None:
        text_rows = rows
    for section, section_rows in ((objects[0].sections[0], text_rows),
                                   (objects[0].sections[2], data_rows)):
        object.__setattr__(section, "relocations", elf._PackedElfRelocations(_pack(section_rows)))
    return objects


def _exception_chain(exc):
    chain = []
    seen = set()
    while exc is not None and len(chain) < 8:
        if id(exc) in seen:
            chain.append(("cycle", type(exc), str(exc)))
            break
        seen.add(id(exc))
        cause = exc.__cause__
        if cause is None and not exc.__suppress_context__:
            cause = exc.__context__
        chain.append((type(exc), str(exc), bool(exc.__suppress_context__)))
        exc = cause
    return tuple(chain)


def _outcome(link, objects, **kwargs):
    try:
        return "image", bytes(link(objects, base_address=BASE, **kwargs))
    except Exception as exc:
        return "error", _exception_chain(exc)


def test_staged_x86_image_removes_classification_and_application_records(tmp_path, monkeypatch):
    objects, text_rows, data_rows = _fixture()
    store = ElfInputStore(TARGET)
    staged = []
    for index, obj in enumerate(objects):
        path = tmp_path / (str(index) + ".o")
        path.write_bytes(elf.emit_relocatable(obj))
        staged.append(store.stage(str(path)))
    constructor = elf.ElfRelocation.__init__
    calls = []

    def counted(self, *args, **kwargs):
        calls.append(1)
        constructor(self, *args, **kwargs)

    def no_arm_proof(*_args):
        raise AssertionError("x86 classification must not enter the ARM negative proof")

    monkeypatch.setattr(elf.ElfRelocation, "__init__", counted)
    monkeypatch.setattr(elf, "_staged_classification_is_empty", no_arm_proof)
    before = _outcome(original_linker(elf), staged, _input_store=store)
    before_count = len(calls)
    calls.clear()
    store.clear()
    after = _outcome(elf._link_static_executable_image, staged, _input_store=store)
    row_count = len(text_rows) + len(data_rows)
    assert after == before and after[0] == "image"
    # Staging/validation already completed. Neither owned packed scan needs a
    # projected public record; the generic protocol path remains unchanged.
    assert (before_count, len(calls)) == (2 * row_count, 0)
    assert elf.parse_static_executable(after[1])["tls_segments"] == 1
    store.verify()


@pytest.mark.parametrize("rows", [
    ((0, 99, elf.R_X86_64_GOTPCREL, 0), (4, 3, elf.R_X86_64_32, 1 << 40)),
    ((0, 3, elf.R_X86_64_32, 1 << 40), (4, 99, elf.R_X86_64_GOTPCREL, 0)),
    ((0, 3, elf.R_X86_64_32, 1 << 40), (4, 99, elf.R_X86_64_PLT32, 0)),
    ((0, 3, elf.R_X86_64_GOTTPOFF, 0),),
    ((0, 3, elf.R_X86_64_64, -(1 << 63)),),
])
def test_postvalidation_first_error_matches_exact_preimage(rows):
    before = _outcome(original_linker(elf), _packed_objects(rows))
    after = _outcome(elf._link_static_executable_image, _packed_objects(rows))
    assert after == before and after[0] == "error"


@pytest.mark.parametrize("shape", ["list", "packed-subclass", "fresh-custom", "custom-iterator"])
def test_generic_x86_field_iteration_and_temporary_order_is_unchanged(shape):
    def run(link):
        objects, rows, _data_rows = _fixture()
        events = []

        class Row:
            def __init__(self, values):
                self.values = dict(zip(("offset", "symbol_index", "type", "addend"), values))

            def __getattr__(self, name):
                events.append(("field", name))
                return self.values[name]

        records = [Row(row) for row in rows]

        class Fresh:
            def __iter__(self):
                events.append(("iter",))
                return iter(records)

            def __del__(self):
                events.append(("released",))

        class Section:
            def __init__(self, source):
                self.source = source

            def __getattr__(self, name):
                if name == "relocations":
                    events.append(("relocations",))
                    return Fresh()
                return getattr(self.source, name)

        class PackedSubclass(elf._PackedElfRelocations):
            def __iter__(self):
                events.append(("packed-subclass-iter",))
                return iter(records)

        class Cursor:
            def __init__(self):
                self.index = 0

            def __iter__(self):
                raise AssertionError("do not iterate the returned cursor again")

            def __next__(self):
                if self.index == len(records):
                    raise StopIteration
                value = records[self.index]
                self.index += 1
                return value

        class Iterable:
            def __iter__(self):
                events.append(("custom-iterable",))
                return Cursor()

        obj = objects[0]
        if shape == "fresh-custom":
            object.__setattr__(obj, "sections", (Section(obj.sections[0]), *obj.sections[1:]))
        else:
            value = (records if shape == "list" else Iterable() if shape == "custom-iterator"
                     else PackedSubclass(_pack(rows)))
            object.__setattr__(obj.sections[0], "relocations", value)
        return _outcome(link, objects), events

    before = run(original_linker(elf))
    after = run(elf._link_static_executable_image)
    assert after == before and after[0][0] == "image"


def test_int_subclass_machine_retains_generic_projection_path(monkeypatch):
    class Machine(int):
        pass

    constructor = elf.ElfRelocation.__init__
    calls = []

    def counted(self, *args, **kwargs):
        calls.append(1)
        constructor(self, *args, **kwargs)

    objects = _packed_objects()
    for obj in objects:
        object.__setattr__(obj, "machine", Machine(elf.EM_X86_64))
    monkeypatch.setattr(elf.ElfRelocation, "__init__", counted)
    before = _outcome(original_linker(elf), objects)
    before_count = len(calls)
    calls.clear()
    after = _outcome(elf._link_static_executable_image, objects)
    assert after == before and after[0] == "image"
    assert before_count == len(calls) == 18


@pytest.mark.parametrize("shape", ["bytes-subclass", "int-offset", "int-size", "custom-decoder"])
def test_protocol_metadata_and_decoder_keep_generic_projection_path(monkeypatch, shape):
    class Bytes(bytes):
        pass

    class Integer(int):
        pass

    class Decoder:
        size = elf._RELA.size

        def unpack_from(self, payload, position):
            return struct.unpack_from("<QQq", payload, position)

    objects = _packed_objects()
    for section in (objects[0].sections[0], objects[0].sections[2]):
        packed = section.relocations
        if shape == "bytes-subclass":
            object.__setattr__(packed, "payload", Bytes(packed.payload))
        elif shape == "int-offset":
            object.__setattr__(packed, "offset", Integer(packed.offset))
        elif shape == "int-size":
            object.__setattr__(packed, "size", Integer(packed.size))
    if shape == "custom-decoder":
        monkeypatch.setattr(elf, "_RELA", Decoder())

    def unexpected(_source):
        raise AssertionError("protocol metadata must keep the generic path")

    monkeypatch.setattr(elf, "_x86_packed_classification_rows", unexpected)
    before = _outcome(original_linker(elf), objects)
    after = _outcome(elf._link_static_executable_image, objects)
    assert after == before and after[0] == "image"


@pytest.mark.parametrize("bad_range", [False, True])
def test_missing_payload_keeps_original_range_before_payload_error(bad_range):
    def objects():
        result = _packed_objects()
        packed = result[0].sections[0].relocations
        object.__delattr__(packed, "payload")
        if bad_range:
            object.__setattr__(packed, "offset", "invalid")
        return result

    before = _outcome(original_linker(elf), objects())
    after = _outcome(elf._link_static_executable_image, objects())
    assert after == before and after[0] == "error"
    assert after[1][0][0] is (TypeError if bad_range else AttributeError)


@pytest.mark.parametrize("fault", ["bad-symbol", "unpack"])
def test_classification_error_traceback_preserves_fresh_packed_view_lifetime(fault):
    def run(link):
        objects, _rows, _data_rows = _fixture()
        views = []

        class Section:
            def __init__(self, source):
                self.source = source

            def __getattr__(self, name):
                if name == "relocations":
                    view = elf._PackedElfRelocations(_pack(((0, 99, elf.R_X86_64_GOTPCREL, 0),)))
                    if fault == "unpack":
                        object.__setattr__(view, "payload", b"")
                    views.append(weakref.ref(view))
                    return view
                return getattr(self.source, name)

        obj = objects[0]
        object.__setattr__(obj, "sections", (Section(obj.sections[0]), *obj.sections[1:]))
        try:
            link(objects, base_address=BASE)
        except Exception as exc:
            assert exc.__traceback__ is not None
            return _exception_chain(exc), tuple(view() is not None for view in views)
        raise AssertionError("expected a classification failure")

    before = run(original_linker(elf))
    after = run(elf._link_static_executable_image)
    assert after == before and after[1] == (fault == "unpack",)


def test_classification_reads_payload_again_after_symbol_callback(monkeypatch):
    identity = elf._symbol_identity

    def run(use_reference):
        rows = ((0, 3, elf.R_X86_64_GOTPCREL, -4), (4, 6, elf.R_X86_64_GOTPCREL, -4))
        objects = _packed_objects(rows)
        packed = objects[0].sections[0].relocations
        replacement = _pack((rows[0], (4, 4, elf.R_X86_64_GOTPCREL, -4)))
        events = []

        def mutate(*args):
            events.append(args[-1])
            if len(events) == 1:
                object.__setattr__(packed, "payload", replacement)
            return identity(*args)

        with monkeypatch.context() as patch:
            patch.setattr(elf, "_symbol_identity", mutate)
            link = original_linker(elf) if use_reference else elf._link_static_executable_image
            return _outcome(link, objects), events

    before = run(True)
    after = run(False)
    assert after == before and after[0][0] == "image"
    assert after[1][:2] == [3, 4]


def test_callback_truncated_payload_keeps_decoder_traceback_owner(monkeypatch):
    identity = elf._symbol_identity

    def run(use_reference):
        objects, _rows, _data_rows = _fixture()
        views = []

        class Section:
            def __init__(self, source):
                self.source = source

            def __getattr__(self, name):
                if name == "relocations":
                    rows = ((0, 3, elf.R_X86_64_GOTPCREL, -4),
                            (4, 6, elf.R_X86_64_GOTPCREL, -4))
                    view = elf._PackedElfRelocations(_pack(rows))
                    views.append(weakref.ref(view))
                    return view
                return getattr(self.source, name)

        def mutate(*args):
            object.__setattr__(views[-1](), "payload", b"")
            return identity(*args)

        obj = objects[0]
        object.__setattr__(obj, "sections", (Section(obj.sections[0]), *obj.sections[1:]))
        with monkeypatch.context() as patch:
            patch.setattr(elf, "_symbol_identity", mutate)
            link = original_linker(elf) if use_reference else elf._link_static_executable_image
            try:
                link(objects, base_address=BASE)
            except Exception as exc:
                assert exc.__traceback__ is not None
                return _exception_chain(exc), tuple(view() is not None for view in views)
        raise AssertionError("expected the second-row decoding failure")

    before = run(True)
    after = run(False)
    assert after == before and after[1] == (True,)


def test_decode_failure_retains_previous_custom_row_and_source():
    def run(link):
        objects, _rows, _data_rows = _fixture()
        events, source_views, row_views = [], [], []

        class Row:
            offset, symbol_index, type, addend = 0, 3, elf.R_X86_64_64, 0

            def __del__(self):
                events.append("row-released")

        class Rows:
            def __iter__(self):
                row = Row()
                row_views.append(weakref.ref(row))
                yield row

        class Section:
            def __init__(self, source):
                self.source = source

            def __getattr__(self, name):
                if name == "relocations":
                    view = elf._PackedElfRelocations(_pack(((0, 3, elf.R_X86_64_64, 0),)))
                    object.__setattr__(view, "payload", b"")
                    source_views.append(weakref.ref(view))
                    return view
                return getattr(self.source, name)

        obj = objects[0]
        object.__setattr__(obj.sections[0], "relocations", Rows())
        object.__setattr__(obj, "sections", (*obj.sections[:2], Section(obj.sections[2])))
        try:
            link(objects, base_address=BASE)
        except Exception as exc:
            assert exc.__traceback__ is not None
            return (_exception_chain(exc), tuple(events),
                    tuple(view() is not None for view in source_views),
                    tuple(view() is not None for view in row_views))
        raise AssertionError("expected the first packed-row decoding failure")

    before = run(original_linker(elf))
    after = run(elf._link_static_executable_image)
    assert after == before and after[1:] == ((), (True,), (True,))


def test_changed_path_decodes_before_prior_row_finalizer_and_identity(monkeypatch):
    identity = elf._symbol_identity

    def run(use_reference):
        objects, _rows, _data_rows = _fixture()
        events, source_views = [], []

        class Row:
            offset, symbol_index, type, addend = 0, 3, elf.R_X86_64_64, 0

            def __del__(self):
                events.append("row-released")
                object.__setattr__(source_views[-1](), "payload", b"")

        class Rows:
            def __iter__(self):
                yield Row()

        class Section:
            def __init__(self, source):
                self.source = source

            def __getattr__(self, name):
                if name == "relocations":
                    view = elf._PackedElfRelocations(_pack((
                        (0, 3, elf.R_X86_64_GOTPCREL, -4),
                        (4, 6, elf.R_X86_64_GOTPCREL, -4))))
                    source_views.append(weakref.ref(view))
                    return view
                return getattr(self.source, name)

        def observe(*args):
            events.append("identity")
            return identity(*args)

        obj = objects[0]
        object.__setattr__(obj.sections[0], "relocations", Rows())
        object.__setattr__(obj, "sections", (*obj.sections[:2], Section(obj.sections[2])))
        with monkeypatch.context() as patch:
            patch.setattr(elf, "_symbol_identity", observe)
            link = original_linker(elf) if use_reference else elf._link_static_executable_image
            try:
                link(objects, base_address=BASE)
            except Exception as exc:
                assert exc.__traceback__ is not None
                return (_exception_chain(exc), tuple(events),
                        tuple(view() is not None for view in source_views))
        raise AssertionError("expected the second packed-row decoding failure")

    before = run(True)
    after = run(False)
    assert after == before and after[1:] == (("row-released", "identity"), (True,))
    assert "at offset 24" in after[0][0][1]


def test_prior_custom_row_release_keeps_unpack_then_identity_order(monkeypatch):
    rela = elf._RELA
    identity = elf._symbol_identity

    def run(use_reference):
        objects = _packed_objects(((0, 3, elf.R_X86_64_64, 0),))
        events = []

        class Row:
            offset, symbol_index, type, addend = 0, 3, elf.R_X86_64_64, 0

            def __del__(self):
                events.append("row-released")

        class Rows:
            def __iter__(self):
                yield Row()

        class Rela:
            size = rela.size

            def unpack_from(self, payload, position):
                events.append("unpack")
                return rela.unpack_from(payload, position)

        def observe(*args):
            events.append("identity")
            return identity(*args)

        obj = objects[0]
        object.__setattr__(obj.sections[0], "relocations", Rows())
        object.__setattr__(obj.sections[2], "relocations", elf._PackedElfRelocations(
            _pack(((0, 3, elf.R_X86_64_GOTPCREL, -4),))))
        with monkeypatch.context() as patch:
            patch.setattr(elf, "_RELA", Rela())
            patch.setattr(elf, "_symbol_identity", observe)
            link = original_linker(elf) if use_reference else elf._link_static_executable_image
            return _outcome(link, objects), events

    before = run(True)
    after = run(False)
    assert after == before and after[0][0] == "image"
    assert after[1][:3] == ["unpack", "row-released", "identity"]


@pytest.mark.parametrize("change", ["append", "truncate"])
def test_staged_identity_error_is_still_before_classification(tmp_path, change):
    objects, _rows, _data_rows = _fixture()
    store = ElfInputStore(TARGET)
    path = tmp_path / "input.o"
    data = elf.emit_relocatable(objects[0])
    path.write_bytes(data)
    staged = store.stage(str(path))
    path.write_bytes(data + b"x" if change == "append" else data[:-1])
    before = _outcome(original_linker(elf), [staged, objects[1]], _input_store=store)
    store.clear()
    after = _outcome(elf._link_static_executable_image, [staged, objects[1]], _input_store=store)
    assert after == before and after[0] == "error"
