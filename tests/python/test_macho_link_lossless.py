"""The merge must not lose relocations — measured, not assumed.

A linker that silently drops a relocation produces a binary that runs until
the un-relocated address is used. This suite counts every section's
relocations across the inputs and across the merged output and requires them
equal, on the real job: pcc's own emitter output plus the runtime-archive
members its symbols pull (92 objects, ~62k relocations).

It exists because a per-section comparison against `ld -r` showed pcc with
half of ld's `__eh_frame` count, which was first written up as a pcc bug.
Counting inputs instead of comparing outputs showed pcc loses nothing and ld
*adds* entries by rewriting FDE/CIE structure. The lesson is the test: when
two tools disagree on a count, count the inputs before deciding who is wrong.
"""

from __future__ import annotations

import collections
import os
from pathlib import Path

import pytest

from pcc.backend import macho_spec as spec
from pcc.backend.macho_archive import read_archive, select_members
from pcc.backend.macho_link import link_relocatable

_IS_ARM64_DARWIN = os.uname().sysname == "Darwin" and os.uname().machine == "arm64"
_GATE = None if _IS_ARM64_DARWIN else "needs Darwin arm64"

pytestmark = pytest.mark.pcc_gate(unavailable=_GATE)


def _repo_root() -> Path:
    cur = Path(__file__).resolve().parent
    while cur != cur.parent:
        if (cur / "AGENTS.md").exists():
            return cur
        cur = cur.parent
    raise RuntimeError("AGENTS.md not found above " + __file__)


REPO = _repo_root()


def _counts(objects):
    total = collections.Counter()
    for data in objects:
        obj = spec.parse_object(data)
        for sec in obj.sections():
            key = (sec["segname_str"], sec["sectname_str"])
            total[key] += len(obj.relocations(sec))
    return total


def test_merging_runtime_members_loses_no_relocation():
    archive = REPO / "pcc" / "runtime" / "libpy_runtime_pcc_py.a"
    if not archive.exists():
        raise AssertionError(
            f"{archive} missing; build it with "
            "`make -C pcc/runtime libpy_runtime_pcc_py.a`"
        )
    members = read_archive(archive.read_bytes())
    # Pull a substantial, dependency-closed slice of the runtime.
    seed = {"py_list_len", "_py_list_len", "py_dict_new", "_py_dict_new"}
    defines = set().union(*(m.defines for m in members))
    seed = {name for name in seed if name in defines}
    assert seed, "expected known runtime entry points in the archive"
    pulled, _pending = select_members(members, seed)
    assert len(pulled) > 5, len(pulled)

    before = _counts(pulled)
    merged = link_relocatable(pulled)
    after = _counts([merged])

    assert sum(before.values()) > 500, sum(before.values())
    lost = {k: (before[k], after.get(k, 0)) for k in before if after.get(k, 0) != before[k]}
    assert not lost, (
        "relocations changed count during the merge (section: in -> out):\n  "
        + "\n  ".join(f"{k}: {v[0]} -> {v[1]}" for k, v in sorted(lost.items()))
    )
