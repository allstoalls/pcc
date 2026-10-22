"""One source/fake-only control; never imports PCC or calls a final linker.

Only the two classification loop ASTs execute, against authored fake globals.
This is neither final-image correctness nor a performance observation.
"""

import ast

from tests.test_elf_packed_application_source_controls import _without_packed_application
import copy
import hashlib
from pathlib import Path
import weakref


# Exact be20e4e input owner with the joined relocatable encoder (repinned
# when emit_relocatable stopped growing buffers); LF source for the frozen
# boundaries.
BASE_OWNER_SHA256 = "06e019c1d44dbe99e042776fbef7d650fff5dde71be744dd66e29d6d4dbc9d1b"
ORIGINAL_FUNCTION_SHA256 = "094f3b9e02b8e6c7e6afaa0ea8bd1dff75cb8dd304594d117511b715e0a6ba59"
UNCHANGED_PREFIX_SHA256 = "13254375b15288e0c52631ca15e337d2e782fad0faa69e3431dc3f63d5f72e55"
UNCHANGED_SUFFIX_SHA256 = "4890d50a32ae80d0497780c459fa732a62609a24b7485d39af438024b89a5b33"


def _text(path):
    # Normalize CRLF explicitly; do not silently accept other line endings.
    text = path.read_bytes().decode("utf-8").replace("\r\n", "\n")
    assert "\r" not in text
    return text


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _classification(source):
    tree = ast.parse(source)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "_link_static_executable_image")
    matches = []
    for index, node in enumerate(function.body[:-1]):
        following = function.body[index + 1]
        if (isinstance(node, ast.For) and isinstance(node.iter, ast.Name)
                and node.iter.id == "alloc_sections"
                and isinstance(following, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "metadata_sections"
                        for target in following.targets)):
            matches.append((node, following))
    assert len(matches) == 1
    loop, following = matches[0]
    lines = source.splitlines(keepends=True)
    prefix = "".join(lines[:loop.lineno - 1])
    # The frozen boundary includes the one blank line before metadata_sections.
    assert not lines[following.lineno - 2].strip()
    suffix = "".join(lines[following.lineno - 2:])
    return loop, prefix, suffix


def _without_negative_summary(source):
    # Invert only the separately source-bound negative-summary candidate. Its
    # actual loop and proof consumer have their own fake-only controls.
    root = Path(__file__).resolve().parents[1]
    progress = ast.parse(_text(root / "tests/elf_relocation_progress_preimage.py"))
    progress_fields = {node.targets[0].id: ast.literal_eval(node.value)
                       for node in progress.body if isinstance(node, ast.Assign)}
    for changed, original in reversed(progress_fields["OWNER_REVERSE_EDITS"]):
        assert source.count(changed) == 1
        source = source.replace(changed, original)
    assert _sha(source) == progress_fields["BASE_OWNER_SHA256"]
    tree = ast.parse(_text(root / "tests/elf_negative_classification_preimage.py"))
    fields = {node.targets[0].id: ast.literal_eval(node.value) for node in tree.body
              if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)}
    lines = source.splitlines(keepends=True)
    for start, end, expected, original in reversed(fields["OWNER_REVERSE_EDITS"]):
        assert "".join(lines[start:end]) == expected
        lines[start:end] = original.splitlines(keepends=True)
    restored = "".join(lines)
    assert _sha(restored) == fields["BASE_OWNER_SHA256"]
    return restored


def _without_image_timing(source):
    # Invert only the optional parameter and the six exact top-level guards
    # (five begin/complete pairs). Historical prefix/suffix hashes below still
    # prove that layout, relocation, cleanup and all other source are unchanged.
    edits = (
        ('    _input_store=None,\n    coordinator_timing=None,\n) -> bytearray:',
         '    _input_store=None,\n) -> bytearray:'),
        ('    """Build a private mutable image, fully validated before publication."""\n'
         '    image_phase_started = 0\n'
         '    if coordinator_timing is not None:\n'
         '        image_phase_started = coordinator_timing.begin("elf_image_select_inputs")\n',
         '    """Build a private mutable image, fully validated before publication."""\n'),
        ('    if coordinator_timing is not None:\n'
         '        coordinator_timing.complete("elf_image_select_inputs", image_phase_started)\n'
         '        image_phase_started = coordinator_timing.begin("elf_image_classify_layout")\n'
         '    alloc_sections: list[tuple[int, int, ElfSection]] = []\n',
         '    alloc_sections: list[tuple[int, int, ElfSection]] = []\n'),
        ('    image_size = max(rx_file_end, rw_file_end, metadata_file_end, header_bytes)\n'
         '    if coordinator_timing is not None:\n'
         '        coordinator_timing.complete("elf_image_classify_layout", image_phase_started)\n'
         '        image_phase_started = coordinator_timing.begin("elf_image_allocate_copy")\n',
         '    image_size = max(rx_file_end, rw_file_end, metadata_file_end, header_bytes)\n'),
        ('    if coordinator_timing is not None:\n'
         '        coordinator_timing.complete("elf_image_allocate_copy", image_phase_started)\n'
         '        image_phase_started = coordinator_timing.begin("elf_image_resolve_relocate")\n'
         '    boundaries = {}\n',
         '    boundaries = {}\n'),
        ('    if coordinator_timing is not None:\n'
         '        coordinator_timing.complete("elf_image_resolve_relocate", image_phase_started)\n'
         '        image_phase_started = coordinator_timing.begin("elf_image_finalize")\n'
         '    if _input_store is not None:\n'
         '        _input_store.clear()\n'
         '    # Validate the exact bytes',
         '    if _input_store is not None:\n'
         '        _input_store.clear()\n'
         '    # Validate the exact bytes'),
        ('    if coordinator_timing is not None:\n'
         '        coordinator_timing.complete("elf_image_finalize", image_phase_started)\n'
         '    return image\n',
         '    return image\n'),
    )
    for instrumented, historical in edits:
        assert source.count(instrumented) == 1
        source = source.replace(instrumented, historical)
    return source


def _runner(loop, environment):
    # Admit only this small loop's known operations before compiling the fake
    # wrapper. Neither complete linker function nor reference loader executes.
    names = {"type", "range", "len", "_symbol_identity"}
    attributes = {"unpack_from", "setdefault", "append", "add", "pop"}
    for node in ast.walk(loop):
        assert not isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef,
                                     ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda))
        if isinstance(node, ast.Call):
            assert ((isinstance(node.func, ast.Name) and node.func.id in names)
                    or (isinstance(node.func, ast.Attribute)
                        and node.func.attr in attributes))
    wrapper = ast.parse(
        "def classify(alloc_sections):\n"
        "    objects = []\n"
        "    definitions = {}\n"
        "    machine = 183\n"
        "    branch_slots = {}\n"
        "    got_identities = []\n"
        "    got_seen = set()\n"
        "    got_reloc_types = {311, 312, 541, 542}\n"
        "    return branch_slots, got_identities\n"
    )
    wrapper.body[0].body.insert(-1, copy.deepcopy(loop))
    namespace = dict(environment)
    namespace["__builtins__"] = {"type": type, "int": int, "range": range,
                                 "len": len, "set": set}
    exec(compile(ast.fix_missing_locations(wrapper),
                 "<classification-only fake source control>", "exec"), namespace)
    return namespace["classify"]


def _fakes(events, projections, *, fail_identity=False):
    class Record:
        def __init__(self, offset, symbol_index, kind, addend):
            projections.append(1)
            self.offset = offset
            self.symbol_index = symbol_index
            self.type = kind
            self.addend = addend

    class Rela:
        size = 24

        def unpack_from(self, payload, position):
            events.append(("unpack", position))
            return payload[position // self.size]

    rela = Rela()

    class Packed:
        def __init__(self, payload):
            self.payload = payload
            self.offset = 0
            self.size = len(payload) * rela.size

        def __iter__(self):
            for position in range(self.offset, self.offset + self.size, rela.size):
                offset, info, addend = rela.unpack_from(self.payload, position)
                yield Record(offset, info >> 32, info & 0xFFFFFFFF, addend)

    class Section:
        def __init__(self, factory):
            self.factory = factory

        @property
        def relocations(self):
            events.append(("relocations",))
            return self.factory()

        @property
        def flags(self):
            events.append(("flags",))
            return 6

    def identity(_objects, _definitions, object_index, symbol_index):
        events.append(("identity", object_index, symbol_index))
        if fail_identity:
            raise ValueError("first classified GOT")
        return ("local", (object_index, symbol_index))

    environment = {"_PackedElfRelocations": Packed, "_RELA": rela,
                   "_symbol_identity": identity, "EM_AARCH64": 183,
                   "SHF_EXECINSTR": 4, "SHF_WRITE": 1,
                   "R_X86_64_GOTTPOFF": 22}
    return environment, Record, Packed, Section


def _packed_case(loop):
    events, projections = [], []
    environment, _Record, Packed, Section = _fakes(events, projections)
    # Three authored raw records: NONE, signed-addend CALL26, and GOT.
    payload = ((0, 0, 0), (4, (1 << 32) | 283, -12), (8, (1 << 32) | 311, 0))
    result = _runner(loop, environment)([(0, 1, Section(lambda: Packed(payload)))])
    assert result == ({(0, 1): {(0, 1, -12): 0}}, [(('local', (0, 1)), 'address')])
    return (result, events), len(projections)


def _generic_case(loop):
    events, projections = [], []
    environment, Record, _Packed, Section = _fakes(events, projections)

    class Cursor:
        def __init__(self):
            self.done = False

        def __iter__(self):
            raise AssertionError("unexpected second iterator protocol call")

        def __next__(self):
            events.append(("next",))
            if self.done:
                raise StopIteration
            self.done = True
            return Record(0, 0, 0, 0)

    class Fresh:
        def __iter__(self):
            events.append(("iter",))
            return Cursor()

        def __del__(self):
            events.append(("container-released",))

    result = _runner(loop, environment)([(0, 1, Section(Fresh))])
    assert events.index(("container-released",)) < events.index(("next",))
    return (result, events), len(projections)


def _exception_case(loop):
    events, projections, views = [], [], []
    environment, _Record, Packed, Section = _fakes(events, projections, fail_identity=True)

    def fresh():
        view = Packed(((0, (1 << 32) | 311, 0),))
        views.append(weakref.ref(view))
        return view

    classify = _runner(loop, environment)
    saved = None
    try:
        classify([(0, 1, Section(fresh))])
    except ValueError as exc:
        saved = exc
    assert saved is not None and saved.__traceback__ is not None
    live = tuple(view() is not None for view in views)
    assert live == (False,)
    return ((type(saved).__name__, str(saved), live, events), len(projections))


def _mixed_lifetime_case(loop):
    events, projections = [], []
    environment, _Record, Packed, Section = _fakes(events, projections)

    class Row:
        type = 0

        def __del__(self):
            events.append(("row-released",))

    class Rows:
        def __iter__(self):
            yield Row()

    payload = ((4, (1 << 32) | 283, -12),)
    result = _runner(loop, environment)([
        (0, 1, Section(Rows)), (0, 2, Section(lambda: Packed(payload)))])
    assert events.index(("unpack", 0)) < events.index(("row-released",))
    assert events.index(("row-released",)) < events.index(("flags",))
    return (result, events), len(projections)


def test_source_bound_classification_loop_fake_controls():
    root = Path(__file__).resolve().parents[1]
    candidate = _without_packed_application(_text(root / "pcc/backend/elf_x86_64.py"))
    reference = ast.parse(_text(root / "tests/elf_link_image_preimage.py"))
    strings = [node.value.value for node in reference.body
               if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
               and any(isinstance(target, ast.Name)
                       and target.id == "ORIGINAL_LINK_IMAGE_SOURCE" for target in node.targets)]
    assert len(strings) == 1
    original = strings[0].replace("\r\n", "\n")
    assert "\r" not in original and _sha(original) == ORIGINAL_FUNCTION_SHA256
    old_loop, _old_prefix, _old_suffix = _classification(original)
    new_loop, prefix, suffix = _classification(_without_image_timing(_without_negative_summary(candidate)))
    # After the narrowly admitted diagnostic additions are removed, preserve
    # the historical whole-owner proof; do not repin it to the new candidate.
    # The prefix includes public construction/parsing/iteration and the suffix
    # includes layout, application, cleanup, finalization and the public parser.
    assert _sha(prefix) == UNCHANGED_PREFIX_SHA256
    assert _sha(suffix) == UNCHANGED_SUFFIX_SHA256
    original_loop_text = "".join(original.splitlines(keepends=True)[
        old_loop.lineno - 1:old_loop.end_lineno])
    assert _sha(prefix + original_loop_text + suffix) == BASE_OWNER_SHA256
    # Seven authored input rows across four scenarios, all globals fake.
    cases = (_packed_case, _generic_case, _exception_case, _mixed_lifetime_case)
    expected_projection_counts = ((3, 0), (1, 1), (1, 0), (1, 0))
    for case, expected_counts in zip(cases, expected_projection_counts):
        before, before_count = case(old_loop)
        after, after_count = case(new_loop)
        assert after == before
        assert (before_count, after_count) == expected_counts
