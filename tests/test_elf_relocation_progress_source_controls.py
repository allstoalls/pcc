"""One bounded source/fake-only control; no PCC import, linking, I/O or clock."""

import ast

from tests.test_elf_packed_application_source_controls import _without_packed_application
import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace
import weakref


def _text(path):
    value = path.read_bytes().decode('utf-8').replace('\r\n', '\n')
    assert '\r' not in value
    return value


def _function(source, name):
    return next(node for node in ast.parse(source).body
                if isinstance(node, ast.FunctionDef) and node.name == name)


def _dump(node):
    return ast.dump(node, include_attributes=False)


def _extract(node, namespace):
    module = ast.fix_missing_locations(ast.Module(body=[copy.deepcopy(node)], type_ignores=[]))
    exec(compile(module, '<relocation progress source/fake only>', 'exec', dont_inherit=True), namespace)
    return namespace[node.name]


def test_relocation_progress_bound_source_and_fake_controls():
    root = Path(__file__).resolve().parents[1]
    source = _without_packed_application(_text(root / 'pcc/backend/elf_x86_64.py'))
    reference = ast.parse(_text(root / 'tests/elf_relocation_progress_preimage.py'))
    fields = {node.targets[0].id: ast.literal_eval(node.value)
              for node in reference.body if isinstance(node, ast.Assign)}
    assert fields['BASE_OWNER_SHA256'] == 'f729763ff5115109f70859af254da4e335b54c55192426a902ed7495a3e78305'

    def restore(text):
        for changed, original in reversed(fields['OWNER_REVERSE_EDITS']):
            assert text.count(changed) == 1
            text = text.replace(changed, original)
        assert hashlib.sha256(text.encode()).hexdigest() == fields['BASE_OWNER_SHA256']
        return text

    original = restore(source)
    old = _function(original, '_link_static_executable_image')
    new = _function(source, '_link_static_executable_image')
    assert _dump(old) == _dump(_function(fields['ORIGINAL_LINK_IMAGE_SOURCE'], '_link_static_executable_image'))
    # Bind the entire owner, all adjacent records and the parser/validator too.
    for kind, names in ((ast.ClassDef, ('ElfRelocation', '_PackedElfRelocations', 'ElfSection', 'ElfSymbol', 'ElfObject')),
                        (ast.FunctionDef, ('_validate_object', '_parse_relocatable', '_parse_relocatable_indexed', '_symbol_identity'))):
        for name in names:
            before = next(node for node in ast.parse(original).body if isinstance(node, kind) and node.name == name)
            after = next(node for node in ast.parse(source).body if isinstance(node, kind) and node.name == name)
            assert _dump(before) == _dump(after)

    def application(function):
        return next(node for node in function.body if isinstance(node, ast.For)
                    and any(isinstance(child, ast.For) and ast.unparse(child.target) == 'relocation'
                            and ast.unparse(child.iter) == 'section.relocations' for child in node.body)
                    and ast.unparse(node.body[0].test) == 'section.type == SHT_NOBITS and section.relocations')

    before_loop, after_loop = application(old), application(new)
    assert _dump(before_loop.target) == _dump(after_loop.target)
    assert _dump(before_loop.iter) == _dump(after_loop.iter)
    assert _dump(ast.Module(body=before_loop.body, type_ignores=[])) == _dump(
        ast.Module(body=after_loop.body[:-1], type_ignores=[]))
    progress = after_loop.body[-1]
    assert isinstance(progress, ast.If) and ast.unparse(progress.test) == 'coordinator_timing is not None'
    assert all(not isinstance(node, (ast.For, ast.While)) for node in ast.walk(progress))
    setup = next(node for node in new.body if isinstance(node, ast.If)
                 and any(isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                         and call.func.id == '_elf_relocation_progress_counts' for call in ast.walk(node)))
    got = next(node for node in new.body if isinstance(node, ast.If)
               and any(isinstance(value, ast.Constant) and value.value == 'got_complete' for value in ast.walk(node)))
    assert ast.unparse(setup.test) == ast.unparse(got.test) == 'coordinator_timing is not None'
    setup_index, got_index = new.body.index(setup), new.body.index(got)
    assert isinstance(new.body[setup_index + 1], ast.FunctionDef) and new.body[setup_index + 1].name == 'resolve_symbol'
    assert ast.unparse(new.body[got_index - 1].iter) == 'got_offsets.items()'
    assert new.body[got_index + 1] is after_loop
    # The inverse must reject each unauthorized observation/order change.
    bad_fragments = (
        ('for relocation in section.relocations:\n            if relocation.type',
         'for relocation in section.relocations:\n            injected_count = 1\n            if relocation.type'),
        ('    boundaries = {}', '    boundaries = {}\n    injected_observation = None'),
        ('        _input_store.clear()', '        _input_store.clear()\n        injected_cleanup = None'),
    )
    for expected, changed in bad_fragments:
        assert expected in source
        try:
            restore(source.replace(expected, changed, 1))
        except AssertionError:
            pass
        else:
            raise AssertionError('unexpected source drift passed the frozen inverse')

    class InputSection:
        def __init__(self, count):
            self.relocation_count = count
        @property
        def relocations(self):
            raise AssertionError('metadata touched staged relocations')
        @property
        def data(self):
            raise AssertionError('metadata touched staged data')
        @property
        def size(self):
            raise AssertionError('metadata touched staged size')

    class Section:
        def __init__(self, rows):
            self.relocations = rows

    class Packed:
        def __init__(self, count):
            self.payload, self.offset, self.size = bytes(count * 24), 0, count * 24

    helper = copy.deepcopy(_function(source, '_elf_relocation_progress_counts'))
    import_node = helper.body[1].body[0]  # docstring, then guarded exact import
    assert isinstance(import_node, ast.ImportFrom)
    assert import_node.module == 'owned_elf_inputs' and [name.name for name in import_node.names] == ['_InputSection']
    helper.body[1].body.pop(0)
    for node in ast.walk(helper):
        assert not isinstance(node, (ast.Import, ast.ImportFrom))
        if isinstance(node, ast.For):
            assert ast.unparse(node.iter) == 'alloc_sections'
        if isinstance(node, ast.Attribute):
            assert node.attr not in ('data', 'symbols', 'load')
    namespace = dict(_InputSection=InputSection, ElfSection=Section, _PackedElfRelocations=Packed)
    count = _extract(helper, namespace)
    rows = [(0, 1, InputSection(3)), (0, 2, Section((1, 2))), (1, 1, Section(Packed(4)))]
    assert count(rows) == [9, 0, 0, 0, 3, 2, 4]
    assert count([]) == [0, 0, 0, 0]
    assert all(type(value) is int for value in count(rows))
    limit = ((1 << 63) - 1) // 8
    class Integer(int):
        pass
    class Unknown:
        def __len__(self):
            raise AssertionError('unknown length hook called')
        def __iter__(self):
            raise AssertionError('unknown iterator called')
    class SectionSubclass(Section):
        @property
        def relocations(self):
            raise AssertionError('subclass property called')
    for invalid in (-1, True, Integer(1), limit + 1):
        assert count([(0, 1, InputSection(invalid))]) is None
    assert count([(0, 1, InputSection(limit)), (0, 2, InputSection(1))]) is None
    assert count([(0, 1, Section(Unknown()))]) is None
    subclass = object.__new__(SectionSubclass)
    assert count([(0, 1, subclass)]) is None
    assert count(tuple(rows)) is None
    for attribute, invalid in (('offset', -1), ('offset', True), ('size', 25), ('size', -1), ('size', Integer(24))):
        packed = Packed(1)
        setattr(packed, attribute, invalid)
        assert count([(0, 1, Section(packed))]) is None

    timing_tree = ast.parse(_text(root / 'pcc/backend/indexed_phase_timing.py'))
    timing = next(node for node in timing_tree.body if isinstance(node, ast.ClassDef) and node.name == 'CoarsePhaseTiming')
    emitter = next(node for node in timing.body if isinstance(node, ast.FunctionDef) and node.name == 'elf_relocation_progress')
    assert not any(isinstance(node, ast.Name) and node.id == 'time' for node in ast.walk(emitter))
    assert not any(isinstance(node, ast.Attribute) and node.attr == 'failed' for node in ast.walk(emitter))
    class Sink:
        def __init__(self):
            self.lines, self.flushes, self.fault = [], 0, None
        def write(self, line):
            self.lines.append(line)
            if self.fault == 'write':
                raise BaseException('authored output failure')
        def flush(self):
            self.flushes += 1
            if self.fault == 'flush':
                raise BaseException('authored output failure')
    sink = Sink()
    emit = _extract(emitter, {'sys': SimpleNamespace(stderr=sink)})
    assert emit(None, 'scale', 0, 3, 0, 9)
    assert emit(None, 'got_complete', 0, 3, 0, 9)
    assert emit(None, 'sections', 2, 3, 5, 9)
    assert emit(None, 'scale_unknown', 0, 0, 0, 0)
    assert sink.lines == [
        'pcc elf relocation progress event=scale count_basis=initial_resident total_relocations_known=1 total_relocations=9\n',
        'pcc elf relocation progress event=got_complete\n',
        'pcc elf relocation progress event=sections count_basis=initial_resident completed_sections=2 total_sections=3 completed_section_initial_relocations=5 total_relocations=9\n',
        'pcc elf relocation progress event=scale total_relocations_known=0\n']
    for event, values in (('bad/path\n', (0, 0, 0, 0)), ('scale', (0, 1, 0, True)),
                          ('sections', (2, 1, 1, 1)), ('sections', (1, 1, 0, 0)),
                          ('scale', (0, 1, 0, limit + 1)), ('scale_unknown', (0, 1, 0, 0))):
        assert emit(None, event, *values) is False
    for fault in ('write', 'flush'):
        sink.fault = fault
        assert emit(None, 'scale', 0, 1, 0, 1) is False
    sink.fault = None

    report = _extract(_function(source, '_report_relocation_progress'), {})
    # Only the selected new control fragments execute, never the actual linker.
    skeleton = ast.parse('def run(alloc_sections, coordinator_timing):\n    pass\n').body[0]
    fake_loop = ast.parse('for object_index, section_index, section in alloc_sections:\n    operation(section)\n').body[0]
    fake_loop.body.append(copy.deepcopy(progress))
    skeleton.body = [copy.deepcopy(setup), ast.parse('got_operation()').body[0], copy.deepcopy(got), fake_loop]
    events = []
    class Evidence:
        def __init__(self, fail=None):
            self.rows, self.fail = [], fail
        def elf_relocation_progress(self, *values):
            self.rows.append(values)
            if values[0] == self.fail:
                raise BaseException('authored sink failure')
            return True
    def operation(section):
        events.append('section')
        if getattr(section, 'fail', False):
            raise ValueError('original section failure')
    def got_operation():
        events.append('got')
    environment = dict(_elf_relocation_progress_counts=count, _report_relocation_progress=report,
                       operation=operation, got_operation=got_operation)
    run = _extract(skeleton, environment)
    # 17 physical rows across 18 resident sections; no per-row work here.
    sections = [(0, index, InputSection(value)) for index, value in enumerate((0,) + (1,) * 17)]
    evidence = Evidence()
    run(sections, evidence)
    assert [row[0] for row in evidence.rows] == ['scale', 'got_complete'] + ['sections'] * 8
    assert evidence.rows[-1] == ('sections', 18, 18, 17, 17)
    skew = Evidence()
    run([(0, 1, InputSection(0)), (0, 2, InputSection(64)), (0, 3, InputSection(0))], skew)
    assert skew.rows == [('scale', 0, 3, 0, 64), ('got_complete', 0, 0, 0, 0), ('sections', 2, 3, 64, 64)]
    zero = Evidence()
    run([(0, 1, InputSection(0))], zero)
    assert [row[0] for row in zero.rows] == ['scale', 'got_complete']
    unknown = Evidence()
    run([(0, 1, Section(Unknown()))], unknown)
    assert unknown.rows == [('scale_unknown', 0, 0, 0, 0), ('got_complete', 0, 0, 0, 0)]
    for fault in ('scale', 'got_complete', 'sections'):
        evidence = Evidence(fault)
        run(sections, evidence)
        assert evidence.rows[-1][0] == fault
        assert len(evidence.rows) == {'scale': 1, 'got_complete': 2, 'sections': 3}[fault]
    failing = InputSection(1)
    failing.fail = True
    evidence = Evidence()
    try:
        run([(0, 1, failing)], evidence)
    except ValueError as error:
        assert str(error) == 'original section failure'
    else:
        raise AssertionError('original failure lost')
    assert [row[0] for row in evidence.rows] == ['scale', 'got_complete']
    def forbidden(*args):
        raise AssertionError('disabled observation ran')
    disabled = _extract(skeleton, dict(environment, _elf_relocation_progress_counts=forbidden,
                                       _report_relocation_progress=forbidden))
    disabled(sections, None)
    # Returned scale owns only numbers, including after source owners disappear.
    owner = InputSection(7)
    reference = weakref.ref(owner)
    scalars = count([(0, 1, owner)])
    del owner
    assert reference() is None and scalars == [7, 0, 0, 0, 7]
