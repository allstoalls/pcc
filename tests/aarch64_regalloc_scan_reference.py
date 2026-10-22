"""Test-only old-scan control; all real liveness and allocation still execute."""

import ast
import inspect

from pcc.backend import self_backend_aarch64_darwin_regalloc as regalloc
from pcc.backend.self_backend_kernel import get_indexed_function_kernel


MODES = ("default", "function", "local")


def select_mode(monkeypatch, mode, *, call_results="1"):
    assert mode in MODES
    if mode == "default":
        monkeypatch.delenv("PCC_SELF_CALLEE_SAVED_REGISTERS", raising=False)
    else:
        monkeypatch.setenv("PCC_SELF_CALLEE_SAVED_REGISTERS", "0")
    monkeypatch.setenv("PCC_SELF_FUNCTION_LIVE_INTERVALS", "1" if mode == "function" else "0")
    monkeypatch.setenv("PCC_SELF_CALL_RESULT_REGISTERS", call_results)


def restore_redundant_scans(tree):
    """Undo only the two scan guards, leaving the allocator's other code intact.

    The safety else-body is the original early-break scan. The call-position
    body is the original full scan. This reference consumes those scans rather
    than stubbing _function_level_facts, safety, liveness, or allocation.
    Fail if the two expected seams move; never silently compare a function to
    itself. The independent instruction_count assignment remains just before
    the safety scan; the preimage placed it after that scan's initialization.
    """
    class Restore(ast.NodeTransformer):
        safety = 0
        calls = 0

        def visit_If(self, node):
            if (ast.unparse(node.test) == "function_level"
                    and len(node.body) == 1
                    and ast.unparse(node.body[0]) == "block_is_safe = block_safe[block_id]"):
                assert node.orelse
                self.safety += 1
                return node.orelse
            if (ast.unparse(node.test) == "not function_level"
                    and any(isinstance(child, ast.Call)
                            and ast.unparse(child.func) == "call_positions.append"
                            for statement in node.body for child in ast.walk(statement))):
                assert not node.orelse
                self.calls += 1
                return node.body
            return self.generic_visit(node)

    restore = Restore()
    tree = restore.visit(tree)
    assert (restore.safety, restore.calls) == (1, 1)
    return ast.fix_missing_locations(tree)


def old_scan_allocator(*, facts_counts=None):
    tree = restore_redundant_scans(ast.parse(inspect.getsource(
        regalloc.allocate_aarch64_block_registers
    )))
    from tests.aarch64_regalloc_facts_reference import old_function_level_facts
    namespace = dict(vars(regalloc))
    facts = old_function_level_facts(regalloc)
    if facts_counts is not None:
        facts = counted_function_facts(facts, facts_counts)
    namespace["_function_level_facts"] = facts
    exec(compile(tree, "<aarch64-original-metadata-scans>", "exec"), namespace)
    return namespace["allocate_aarch64_block_registers"]


def allocation_state(func):
    kernel = get_indexed_function_kernel(func)
    return {
        "value_names": tuple(kernel.value_names),
        "value_scalars": tuple(kernel.value_scalars.diagnostic_values()),
        "slot_scalars": tuple(kernel.slot_scalars.diagnostic_values()),
        "frame_size": func.frame_size,
        "callee_saved": tuple(func.aarch64_callee_saved),
        "fused_branch_values": dict(func.aarch64_fused_branch_values),
        "frameless": func.aarch64_frameless,
        "madd_fusions": tuple(func.aarch64_madd_fusions),
        "reload_offsets": tuple(func.aarch64_reload_slot_offsets),
    }


def removed_metadata_reads(func):
    """Exact old safety prefix plus old safe-block call scan visits."""
    if func.is_vararg:
        return 0
    kernel = get_indexed_function_kernel(func)
    count = 0
    for block_id in range(len(kernel.block_names)):
        block = kernel.block_fact(block_id)
        for index in range(block.second):
            kind = kernel.instruction_metadata_by_id(block.first + index).first
            count += 1
            if (not 0 <= kind < len(regalloc.PARSED_INSTRUCTION_KINDS)
                    or kind in regalloc._POOL_REJECTED_INSTRUCTION_KIND_IDS):
                break
        else:
            count += block.second
    return count


def counted_function_facts(function, counts):
    """Test-only graph visits; the cache-hit pending.pop is not a graph visit."""
    class Count(ast.NodeTransformer):
        pops = 0
        edges = 0

        def increment(self, name):
            return ast.parse("_facts_counts[" + repr(name) + "] += 1").body[0]

        def visit_While(self, node):
            self.generic_visit(node)
            if isinstance(node.test, ast.Name) and node.test.id == "pending":
                node.body.insert(0, self.increment("pops"))
                self.pops += 1
            return node

        def visit_For(self, node):
            self.generic_visit(node)
            if (ast.unparse(node.target) == "predecessor"
                    and ast.unparse(node.iter) == "predecessors[block_id]"):
                node.body.insert(0, self.increment("edges"))
                self.edges += 1
            return node

        def visit_If(self, node):
            self.generic_visit(node)
            if ast.unparse(node.test) == "cached_low >= 0":
                node.body.insert(0, self.increment("hits"))
            return node

    from tests.aarch64_regalloc_facts_reference import _function_level_facts as reference
    tree = ast.parse(inspect.getsource(function))
    reference_tree = ast.parse(inspect.getsource(reference))
    reference_loop = next(node for node in ast.walk(reference_tree)
                          if isinstance(node, ast.While) and isinstance(node.test, ast.Name)
                          and node.test.id == "pending")
    loops = [node for node in ast.walk(tree) if isinstance(node, ast.While)
             and isinstance(node.test, ast.Name) and node.test.id == "pending"]
    expected = 2 if any(isinstance(node, ast.Name) and node.id == "_HOST_FACTS_STRUCTURE_OBSERVER"
                        for node in ast.walk(tree)) else 1
    assert len(loops) == expected
    assert sorted(len(node.body) for node in loops) == ([4, 6] if expected == 2 else [4])
    canonical = ast.dump(ast.Module(body=reference_loop.body, type_ignores=[]))
    for loop in loops:
        assert ast.dump(ast.Module(body=loop.body[:4], type_ignores=[])) == canonical
    counter = Count()
    tree = counter.visit(tree)
    assert (counter.pops, counter.edges) == (expected, expected)
    namespace = dict(function.__globals__)
    namespace["_facts_counts"] = counts
    exec(compile(ast.fix_missing_locations(tree), "<counted-facts-graph-visits>", "exec"), namespace)
    return namespace["_function_level_facts"]
