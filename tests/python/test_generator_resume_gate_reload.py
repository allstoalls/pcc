"""A generator resume body holds no unrooted managed value across a gate.

``pcc_thread_safepoint_gate`` is where a moving collector may relocate
objects while the thread is parked, so compiled code reloads every managed
reference from its root after it. The resume function used to load the
generator object and its frame once at entry and keep both in SSA across
its loop-latch gates; a relocation then left the body spilling locals into
the old frame. This checks the emitted IR by liveness: no value produced by
a call or a pointer load (and not pinned) is live across a gate.
"""

import re
from collections import defaultdict
from pathlib import Path

SOURCE = '''import pcc.virtual_thread as vt


def walk(items):
    total = 0
    for item in items:
        total = total + item
        vt.yield_now()
    return total


def counter(limit):
    index = 0
    while index < limit:
        yield index
        index = index + 1
'''


def _function_blocks(text: str, name_pattern: str):
    match = re.search(r"^define [^\n]*@(" + name_pattern + r")\([^\n]*\n(.*?)^}", text, re.M | re.S)
    assert match is not None, name_pattern
    blocks, order, label = {}, [], None
    for line in match.group(2).splitlines():
        stripped = line.strip()
        block_label = re.fullmatch(r"([-.\w]+):", stripped)
        if block_label:
            label = block_label.group(1)
            blocks[label] = []
            order.append(label)
            continue
        if not stripped or stripped.startswith(";"):
            continue
        if label is None:
            label = "entry"
            blocks[label] = []
            order.append(label)
        blocks[label].append(stripped)
    return match.group(1), blocks, order


def _managed_values_live_across_gates(text: str, name_pattern: str):
    _name, blocks, order = _function_blocks(text, name_pattern)
    successors = {
        label: re.findall(r"label %([-.\w]+)", blocks[label][-1]) if blocks[label] else []
        for label in order
    }
    managed, pinned = set(), set()
    uses, defs = {}, {}
    phi_uses = defaultdict(set)
    for label in order:
        used, defined = set(), set()
        for instruction in blocks[label]:
            assignment = re.match(r"(%[-.\w]+) = (.*)", instruction)
            rhs = assignment.group(2) if assignment else instruction
            if assignment:
                value = assignment.group(1)
                if re.match(r"(tail )?call ptr", rhs) or rhs.startswith("load ptr") or rhs.startswith("phi ptr"):
                    managed.add(value)
            pin = re.search(r"@pcc_gc_pin\(ptr (%[-.\w]+)\)", instruction)
            if pin:
                pinned.add(pin.group(1))
            if assignment and rhs.startswith("phi"):
                for value, predecessor in re.findall(r"\[\s*(%[-.\w]+)\s*,\s*%([-.\w]+)\s*\]", rhs):
                    phi_uses[predecessor].add(value)
            else:
                for value in re.findall(r"%[-.\w]+", rhs):
                    if value not in defined:
                        used.add(value)
            if assignment:
                defined.add(assignment.group(1))
        uses[label], defs[label] = used, defined
    live_in = {label: set() for label in order}
    live_out = {label: set() for label in order}
    changed = True
    while changed:
        changed = False
        for label in reversed(order):
            out = set(phi_uses[label])
            for successor in successors[label]:
                out |= live_in.get(successor, set())
            incoming = uses[label] | (out - defs[label])
            if out != live_out[label] or incoming != live_in[label]:
                live_out[label], live_in[label] = out, incoming
                changed = True
    across = {}
    for label in order:
        if any("@pcc_thread_safepoint_gate(" in instruction for instruction in blocks[label]):
            across[label] = sorted(value for value in live_out[label] if value in managed and value not in pinned)
    return across


def test_generator_resume_reloads_its_generator_and_frame_after_gates(tmp_path: Path) -> None:
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "gates.py"
    src.write_text(SOURCE, encoding="utf-8")
    out = tmp_path / "gates.ll"
    compile_python(str(src), str(out), emit_llvm_only=True, libpython_mode="off")
    text = out.read_text(encoding="utf-8")
    for name in ("walk", "counter"):
        across = _managed_values_live_across_gates(text, r"[^ (]*_" + name + r"__gen_resume")
        assert across, name + ": the resume body has no loop-latch gate to check"
        assert all(values == [] for values in across.values()), (name, across)
