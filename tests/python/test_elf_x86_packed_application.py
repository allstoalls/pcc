"""Host x86 application contracts, not native execution or timing evidence."""

import ast
import copy
from pathlib import Path
import struct
import weakref

import pytest

from pcc.backend import elf_x86_64 as elf
from pcc.backend.owned_elf_inputs import ElfInputStore
from tests.elf_x86_packed_application_reference import (
    original_linker,
    without_x86_packed_application,
)
from tests.python.test_elf_x86_packed_classification import (
    BASE,
    TARGET,
    _exception_chain,
    _fixture,
    _outcome,
    _pack,
    _packed_objects,
)


@pytest.mark.parametrize("kind", [
    elf.R_X86_64_NONE, elf.R_X86_64_64, elf.R_X86_64_PC32,
    elf.R_X86_64_PLT32, elf.R_X86_64_32, elf.R_X86_64_32S,
    elf.R_X86_64_GOTPCREL, elf.R_X86_64_GOTPCRELX,
    elf.R_X86_64_REX_GOTPCRELX, elf.R_X86_64_GOTTPOFF, elf.R_X86_64_TPOFF32,
])
def test_every_x86_family_matches_tuple_packed_and_staged(kind, tmp_path):
    symbol = 5 if kind in (elf.R_X86_64_GOTTPOFF, elf.R_X86_64_TPOFF32) else 3
    objects = _packed_objects(((0, symbol, kind, 0),))
    encoded = [elf.emit_relocatable(obj) for obj in objects]
    ordinary = [elf.parse_relocatable(data) for data in encoded]
    expected = _outcome(original_linker(elf), ordinary)
    assert expected[0] == "image", expected
    assert _outcome(elf._link_static_executable_image, objects) == expected
    assert _outcome(elf._link_static_executable_image, ordinary) == expected
    store = ElfInputStore(TARGET)
    staged = []
    for index, data in enumerate(encoded):
        path = tmp_path / (str(index) + ".o")
        path.write_bytes(data)
        staged.append(store.stage(str(path)))
    assert _outcome(
        elf._link_static_executable_image, staged, _input_store=store,
    ) == expected
    store.verify()


def test_application_removes_exactly_one_record_per_packed_row(monkeypatch):
    objects, text_rows, data_rows = _fixture()
    encoded = [elf.emit_relocatable(obj) for obj in objects]
    packed = [elf.parse_relocatable(data, compact_relocations=True) for data in encoded]
    constructor = elf.ElfRelocation.__init__
    calls = []

    def counted(self, *args, **kwargs):
        calls.append(1)
        constructor(self, *args, **kwargs)

    monkeypatch.setattr(elf.ElfRelocation, "__init__", counted)
    expected = _outcome(original_linker(elf), packed)
    prior = len(calls)
    calls.clear()
    actual = _outcome(elf._link_static_executable_image, packed)
    assert actual == expected and actual[0] == "image"
    assert (prior, len(calls)) == (len(text_rows) + len(data_rows), 0)


@pytest.mark.parametrize("rows", [
    ((0, 99, elf.R_X86_64_64, 0), (8, 3, elf.R_X86_64_32, 1 << 40)),
    ((0, 3, elf.R_X86_64_32, 1 << 40), (8, 99, elf.R_X86_64_64, 0)),
    ((0, 3, elf.R_X86_64_32S, 1 << 40),),
    ((0, 3, elf.R_X86_64_PC32, 1 << 40),),
    ((0, 3, elf.R_X86_64_64, -(1 << 63)),),
    ((0, 3, elf.R_X86_64_TPOFF32, 0),),
    ((0, 4, elf.R_X86_64_GOTTPOFF, 0),),
    ((0, 3, 999, 0),),
    ((0, 99, 999, 0),),
])
def test_first_application_error_and_chain_match(rows):
    expected = _outcome(original_linker(elf), _packed_objects(rows))
    assert expected[0] == "error"
    assert _outcome(elf._link_static_executable_image, _packed_objects(rows)) == expected


def test_none_still_skips_resolution_after_validation():
    rows = ((0, 99, elf.R_X86_64_NONE, 0), (8, 3, elf.R_X86_64_64, 7))
    expected = _outcome(original_linker(elf), _packed_objects(rows))
    assert expected[0] == "image"
    assert _outcome(elf._link_static_executable_image, _packed_objects(rows)) == expected


@pytest.mark.parametrize("shape", [
    "subclass", "payload", "offset", "size", "machine", "decoder",
])
def test_noncanonical_inputs_keep_public_application_iteration(monkeypatch, shape):
    class PackedSubclass(elf._PackedElfRelocations):
        pass

    class Integer(int):
        pass

    class Bytes(bytes):
        pass

    class Decoder:
        size = elf._RELA.size

        def unpack_from(self, payload, position):
            return struct.unpack_from("<QQq", payload, position)

    objects = _packed_objects()
    for obj in objects:
        if shape == "machine":
            object.__setattr__(obj, "machine", Integer(obj.machine))
        for section in obj.sections:
            packed = section.relocations
            if type(packed) is not elf._PackedElfRelocations:
                continue
            if shape == "subclass":
                object.__setattr__(section, "relocations", PackedSubclass(packed.payload))
            elif shape == "payload":
                object.__setattr__(packed, "payload", Bytes(packed.payload))
            elif shape == "offset":
                object.__setattr__(packed, "offset", Integer(packed.offset))
            elif shape == "size":
                object.__setattr__(packed, "size", Integer(packed.size))
    if shape == "decoder":
        monkeypatch.setattr(elf, "_RELA", Decoder())

    def unexpected(_source):
        raise AssertionError("noncanonical application must use the public iterator")

    monkeypatch.setattr(elf, "_x86_packed_application_rows", unexpected)
    expected = _outcome(original_linker(elf), objects)
    assert expected[0] == "image"
    assert _outcome(elf._link_static_executable_image, objects) == expected


@pytest.mark.parametrize("fault", ["unpack", "resolve", "write", "callback-unpack"])
def test_application_error_preserves_decoder_and_body_source_lifetimes(monkeypatch, fault):
    identity = elf._symbol_identity

    def run(reference):
        objects, _text_rows, _data_rows = _fixture()
        views = []

        class Section:
            def __init__(self, source):
                self.source = source
                self.reads = 0

            def __getattr__(self, name):
                if name != "relocations":
                    return getattr(self.source, name)
                self.reads += 1
                if self.reads == 1:  # Classification has no GOT work.
                    return ()
                symbol = 99 if fault == "resolve" else 3
                kind = elf.R_X86_64_32 if fault == "write" else elf.R_X86_64_64
                addend = 1 << 40 if fault == "write" else 0
                view = elf._PackedElfRelocations(_pack((
                    (0, symbol, kind, addend), (8, 3, elf.R_X86_64_64, 1),
                )))
                if fault == "unpack":
                    object.__setattr__(view, "payload", b"")
                views.append(weakref.ref(view))
                return view

        source = objects[0].sections[0]
        object.__setattr__(objects[0], "sections", (Section(source), *objects[0].sections[1:]))

        def mutate(*args):
            if fault == "callback-unpack" and views:
                object.__setattr__(views[-1](), "payload", b"")
            return identity(*args)

        with monkeypatch.context() as patch:
            patch.setattr(elf, "_symbol_identity", mutate)
            link = original_linker(elf) if reference else elf._link_static_executable_image
            try:
                link(objects, base_address=BASE)
            except Exception as exc:
                assert exc.__traceback__ is not None
                return _exception_chain(exc), tuple(view() is not None for view in views)
        raise AssertionError("expected an application failure")

    expected = run(True)
    assert expected[1] == (fault in ("unpack", "callback-unpack"),)
    assert run(False) == expected


def test_application_rereads_payload_after_resolution_callback(monkeypatch):
    identity = elf._symbol_identity

    def run(reference):
        rows = ((0, 3, elf.R_X86_64_64, 0), (8, 3, elf.R_X86_64_64, 1))
        objects = _packed_objects(rows)
        packed = objects[0].sections[0].relocations
        replacement = _pack((rows[0], (8, 4, elf.R_X86_64_64, 1)))
        events = []

        def mutate(*args):
            events.append(args[-1])
            if len(events) == 1:
                object.__setattr__(packed, "payload", replacement)
            return identity(*args)

        with monkeypatch.context() as patch:
            patch.setattr(elf, "_symbol_identity", mutate)
            link = original_linker(elf) if reference else elf._link_static_executable_image
            return _outcome(link, objects), events

    expected = run(True)
    assert expected[0][0] == "image" and expected[1][:2] == [3, 4]
    assert run(False) == expected


def test_prior_custom_application_row_is_released_after_decode(monkeypatch):
    identity = elf._symbol_identity

    def run(reference):
        objects = _packed_objects()
        events, views = [], []

        class Row:
            offset, symbol_index, type, addend = 0, 3, elf.R_X86_64_64, 0

            def __del__(self):
                if views:
                    events.append("row-released")
                    object.__setattr__(views[-1](), "payload", b"")

        class Rows:
            def __iter__(self):
                yield Row()

        class Section:
            def __init__(self, source):
                self.source = source
                self.reads = 0

            def __getattr__(self, name):
                if name != "relocations":
                    return getattr(self.source, name)
                self.reads += 1
                if self.reads == 1:
                    return ()
                view = elf._PackedElfRelocations(_pack((
                    (0, 3, elf.R_X86_64_64, 0), (0, 3, elf.R_X86_64_64, 1),
                )))
                views.append(weakref.ref(view))
                return view

        object.__setattr__(objects[0].sections[0], "relocations", Rows())
        object.__setattr__(objects[0], "sections", (
            *objects[0].sections[:2], Section(objects[0].sections[2]),
        ))

        def observe(*args):
            if views:
                events.append("identity")
            return identity(*args)

        with monkeypatch.context() as patch:
            patch.setattr(elf, "_symbol_identity", observe)
            link = original_linker(elf) if reference else elf._link_static_executable_image
            try:
                link(objects, base_address=BASE)
            except Exception as exc:
                assert exc.__traceback__ is not None
                return _exception_chain(exc), events, tuple(view() is not None for view in views)
        raise AssertionError("expected a second-row decode error")

    expected = run(True)
    assert expected[1:] == (["row-released", "identity"], (True,))
    assert "at offset 24" in expected[0][0][1]
    assert run(False) == expected


def test_application_source_keeps_generic_body_and_x86_operation_order():
    source = Path(elf.__file__).read_text(encoding="utf-8")
    before = without_x86_packed_application(source)
    assert before != source
    with pytest.raises(AssertionError):
        without_x86_packed_application(source.replace(
            "_write_int(image, patch, symbol_address + addend, 64, False)",
            "_write_int(image, patch, symbol_address - addend, 64, False)", 1,
        ))

    def application(text):
        owner = next(node for node in ast.parse(text).body
                     if isinstance(node, ast.FunctionDef)
                     and node.name == "_link_static_executable_image")
        return next(node for node in owner.body if isinstance(node, ast.For)
                    and any(isinstance(child, ast.Name) and child.id == "patch"
                            for child in ast.walk(node)))

    old, new = application(before), application(source)
    old_dispatch = old.body[3]
    new_dispatch = new.body[3]
    assert isinstance(old_dispatch, ast.If) and isinstance(new_dispatch, ast.If)
    x86 = new_dispatch.orelse[0]
    assert isinstance(x86, ast.If)
    assert ast.dump(ast.Module(body=old_dispatch.orelse, type_ignores=[])) == ast.dump(
        ast.Module(body=x86.orelse, type_ignores=[]),
    )
    generic = next(node for node in old_dispatch.orelse if isinstance(node, ast.For))
    packed = next(node for node in x86.body if isinstance(node, ast.For))
    branch = generic.body[-1]
    assert isinstance(branch, ast.If)
    expected = copy.deepcopy(generic.body[:-1] + branch.orelse)

    class Fields(ast.NodeTransformer):
        def visit_Attribute(self, node):
            if isinstance(node.value, ast.Name) and node.value.id == "relocation":
                return ast.Name(id={
                    "offset": "relocation_offset", "symbol_index": "symbol_index",
                    "type": "relocation_type", "addend": "addend",
                }[node.attr], ctx=node.ctx)
            return self.generic_visit(node)

    expected = Fields().visit(ast.Module(body=expected, type_ignores=[]))
    assert ast.dump(expected) == ast.dump(ast.Module(body=packed.body[1:], type_ignores=[]))
    assert ast.dump(ast.Module(body=old.body[4:], type_ignores=[])) == ast.dump(
        ast.Module(body=new.body[4:], type_ignores=[]),
    )
