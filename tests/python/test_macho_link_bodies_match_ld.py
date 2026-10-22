"""Function bodies must survive the merge byte-identically.

Comparing whole `__text` payloads against `ld -r` reports a difference, and
the first read of that was "placement or addend detail". Bisecting showed the
first differing byte is an *instruction* inside a function — with identical
section sizes, meaning the two linkers placed different functions there.

Slicing `__text` by symbol and comparing bodies is the measurement that
actually answers the question: ld -r reorders atoms, pcc concatenates in
input order, and every function body is identical. This pins that — a real
corruption would change a body, not just its address.

The slicing itself needed one correction: several symbols can share an
address (aliases), and slicing per *symbol* gave the second name at an
address a zero-length body, which then "differed". `_fabs` and `_pow` were
reported that way before the slice was keyed on distinct addresses. Two of
the measurement artifacts in this file's history were in the harness, not
the linker; that is why the checks say what they measure.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
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


def _bodies(data: bytes) -> dict[str, bytes]:
    obj = spec.parse_object(data)
    text = next(
        (s for s in obj.sections() if s["sectname_str"] == "__text"), None
    )
    assert text is not None
    payload = obj.data[text["offset"]:text["offset"] + text["size"]]
    # Several symbols can share an address (aliases). Slice by DISTINCT
    # address, or the second name at an address gets a zero-length body and
    # a comparison reports a difference that is an artifact of the slicing —
    # which is exactly what happened to `_fabs` and `_pow` the first time.
    by_addr: dict[int, list[str]] = {}
    for sym in obj.symbols():
        if (sym["n_type"] & spec.N_TYPE) != spec.N_SECT or sym["n_sect"] != 1:
            continue
        by_addr.setdefault(sym["n_value"], []).append(sym["name"])
    addrs = sorted(by_addr)
    out = {}
    for i, addr in enumerate(addrs):
        end = addrs[i + 1] if i + 1 < len(addrs) else text["size"]
        body = payload[addr:end]
        for name in by_addr[addr]:
            out[name] = body
    return out


def test_every_function_body_matches_ld_r(tmp_path):
    archive = REPO / "pcc" / "runtime" / "libpy_runtime_pcc_py.a"
    if not archive.exists():
        raise AssertionError(
            f"{archive} missing; build it with "
            "`make -C pcc/runtime libpy_runtime_pcc_py.a`"
        )
    members = read_archive(archive.read_bytes())
    defines = set().union(*(m.defines for m in members))
    seed = {n for n in ("py_list_len", "_py_list_len", "py_dict_new",
                        "_py_dict_new", "py_str_concat", "_py_str_concat")
            if n in defines}
    assert seed, "expected known runtime entry points in the archive"
    pulled, _pending = select_members(members, seed)
    assert len(pulled) > 5, len(pulled)

    ours = link_relocatable(pulled)

    work = Path(tempfile.mkdtemp(prefix="bodies_", dir=tmp_path))
    paths = []
    for i, data in enumerate(pulled):
        path = work / f"i{i:03d}.o"
        path.write_bytes(data)
        paths.append(str(path))
    out = work / "ld_r.o"
    run = subprocess.run(
        ["xcrun", "ld", "-r", "-o", str(out)] + paths,
        capture_output=True, text=True, timeout=280,
    )
    assert run.returncode == 0, run.stderr
    theirs = out.read_bytes()

    a, b = _bodies(ours), _bodies(theirs)
    common = set(a) & set(b)
    assert len(common) > 50, len(common)
    differing = sorted(name for name in common if a[name] != b[name])
    assert not differing, (
        f"{len(differing)} of {len(common)} function bodies differ from "
        f"ld -r's; sample: {differing[:5]}"
    )
