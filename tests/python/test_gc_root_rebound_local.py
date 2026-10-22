"""Regression: owned locals re-bound to a fresh alloca keep a GC frame root.

Root cause (pproxy pcc1 worker BAD_INCREF on gc3/gc4): frontend owned-local
bookkeeping was NAME-keyed while physical storage is ALLOCA-keyed. When one
local name got a second alloca inside one function (scope pop -> fresh env
slot), ``_ensure_owned_local_gc_root`` could skip the new root, and the
owned-flag cache could also reuse the old same-name flag for the new physical
slot. The moving backends' relocation remap then could not heal every slot,
or a stale true owned flag could release a fresh/uninitialized slot.

The compile below reproduces the shape (``for i, lhs in enumerate(...)``
binding then a later ``lhs = elems[i]`` rebinding). The assertions check the
emitted IR invariants that were violated before the fix:

* every ptr alloca that is ever frame-left is also frame-entered
  (pre-fix: the second ``lhs`` alloca had leaves but no enter), and
* every exit block that leaves any registered slot leaves ALL registered
  slots (entry enters always run, so each exit must balance every slot), and
* every distinct same-name ``lhs`` alloca has its own owned flag.
"""

from __future__ import annotations

import os
import re
import subprocess

from pathlib import Path


def _repo_root() -> Path:
    cur = Path(__file__).resolve().parent
    while cur != cur.parent:
        if (cur / "AGENTS.md").exists():
            return cur
        cur = cur.parent
    raise RuntimeError("AGENTS.md not found walking up")


REPO = _repo_root()

_SNIPPET = """
def scan(elems: list) -> int:
    # Tuple-unpack comprehension targets are owned+rooted while the
    # comprehension compiles, and the comprehension saves/restores env
    # afterwards: the name ``lhs`` then has NO env slot while it stays in
    # the compile-time rooted-name set -- the exact precondition for the
    # bug (mirrors assignment_statement_lowering's own source).
    names = [lhs for i, lhs in enumerate(elems) if lhs is not None]
    count = len(names)
    ok = True
    i = 0
    while i < len(elems):
        # Re-binding creates a FRESH alloca for ``lhs``; pre-fix it was
        # owned-flag managed but never frame-registered.
        lhs = elems[i]
        if lhs is None:
            ok = False
            break
        i += 1
    if ok:
        count += 1
    return count


def main() -> int:
    xs = ["a", "b", "c"]
    print(scan(xs))
    return 0
"""


def _compile_and_dump(tmp_path: Path) -> str:
    src = tmp_path / "rebound_local_probe.py"
    src.write_text(_SNIPPET)
    dump_dir = tmp_path / "irdump"
    dump_dir.mkdir()
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_DEBUG_SELF_IR_DUMP_DIR"] = str(dump_dir)
    proc = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(src),
            "-o",
            str(tmp_path / "rebound_local_probe_bin"),
        ],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        timeout=240,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    lls = sorted(dump_dir.glob("*.ll"))
    assert lls, "no IR dumped"
    for ll in lls:
        text = ll.read_text()
        if "@user_rebound_local_probe_scan(" in text:
            return text
    raise AssertionError("scan() module IR not found in dump")


def _function_body(text: str, marker: str) -> list[str]:
    lines = text.splitlines()
    start = next(
        i for i, ln in enumerate(lines) if ln.startswith("define") and marker in ln
    )
    end = next(j for j in range(start + 1, len(lines)) if lines[j] == "}")
    return lines[start:end]


def test_rebound_owned_local_slots_have_balanced_frame_roots(tmp_path) -> None:
    text = _compile_and_dump(tmp_path)
    body = _function_body(text, "@user_rebound_local_probe_scan(")

    bitcasts: dict[str, str] = {}
    for ln in body:
        m = re.match(r'\s*%([\w."]+) = bitcast ptr %([\w."]+) to ptr', ln)
        if m:
            bitcasts[m.group(1)] = m.group(2)

    cur_block = "entry"
    enters: dict[str, int] = {}
    leave_blocks: dict[str, set[str]] = {}
    leave_lists: dict[str, list[str]] = {}
    jumps: dict[str, str] = {}
    for ln in body:
        mbb = re.match(r'([A-Za-z0-9_."]+):\s*$', ln)
        if mbb:
            cur_block = mbb.group(1)
        mjump = re.match(r'\s*br label %([\w."]+)\s*$', ln)
        if mjump:
            jumps[cur_block] = mjump.group(1)
        # The frame map is a global, passed directly or through an alias.
        menter = re.search(r'@pcc_gc_frame_enter\(ptr [%@][\w."]+, ptr %([\w."]+)\)', ln)
        if menter:
            slot = bitcasts.get(menter.group(1), menter.group(1))
            enters[slot] = enters.get(slot, 0) + 1
        mleave = re.search(r'@pcc_gc_frame_leave\(ptr %([\w."]+)\)', ln)
        if mleave:
            slot = bitcasts.get(mleave.group(1), mleave.group(1))
            leave_blocks.setdefault(slot, set()).add(cur_block)
            leave_lists.setdefault(cur_block, []).append(slot)

    # Invariant 1 (the regression): a slot with any frame_leave must have a
    # frame_enter. Pre-fix the rebound ``lhs`` alloca violated this.
    for slot, blocks in leave_blocks.items():
        assert enters.get(slot, 0) >= 1, (
            f"slot {slot} is frame-left in {sorted(blocks)} but never "
            "frame-entered (rebound owned local lost its GC root)"
        )

    # Invariant 2: entry enters always execute, so every exit block that
    # leaves one registered slot must leave every registered slot.
    registered = set(enters)
    exit_blocks: set[str] = set()
    for slot in registered:
        exit_blocks.update(leave_blocks.get(slot, set()))
    # An exit may leave its slots across a straight-line chain (the shared
    # return exit leaves the frames, then the value's own slot after reading
    # it); the chain is one exit path and must leave each slot exactly once.
    continuations = {jumps[block] for block in exit_blocks if block in jumps}
    for block in exit_blocks - continuations:
        left_on_path: list[str] = []
        step, seen = block, set()
        while step is not None and step not in seen:
            seen.add(step)
            left_on_path.extend(leave_lists.get(step, []))
            step = jumps.get(step)
        assert len(left_on_path) == len(set(left_on_path)), (block, left_on_path)
        assert set(left_on_path) == registered, (
            f"exit path from {block} leaves {sorted(left_on_path)} but the function "
            f"registered {sorted(registered)}; unbalanced roots dangle into "
            "a dead frame under moving GC"
        )

    # The probe shape must actually exercise the re-bind: the while-loop's
    # ``lhs`` slot must be registered (pre-fix it was managed but unrooted,
    # so it appeared only in leave_blocks -- caught by invariant 1 -- or in
    # no frame call at all while still being flag-released).
    lhs_slots = [slot for slot in registered if slot.startswith("lhs.addr")]
    assert lhs_slots, (
        f"expected the re-bound ``lhs`` alloca to be frame-registered, got "
        f"registered={sorted(registered)}"
    )

    lhs_allocas = {
        m.group(1)
        for ln in body
        for m in [re.match(r'\s*%([\w."]+) = alloca ptr', ln)]
        if m and m.group(1).startswith("lhs.addr")
    }
    lhs_owned_flags = {
        m.group(1)
        for ln in body
        for m in [re.match(r'\s*%([\w."]+) = alloca i1', ln)]
        if m and m.group(1).startswith("lhs.owned")
    }
    assert len(lhs_owned_flags) >= len(lhs_allocas), (
        "same-named owned locals rebound to distinct allocas must not share "
        f"one owned flag: lhs_allocas={sorted(lhs_allocas)} "
        f"lhs_owned_flags={sorted(lhs_owned_flags)}"
    )


def _typed_module(source: str, name: str):
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python import type_infer

    return type_infer.infer_module(parse_and_lift(source, f"<{name}>", name))


def test_module_override_resets_gc_root_slot_registries() -> None:
    from pcc.frontends.python.codegen import layer1

    first = _typed_module(
        """
def main() -> int:
    return 0
""",
        "first_module",
    )
    second = _typed_module(
        """
def main() -> int:
    return 1
""",
        "second_module",
    )
    codegen = layer1.L1CodeGen(first, ir_scaffold_mode="on")
    codegen._fn_gc_root_slot_registry = {"old_function": [object()]}
    codegen._fn_err_exit_gc_root_slots = {"old_function": [object()]}
    codegen._fn_gc_root_exit_sites = {"old_function": [object()]}

    codegen.generate(second)

    assert "old_function" not in codegen._fn_gc_root_slot_registry
    assert "old_function" not in codegen._fn_err_exit_gc_root_slots
    assert "old_function" not in codegen._fn_gc_root_exit_sites
