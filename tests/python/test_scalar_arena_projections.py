"""Finite source-extracted scalar controls; no PCC imports or compilation.

Only the arena's list-storage branch and isolated record accessors execute.
The production compiler, parser, emitters, unsafe/native operations, and FFI
are never imported or called by this file.
"""

import ast
import copy
from dataclasses import dataclass, FrozenInstanceError
import hashlib
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
KERNEL = "pcc/backend/self_backend_kernel.py"
REFERENCE = ROOT / "tests/scalar_arena_projection_reference.json"
REFERENCE_SHA256 = "c3909b4240049c84b01cbf3ba0dc2a92148f1b919c5a2de64f8af25acd1c77d0"
SITES = json.loads(REFERENCE.read_text())["sites"]
_CENTRAL = (
    "instruction_payload_id_by_id", "value_slot_id", "value_slot_offset",
    "value_slot_type_id", "alloca_offset", "alloca_type_id", "value_register",
    "legacy_value_registers", "terminator_successor_count",
)
_SNAPSHOTS = (
    "instruction_metadata_by_id", "instruction_fact_by_id", "type_header",
    "value_header", "value_state", "slot_record", "terminator_header",
    "terminator_span", "instruction_record",
)


def _tree(path):
    return ast.parse((ROOT / path).read_text())


def _function(path, name, *, original=False):
    if original:
        entry = json.loads(REFERENCE.read_text())["functions"][path + ":" + name]
        return ast.parse(entry["source"]).body[0]
    tree = _tree(path)
    candidates = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                  and node.name == name]
    # IndexedFunctionSeed precedes IndexedFunctionKernel in the shared file.
    return copy.deepcopy(candidates[-1])


def _execute_nodes(nodes, namespace):
    module = ast.Module(body=[ast.ImportFrom(module="__future__",
                       names=[ast.alias(name="annotations")], level=0), *nodes],
                       type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "<scalar-source-extract>", "exec"), namespace)


def _forbidden(*args, **kwargs):
    raise AssertionError("native/unsafe operation is outside finite host controls")


def _namespace(*, original=False):
    namespace = dict(valueclass=lambda cls: dataclass(frozen=True)(cls),
                     _HOST_FACTS_STRUCTURE_OBSERVER=None)
    arena_tree = _tree("pcc/backend/self_backend_value_arena.py")
    records = [copy.deepcopy(node) for node in arena_tree.body
               if isinstance(node, ast.ClassDef) and node.name in ("CompilerInt2", "CompilerInt4")]
    arena = copy.deepcopy(next(node for node in arena_tree.body
                               if isinstance(node, ast.ClassDef) and node.name == "CompilerIntArena"))
    allowed = {"__len__", "get_unchecked", "get2_unchecked", "get4_unchecked",
               "set_unchecked", "set2_unchecked", "set3_unchecked", "append", "clear"}
    arena.body = [node for node in arena.body if isinstance(node, ast.Assign)
                  or isinstance(node, ast.FunctionDef) and node.name in allowed]
    namespace.update({name: _forbidden for name in ("load_i64", "store_i64", "int_to_ptr", "free")})
    _execute_nodes([*records, arena], namespace)
    methods = [_function(KERNEL, name, original=original and name in _CENTRAL)
               for name in (*_SNAPSHOTS, *_CENTRAL)]
    kernel = ast.ClassDef(name="IndexedFunctionKernel", bases=[], keywords=[],
                          body=methods, decorator_list=[], type_params=[])
    namespace.update(TYPE_KIND_VOID=0, TYPE_KIND_INT=1, TYPE_KIND_FP=2,
                     TYPE_KIND_PTR=3, TYPE_KIND_ARRAY=4, TYPE_KIND_STRUCT=5,
                     PARSED_INSTRUCTION_KIND_BR=10, PARSED_INSTRUCTION_KIND_BR_COND=11,
                     PARSED_INSTRUCTION_KIND_SWITCH=12,
                     PARSED_INSTRUCTION_KIND_LOAD=20, PARSED_INSTRUCTION_KIND_BINOP=21,
                     PARSED_INSTRUCTION_KIND_ICMP=22, PARSED_INSTRUCTION_KIND_CAST=23)
    _execute_nodes([kernel, _function(KERNEL, "_scalar_window_is_complete")], namespace)
    return namespace


class _Reads(list):
    def __init__(self, values):
        super().__init__(values)
        self.reads = []

    def __getitem__(self, index):
        self.reads.append(index)
        return super().__getitem__(index)


def _arena(namespace, values, *, subclass=False, closed=False):
    cls = namespace["CompilerIntArena"]
    if subclass:
        cls = type("ArenaSubclass", (cls,), {})
    arena = object.__new__(cls)
    arena._address = 0
    arena._values = _Reads(values)
    arena._length = len(values)
    arena._capacity = 0
    arena._closed = closed
    return arena


def _kernel(namespace, *, subclass=False):
    cls = namespace["IndexedFunctionKernel"]
    if subclass:
        cls = type("KernelSubclass", (cls,), {})
    kernel = cls()
    for name in ("instruction_metadata", "instruction_facts", "type_scalars",
                 "value_scalars", "terminator_scalars", "slot_scalars",
                 "instruction_record_scalars"):
        setattr(kernel, name, _arena(namespace, list(range(48))))
    kernel.value_names = ["a", "b", "c"]
    kernel.alloca_offsets = [101, 102, 103]
    kernel.alloca_type_ids = [201, 202, 203]
    return kernel


def _result(function, *args):
    try:
        return ("value", function(*args))
    except Exception as error:
        return ("error", type(error).__name__, error.args)


def _probe(site, namespace, *, original=False):
    """Execute the actual changed statement, with its input expression supplied.

    This deliberately excludes its enclosing compiler/allocator operation.
    The fallback field expression remains the production conditional expression.
    """
    function = _function(site["path"], site["owner"], original=original)
    accessor = site["original_accessor"]
    related = [entry for entry in SITES if entry["path"] == site["path"]
               and entry["owner"] == site["owner"] and entry["original_accessor"] == accessor]
    ordinal = sorted(related, key=lambda entry: entry["line"]).index(site)
    if original:
        calls = sorted((node for node in ast.walk(function)
                        if isinstance(node, (ast.Assign, ast.AnnAssign))
                        and isinstance(node.value, ast.Call)
                        and isinstance(node.value.func, ast.Attribute)
                        and node.value.func.attr == accessor), key=lambda node: node.lineno)
        # Unchanged multifield calls can appear between changed calls.
        source_line = site["line"] - json.loads(REFERENCE.read_text())["functions"][site["path"] + ":" + site["owner"]]["lineno"] + 1
        statement = next(node for node in calls if node.lineno <= source_line <= node.end_lineno)
        variable = statement.target.id if isinstance(statement, ast.AnnAssign) else statement.targets[0].id
        statement.value.args = [ast.Name(id="record_id", ctx=ast.Load())]
        statement.value.func.value = ast.Name(id="kernel", ctx=ast.Load())
        body = [statement, ast.parse("return " + variable + "." + site["field"]).body[0]]
    else:
        guards = sorted((node for node in ast.walk(function) if isinstance(node, ast.If)
                         and ast.unparse(node.test) in ("type(kernel) is IndexedFunctionKernel",
                                                       "type(self) is IndexedFunctionKernel",
                                                       "type(indexed_kernel) is IndexedFunctionKernel")
                         and any(isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                                 and child.func.attr == accessor for child in ast.walk(node))),
                        key=lambda node: node.lineno)
        guard = guards[ordinal]
        flag = guard.body[1].targets[0].id
        variable = flag.removesuffix("_is_scalar")
        id_name = guard.body[0].targets[0].id
        guard.body[0].value = ast.Name(id="record_id", ctx=ast.Load())
        guard.orelse[0].value.args = [ast.Name(id="record_id", ctx=ast.Load())]

        class Receiver(ast.NodeTransformer):
            def visit_Name(self, node):
                return ast.copy_location(ast.Name(id="kernel", ctx=node.ctx), node) if node.id in ("self", "indexed_kernel") else node

        guard = Receiver().visit(guard)
        expression = variable + "_scalar if " + flag + " else " + variable + "." + site["field"]
        body = [ast.parse(flag + " = False").body[0], guard, ast.parse("return " + expression).body[0]]
    probe = ast.FunctionDef(name="probe", args=ast.arguments(posonlyargs=[],
             args=[ast.arg(arg="kernel"), ast.arg(arg="record_id")], kwonlyargs=[],
             kw_defaults=[], defaults=[]), body=body, decorator_list=[], type_params=[])
    _execute_nodes([probe], namespace)
    return namespace["probe"]


def _reference_text_sha256(content):
    # Git checkouts can use CRLF; only that newline spelling is equivalent.
    # Keep lone CR bytes and every other byte significant to the frozen hash.
    return hashlib.sha256(content.replace(b"\r\n", b"\n")).hexdigest()


def test_frozen_source_oracle_and_complete_site_inventory():
    reference = REFERENCE.read_bytes()
    assert _reference_text_sha256(reference) == REFERENCE_SHA256
    lf = reference.replace(b"\r\n", b"\n")
    crlf = lf.replace(b"\n", b"\r\n")
    assert _reference_text_sha256(lf) == _reference_text_sha256(crlf) == REFERENCE_SHA256
    for changed in (lf + b" ", lf.replace(b"\n", b"\r", 1), lf.replace(b"1afe", b"0afe", 1)):
        assert changed != lf
        assert _reference_text_sha256(changed) != REFERENCE_SHA256
    assert [sum(site["group"] == group for site in SITES) for group in "ABC"] == [5, 40, 10]
    assert len({site["path"] for site in SITES}) == 10
    for site in SITES:
        assert site["window_offset"] in (0, 4)
        assert site["window_offset"] <= site["cell_offset"] <= site["window_offset"] + 3
    # Keep snapshot locals on their original four-cell representation. The
    # native frontend inventories the first value-bearing local assignment;
    # a fast-path None store before the fallback allocates an object slot.
    owners = {(site["path"], site["owner"]) for site in SITES}
    projections = 0
    bindings = 0
    for path, owner in owners:
        function = _function(path, owner)
        guards = [node for node in ast.walk(function)
                  if isinstance(node, ast.If) and isinstance(node.test, ast.Name)
                  and node.test.id.endswith("_is_scalar")]
        names = {node.test.id.removesuffix("_is_scalar") for node in guards}
        for guard in guards:
            assert len(guard.body) == 1
            scalar_read = guard.body[0]
            assert isinstance(scalar_read, ast.Assign)
            assert ast.unparse(scalar_read.targets[0]) == guard.test.id.removesuffix("_is_scalar") + "_scalar"
            assert isinstance(scalar_read.value, ast.Call)
            assert scalar_read.value.func.attr == "get_unchecked"
            projections += 1
        for node in ast.walk(function):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id in names:
                    assert isinstance(node.value, ast.Call)
                    assert isinstance(node.value.func, ast.Attribute)
                    bindings += 1
    assert (len(owners), projections, bindings) == (35, 55, 111)


@pytest.mark.parametrize("site", SITES, ids=lambda site: site["group"] + "-" + Path(site["path"]).stem + "-" + str(site["line"]))
def test_every_projection_reads_one_cell_and_preserves_all_fallbacks(site):
    old, new = _namespace(original=True), _namespace()
    expected, actual = _probe(site, old, original=True), _probe(site, new)
    stride, start, offset = site["stride"], site["window_offset"], site["cell_offset"]

    class IntSubclass(int):
        pass

    class CustomId:
        def __mul__(self, multiplier):
            return 0

    for record_id in (0, 1, -1, -2, 10**30, True, IntSubclass(0), CustomId(), 0.5):
        left, right = _kernel(old), _kernel(new)
        assert _result(actual, right, record_id) == _result(expected, left, record_id)
        left_reads = getattr(left, site["arena"])._values.reads
        right_reads = getattr(right, site["arena"])._values.reads
        if type(record_id) is int and record_id in (0, 1):
            assert left_reads == [record_id * stride + start + index for index in range(4)]
            assert right_reads == [record_id * stride + offset]
        else:
            assert right_reads == left_reads
    for tail in range(4):
        left, right = _kernel(old), _kernel(new)
        values = list(range(stride + start + tail))
        setattr(left, site["arena"], _arena(old, values))
        setattr(right, site["arena"], _arena(new, values))
        assert _result(actual, right, 1) == _result(expected, left, 1)
        assert getattr(right, site["arena"])._values.reads == getattr(left, site["arena"])._values.reads
    for kernel_subclass, arena_subclass, closed in ((True, False, False), (False, True, False), (False, False, True)):
        left, right = _kernel(old, subclass=kernel_subclass), _kernel(new, subclass=kernel_subclass)
        values = [] if closed else list(range(48))
        setattr(left, site["arena"], _arena(old, values, subclass=arena_subclass, closed=closed))
        setattr(right, site["arena"], _arena(new, values, subclass=arena_subclass, closed=closed))
        assert _result(actual, right, 0) == _result(expected, left, 0)
        assert getattr(right, site["arena"])._values.reads == getattr(left, site["arena"])._values.reads


@pytest.mark.parametrize("method", _CENTRAL)
def test_central_getters_match_and_observe_mutation(method):
    old, new = _namespace(original=True), _namespace()
    left, right = _kernel(old), _kernel(new)
    for value in (-1, 0, 2, 9):
        for kernel in (left, right):
            kernel.value_scalars.set_unchecked(2, value)
            kernel.value_scalars.set_unchecked(3, value)
            kernel.value_scalars.set_unchecked(4, value)
            kernel.value_scalars.set_unchecked(5, value)
            kernel.terminator_scalars.set_unchecked(0, 12)
            kernel.terminator_scalars.set_unchecked(6, value)
        arguments = () if method == "legacy_value_registers" else (0,)
        assert _result(getattr(right, method), *arguments) == _result(getattr(left, method), *arguments)
    if method in ("alloca_offset", "alloca_type_id"):
        left.value_scalars, right.value_scalars = _arena(old, []), _arena(new, [])
        assert getattr(right, method)(1) == getattr(left, method)(1)


@pytest.mark.parametrize("method", ("instruction_metadata_by_id", "instruction_fact_by_id", "type_header", "value_header", "value_state", "terminator_header", "terminator_span"))
def test_multifield_snapshots_stay_fresh_frozen_and_retained(method):
    namespace = _namespace()
    kernel = _kernel(namespace)
    first, second = getattr(kernel, method)(0), getattr(kernel, method)(0)
    assert first == second and first is not second
    before = (first.first, first.second, first.third, first.fourth)
    for name in ("instruction_metadata", "instruction_facts", "type_scalars", "value_scalars", "terminator_scalars"):
        arena = getattr(kernel, name)
        for index in range(len(arena)):
            arena.set_unchecked(index, -99)
    assert (first.first, first.second, first.third, first.fourth) == before
    assert getattr(kernel, method)(0).first == -99
    with pytest.raises(FrozenInstanceError):
        first.first = 9


def _type_function(namespace, name, *, original=False):
    _execute_nodes([_function("pcc/backend/self_backend_verify.py", name, original=original)], namespace)
    return namespace[name]


@pytest.mark.parametrize("left_kind,right_kind", ((1, 3), (3, 1), (3, 3), (0, 0), (2, 2), (4, 5)))
def test_paired_reads_short_circuit_properties_and_equal_id_shortcut(left_kind, right_kind):
    def run(original, equal=False, fail_second=False):
        namespace = _namespace(original=original)
        function = _type_function(namespace, "_type_ids_match", original=original)
        events = []

        class Record:
            def __init__(self, record_id):
                self.record_id = record_id

            @property
            def first(self):
                events.append(("first", self.record_id))
                return left_kind if self.record_id == 0 else right_kind

        class FakeKernel:
            @property
            def type_scalars(self):
                raise AssertionError("custom arena property evaluated early")

            def type_header(self, record_id):
                events.append(("header", record_id))
                if fail_second and record_id == 1:
                    raise IndexError("second header fails")
                return Record(record_id)

        result = _result(function, FakeKernel(), 0, 0 if equal else 1)
        return result, events

    for equal, fail_second in ((False, False), (True, False), (False, True)):
        assert run(False, equal, fail_second) == run(True, equal, fail_second)
    assert run(False, True)[1] == []
    assert run(False)[1][:2] == [("header", 0), ("header", 1)]


def test_scalar_type_reads_use_authoritative_arena_and_live_mutations():
    namespace = _namespace()
    function = _type_function(namespace, "_type_id_is_int")
    kernel = _kernel(namespace)
    kernel.instruction_kind_ids = _arena(namespace, [999] * 48)
    type_checks, length_checks = [], []

    def counted_type(value):
        type_checks.append(type(value).__name__)
        return type(value)

    def counted_len(value):
        length_checks.append(value)
        return len(value)

    namespace.update(type=counted_type, len=counted_len)
    for kind in range(6):
        kernel.type_scalars.set_unchecked(0, kind)
        assert function(kernel, 0) == (kind == 1)
    assert kernel.type_scalars._values.reads == [0] * 6
    # These are data-read savings, not a net-cost claim: each normal call also
    # pays three exact-type checks, one full-window bound and guard branches.
    assert len(type_checks) == 18 and len(length_checks) == 6


def test_original_window_not_entire_logical_record_controls_projection():
    namespace = _namespace()
    function = _type_function(namespace, "_type_id_is_int")
    kernel = _kernel(namespace)
    kernel.type_scalars = _arena(namespace, [1, 64, 0, -1])
    assert function(kernel, 0)
    assert kernel.type_scalars._values.reads == [0]
    kernel.value_scalars = _arena(namespace, [0, 1, 2, 3])
    assert kernel.value_slot_id(0) == 2
    assert kernel.value_scalars._values.reads == [2]
    assert _result(kernel.value_register, 0) == ("error", "IndexError", ("list index out of range",))


@pytest.mark.parametrize("kind,expected", ((10, 1), (11, 2), (12, 8), (99, 0)))
def test_terminator_count_only_reads_span_for_switch(kind, expected):
    namespace = _namespace()
    kernel = _kernel(namespace)
    kernel.terminator_scalars.set_unchecked(0, kind)
    kernel.terminator_scalars.set_unchecked(6, 7)
    assert kernel.terminator_successor_count(0) == expected
    assert kernel.terminator_scalars._values.reads == ([0, 6] if kind == 12 else [0])


@pytest.mark.parametrize("unsafe", (False, True))
def test_facts_fallback_preserves_repeated_and_skipped_properties(unsafe):
    from tests.python import test_aarch64_regalloc_single_seed as facts

    def run(original):
        namespace = _namespace(original=original)
        namespace.update(CompilerInt4=facts.Record, PARSED_INSTRUCTION_KINDS=tuple(range(16)),
                         PARSED_INSTRUCTION_KIND_CALL=2, _POOL_REJECTED_INSTRUCTION_KIND_IDS=(14, 15))
        path = "pcc/backend/self_backend_aarch64_darwin_regalloc.py"
        _execute_nodes([_function(path, "_note_use_block"),
                        _function(path, "_function_level_facts", original=original)], namespace)
        blocks = facts.case([[]], {0: [0, 1]}, kinds={0: 14 if unsafe else 1})
        events = []

        class PropertyRecord:
            def __init__(self, value):
                self.value = value

            @property
            def first(self):
                events.append(("first", self.value))
                return self.value

        class Kernel(facts.FakeKernel):
            @property
            def instruction_metadata(self):
                raise AssertionError("fake arena was read")

            def instruction_metadata_by_id(self, instruction):
                self.read("instruction_metadata_by_id", instruction)
                return PropertyRecord(self.instructions[instruction][0])

        kernel = Kernel(blocks, events=events)
        return facts.evaluate(namespace["_function_level_facts"], kernel), events

    assert run(False) == run(True)
    assert sum(event[0] == "first" for event in run(False)[1]) == (4 if unsafe else 6)


@pytest.mark.parametrize("shape", ("low-reuse", "high-reuse"))
def test_real_list_arena_facts_reduce_payload_reads_without_changing_cfg(shape):
    from tests.python import test_aarch64_regalloc_single_seed as facts

    def run(original):
        namespace = _namespace(original=original)
        values = list(range(10))
        uses = {2: values} if shape == "high-reuse" else {index: [index] for index in range(3)}
        blocks = facts.case([[1], [2], []], uses)

        class PackedKernel(facts.FakeKernel):
            def __init__(self):
                super().__init__(blocks)
                metadata, definitions = [], []
                for kind, destination, used in self.instructions:
                    metadata.extend((kind, 0, 0, 0))
                    definitions.extend((destination, len(used), 0, 0))
                self.instruction_metadata = _arena(namespace, metadata)
                self.instruction_facts = _arena(namespace, definitions)
                self.instruction_kind_ids = _arena(namespace, [999] * len(self.instructions))

            def instruction_metadata_by_id(self, instruction):
                self.read("instruction_metadata_by_id", instruction)
                return self.instruction_metadata.get4_unchecked(instruction)

            def instruction_fact_by_id(self, instruction):
                self.read("instruction_fact_by_id", instruction)
                return self.instruction_facts.get4_unchecked(instruction)

        namespace.update(IndexedFunctionKernel=PackedKernel, CompilerInt4=facts.Record,
                         PARSED_INSTRUCTION_KINDS=tuple(range(16)),
                         PARSED_INSTRUCTION_KIND_CALL=2, _POOL_REJECTED_INSTRUCTION_KIND_IDS=(14, 15))
        path = "pcc/backend/self_backend_aarch64_darwin_regalloc.py"
        _execute_nodes([_function(path, "_note_use_block"),
                        _function(path, "_function_level_facts", original=original)], namespace)
        kernel = PackedKernel()
        result = facts.evaluate(namespace["_function_level_facts"], kernel)
        events = [event for event in kernel.events if event[0] not in
                  ("instruction_metadata_by_id", "instruction_fact_by_id")]
        reads = len(kernel.instruction_metadata._values.reads) + len(kernel.instruction_facts._values.reads)
        return result, events, reads, len(kernel.instructions)

    old, new = run(True), run(False)
    assert new[:2] == old[:2]
    assert old[2] == 12 * old[3]
    assert new[2] == 3 * new[3]


def test_mixed_fallback_and_scalar_reads_keep_real_snapshots_fresh():
    # A previous version monkeypatched the exact kernel to return a custom
    # weakref-observed object and required its temporary local to be released
    # by the next scalar read. That is not the CompilerInt4 snapshot contract.
    # Keep real retained snapshots fresh/frozen without introducing a None
    # binding (or extra ownership flags) into native aggregate local storage.
    namespace = _namespace()
    site = next(site for site in SITES if site["path"].endswith("verify.py") and site["line"] == 166)
    _probe(site, namespace)
    function = _function(site["path"], site["owner"])
    flag, guard = function.body[:2]
    # This is the exact guarded assignment reused in a finite loop. Integer
    # subclasses force selected iterations through the original accessor.
    guard.body[0].value = ast.Name(id="type_id", ctx=ast.Load())
    loop = ast.For(target=ast.Name(id="type_id", ctx=ast.Store()),
                   iter=ast.Name(id="ids", ctx=ast.Load()),
                   body=[flag, guard, *ast.parse(
                       "if not header_is_scalar:\n"
                       "    retained.append(header)\n"
                       "observed.append(header_scalar if header_is_scalar else header.first)\n"
                       "kernel.type_scalars.set_unchecked(0, len(observed) + 10)\n"
                   ).body], orelse=[])
    probe = ast.parse("def repeated(kernel, ids, observed, retained):\n    pass\n").body[0]
    probe.body = [loop]
    _execute_nodes([probe], namespace)
    kernel = _kernel(namespace)
    kernel.type_scalars.set_unchecked(0, 7)

    class Id(int):
        pass

    observed = []
    retained = []
    namespace["repeated"](kernel, [0, Id(0), 0, Id(0)], observed, retained)
    assert observed == [7, 11, 12, 13]
    assert len(retained) == 2 and retained[0] is not retained[1]
    assert all(type(record) is namespace["CompilerInt4"] for record in retained)
    assert [record.first for record in retained] == [11, 13]
    assert kernel.type_header(0).first == 14
    assert [record.first for record in retained] == [11, 13]
    for record in retained:
        with pytest.raises(FrozenInstanceError):
            record.first = 99
