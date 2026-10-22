from __future__ import annotations

import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"
PY_RUNTIME_DIR = RUNTIME_DIR / "py"

# These are freestanding storage/synchronization helpers, not collector policy.
# They are the complete pcc-Python GC-substrate surface under the runtime
# layering contract; additions must be reviewed rather than hidden by a broad
# member-name allowlist.
PCC_PY_GC_SUBSTRATE_SYMBOLS = {
    "g_pcc_py_gc_minor_graph_lock",
    "g_tls_pcc_py_gc_deferred_tripwire_file",
    "g_tls_pcc_py_gc_deferred_tripwire_file$tlv$init",
    "g_tls_pcc_py_gc_deferred_tripwire_line",
    "g_tls_pcc_py_gc_deferred_tripwire_line$tlv$init",
    "g_tls_pcc_py_gc_deferred_tripwire_message",
    "g_tls_pcc_py_gc_deferred_tripwire_message$tlv$init",
    "g_tls_pcc_py_gc_minor_current",
    "g_tls_pcc_py_gc_minor_current$tlv$init",
    "g_tls_pcc_py_gc_minor_graph_lock_depth",
    "g_tls_pcc_py_gc_minor_graph_lock_depth$tlv$init",
    "g_tls_pcc_py_gc_pending_minor_block",
    "g_tls_pcc_py_gc_pending_minor_block$tlv$init",
    "pcc_py_gc_defer_tripwire",
    "pcc_py_gc_finish_deferred_tripwire",
    "pcc_py_gc_minor_current_get",
    "pcc_py_gc_minor_current_set",
    "pcc_py_gc_minor_graph_lock",
    "pcc_py_gc_minor_graph_unlock",
    "pcc_py_gc_pending_minor_block_get",
    "pcc_py_gc_pending_minor_block_set",
}


def _defined_symbols(archive: Path) -> list[tuple[str, str]]:
    result = subprocess.run(
        ["nm", "-A", str(archive)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    prefix = str(archive) + ":"
    definitions: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        if not line.startswith(prefix):
            continue
        member, separator, body = line[len(prefix) :].partition(":")
        if not separator:
            continue
        fields = body.split()
        if not fields or fields[0] in {"U", "u"} or "(undefined)" in body:
            continue
        definitions.append((member, fields[-1].lstrip("_")))
    return definitions


def _python_source_for_member(member: str) -> Path:
    stem = member[:-2] if member.endswith(".o") else member
    return PY_RUNTIME_DIR / (stem + ".py")


def test_all_production_collector_symbols_are_pcc_python_owned(
    pcc_runtime_archive: Path,
) -> None:
    definitions = _defined_symbols(pcc_runtime_archive)
    collector_symbols = [
        (member, symbol)
        for member, symbol in definitions
        if symbol.startswith("pcc_gc_") or symbol.startswith("py_gc_")
    ]

    # This lower bound makes the ownership claim fail closed if nm parsing or
    # archive selection accidentally yields an empty/partial symbol family.
    assert len(collector_symbols) >= 600
    non_python = [
        (member, symbol)
        for member, symbol in collector_symbols
        if not _python_source_for_member(member).is_file()
    ]
    assert non_python == []


def test_gc_substrate_storage_and_synchronization_are_pcc_python_owned(
    pcc_runtime_archive: Path,
) -> None:
    definitions = _defined_symbols(pcc_runtime_archive)
    substrate_symbols = {
        symbol
        for member, symbol in definitions
        if member == "freestanding_runtime_high_substrate.o"
        and (
            symbol.startswith("pcc_py_gc_")
            or symbol.startswith("g_pcc_py_gc_")
            or symbol.startswith("g_tls_pcc_py_gc_")
        )
    }
    assert substrate_symbols == PCC_PY_GC_SUBSTRATE_SYMBOLS

    unexpected_c_gc = [
        (member, symbol)
        for member, symbol in definitions
        if not _python_source_for_member(member).is_file()
        and (
            symbol.startswith("pcc_gc_")
            or symbol.startswith("py_gc_")
            or symbol.startswith("pcc_py_gc_")
            or symbol.startswith("g_pcc_py_gc_")
            or symbol.startswith("g_tls_pcc_py_gc_")
        )
    ]
    assert unexpected_c_gc == []


def test_retained_c_gc_oracles_are_absent_from_production_archive_plan(
    pcc_runtime_archive: Path,
) -> None:
    members_result = subprocess.run(
        ["ar", "-t", str(pcc_runtime_archive)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert members_result.returncode == 0, (
        members_result.stdout + members_result.stderr
    )
    members = set(members_result.stdout.splitlines())

    # Same-named Python owners such as py_gc_backend.o are expected. These are
    # the C-only oracle member names that must not enter the production plan.
    assert "py_gc_index_table.o" not in members
    assert "pcc_gc_external_resource.o" not in members

    definitions = _defined_symbols(pcc_runtime_archive)
    capi_gc = {
        (member, symbol)
        for member, symbol in definitions
        if symbol
        in {
            "PyObject_GC_Del",
            "PyObject_GC_Track",
            "PyObject_GC_UnTrack",
            "PyObject_GC_New",
        }
    }
    assert capi_gc == {
        ("py_capi_object_runtime.o", "PyObject_GC_Del"),
        ("py_capi_object_runtime.o", "PyObject_GC_Track"),
        ("py_capi_object_runtime.o", "PyObject_GC_UnTrack"),
        ("py_capi_private_runtime.o", "PyObject_GC_New"),
    }
