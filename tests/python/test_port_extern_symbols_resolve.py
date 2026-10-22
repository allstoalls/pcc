"""Every runtime-port ``extern("py_*"/"pcc_*")`` must resolve at link time.

Regression for docs/investigations/capi-port-extern-static-inline-py-type-of.md:
commit 93cfbca5 made six py_capi_*_runtime port modules bind
``extern("py_type_of", ...)`` — but ``py_type_of`` is a ``static inline`` in
py_internal.h with no linkable symbol, so the port-tier app link died with
undefined ``_py_type_of`` far away from the cause (and only on a full
archive rebuild; incremental stamps hid it).

This test statically closes the class: collect every ``extern("<name>")``
binding in pcc/runtime/py/*.py whose name starts with ``py_`` or
``pcc_`` (runtime-owned namespaces), and assert each name is an exported
text symbol in the C runtime archive or the pcc-Python port archive.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).absolute().parents[2]
PORT_DIR = REPO / "pcc" / "runtime" / "py"

_EXTERN_RE = re.compile(r'extern\(\s*"((?:py_|pcc_)[A-Za-z0-9_]+)"')


def _archive_defined_symbols(archive: Path) -> set[str]:
    out = subprocess.run(
        ["nm", "-g", str(archive)],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    ).stdout
    syms = set()
    for line in out.splitlines():
        parts = line.split()
        # "<addr> T _name" — exported text/data symbols only.
        if len(parts) == 3 and parts[1] in ("T", "D", "S", "B"):
            syms.add(parts[2].lstrip("_"))
    return syms


def test_port_extern_names_resolve_in_runtime_archive(pcc_runtime_archive):
    externed: dict[str, list[str]] = {}
    for src in sorted(PORT_DIR.glob("*.py")):
        for name in _EXTERN_RE.findall(src.read_text()):
            externed.setdefault(name, []).append(src.name)
    assert externed, "no extern() bindings found — regex or layout drift"

    defined = _archive_defined_symbols(Path(pcc_runtime_archive))

    missing = {
        name: files for name, files in externed.items() if name not in defined
    }
    assert not missing, (
        "port modules extern() runtime symbols with no exported definition "
        "(static inline? typo? module not archived?):\n"
        + "\n".join(f"  {n}  <- {', '.join(fs)}" for n, fs in sorted(missing.items()))
    )
