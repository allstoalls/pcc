"""Finite stdlib/fake-CFG facts controls; no PCC imports, parse or emission."""

import ast
import copy
import hashlib
from pathlib import Path
import random

import pytest


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "pcc/backend/self_backend_aarch64_darwin_regalloc.py"
ORACLE = ROOT / "tests/aarch64_regalloc_facts_reference.py"
ORACLE_FUNCTION_SHA256 = "c3233aa65b468c080334c5871d63899ec34e1d21b92e6495016e4c2c2bf2fca6"


class Record:
    def __init__(self, first=-1, second=0, third=0, fourth=-1):
        self.first, self.second, self.third, self.fourth = first, second, third, fourth


def case(edges, uses=None, homes=(), phis=None, errors=None, kinds=None, terms=None):
    uses, phis, errors, kinds, terms = uses or {}, phis or {}, errors or {}, kinds or {}, terms or {}
    blocks = [dict(instructions=[], edges=list(targets), phis=list(phis.get(index, ())),
                   errors=list(errors.get(index, ())), terms=list(terms.get(index, ())))
              for index, targets in enumerate(edges)]
    for value, home in homes:
        blocks[home]["instructions"].append((1, value, []))
    for index, values in uses.items():
        for value in values:
            blocks[index]["instructions"].append((kinds.get(index, 1), -1, [value]))
    return blocks


class FakeKernel:
    instruction_metadata = None
    instruction_facts = None

    def __init__(self, blocks, fail=None, events=None):
        self.block_names = ["b" + str(index) for index in range(len(blocks))]
        self.blocks, self.fail, self.events = blocks, fail, events if events is not None else []
        self.instructions, self.facts, self.phis, self.incoming = [], [], [], []
        self.phi_facts, self.cases, self.spans, self.error_targets, self.error_spans = [], [], [], [], []
        for block in blocks:
            self.facts.append(Record(len(self.instructions), len(block["instructions"])))
            self.instructions.extend(block["instructions"])
            self.phi_facts.append(Record(len(self.phis), len(block["phis"])))
            for value, incoming in block["phis"]:
                self.phis.append(Record(value, 0, len(self.incoming), len(incoming)))
                self.incoming.extend(Record(value, predecessor) for value, predecessor in incoming)
            edges = block["edges"]
            self.spans.append(Record(edges[1] if len(edges) > 1 else -1,
                                     len(self.cases), max(0, len(edges) - 2)))
            self.cases.extend(Record(0, target) for target in edges[2:])
            self.error_spans.append(Record(len(self.error_targets), len(block["errors"])))
            self.error_targets.extend(block["errors"])

    def read(self, name, *args):
        self.events.append((name, args))
        if self.fail is not None and name == self.fail[0]:
            raise self.fail[1]("fake " + name)

    def block_fact(self, block):
        self.read("block_fact", block)
        return self.facts[block]

    def instruction_metadata_by_id(self, instruction):
        self.read("instruction_metadata_by_id", instruction)
        return Record(self.instructions[instruction][0])

    def instruction_fact_by_id(self, instruction):
        self.read("instruction_fact_by_id", instruction)
        return Record(self.instructions[instruction][1])

    def instruction_use_count(self, block, instruction):
        self.read("instruction_use_count", block, instruction)
        return len(self.blocks[block]["instructions"][instruction][2])

    def instruction_use_id(self, block, instruction, use):
        self.read("instruction_use_id", block, instruction, use)
        return self.blocks[block]["instructions"][instruction][2][use]

    def terminator_use_id(self, block, use):
        self.read("terminator_use_id", block, use)
        return self.blocks[block]["terms"][use]

    def block_phi_fact(self, block):
        self.read("block_phi_fact", block)
        return self.phi_facts[block]

    def phi_record(self, phi):
        self.read("phi_record", phi)
        return self.phis[phi]

    def phi_incoming(self, incoming):
        self.read("phi_incoming", incoming)
        return self.incoming[incoming]

    def terminator_header(self, block):
        self.read("terminator_header", block)
        edges = self.blocks[block]["edges"]
        return Record(fourth=edges[0] if edges else -1)

    def terminator_span(self, block):
        self.read("terminator_span", block)
        return self.spans[block]

    def terminator_case(self, index):
        self.read("terminator_case", index)
        return self.cases[index]

    def inline_error_edge_span(self, block):
        self.read("inline_error_edge_span", block)
        return self.error_spans[block]

    def inline_error_edge_target(self, index):
        self.read("inline_error_edge_target", index)
        return self.error_targets[index]


# The pin is the immutable 7c full AST, including empty fields and contexts.
# Python 3.15 adds an eager-import field; normalize only that schema detail.
PRE_SUMMARY_AST_SHA256 = "5d3488c0dab0c223b2a6751b16eeb32bdf8ae64a69918e146221b15595f3bf26"


def canonical_facts_ast_dump(node):
    """Full identity dump; only absent/eager import schemas are equivalent."""
    normalized = copy.deepcopy(node)
    for child in ast.walk(normalized):
        if isinstance(child, (ast.Import, ast.ImportFrom)):
            eager = getattr(child, "is_lazy", 0)
            assert type(eager) in (int, bool) and eager == 0
            # This affects the identity copy only. Never drop another field,
            # and never equate a lazy import with the old eager-import source.
            child._fields = tuple(name for name in child._fields if name != "is_lazy")
    return ast.dump(normalized, show_empty=True)


def without_home_summary(function):
    """Recover the exact 7c1218d memo/scalar preimage, not the fc32 oracle."""
    removed = dict(assign=0, results=0, blocks=0, publish=0, clear=0)
    class Remove(ast.NodeTransformer):
        def visit_Assign(self, node):
            if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "summary_enabled":
                removed["assign"] += 1
                return None
            return self.generic_visit(node)
        def visit_AnnAssign(self, node):
            if isinstance(node.target, ast.Name) and node.target.id in ("summary_low", "summary_high"):
                removed["results"] += 1
                return None
            return self.generic_visit(node)
        def visit_If(self, node):
            condition = ast.unparse(node.test)
            if condition == "summary_enabled":
                removed["blocks"] += 1
                return None
            if condition == "summary_enabled and value_id in summary_low":
                removed["publish"] += 1
                return None
            return self.generic_visit(node)
        def visit_Expr(self, node):
            if ast.unparse(node.value) in ("summary_low.clear()", "summary_high.clear()"):
                removed["clear"] += 1
                return None
            return self.generic_visit(node)
    result = Remove().visit(function)
    assert removed == dict(assign=2, results=2, blocks=2, publish=1, clear=2)
    assert hashlib.sha256(canonical_facts_ast_dump(result).encode()).hexdigest() == PRE_SUMMARY_AST_SHA256
    return result


def load_facts(path, *, count=False, canonical=False, count_summary=False, transform=None,
               pre_summary=False):
    source = ast.parse(path.read_text())
    production = ast.parse(SOURCE.read_text())
    helper = next(node for node in production.body if isinstance(node, ast.FunctionDef)
                  and node.name == "_note_use_block")
    function = next(node for node in source.body if isinstance(node, ast.FunctionDef)
                    and node.name == "_function_level_facts")
    if pre_summary:
        assert path == SOURCE
        function = without_home_summary(function)
    if path == ORACLE:
        frozen = ast.get_source_segment(path.read_text(), function)
        assert hashlib.sha256(frozen.encode()).hexdigest() == ORACLE_FUNCTION_SHA256
    counts = dict(pops=0, edges=0, hits=0, writes=0)
    if count_summary:
        counts.update(summary_nodes=0, summary_edges=0, summary_fold_edges=0,
                      summary_members=0, summary_folds=0, summary_resets=0,
                      summary_components=0, summary_values=0, summary_homes=0,
                      seed_visits=0, summary_frames=0, summary_slots=0)

    class Count(ast.NodeTransformer):
        pops = 0
        edges = 0

        def bump(self, key):
            return ast.parse("_counts[" + repr(key) + "] += 1").body[0]

        def visit_Expr(self, node):
            self.generic_visit(node)
            expression = ast.unparse(node.value)
            if count_summary:
                if expression.startswith("summary_nodes.append("):
                    return [node, self.bump("summary_nodes")]
                if expression == "summary_nodes.pop()":
                    return [node, self.bump("summary_frames")]
                if expression == "component_low.append(lowest)":
                    return [node, self.bump("summary_components")]
            return node

        def visit_Assign(self, node):
            self.generic_visit(node)
            if count_summary:
                target = ast.unparse(node.targets[0])
                if target == "predecessor" and ast.unparse(node.value) == "predecessors[node_id][edge_index]":
                    return [node, self.bump("summary_edges")]
                if target == "summary_component[member_id]" and ast.unparse(node.value) == "component_id":
                    return [node, self.bump("summary_members")]
                if target in ("summary_index", "summary_link", "summary_component") and isinstance(node.value, ast.BinOp):
                    return [node, ast.parse("_counts['summary_slots'] += len(predecessors)").body[0]]
                if target == "summary_low[value_id]":
                    return [node, self.bump("summary_values")]
            return node

        def visit_While(self, node):
            self.generic_visit(node)
            if isinstance(node.test, ast.Name) and node.test.id == "pending":
                node.body.insert(0, self.bump("pops"))
                self.pops += 1
            return node

        def visit_For(self, node):
            self.generic_visit(node)
            if ast.unparse(node.target) == "predecessor" and ast.unparse(node.iter) == "predecessors[block_id]":
                node.body.insert(0, self.bump("edges"))
                self.edges += 1
            if count_summary:
                iterator = ast.unparse(node.iter)
                if iterator == "predecessors[member_id]":
                    node.body.insert(0, self.bump("summary_fold_edges"))
                elif iterator == "summary_members":
                    node.body.insert(0, self.bump("summary_folds"))
                elif iterator == "summary_touched":
                    node.body.insert(0, self.bump("summary_resets"))
                elif iterator == "group_values":
                    # One group_values loop per admitted home.
                    return [self.bump("summary_homes"), node]
                if ast.unparse(node.target) == "seed" and iterator == "use_blocks[value_id]":
                    node.body.insert(0, self.bump("seed_visits"))
            return node

        def visit_If(self, node):
            self.generic_visit(node)
            if ast.unparse(node.test) == "cached_low >= 0":
                node.body.insert(0, self.bump("hits"))
            if isinstance(node.test, ast.Name) and node.test.id == "cache_miss":
                node.body.insert(0, self.bump("writes"))
            return node

    loops = [node for node in ast.walk(function) if isinstance(node, ast.While)
             and isinstance(node.test, ast.Name) and node.test.id == "pending"]
    if path == ORACLE:
        assert len(loops) == 1
    else:
        assert len(loops) == 2
        plain = next(node for node in loops if len(node.body) == 4)
        observed = next(node for node in loops if len(node.body) == 6)
        # Do not weaken the traversal oracle: the prefix's four business
        # statements and the untouched original body must be AST-identical.
        reference = ast.parse(ORACLE.read_text())
        old_loop = next(node for node in ast.walk(reference) if isinstance(node, ast.While)
                        and isinstance(node.test, ast.Name) and node.test.id == "pending")
        canonical_loop = ast.dump(ast.Module(body=old_loop.body, type_ignores=[]))
        assert ast.dump(ast.Module(body=plain.body, type_ignores=[])) == canonical_loop
        assert ast.dump(ast.Module(body=observed.body[:4], type_ignores=[])) == canonical_loop
    if count or count_summary:
        counter = Count()
        function = counter.visit(function)
        assert (counter.pops, counter.edges) == ((1, 1) if path == ORACLE else (2, 2))
    if transform is not None:
        function = transform.visit(function)
    tree = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                           helper, function], type_ignores=[])
    namespace = dict(CompilerInt4=Record,
                     IndexedFunctionKernel=FakeKernel if canonical else type("ExactKernel", (), {}),
                     _scalar_window_is_complete=lambda *args: False,
                     PARSED_INSTRUCTION_KINDS=tuple(range(16)),
                     PARSED_INSTRUCTION_KIND_CALL=2, _POOL_REJECTED_INSTRUCTION_KIND_IDS=(14, 15),
                     _counts=counts, _HOST_FACTS_STRUCTURE_OBSERVER=None)
    exec(compile(ast.fix_missing_locations(tree), str(path), "exec"), namespace)
    return namespace["_function_level_facts"], counts


def evaluate(function, kernel):
    try:
        values = function(kernel)
        assert len(values) == 7
        return ("value", tuple(tuple(value.items()) if isinstance(value, dict) else tuple(value)
                               for value in values))
    except Exception as error:
        return ("error", type(error), error.args)


def compare(builder, *, count=False, functions=None):
    if functions is None:
        expected, old_counts = load_facts(ORACLE, count=count)
        actual, new_counts = load_facts(SOURCE, count=count)
    else:
        expected, actual = functions
        old_counts, new_counts = {}, {}
    old_kernel, new_kernel = builder(), builder()
    old_result, new_result = evaluate(expected, old_kernel), evaluate(actual, new_kernel)
    assert new_kernel.events == old_kernel.events
    assert new_result == old_result
    return new_result, old_counts, new_counts


@pytest.mark.parametrize("seed", (0, 1, 71, 1729))
def test_random_small_cfg_matches_frozen_facts(seed):
    randomizer = random.Random(seed)
    functions = (load_facts(ORACLE)[0], load_facts(SOURCE)[0])
    for _ in range(128):
        size = randomizer.randrange(8)
        edges = [[randomizer.randrange(-2, size + 3) for _ in range(randomizer.randrange(5))]
                 for _ in range(size)]
        uses = {block: [randomizer.randrange(-2, 10) for _ in range(randomizer.randrange(6))]
                for block in range(size)}
        homes = [(value, randomizer.randrange(size)) for value in range(8)
                 if size and randomizer.randrange(2)]
        phis = {block: [(randomizer.randrange(10), [(randomizer.randrange(-2, 10),
                  randomizer.randrange(-2, size + 2)) for _ in range(3)])]
                for block in range(size) if randomizer.randrange(3) == 0}
        errors = {block: [randomizer.randrange(-2, size + 2) for _ in range(2)]
                  for block in range(size)}
        kinds = {block: randomizer.choice((-1, 1, 2, 14, 16)) for block in range(size)}
        blocks = case(edges, uses, homes, phis, errors, kinds)
        compare(lambda: FakeKernel(blocks), functions=functions)


@pytest.mark.parametrize("shape", ("empty", "home-only", "undefined", "duplicate-edges",
                                    "home-cut-cycle", "permuted-layout", "phi-home", "error-barrier"))
def test_named_cfg_contracts(shape):
    if shape == "empty":
        blocks = []
    elif shape == "home-only":
        blocks = case([[]], {0: [0, 1]}, ((0, 0), (1, 0)))
    elif shape == "undefined":
        blocks = case([[1], []], {1: [-2, 0, 1]})
    elif shape == "duplicate-edges":
        blocks = case([[1, 1, 1, -1, 99], [1]], {1: [0, 1, 2]})
    elif shape == "home-cut-cycle":
        blocks = case([[1, 2], [0], [0, 3], [2]], {1: [0, 1], 3: [2, 3]},
                      ((0, 0), (1, 0), (2, 0), (3, 0)))
    elif shape == "permuted-layout":
        blocks = case([[3], [0], [1], [2]], {0: [0, 1]}, ((0, 2), (1, 2)))
    elif shape == "phi-home":
        blocks = case([[1], [2], []], {2: [0, 1]}, ((0, 0), (1, 0)),
                      {1: [(0, [(2, 0)]), (1, [(3, 0)])]})
    else:
        blocks = case([[], [2], [1]], {1: [0, 1], 2: [2, 3]}, errors={0: [1, 1, 99]},
                      kinds={0: 14, 1: 2, 2: 16})
    compare(lambda: FakeKernel(blocks))


@pytest.mark.parametrize("position", (0, 1, 3))
def test_minus_one_retains_whole_old_stamp_history(position):
    values = [0, 1, 2]
    values.insert(position, -1)
    blocks = case([[1], [2], [1]], {2: values})
    _, old, new = compare(lambda: FakeKernel(blocks), count=True)
    assert new == old and new["hits"] == 0


@pytest.mark.parametrize("kind", ("custom-use", "custom-definition", "bool", "float", "custom-phi-seed"))
@pytest.mark.parametrize("canonical", (False, True))
def test_custom_protocols_keep_original_calls(kind, canonical):
    def build():
        events = []

        class CustomInt(int):
            def __hash__(self):
                events.append(("hash", int(self)))
                return int.__hash__(self)

            def __eq__(self, other):
                events.append(("eq", int(self), int(other)))
                return int.__eq__(self, other)

            def __ne__(self, other):
                events.append(("ne", int(self), int(other)))
                return int.__ne__(self, other)

        if kind == "custom-use":
            blocks = case([[1], []], {1: [0, CustomInt(1), 2]})
        elif kind == "custom-definition":
            blocks = case([[1], []], {1: [0, 1]}, ((CustomInt(0), 0),))
        elif kind == "bool":
            blocks = case([[1], []], {1: [0, True, 2]})
        elif kind == "float":
            blocks = case([[1], []], {1: [0, 1]}, ((0.0, 0),))
        else:
            blocks = case([[1], []], {1: [0, 1]}, phis={1: [(2, [(0, CustomInt(0)), (1, CustomInt(0))])]})
        return FakeKernel(blocks, events=events)

    expected, old = load_facts(ORACLE, count=True, canonical=canonical)
    actual, new = load_facts(SOURCE, count=True, canonical=canonical)
    compare(build, functions=(expected, actual))
    assert new == old and new["hits"] == 0


@pytest.mark.parametrize("failure", (("instruction_metadata_by_id", IndexError),
                                     ("terminator_use_id", IndexError),
                                     ("terminator_use_id", ValueError),
                                     ("phi_record", IndexError),
                                     ("terminator_case", IndexError),
                                     ("inline_error_edge_target", ValueError)))
def test_accessor_errors_and_swallowed_terminator_indexerror(failure):
    blocks = case([[1, 1, 1], []], {0: [0, 1]}, phis={1: [(2, [(0, 0)])]}, errors={0: [1]})
    result, _, _ = compare(lambda: FakeKernel(blocks, fail=failure))
    assert (result[0] == "value") == (failure == ("terminator_use_id", IndexError))


@pytest.mark.parametrize("shape", ("high-reuse", "multiple-seeds", "unique-seeds", "full-cache",
                                    "graph-only-high", "duplicate-seed"))
def test_counted_walk_and_cache_bound(shape):
    if shape == "high-reuse":
        blocks = case([[1], [2], []], {2: list(range(10))})
    elif shape == "multiple-seeds":
        blocks = case([[1], [2], []], {1: list(range(10)), 2: list(range(10))})
    elif shape == "unique-seeds":
        blocks = case([[1], [2], [3], []], {index: [index] for index in range(4)})
    elif shape == "full-cache":
        blocks = case([[1], [2], [3], []], {0: [0], 1: [1], 2: [2], 3: [3, 20, 21, 22]},
                      ((20, 0), (21, 0)))
    elif shape == "graph-only-high":
        blocks = case([[]], {0: [0, 1, 2]}, terms={0: [0]})
    else:
        blocks = case([[1], []], {1: [0, 1]}, ((0, 0), (1, 0)),
                      phis={1: [(2, [(0, 0), (1, 0), (0, 1), (1, 1)])]})
    result, old, new = compare(lambda: FakeKernel(blocks), count=True)
    assert result[0] == "value"
    assert new["writes"] <= len(blocks)
    assert new["pops"] <= old["pops"] and new["edges"] <= old["edges"]
    if shape in ("multiple-seeds", "unique-seeds"):
        assert new["hits"] == 0
        assert (new["pops"], new["edges"]) == (old["pops"], old["edges"])
    else:
        assert new["hits"] > 0 and new["pops"] < old["pops"]
    if shape == "high-reuse":
        assert old["pops"] == 30 and old["edges"] == 20
        assert new == dict(pops=3, edges=2, hits=9, writes=1)
    if shape == "full-cache":
        # Four keys fill the memo; two new-home misses cannot evict them.
        # The later undefined value 22 must still hit the existing (-1, 3).
        assert old["pops"] == 20 and old["edges"] == 15
        assert new == dict(pops=16, edges=12, hits=1, writes=len(blocks))
    if shape == "graph-only-high":
        assert result[1][1] == ((0, 3), (1, 1), (2, 2))


def compare_summary(blocks, *, observer=None):
    expected, _ = load_facts(ORACLE)
    prior, old_counts = load_facts(SOURCE, canonical=True, count_summary=True, pre_summary=True)
    actual, new_counts = load_facts(SOURCE, canonical=True, count_summary=True)
    actual.__globals__["_HOST_FACTS_STRUCTURE_OBSERVER"] = observer
    result, _, _ = compare(lambda: FakeKernel(blocks), functions=(expected, actual))
    assert evaluate(prior, FakeKernel(blocks)) == result
    return result, old_counts, new_counts


@pytest.mark.parametrize("seed", (0, 1, 71, 1729))
def test_home_summary_random_canonical_cfg_matches_frozen_facts(seed):
    randomizer = random.Random(seed)
    expected, _ = load_facts(ORACLE)
    actual, counts = load_facts(SOURCE, canonical=True, count_summary=True)
    for _ in range(96):
        size = randomizer.randrange(1, 9)
        edges = [[randomizer.randrange(-2, size + 3) for _ in range(randomizer.randrange(5))]
                 for _ in range(size)]
        uses = {block: [randomizer.randrange(10) for _ in range(randomizer.randrange(6))]
                for block in range(size)}
        homes = [(value, randomizer.randrange(size)) for value in range(8)
                 if randomizer.randrange(2)]
        phis = {block: [(randomizer.randrange(10), [(randomizer.randrange(10),
                  randomizer.randrange(-2, size + 2)) for _ in range(3)])]
                for block in range(size) if randomizer.randrange(3) == 0}
        errors = {block: [randomizer.randrange(-2, size + 2) for _ in range(2)]
                  for block in range(size)}
        kinds = {block: randomizer.choice((-1, 1, 2, 14, 16)) for block in range(size)}
        blocks = case(edges, uses, homes, phis, errors, kinds)
        compare(lambda: FakeKernel(blocks), functions=(expected, actual))
    assert counts["summary_homes"] > 0 and counts["summary_nodes"] > 0


@pytest.mark.parametrize("shape", ("cycle", "home-cut-stop-max", "arbitrary-layout",
                                    "phi-error", "graph-text-separation", "duplicate-seeds"))
def test_home_summary_adversarial_cfg_and_order(shape):
    if shape == "cycle":
        blocks = case([[1], [2], [3], [1, 4], []], {2: [0, 1], 4: [0, 1]},
                      ((0, 0), (1, 0)))
    elif shape == "home-cut-stop-max":
        # Home has the largest end; its own predecessor must not contribute
        # a low bound, but the home -> use stop edge must contribute its end.
        blocks = case([[4], [], [], [], [2, 3]], {2: [0, 1], 3: [0, 1]},
                      ((0, 4), (1, 4)))
    elif shape == "arbitrary-layout":
        blocks = case([[4], [0], [1], [2], [3]], {0: [0, 1], 1: [0, 1]},
                      ((0, 3), (1, 3)))
    elif shape == "phi-error":
        blocks = case([[1], [2], [1, 3], []], {1: [0, 1], 2: [0, 1], 3: [0, 1]}, ((0, 0), (1, 0)),
                      phis={2: [(2, [(0, 1), (1, 1)])]}, errors={0: [3], 2: [3]},
                      kinds={1: 2, 2: 14})
    elif shape == "graph-text-separation":
        blocks = case([[1], [2], []], {1: [0, 1], 2: [0, 1]}, terms={2: [0]})
    else:
        blocks = case([[1], [2], []], {1: [0, 1], 2: [0, 1]},
                      phis={2: [(2, [(0, 1), (1, 1), (0, 2), (1, 2)])]})
    result, _, counts = compare_summary(blocks)
    assert result[0] == "value" and counts["summary_homes"] > 0
    assert counts["summary_members"] == counts["summary_nodes"]
    assert counts["summary_frames"] == counts["summary_nodes"]
    assert counts["summary_folds"] == counts["summary_resets"] == counts["summary_nodes"]
    assert counts["summary_edges"] == counts["summary_fold_edges"]
    if shape == "cycle":
        assert counts["summary_components"] < counts["summary_nodes"]
    if shape == "phi-error":
        assert result[1][4] and result[1][5]
    if shape == "home-cut-stop-max":
        assert result[1][6] == ((0, 2), (1, 2))
        assert result[1][1] == ((0, 10), (1, 10))
    if shape == "graph-text-separation":
        assert result[1][1] == ((0, 6), (1, 5))


@pytest.mark.parametrize("shape", ("high-reuse", "zero-overlap", "same-sole-seed",
                                    "singleton-homes", "home-only", "interleaved-homes"))
def test_home_summary_exact_work_and_reusable_scratch_bounds(shape):
    if shape == "high-reuse":
        values = list(range(12))
        blocks = case([[1], [2], [3], [4], [5], []], {4: values, 5: values},
                      [(value, 0) for value in values])
    elif shape == "zero-overlap":
        blocks = case([[1], [], [3], []], {1: [0], 3: [1]})
    elif shape == "same-sole-seed":
        blocks = case([[1], [2], []], {2: list(range(12))})
    elif shape == "home-only":
        blocks = case([[]], {0: [0, 1]}, ((0, 0), (1, 0)))
    elif shape == "singleton-homes":
        blocks = case([[1], [2], [3], []], {1: [0], 2: [1], 3: [2]},
                      ((0, 0), (1, 1), (2, 2)))
    else:
        # A/B/A/B insertion order, two summarized homes, and a later pair
        # of identical singleton seeds that must still use the original memo.
        blocks = case([[1], [2], [3], [4], [5], []],
                      {2: [0, 2, 1, 3], 3: [0, 2, 1, 3], 5: [4, 5]},
                      ((0, 0), (1, 0), (2, 1), (3, 1), (4, 4), (5, 4)))
    result, old, new = compare_summary(blocks)
    assert result[0] == "value"
    if shape in ("same-sole-seed", "singleton-homes", "home-only"):
        assert new["summary_homes"] == new["summary_nodes"] == new["summary_slots"] == 0
        if shape == "same-sole-seed":
            assert (new["pops"], new["edges"], new["hits"], new["writes"]) == (3, 2, 11, 1)
        else:
            assert (new["pops"], new["edges"]) == (old["pops"], old["edges"])
        return
    assert new["summary_slots"] == 3 * len(blocks)
    assert new["summary_nodes"] == new["summary_members"] == new["summary_frames"]
    assert new["summary_nodes"] == new["summary_folds"] == new["summary_resets"]
    assert new["summary_edges"] == new["summary_fold_edges"]
    if shape == "high-reuse":
        assert (old["pops"], old["edges"]) == (60, 60)
        assert (new["pops"], new["edges"]) == (0, 0)
        assert new["summary_nodes"] == new["summary_edges"] == 5
        assert new["summary_values"] == 12 and new["summary_homes"] == 1
        assert new["seed_visits"] == 120
        # Count every named vertex pass and both edge scans, not elapsed time.
        assert sum(new[key] for key in ("summary_nodes", "summary_frames", "summary_members",
                                       "summary_folds", "summary_resets")) == 25 < old["pops"]
        assert new["summary_edges"] + new["summary_fold_edges"] == 10 < old["edges"]
    elif shape == "zero-overlap":
        assert (old["pops"], old["edges"]) == (4, 2)
        assert new["summary_nodes"] == 4 and new["summary_edges"] == 2
        assert new["summary_homes"] == 1 and new["summary_values"] == 2
        assert new["seed_visits"] == 10
        # With disjoint closures there is no graph reduction. The bounded
        # SCC/member/reset passes are extra work; this is not a speed claim.
        assert new["summary_nodes"] == old["pops"]
        assert new["summary_edges"] + new["summary_fold_edges"] == 2 * old["edges"]
    else:
        assert new["summary_homes"] == 2 and new["summary_nodes"] == 5
        assert new["summary_slots"] == 18 and new["summary_resets"] == 5
        assert (new["pops"], new["edges"], new["hits"], new["writes"]) == (1, 1, 1, 1)
        assert tuple(key for key, _ in result[1][6]) == (0, 2, 1, 3, 4, 5)


@pytest.mark.parametrize("value", (-1, -2))
def test_home_summary_negative_values_keep_original_stamp_path(value):
    blocks = case([[1], [2], [1]], {1: [0, value, 1], 2: [0, value, 1]})
    _, old, new = compare_summary(blocks)
    assert new["summary_nodes"] == 0
    assert (new["pops"], new["edges"]) == (old["pops"], old["edges"])


def test_home_summary_custom_extra_hash_cannot_change_first_error():
    def run(path):
        function, counts = load_facts(path, canonical=True, count_summary=True)
        events = []
        class CustomInt(int):
            calls = 0
            def __hash__(self):
                self.calls += 1
                events.append(("hash", self.calls))
                # Empty-dict membership still hashes. The first additional
                # membership in a rejected summary route must not occur.
                if self.calls == 9:
                    raise RuntimeError("ninth hash")
                return int.__hash__(self)
        blocks = case([[1], [2], []], {1: [CustomInt(0), 1], 2: [1]})
        kernel = FakeKernel(blocks, events=events)
        return evaluate(function, kernel), events, counts
    old, new = run(ORACLE), run(SOURCE)
    assert old[0][0] == "value"
    assert [event for event in old[1] if event[0] == "hash"] == [("hash", index) for index in range(1, 9)]
    assert new[:2] == old[:2]
    assert new[2]["summary_nodes"] == 0


@pytest.mark.parametrize("where", ("begin-none", "begin-error", "query-error", "active"))
def test_home_summary_installed_observer_retains_established_walks(where):
    class Observer:
        def __init__(self):
            self.rows = []
            self.failed = False
        def begin(self, *args):
            if where == "begin-error":
                raise RuntimeError("begin")
            return None if where == "begin-none" else object()
        def query(self, sample, home, active, hit):
            if where == "query-error":
                raise RuntimeError("query")
            return active
        def row(self, sample, block, degree):
            self.rows.append((block, degree))
            return True
        def complete(self, sample):
            pass
        def fail(self):
            self.failed = True
    observer = Observer()
    blocks = case([[1], [2], [3], []], {2: [0, 1], 3: [0, 1]}, ((0, 0), (1, 0)))
    _, old, new = compare_summary(blocks, observer=observer)
    assert new["summary_homes"] == new["summary_nodes"] == 0
    assert (new["pops"], new["edges"]) == (old["pops"], old["edges"]) == (6, 6)
    if where == "active":
        assert len(observer.rows) == sum(degree for _, degree in observer.rows) == 6
    assert observer.failed == (where in ("begin-error", "query-error"))


def test_home_summary_partial_scc_failure_releases_scratch_and_preserves_error():
    class InjectFailure(ast.NodeTransformer):
        def visit_Expr(self, node):
            if ast.unparse(node.value) == "summary_nodes.append(predecessor)":
                return [node, ast.parse("_raise_summary_failure()").body[0]]
            return node
    function, _ = load_facts(SOURCE, canonical=True, transform=InjectFailure())
    error = RuntimeError("partial SCC")
    def fail():
        raise error
    function.__globals__["_raise_summary_failure"] = fail
    blocks = case([[1], [2], [1]], {1: [0, 1], 2: [0, 1]})
    with pytest.raises(RuntimeError) as caught:
        function(FakeKernel(blocks))
    assert caught.value is error
    traceback = error.__traceback__
    while traceback.tb_frame.f_code.co_name != "_function_level_facts":
        traceback = traceback.tb_next
    local = traceback.tb_frame.f_locals
    assert local["summary_complete"] is False
    for name in ("home_values", "home_seed", "group_values", "summary_index", "summary_link",
                 "summary_component", "summary_touched", "summary_active", "summary_nodes",
                 "summary_edges", "summary_members", "component_low", "component_high",
                 "summary_low", "summary_high"):
        assert not local[name], name


@pytest.mark.parametrize("change", ("none", "old-memo", "new-boundary", "collection"))
def test_home_summary_preimage_is_exact_and_rejects_drift(change):
    tree = ast.parse(SOURCE.read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "_function_level_facts")
    if change == "none":
        restored = without_home_summary(function)
        assert "summary_enabled" not in ast.unparse(restored)
        original_dump = ast.dump(restored, show_empty=True)
        canonical_facts_ast_dump(restored)
        assert ast.dump(restored, show_empty=True) == original_dump
        for text in ("import sys", "from sys import version"):
            probe = ast.parse(text).body[0]
            probe._fields = tuple(name for name in probe._fields if name != "is_lazy")
            legacy = canonical_facts_ast_dump(probe)
            probe._fields = (*probe._fields, "is_lazy")
            probe.is_lazy = 0
            assert canonical_facts_ast_dump(probe) == legacy
            assert probe.is_lazy == 0 and "is_lazy" in probe._fields
            probe.is_lazy = 1
            with pytest.raises(AssertionError):
                canonical_facts_ast_dump(probe)
            probe.is_lazy = 0
            probe._fields = (*probe._fields, "future_semantic_field")
            probe.future_semantic_field = 7
            assert canonical_facts_ast_dump(probe) != legacy
        assert "value=None" in canonical_facts_ast_dump(ast.Constant(value=None))
        empty = canonical_facts_ast_dump(ast.parse("[]", mode="eval").body)
        assert "elts=[]" in empty and "ctx=Load()" in empty
        return
    if change == "old-memo":
        assignment = next(node for node in ast.walk(function) if isinstance(node, ast.Assign)
                          and ast.unparse(node.targets[0]) == "cache_enabled")
        assignment.value = ast.Constant(False)
    elif change == "new-boundary":
        assignment = next(node for node in ast.walk(function) if isinstance(node, ast.Assign)
                          and ast.unparse(node.targets[0]) == "summary_enabled")
        assignment.targets[0].id = "different_summary_enabled"
    else:
        assignment = next(node for node in ast.walk(function) if isinstance(node, ast.Assign)
                          and ast.unparse(node.targets[0]) == "position")
        assignment.value = ast.Constant(10)
    with pytest.raises(AssertionError):
        without_home_summary(function)



def test_home_summary_deep_chain_uses_iterative_bounded_scratch():
    size = 1536
    edges = [[index + 1] for index in range(size - 1)] + [[]]
    blocks = case(edges, {size - 2: [0, 1], size - 1: [0, 1]}, ((0, 0), (1, 0)))
    _, old, new = compare_summary(blocks)
    assert new["summary_nodes"] == new["summary_resets"] == size - 1
    assert new["summary_slots"] == 3 * size
    assert old["pops"] == 2 * (size - 1)


def test_home_summary_canonical_custom_seed_keeps_original_calls():
    def run(path):
        function, counts = load_facts(path, canonical=True, count_summary=True)
        events = []
        class CustomInt(int):
            def __eq__(self, other):
                events.append(("eq", int(self), int(other)))
                return int.__eq__(self, other)
            def __ne__(self, other):
                events.append(("ne", int(self), int(other)))
                return int.__ne__(self, other)
        blocks = case([[1], [2], []], {1: [0, 1], 2: [0, 1]},
                      phis={2: [(2, [(0, CustomInt(0)), (1, CustomInt(0))])]})
        return evaluate(function, FakeKernel(blocks, events=events)), events, counts
    old, new = run(ORACLE), run(SOURCE)
    assert new[:2] == old[:2]
    assert new[2]["summary_nodes"] == 0
