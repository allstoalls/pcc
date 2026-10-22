"""One finite source/fake/raw-data control; no PCC import or real link/patch.

The owner inverse binds the complete current preimage. Only its application
loop executes, with authored helpers replacing all linker/relocation work.
"""

import ast
import copy
import hashlib
from pathlib import Path
import struct
from types import SimpleNamespace
import weakref

from tests.elf_x86_packed_classification_reference import without_x86_packed_classification
from tests.elf_x86_packed_application_reference import without_x86_packed_application


BASE_OWNER_SHA256 = "8f7544ae5c1ab476ecfb7ff6e285140b596b37926fadeba11310231604434114"


def _text(path):
    text = path.read_bytes().decode("utf-8").replace("\r\n", "\n")
    assert "\r" not in text
    return text


def _without_packed_application(source):
    source = without_x86_packed_classification(source)
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse(_text(root / "tests/elf_packed_application_preimage.py"))
    fields = {node.targets[0].id: ast.literal_eval(node.value)
              for node in tree.body if isinstance(node, ast.Assign)}
    assert fields["BASE_OWNER_SHA256"] == BASE_OWNER_SHA256
    for changed, original in reversed(fields["OWNER_REVERSE_EDITS"]):
        assert source.count(changed) == 1
        source = source.replace(changed, original)
    assert hashlib.sha256(source.encode()).hexdigest() == BASE_OWNER_SHA256
    owner = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)
                 and node.name == "_link_static_executable_image")
    oracle = next(node for node in ast.parse(fields["ORIGINAL_LINK_IMAGE_SOURCE"]).body
                  if isinstance(node, ast.FunctionDef) and node.name == owner.name)
    assert ast.dump(owner, include_attributes=False) == ast.dump(oracle, include_attributes=False)
    return source


def _application(source):
    function = next(node for node in ast.parse(source).body
                    if isinstance(node, ast.FunctionDef)
                    and node.name == "_link_static_executable_image")
    return next(node for node in function.body if isinstance(node, ast.For)
                and isinstance(node.body[0], ast.If)
                and _matches_expression(node.body[0].test, "section.type == SHT_NOBITS and section.relocations"))


def _dump(node):
    return ast.dump(node, include_attributes=False)


def _matches_expression(node, expected):
    # Compare syntax trees within this interpreter, never unparser formatting.
    # Operand ordering, exact operators and every type guard remain significant.
    return _dump(node) == _dump(ast.parse(expected, mode="eval").body)


def _runner(loop, environment):
    loop = copy.deepcopy(loop)
    # Only the known three-name import is replaced by fake bindings. No real
    # package or helper is imported by the extracted application fragment.
    class Imports(ast.NodeTransformer):
        def visit_ImportFrom(self, node):
            assert node.level == 1 and node.module == "elf_aarch64_relocations"
            assert [alias.name for alias in node.names] == [
                "apply_call26_with_veneer", "apply_relocation", "veneer_slot_offset"]
            return ast.Expr(value=ast.Call(func=ast.Name(id="fake_import", ctx=ast.Load()), args=[], keywords=[]))
    loop = Imports().visit(loop)
    allowed = {"type", "range", "len", "resolve_symbol", "_symbol_identity", "tls_offset",
               "tuple", "veneer_slot_offset", "apply_call26_with_veneer",
               "apply_relocation", "ElfError", "str", "_aarch64_relocation_error_context",
               "_write_int", "fake_import", "_report_relocation_progress"}
    for node in ast.walk(loop):
        assert not isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.ClassDef, ast.Lambda))
        if isinstance(node, ast.Call):
            assert ((isinstance(node.func, ast.Name) and node.func.id in allowed)
                    or (isinstance(node.func, ast.Attribute) and node.func.attr in ("pop", "unpack_from")))
    module = ast.parse("def run(alloc_sections):\n    relocation = None\n    pass\n")
    module.body[0].body[-1] = loop
    namespace = dict(environment)
    exec(compile(ast.fix_missing_locations(module), "<application source/fake/raw only>", "exec", dont_inherit=True), namespace)
    return namespace["run"]


def _case(loop, shape, fault=None):
    events, projections, views = [], [], []
    codec = struct.Struct("<QQq")
    rows = ((0, 0, 0), (4, (1 << 32) | 257, -12), (8, (1 << 32) | 257, 5))
    payload = b"".join(codec.pack(*row) for row in rows)

    class Record:
        def __init__(self, offset, symbol_index, kind, addend):
            projections.append(1)
            self.values = dict(offset=offset, symbol_index=symbol_index, type=kind, addend=addend)
        def __getattr__(self, name):
            if shape == "custom":
                events.append(("field", name))
            return self.values[name]

    class Rela:
        size = 24
        def unpack_from(self, data, position):
            events.append(("unpack", position))
            return codec.unpack_from(data, position)

    rela = Rela()
    class Packed:
        def __init__(self, data):
            self.payload, self.offset, self.size = data, 0, len(data)
        def __iter__(self):
            for position in range(self.offset, self.offset + self.size, rela.size):
                offset, info, addend = rela.unpack_from(self.payload, position)
                yield Record(offset, info >> 32, info & 0xFFFFFFFF, addend)

    class Subclass(Packed):
        def __iter__(self):
            events.append(("subclass-iter",))
            return super().__iter__()
    class Integer(int):
        pass
    class Bytes(bytes):
        pass
    class Cursor:
        def __init__(self):
            self.index = 0
        def __iter__(self):
            raise AssertionError("unexpected second GET_ITER")
        def __next__(self):
            events.append(("next",))
            if self.index == len(rows):
                raise StopIteration
            offset, info, addend = rows[self.index]
            self.index += 1
            return Record(offset, info >> 32, info & 0xFFFFFFFF, addend)
    class Fresh:
        def __iter__(self):
            events.append(("iter",))
            return Cursor()
        def __del__(self):
            events.append(("released",))
    class PriorRow:
        type = 0
        def __del__(self):
            events.append(("row-released",))
    class PriorRows:
        def __iter__(self):
            yield PriorRow()
    class Section:
        type, name, size = 1, ".text", 16
        def __init__(self, variant):
            self.variant = variant
        @property
        def relocations(self):
            events.append(("get", self.variant))
            if self.variant == "prior":
                return PriorRows()
            if self.variant == "custom":
                return Fresh()
            value = (Subclass if self.variant == "subclass" else Packed)(payload)
            if self.variant == "empty":
                value.size = 0
            elif self.variant in ("offset", "size"):
                setattr(value, self.variant, Integer(getattr(value, self.variant)))
            elif self.variant == "payload":
                value.payload = Bytes(value.payload)
            views.append(weakref.ref(value))
            return value
    symbol = SimpleNamespace(type=2, section_index=1, name="target")
    def resolve(oi, si):
        events.append(("resolve", oi, si))
        if fault == "resolve":
            raise IndexError("first resolver failure")
        return 32, symbol
    def apply(image, patch, kind, target, place):
        events.append(("apply", patch, kind, target, place))
        if fault == "apply":
            raise ValueError("first site failure")
        if shape == "mutate" and patch == 4:
            # Match generator semantics even when a callback replaces payload
            # and range fields. The range is already fixed; payload is not.
            value = views[-1]()
            value.payload = payload[:-8] + codec.pack(8, (1 << 32) | 257, -7)[-8:]
            value.offset, value.size = 999, 0
    def context(*args):
        events.append(("context", args))
        return "; authored context"
    class Error(Exception):
        pass
    environment = dict(machine=Integer(183) if shape == "machine" else 183,
        EM_AARCH64=183, _PackedElfRelocations=Packed, _RELA=rela,
        SHT_NOBITS=8, R_X86_64_NONE=0, STT_FUNC=2, SHN_UNDEF=0,
        placements={(0, index): SimpleNamespace(file_offset=0, address=0) for index in (1, 2)},
        resolve_symbol=resolve, apply_relocation=apply, image=bytearray(),
        branch_pools={}, branch_slots={}, coordinator_timing=None,
        objects=[SimpleNamespace(symbols=(symbol, symbol))], image_size=16,
        _aarch64_relocation_error_context=context, ElfError=Error,
        fake_import=lambda: events.append(("import",)))
    selected = [(0, 1, Section(shape))]
    if shape in ("mixed", "empty"):
        selected = [(0, 1, Section("prior")), (0, 2, Section(shape))]
    # Source admission/compilation failures are harness failures, never a
    # comparable relocation outcome. Prepare outside the operation's catch.
    run = _runner(loop, environment)
    saved = None
    try:
        run(selected)
    except Exception as error:
        saved = error
    if fault is None:
        assert saved is None, (type(saved).__name__, str(saved))
        expected_sites = 0 if shape == "empty" else 2
        assert sum(event[0] == "resolve" for event in events) == expected_sites
        assert sum(event[0] == "apply" for event in events) == expected_sites
    elif fault == "resolve":
        assert type(saved) is IndexError and str(saved) == "first resolver failure"
        assert saved.__cause__ is None and saved.__context__ is None
        assert saved.__suppress_context__ is False
        assert sum(event[0] == "resolve" for event in events) == 1
        assert not any(event[0] in ("apply", "context") for event in events)
    elif fault == "apply":
        assert type(saved) is Error and str(saved) == "first site failure; authored context"
        assert type(saved.__cause__) is ValueError and str(saved.__cause__) == "first site failure"
        assert saved.__context__ is saved.__cause__ and saved.__suppress_context__ is True
        assert saved.__cause__.__cause__ is None and saved.__cause__.__context__ is None
        assert sum(event[0] == "resolve" for event in events) == 1
        assert sum(event[0] == "apply" for event in events) == 1
        assert sum(event[0] == "context" for event in events) == 1
    else:
        raise AssertionError("unknown authored fault")
    outcome = None if saved is None else (type(saved).__name__, str(saved),
        type(saved.__cause__).__name__ if saved.__cause__ else None,
        str(saved.__cause__) if saved.__cause__ else None)
    assert all(view() is None for view in views)
    if saved is not None:
        assert saved.__traceback__ is not None
    if shape == "custom":
        assert events.index(("released",)) < events.index(("next",))
    if shape == "mixed":
        assert events.index(("unpack", 0)) < events.index(("row-released",)) < events.index(("resolve", 0, 1))
    return (outcome, events), len(projections)


def test_packed_application_bound_source_and_fake_controls():
    root = Path(__file__).resolve().parents[1]
    # Keep this ARM-only AST proof at its original boundary. The later x86
    # specialization has a separate whole-owner inverse and application tests.
    source = without_x86_packed_application(_text(root / "pcc/backend/elf_x86_64.py"))
    before = _without_packed_application(source)
    old, new = _application(before), _application(source)
    assert _dump(ast.Module(body=old.body[:2], type_ignores=[])) == _dump(ast.Module(body=new.body[:2], type_ignores=[]))
    expected_dispatch = (
        "type(machine) is int and machine == EM_AARCH64 and type(relocations) is _PackedElfRelocations "
        "and type(relocations.payload) is bytes and type(relocations.offset) is int and type(relocations.size) is int")
    dispatch = next(node for node in new.body if isinstance(node, ast.If)
                    and _matches_expression(node.test, expected_dispatch))
    generic = next(node for node in dispatch.orelse if isinstance(node, ast.For))
    original = next(node for node in old.body if isinstance(node, ast.For)
                    and _matches_expression(node.iter, "section.relocations"))
    old_progress = next(node for node in old.body if isinstance(node, ast.If)
                        and _matches_expression(node.test, "coordinator_timing is not None"))
    new_progress = next(node for node in new.body if isinstance(node, ast.If)
                        and _matches_expression(node.test, "coordinator_timing is not None"))
    assert _dump(old_progress) == _dump(new_progress)
    assert new.body.index(new_progress) == new.body.index(dispatch) + 1
    assert isinstance(generic, ast.For) and _matches_expression(generic.iter, "relocation_sources.pop()")
    assert _dump(ast.Module(body=original.body, type_ignores=[])) == _dump(ast.Module(body=generic.body, type_ignores=[]))
    assert _matches_expression(dispatch.test, expected_dispatch)
    cleanup = next(node for node in dispatch.body if isinstance(node, ast.Try))
    raw = next(node for node in cleanup.body if isinstance(node, ast.For))
    assert _matches_expression(raw.iter, "range(relocations.offset, relocations.offset + relocations.size, _RELA.size)")
    expected_decode = ast.parse(
        "relocation_offset, info, addend = _RELA.unpack_from(relocations.payload, position)\n"
        "symbol_index, relocation_type = info >> 32, info & 0xFFFFFFFF\n"
        "relocation = None\n")
    assert _dump(ast.Module(body=raw.body[:3], type_ignores=[])) == _dump(expected_decode)
    class Scalars(ast.NodeTransformer):
        def visit_Attribute(self, node):
            if isinstance(node.value, ast.Name) and node.value.id == "relocation":
                return ast.Name(id={"offset": "relocation_offset", "symbol_index": "symbol_index",
                                    "type": "relocation_type", "addend": "addend"}[node.attr], ctx=node.ctx)
            return self.generic_visit(node)
    arm = next(node for node in original.body if isinstance(node, ast.If)
               and _matches_expression(node.test, "machine == EM_AARCH64"))
    expected = copy.deepcopy(original.body[:original.body.index(arm)] + arm.body)
    expected = Scalars().visit(ast.Module(body=expected, type_ignores=[]))
    assert _dump(expected) == _dump(ast.Module(body=raw.body[3:], type_ignores=[]))
    assert _dump(ast.Module(body=cleanup.finalbody, type_ignores=[])) == _dump(ast.parse("del relocations"))
    # This was already one None|generic-record local before application changed;
    # do not silently create another boxed native value-class merge.
    def assignments(text, name):
        owner = next(node for node in ast.parse(text).body if isinstance(node, ast.FunctionDef)
                     and node.name == "_link_static_executable_image")
        return [node for node in ast.walk(owner) if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)]
    assert len(assignments(before, "relocation")) == 2
    assert len(assignments(without_x86_packed_classification(source), "relocation")) == 3
    assert len(assignments(source, "relocation")) == 4
    assert all(isinstance(node.value, ast.Constant) and node.value.value is None
               for node in assignments(source, "relocation"))
    assert all(any(isinstance(node, ast.Name) and node.id == name for node in ast.walk(ast.parse(before)))
               for name in ("position", "info", "addend", "symbol_index", "relocation_type"))
    for shape in ("packed", "custom", "subclass", "offset", "size", "payload", "machine", "mutate", "mixed", "empty"):
        prior, prior_count = _case(old, shape)
        actual, actual_count = _case(new, shape)
        assert actual == prior, shape
        assert (prior_count, actual_count) == ((0, 0) if shape == "empty" else
            (3, 0) if shape in ("packed", "mutate", "mixed") else (3, 3)), shape
    for fault in ("resolve", "apply"):
        prior, prior_count = _case(old, "packed", fault)
        actual, actual_count = _case(new, "packed", fault)
        assert actual == prior and prior[0] is not None
        assert (prior_count, actual_count) == (2, 0)
    # A one-token application edit must fail the whole-owner inverse.
    tampered = source.replace("target + addend, place", "target - addend, place", 1)
    assert tampered != source
    try:
        _without_packed_application(tampered)
    except AssertionError:
        pass
    else:
        raise AssertionError("unreviewed scalar application drift passed")
