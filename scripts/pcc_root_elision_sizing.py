#!/usr/bin/env python3
"""Size the allocation-point root-elision domain on real parsed IR.

Design-stage gate for the `S-P1-ALLOCATION-POINT-ROOT-ELISION` task row: a
read-only count of `pcc_gc_store_root` sites whose window — from the store to
the last reload of the SLOT — provably contains no allocation point.  It runs
on the same `ParsedModule`/CFG the precise stack-map analysis consumes, so the
number is what the real transform would see, but it changes nothing and is not
part of the compiled stage1 closure.

Two scan lessons are baked in (both produced wrong numbers before):

* Windows are keyed on the SLOT's reloads, not the rooted value.  Root codegen
  reloads from the slot after the store — that is what a root is for — so a
  value-keyed scan finds no uses and reports a vacuous 100%.
* Block-local scanning is structurally blind: every call is followed by a
  `py_err_occurred` branch, so every window crosses blocks.

The count is a sound, path-insensitive lower bound: region R is every block
reachable from the store block that can still reach a reload block, and the
store block's remainder plus every full block of R must be free of calls
outside the whitelist.  v1 restriction: only single-store slots (the rewrite
can then replace reloads with one SSA value without PHI construction).

The v1 whitelist deliberately EXCLUDES pcc_gc_release/py_decref: a release
reaching refcount zero dispatches `__del__`, and a finalizer can allocate.

Usage:
    scripts/pcc_root_elision_sizing.py <module.ll> [more.ll ...]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Calls that provably cannot allocate or trigger a collection.  Growing this
# list is a proof obligation, not an edit: see the design evidence file.
V1_WHITELIST = (
    "pcc_gc_frame_enter",
    # NOT pcc_gc_frame_leave / pcc_gc_store_root: both decref their old
    # value, which can run a finalizer and therefore an arbitrary GC while
    # the elided value lives only in a register (review P1-6).  A store_root
    # to the SAME slot still ends the window (handled before this check);
    # one to any OTHER slot dirties it.
    "pcc_gc_load_ptr",
    "pcc_gc_retain",
    "pcc_gc_pin",
    "pcc_gc_unpin",
    "py_err_occurred",
    "llvm.",
)


def _call_is_safe(callee: str) -> bool:
    for prefix in V1_WHITELIST:
        if callee.startswith(prefix):
            return True
    return False


def _successors(block) -> tuple[str, ...]:
    term = block.terminator
    if term is None:
        return ()
    if term.kind == "br":
        return (term.data[0],)
    if term.kind == "br_cond":
        return (term.data[1], term.data[2])
    if term.kind == "switch":
        _value_type, _value, default_target, cases = term.data
        return (default_target,) + tuple(target for _const, target in cases)
    if term.kind in ("ret", "ret_void", "unreachable"):
        return ()
    # Unknown terminator: successors cannot be enumerated, so no window
    # crossing this block may be called elidable (review P1-6: the old
    # `_all_labels` fallback silently returned () and DROPPED paths).
    return None


def _canonical_slots(func) -> dict:
    """Map every bitcast alias back to its underlying storage name.

    One alloca is bitcast to a FRESH alias per window
    (``exact.int.lhs.tmp.root.ptr.414.28``, ``container.tmp.root.ptr.428.41``
    both aliasing ``exact.int.lhs.tmp.root.27``).  Windows keyed on the alias
    are blind to reads through sibling aliases, which made stores look dead
    while their memory was read one alias later -- the exact mechanism behind
    the first end-to-end failure.  Resolve chains transitively.
    """
    direct = {}
    for block in func.blocks:
        for ins in block.instructions:
            if ins.kind == "cast" and len(ins.data) >= 4 and ins.data[0] == "bitcast":
                direct[ins.data[1]] = ins.data[3]
    resolved = {}
    for name in direct:
        cur = name
        hops = 0
        while cur in direct and hops < 32:
            cur = direct[cur]
            hops = hops + 1
        resolved[name] = cur
    return resolved


def function_sizing(func) -> dict:
    """Return the sizing counts for one ParsedFunction, changing nothing."""
    blocks = {b.name: b for b in func.blocks}
    canon = _canonical_slots(func)

    def _c(name):
        return canon.get(name, name)
    succ = {}
    unknown_term = set()
    for name, b in blocks.items():
        s = _successors(b)
        if s is None:
            unknown_term.add(name)
            succ[name] = []
        else:
            succ[name] = [x for x in s if x in blocks]

    reach: dict[str, set] = {}
    for name in blocks:
        seen = {name}
        stack = [name]
        while stack:
            cur = stack.pop()
            for nxt in succ[cur]:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        reach[name] = seen

    # store_root sites and slot reloads, from the parsed instructions.
    stores: list[tuple[str, int, str]] = []  # (block, index, slot)
    store_count: dict[str, int] = {}
    loads: dict[str, list[tuple[str, int]]] = {}  # slot -> [(block, index)]
    unsafe_at: dict[str, list[int]] = {name: [] for name in blocks}
    for name, block in blocks.items():
        for index, ins in enumerate(block.instructions):
            if ins.kind == "call":
                callee = ins.data[2]
                if callee == "pcc_gc_store_root":
                    slot = _c(ins.data[4][0][1])
                    stores.append((name, index, slot))
                    store_count[slot] = store_count.get(slot, 0) + 1
                elif not _call_is_safe(callee):
                    unsafe_at[name].append(index)
            elif ins.kind == "load":
                loads.setdefault(ins.data[3], []).append((name, index))

    # Per-window, all-paths-clean lower bound.  A window starts at its
    # store_root and ends at the next store_root to the SAME slot on each
    # path (slots are reused across windows -- 9,201 stores share far fewer
    # slots, so a whole-slot single-def rule has an empty domain).  A window
    # is elidable only if EVERY path from the store to EVERY reload of the
    # slot inside the window is free of non-whitelisted calls: one dirty path
    # means a GC could run while the value lives only in a register.
    store_sites: dict[str, set] = {}
    for name, index, slot in stores:
        store_sites.setdefault(slot, set()).add((name, index))
    total = len(stores)
    windowed = 0
    elidable = 0
    for name, index, slot in stores:
        others = store_sites[slot] - {(name, index)}
        reads_clean = 0
        reads_dirty = 0
        went_dirty = False
        # states: (block, start_index, dirty); memo on (block, dirty) for
        # whole-block entries.
        stack = [(name, index + 1, False)]
        seen = set()
        while stack:
            cur, start, dirty = stack.pop()
            if start == 0:
                if (cur, dirty) in seen:
                    continue
                seen.add((cur, dirty))
            block = blocks[cur]
            stopped = False
            for i2 in range(start, len(block.instructions)):
                ins = block.instructions[i2]
                if ins.kind == "call":
                    callee = ins.data[2]
                    if callee == "pcc_gc_store_root" and _c(ins.data[4][0][1]) == slot:
                        stopped = True  # next window begins here
                        break
                    if callee == "pcc_gc_load_ptr" and any(
                        _c(arg_name) == slot for _t, arg_name in ins.data[4]
                    ):
                        # Root slots are read exclusively through the read
                        # barrier, never by a plain `load` (13,603 barrier
                        # reads vs 0 plain loads on the representative
                        # module); frame_leave releases them and nothing else
                        # ever takes the slot address.
                        if dirty:
                            reads_dirty += 1
                        else:
                            reads_clean += 1
                    elif not _call_is_safe(callee):
                        dirty = True
                        went_dirty = True
                elif ins.kind == "load" and _c(ins.data[3]) == slot:
                    if dirty:
                        reads_dirty += 1
                    else:
                        reads_clean += 1
            if cur in unknown_term:
                # Path continues through edges we cannot enumerate: veto.
                went_dirty = True
                stopped = True
            if not stopped:
                for nxt in succ[cur]:
                    stack.append((nxt, 0, dirty))
        if reads_clean or reads_dirty:
            windowed += 1
        # A dirty path WITHOUT a reload still vetoes: the frame release at
        # function exit uses the slot value, so on a moving backend the
        # register copy is stale after any GC point on any path (P1-6).
        if reads_clean and not reads_dirty and not went_dirty:
            elidable += 1
    return {"stores": total, "single_def": windowed, "elidable": elidable}


def main() -> int:
    from pcc.backend.self_backend_kernel import get_indexed_function_kernel
    from pcc.backend.self_backend_parse import parse_self_backend_module

    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    grand = {"stores": 0, "single_def": 0, "elidable": 0}
    for path in sys.argv[1:]:
        module = parse_self_backend_module(
            Path(path).read_text(errors="replace")
        )
        totals = {"stores": 0, "single_def": 0, "elidable": 0}
        for func in module.functions:
            # The parser stopped populating `blocks` when the indexed kernel
            # landed: bodies live in the packed representation and a consumer
            # asks for the legacy projection.  Without this the loop skipped
            # every function of every real module and the tool reported
            # store_root=0 for all of them -- it only ever ran on the
            # hand-built ParsedFunctions in its own test.
            if not func.blocks:
                get_indexed_function_kernel(func).materialize_legacy_blocks(
                    func
                )
            if not func.blocks:
                continue
            row = function_sizing(func)
            for key in totals:
                totals[key] += row[key]
        share = 100.0 * totals["elidable"] / max(totals["stores"], 1)
        print(
            "%s: store_root=%d windows_with_reads=%d elidable(LB)=%d = %.1f%%"
            % (path, totals["stores"], totals["single_def"], totals["elidable"], share)
        )
        for key in grand:
            grand[key] += totals[key]
    if len(sys.argv) > 2:
        share = 100.0 * grand["elidable"] / max(grand["stores"], 1)
        print(
            "TOTAL: store_root=%d windows_with_reads=%d elidable(LB)=%d = %.1f%%"
            % (grand["stores"], grand["single_def"], grand["elidable"], share)
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
