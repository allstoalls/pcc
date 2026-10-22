"""One source/fake-only control, with seven authored RELA/custom rows.

No PCC import, parser, saved-reference loader, final image, I/O payload, clock,
profile, or measurement. Only the proof predicate and classifier AST execute.
"""

import ast

from tests.test_elf_packed_application_source_controls import _without_packed_application
import copy
import hashlib
from pathlib import Path
import struct
from types import SimpleNamespace
import weakref


def _read(path):
    value = path.read_bytes().decode("utf-8").replace("\r\n", "\n")
    assert "\r" not in value
    return value


def _function(source, name):
    return next(node for node in ast.parse(source).body
                if isinstance(node, ast.FunctionDef) and node.name == name)


def _loop(function):
    return next(node for index, node in enumerate(function.body[:-1])
                if isinstance(node, ast.For) and ast.unparse(node.iter) == "alloc_sections"
                and isinstance(function.body[index + 1], ast.Assign)
                and ast.unparse(function.body[index + 1].targets[0]) == "metadata_sections")


def _dump(value):
    return ast.dump(value, include_attributes=False)


def _extract(node, namespace):
    # The caller selects a predicate or a small fake wrapper, never the linker.
    module = ast.fix_missing_locations(ast.Module(body=[copy.deepcopy(node)], type_ignores=[]))
    exec(compile(module, "<negative classification source/fake only>", "exec",
                 dont_inherit=True), namespace)
    return namespace[node.name]


def _environment():
    class Store:
        pass

    class Owner:
        pass

    class Section:
        flags = 6

    class Source:
        pass

    class Proof:
        pass

    class Packed:
        pass

    rela = struct.Struct("<QQq")
    environment = dict(ElfInputStore=Store, _InputObject=Owner, _InputSection=Section,
                       _InputSource=Source, _ClassificationProof=Proof, struct=struct,
                       elf=SimpleNamespace(EM_AARCH64=183, _PackedElfRelocations=Packed, _RELA=rela))
    return environment, (Store, Owner, Section, Source, Proof, Packed)


def _owned(classes, rows, bit=True):
    Store, Owner, Section, Source, Proof, Packed = classes
    payload = b"".join(struct.pack("<QQq", offset, symbol << 32 | kind, addend)
                       for offset, symbol, kind, addend in rows)
    store, owner, section, source, proof, view = (kind() for kind in classes)
    view.payload, view.offset, view.size = payload, 0, len(payload)
    section.store, section.source_index, section.section_index = store, 0, 0
    section.relocations = view
    owner.sections, owner.machine = (section,), 183
    source.kind, source.sha256, source.size = "PCO", "authored digest", len(payload)
    source.section_ranges = ((0, 0, 0, len(payload)),)
    proof.sha256, proof.size = source.sha256, source.size
    proof.section_ranges, proof.section_bits = source.section_ranges, (bit,)
    source.classification_proof = proof
    store.sources, store.current_index, store.current = [source], 0, payload
    store.current_classification_proof, store.current_classification_payload = proof, payload
    return store, owner, section, view


def test_negative_summary_bound_source_and_fake_controls():
    root = Path(__file__).resolve().parents[1]
    source = _without_packed_application(_read(root / "pcc/backend/elf_x86_64.py"))
    progress = ast.parse(_read(root / "tests/elf_relocation_progress_preimage.py"))
    progress_fields = {node.targets[0].id: ast.literal_eval(node.value)
                       for node in progress.body if isinstance(node, ast.Assign)}
    for changed, original in reversed(progress_fields["OWNER_REVERSE_EDITS"]):
        assert source.count(changed) == 1
        source = source.replace(changed, original)
    assert hashlib.sha256(source.encode()).hexdigest() == progress_fields["BASE_OWNER_SHA256"]
    references = ast.parse(_read(root / "tests/elf_negative_classification_preimage.py"))
    constants = {node.targets[0].id: ast.literal_eval(node.value)
                 for node in references.body if isinstance(node, ast.Assign)}
    assert constants["BASE_OWNER_SHA256"] == "750356589f9e47a179ae1cf9a3b63425f566f05fc0d30330dfc179f42000e9ed"
    lines = source.splitlines(keepends=True)
    for start, end, expected, original in reversed(constants["OWNER_REVERSE_EDITS"]):
        assert "".join(lines[start:end]) == expected
        lines[start:end] = original.splitlines(keepends=True)
    old_source = "".join(lines)
    assert hashlib.sha256(old_source.encode()).hexdigest() == constants["BASE_OWNER_SHA256"]
    old_function = _function(old_source, "_link_static_executable_image")
    assert _dump(old_function) == _dump(_function(constants["ORIGINAL_LINK_IMAGE_SOURCE"], "_link_static_executable_image"))
    assert _dump(_function(old_source, "_validate_object")) == _dump(
        _function(constants["ORIGINAL_VALIDATE_SOURCE"], "_validate_object"))
    new_function = _function(source, "_link_static_executable_image")
    old_loop, new_loop = _loop(old_function), _loop(new_function)
    old_branch, new_branch = old_loop.body[1], new_loop.body[1]
    # Packed fallback and the complete generic protocol keep their exact ASTs.
    assert _dump(ast.Module(body=new_branch.body[0].body[0].orelse, type_ignores=[])) == _dump(
        ast.Module(body=old_branch.body[0].body, type_ignores=[]))
    assert _dump(ast.Module(body=new_branch.orelse, type_ignores=[])) == _dump(
        ast.Module(body=old_branch.orelse, type_ignores=[]))
    for function in (old_function, new_function):
        index = function.body.index(_loop(function)) + 1
        function.body = function.body[index:]
    assert _dump(old_function) == _dump(new_function)  # Layout/application/finalization unchanged.
    old_tree, new_tree = ast.parse(old_source), ast.parse(source)
    for name in ("ElfObject",):
        old = next(node for node in old_tree.body if isinstance(node, ast.ClassDef) and node.name == name)
        new = next(node for node in new_tree.body if isinstance(node, ast.ClassDef) and node.name == name)
        assert _dump(old) == _dump(new)
    def iterator(tree):
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "_PackedElfRelocations")
        return next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "__iter__")
    assert _dump(iterator(old_tree)) == _dump(iterator(new_tree))
    wrapper = _function(source, "_staged_classification_is_empty")
    assert isinstance(wrapper.body[0], ast.Try)
    assert ast.unparse(wrapper.body[0].handlers[0].type) == "MemoryError"
    assert _dump(wrapper.body[0].body[0].test) == _dump(ast.parse(
        "type(objects) is not list or type(object_index) is not int or not 0 <= object_index < len(objects)",
        mode="eval").body)

    environment, classes = _environment()
    predicate = _extract(_function(_read(root / "pcc/backend/owned_elf_inputs.py"), "_classification_is_empty"), environment)
    store, owner, section, view = _owned(classes, ((0, 1, 257, -7), (8, 1, 0, 0)))
    assert predicate(store, owner, 1, section, view)
    view.size -= 24
    assert not predicate(store, owner, 1, section, view)
    view.size += 24
    assert not predicate(store, SimpleNamespace(**vars(owner)), 1, section, view)
    source_record = store.sources[0]
    proof = source_record.classification_proof
    source_record.classification_proof = classes[4]()
    assert not predicate(store, owner, 1, section, view)
    source_record.classification_proof = proof
    store.current = bytes(bytearray(view.payload))
    assert not predicate(store, owner, 1, section, view)
    store.current = view.payload
    store.current_classification_payload = bytes(bytearray(view.payload))
    assert not predicate(store, owner, 1, section, view)
    store.current_classification_payload = view.payload
    class Kind(str):
        def __eq__(self, other):
            raise AssertionError("new proof checks must not call custom equality")
    source_record.kind = Kind("PCO")
    assert not predicate(store, owner, 1, section, view)
    source_record.kind = "PCO"

    # Only seven authored records across four scenarios below. Counting the
    # mandatory fake unpack makes exact skipped work visible without a linker.
    calls, checks, events = [], [], []
    class CountingRela:
        size = 24
        def unpack_from(self, payload, position):
            calls.append(position)
            events.append("unpack")
            return struct.unpack_from("<QQq", payload, position)
    def qualify(store, objects, index, section_index, section, relocations):
        checks.append(1)
        return (type(objects) is list and type(index) is int and 0 <= index < len(objects)
                and predicate(store, objects[index], section_index, section, relocations))
    def identity(objects, definitions, index, symbol):
        if symbol == 99:
            raise ValueError("authored first symbol error")
        return ("symbol", symbol)
    observations = []
    namespace = dict(_PackedElfRelocations=classes[-1], _RELA=CountingRela(), EM_AARCH64=183,
                     SHF_EXECINSTR=4, SHF_WRITE=1, R_X86_64_GOTTPOFF=22,
                     _staged_classification_is_empty=qualify, _symbol_identity=identity,
                     _report_classification_skips=lambda *args: observations.append(args))
    fake = ast.parse(
        "def classify(alloc_sections, objects, _input_store):\n"
        "    machine = 183\n    definitions = {}\n    branch_slots = {}\n"
        "    got_seen = set()\n    got_identities = []\n"
        "    got_reloc_types = {311, 312, 541, 542}\n"
        "    relocation = None\n    summary_counts = None\n    coordinator_timing = None\n"
        "    return branch_slots, got_identities\n").body[0]
    fake.body.insert(-1, copy.deepcopy(new_loop))
    run = _extract(fake, namespace)
    assert run([(0, 1, section)], [owner], store) == ({}, [])
    assert calls == [] and checks == [1]
    store, owner, section, view = _owned(classes, ((0, 1, 311, 0),), bit=False)
    assert run([(0, 1, section)], [owner], store)[1] == [(("symbol", 1), "address")]
    assert calls == [0]

    class Row:
        type = 0
        def __del__(self):
            events.append("released")
    class Rows:
        def __iter__(self):
            yield Row()
    generic = SimpleNamespace(relocations=Rows())
    store, owner, section, view = _owned(classes, ((0, 1, 257, 0),))
    calls.clear(); checks.clear(); events.clear()
    run([(0, 1, generic), (0, 1, section)], [owner], store)
    assert calls == [0] and checks == [] and events[:2] == ["unpack", "released"]

    store, owner, section, view = _owned(classes, ((0, 99, 311, 0),), bit=False)
    refs = []
    class FreshSection:
        flags = 6
        @property
        def relocations(self):
            fresh = classes[-1]()
            fresh.payload, fresh.offset, fresh.size = view.payload, 0, 24
            refs.append(weakref.ref(fresh))
            return fresh
    caught = None
    try:
        run([(0, 1, FreshSection())], [owner], store)
    except ValueError as error:
        caught = error
    assert caught is not None and str(caught) == "authored first symbol error"
    assert refs and all(ref() is None for ref in refs)
    store, owner, section, view = _owned(classes, ((0, 1, 257, 0),))
    calls.clear()
    run([(0, 1, section)], [], store)
    assert calls == [0]  # A shortened owner list only loses eligibility.

    assert observations == []  # Disabled timing performs no observation.

    # Extract only the new fixed-field emitter. All output is a fake sink;
    # neither the timing module nor a real clock or stderr is imported here.
    timing_tree = ast.parse(_read(root / "pcc/backend/indexed_phase_timing.py"))
    timing_class = next(node for node in timing_tree.body
                        if isinstance(node, ast.ClassDef) and node.name == "CoarsePhaseTiming")
    emitter_node = next(node for node in timing_class.body
                        if isinstance(node, ast.FunctionDef) and node.name == "elf_classification_skips")
    assert not any(isinstance(node, ast.Name) and node.id == "time" for node in ast.walk(emitter_node))
    class Sink:
        def __init__(self):
            self.lines, self.flushes, self.fail = [], 0, False
        def write(self, line):
            self.lines.append(line)
        def flush(self):
            self.flushes += 1
            if self.fail:
                raise OSError("authored flush failure")
    sink = Sink()
    emit = _extract(emitter_node, {"sys": SimpleNamespace(stderr=sink)})
    assert emit(None, 1, 3, 1, 2) is True
    assert sink.flushes == 1 and sink.lines == [
        "pcc elf classification skips completed_sections=1 total_sections=3 "
        "skipped_sections=1 skipped_relocations_lower_bound=2\n"]
    assert emit(None, True, 3, 1, 2) is False and sink.flushes == 1
    sink.fail = True
    assert emit(None, 2, 3, 1, 2) is False and sink.flushes == 2
