"""Run current emitted copy/index modules against counted raw-ABI stubs.

This is a bounded two-module regression, not a managed-runtime GC gate.
No runtime archive, pcc1, PE image, historical compiler, or cached object is used.
The C fixture supplies dependency behavior; PCC emits the actual transaction
and exact membership table.
"""
from __future__ import annotations

import ast
from dataclasses import replace
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time

import pytest

from pcc.backend import elf_x86_64 as elf
from tests.python import test_freestanding_gc_relocation_copy as contract

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).with_name("fixtures")
pytestmark = [
    # The repository's default six-worker loadgroup run compiles this once.
    pytest.mark.xdist_group("gc4-copy-page-probe-native"),
    pytest.mark.pcc_gate(
        probe=lambda: sys.platform.startswith("linux")
        and platform.machine().lower() in ("x86_64", "amd64")
        and shutil.which("cc") is not None,
    ),
]

EMIT_MODULE = r"""
from pathlib import Path
import hashlib, json, sys
from pcc.frontends.python.owned_runtime_build import _compile_runtime_module, runtime_ir_passes
from pcc.ir.optimization.driver import optimize_ir
from pcc.backend.owned_object_emit import emit_owned_object
import resource
resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
copy_source, index_source, build = map(Path, sys.argv[1:])
triple = 'x86_64-unknown-linux-gnu'
for source, name in ((copy_source, 'copy'), (index_source, 'index')):
    output = build / (name + '.o')
    ir = output.with_suffix('.ll')
    _compile_runtime_module(source.stem, str(source), str(ir), triple)
    passes = runtime_ir_passes(str(source.parent.parent))
    text = optimize_ir(ir.read_text(), passes)
    ir.write_text(text)
    output.write_bytes(emit_owned_object(text, triple))
    print(json.dumps({'module': source.stem,
                      'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                      'ir_sha256': hashlib.sha256(ir.read_bytes()).hexdigest(),
                      'object_sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
                      'runtime_passes': passes, 'target': triple}), flush=True)
"""

# Explicit semantic snapshots, independent of the implementation's page-probe
# algorithm. IDs are fixture array positions (one based), never raw addresses.
# Every output field is checked; overrides describe only the named case's delta.
SUCCESS = {
    "result": 1, "second": -1, "second_clear": -1,
    "rel_nodes": [2, 3], "rel_owners": [2, 3],
    "page_nodes": [2, 3], "pages": [2, 3],
    "finish": [1, 1, 1], "finish_next_clear": 1,
    "rel_cursor": 2, "page_cursor": 2,
    "rel_revision": 38, "page_revision": 74,
    "page_flag": 0, "evacuated": 75,
    "source_flags": 8449, "target_flags": 65792,
    "source_refs": 7, "target_refs": 8, "payload_copied": 1, "forwarded": 1,
    "source_page_head": 0, "find_calls": 1, "detach_calls": 1,
    "payload_calls": 1, "install_calls": 1, "forwarding_calls": 1,
    "other_flags": 0, "cache_epoch": 23,
    "member_before": 1, "member_after": 0, "member_other": 1,
    "member_keys": 2, "member_cap": 256, "member_incomplete": 0, "calloc_calls": 0,
    "free_calls": 0, "finish_calls": 0, "unexpected_calls": 0, "stub_errors": 0,
}
PAGE_KEPT = {
    "page_nodes": [1, 2, 3], "pages": [1, 2, 3], "finish": [1, 0, 1],
    "page_cursor": 1, "page_revision": 73, "page_flag": 1,
}
UNCHANGED = {
    **SUCCESS,
    "result": 0, "rel_nodes": [1, 2, 3], "rel_owners": [1, 2, 3],
    "page_nodes": [1, 2, 3], "pages": [1, 2, 3], "finish": [0, 0, 0],
    "rel_cursor": 1, "page_cursor": 1, "rel_revision": 37, "page_revision": 73,
    "page_flag": 1, "evacuated": 11, "source_flags": 9472,
    "target_flags": 65600, "target_refs": 99, "payload_copied": 0,
    "forwarded": 0, "source_page_head": 1, "find_calls": 0,
    "detach_calls": 0, "payload_calls": 0, "install_calls": 0,
    "member_after": 1, "member_keys": 3,
}

CASES = {
    "singleton-head": SUCCESS,
    "singleton-middle": {
        **SUCCESS, "rel_nodes": [1, 3], "page_nodes": [1, 3],
        "finish": [2, 2, 1], "rel_cursor": 3, "page_cursor": 3,
    },
    "singleton-tail": {
        **SUCCESS, "rel_nodes": [1, 2], "page_nodes": [1, 2],
        "finish": [3, 3, 1], "rel_cursor": 0, "page_cursor": 0,
    },
    "cursor-other": {
        **SUCCESS, "rel_nodes": [1, 3], "page_nodes": [1, 3],
        "finish": [2, 2, 1], "rel_cursor": 1, "page_cursor": 1,
    },
    "singleton-empty": {
        **SUCCESS, "rel_nodes": [], "rel_owners": [], "page_nodes": [], "pages": [],
        "rel_cursor": 0, "page_cursor": 0, "member_other": 0, "member_keys": 0,
    },
    # Duplicate entries are possible. Removing one must keep the page selected,
    # even though the one live source owner is detached after the page decision.
    "duplicate-head": {
        **SUCCESS, **PAGE_KEPT, "rel_nodes": [2, 3, 4], "rel_owners": [1, 2, 3],
        "member_before": 2, "member_after": 1, "member_keys": 3,
    },
    "duplicate-tail": {
        **SUCCESS, **PAGE_KEPT, "rel_nodes": [2, 3, 4], "rel_owners": [2, 3, 1],
        "member_before": 2, "member_after": 1, "member_keys": 3,
    },
    # These nominal provider settings are retained as regression inputs; the
    # locked entry never calls the threads ABI. They are not concurrency tests.
    "threaded-duplicate": {
        **SUCCESS, **PAGE_KEPT, "rel_nodes": [2, 3, 4], "rel_owners": [2, 3, 1],
        "member_before": 2, "member_after": 1, "member_keys": 3,
    },
    "multi-candidate": {
        **SUCCESS, **PAGE_KEPT, "source_page_head": 2, "find_calls": 2,
        "other_flags": 2048,
    },
    # Read barriers clear bit 2048 without removing relocation membership.
    # A flags-only owner check would wrongly retire this page.
    "multi-cleared-flags": {
        **SUCCESS, **PAGE_KEPT, "source_page_head": 2, "find_calls": 2,
    },
    "multi-clear-page-flag": {
        **SUCCESS, **PAGE_KEPT, "source_page_head": 2, "find_calls": 2,
        "page_flag": 0,
    },
    "multi-no-candidate": {
        **SUCCESS, "rel_owners": [3, 4], "source_page_head": 2, "member_other": 0,
    },
    # Walk all current page owners regardless of the source's chain position.
    "multi-source-tail": {
        **SUCCESS, "rel_owners": [3, 4], "source_page_head": 2, "member_other": 0,
    },
    "stale-nonlinked": {
        **SUCCESS, "rel_nodes": [2, 3, 4], "rel_owners": [2, 3, 4], "member_keys": 3,
    },
    "threaded-stale": {
        **SUCCESS, "rel_nodes": [2, 3, 4], "rel_owners": [2, 3, 4], "member_keys": 3,
    },
    "threaded": SUCCESS,
    "payload-failure": {
        **UNCHANGED, "target_flags": 65792, "target_refs": 1,
        "payload_copied": 1, "payload_calls": 1,
    },
    "forwarding-failure": {
        **UNCHANGED, "target_flags": 65792, "target_refs": 1,
        "payload_copied": 1, "payload_calls": 1, "install_calls": 1,
    },
    "missing-source": {
        **UNCHANGED, "rel_nodes": [1, 2], "rel_owners": [2, 3], "rel_cursor": 0,
        "member_before": 0, "member_after": 0, "member_keys": 2,
    },
    "known-size-zero": UNCHANGED,
    "known-size-small": UNCHANGED,
    "target-size-small": UNCHANGED,
    "pinned": UNCHANGED,
    "pinned-flag": {**UNCHANGED, "source_flags": 533760},
    "unsupported": UNCHANGED,
    "already-forwarded": {**UNCHANGED, "forwarded": 1},
    "wrong-backend": {**UNCHANGED, "forwarding_calls": 0},
    "source-no-index": {
        **SUCCESS, **PAGE_KEPT, "finish": [1, 0, 0], "source_page_head": 1,
    },
    "source-null-page": {**SUCCESS, **PAGE_KEPT},
    "page-unselected": {**SUCCESS, "finish": [1, 0, 1], "page_revision": 73},
    "reentry": {**SUCCESS, "second": 0, "second_clear": 1, "forwarding_calls": 2},
    # The page-owner membership query skips the other 64 relocation entries.
    # This does not establish end-to-end GC/bootstrap performance: this module
    # still performs its separate source-membership/removal list searches.
    "scale": {
        **SUCCESS, "rel_nodes": list(range(2, 66)), "rel_owners": list(range(2, 66)),
        "page_nodes": list(range(2, 66)), "pages": list(range(2, 66)), "member_keys": 64,
    },
}


UNKNOWN_MEMBERSHIP = {
    "member_before": -1, "member_after": -1, "member_other": -1,
    "member_keys": 0, "member_cap": 0, "member_incomplete": 1,
}
CASES.update({
    "incomplete-head": {**SUCCESS, **UNKNOWN_MEMBERSHIP, "find_calls": 3},
    "incomplete-duplicate": {
        **CASES["duplicate-tail"], **UNKNOWN_MEMBERSHIP, "find_calls": 4,
    },
    "incomplete-multi": {**CASES["multi-cleared-flags"], **UNKNOWN_MEMBERSHIP},
    "incomplete-stale": {
        **CASES["stale-nonlinked"], **UNKNOWN_MEMBERSHIP, "find_calls": 4,
    },
    # Initial source count is exact, but a corrupt local owner's value becomes
    # unknown during the page walk. The authoritative fallback must still run.
    "invalid-local-count": {
        **CASES["multi-cleared-flags"], "member_after": -1, "member_other": -1,
        "member_incomplete": 1,
    },
    "owner-no-index": {**SUCCESS, "source_page_head": 2, "find_calls": 2},
    "owner-other-page": {**SUCCESS, "source_page_head": 2, "find_calls": 2},
    "duplicate-payload-failure": {
        **CASES["payload-failure"], "rel_nodes": [1, 2, 3, 4],
        "rel_owners": [1, 2, 3, 1], "member_before": 2, "member_after": 2,
    },
})

EMPTY_INDEX = {
    "keys": 0, "used": 0, "cap": 0, "incomplete": 0,
    "calloc_calls": 1, "free_calls": 1, "stub_errors": 0,
}
MEMBERSHIP_CASES = {
    "member-counts": {
        **EMPTY_INDEX, "counts": [0, 3, 3, 2, 1, 0, 0], "maybe": [0, 1, 0],
        "same_empty_table": 0, "retained_before_clear": 1,
    },
    "member-growth": {
        **EMPTY_INDEX, "grown_cap": 1024, "after_once": 43, "values_ok": 1,
        "same_empty_table": 0, "retained_before_clear": 1, "peak_bytes": 24576,
        "calloc_calls": 3, "free_calls": 3,
    },
    "member-collisions": {
        **EMPTY_INDEX, "before": [255, 0, 1, 2], "shifted": [255, 0, 1],
        "moved_count": 3, "decremented": 2, "last_slot": 255, "last_count": 1,
    },
    "member-initial-failure": {
        **EMPTY_INDEX, "unknown": -1, "absent_unknown": -1, "maybe_unknown": 1,
        "attempts_before_clear": 1, "recovered_empty": 0, "recovered_present": 1,
        "calloc_calls": 2,
    },
    "member-growth-failure": {
        **EMPTY_INDEX, "unknown": -1, "stale_keys": 128, "cleanup_frees": 1,
        "cleanup_cap": 0, "recovered_present": 1, "calloc_calls": 3, "free_calls": 2,
    },
    "member-overflow-invalid": {
        **EMPTY_INDEX, "limit": 9223372036854775807, "overflow": -1,
        "invalid_value": -1, "missing_forget": -1, "null_count": 0,
        "tagged_count": 0, "invalid_note": -1, "cap": 0,
        "calloc_calls": 2, "free_calls": 2,
    },
}


@pytest.fixture(scope="module")
def emitted_copy(tmp_path_factory):
    build = tmp_path_factory.mktemp("gc4-copy-page-probe-native")
    obj = build / "copy.o"
    index_obj = build / "index.o"
    index_source = contract.STRICT_SOURCE.with_name("freestanding_gc_index_table.py")
    env = dict(os.environ)
    env.pop("LC_ALL", None)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PCC_TEST_NO_NATIVE_PROVISIONING"] = "1"
    # One total 30-second two-module build budget, including C-stub linking.
    deadline = time.monotonic() + 30
    compile_result = subprocess.run(
        [sys.executable, "-c", EMIT_MODULE, str(contract.STRICT_SOURCE), str(index_source), str(build)],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=30,
    )
    (build / "compile.log").write_text(compile_result.stdout + compile_result.stderr)
    assert compile_result.returncode == 0, compile_result.stdout + compile_result.stderr
    (build / "harness_globals.h").write_text(
        "\n".join("uint64_t " + name + ";" for name in sorted(contract.RAW_GLOBAL_IMPORTS))
        + "\n"
    )
    executable = build / "copy-harness"
    remaining = deadline - time.monotonic()
    assert remaining > 0, "two-module fixture exhausted its 30-second build budget"
    link = subprocess.run(
        ["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-I", str(build),
         str(FIXTURES / "gc4_copy_page_probe_native.c"), str(obj), str(index_obj),
         "-Wl,--wrap=calloc", "-Wl,--wrap=free", "-o", str(executable)],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=remaining,
    )
    (build / "link.log").write_text(link.stdout + link.stderr)
    assert link.returncode == 0, link.stdout + link.stderr
    return build, obj, index_obj, executable


@pytest.mark.parametrize("case", CASES)
def test_current_native_copy_preserves_page_probe_semantics(emitted_copy, case):
    build, _obj, _index_obj, executable = emitted_copy
    result = subprocess.run(
        [str(executable), case], capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    observed = json.loads(result.stdout)
    with (build / "results.jsonl").open("a") as stream:
        stream.write(json.dumps({"case": case, "observed": observed}) + "\n")
    assert observed == CASES[case]


@pytest.mark.parametrize("case", MEMBERSHIP_CASES)
def test_current_native_membership_state_machine(emitted_copy, case):
    build, _obj, _index_obj, executable = emitted_copy
    result = subprocess.run([str(executable), case], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stdout + result.stderr
    observed = json.loads(result.stdout)
    with (build / "results.jsonl").open("a") as stream:
        stream.write(json.dumps({"case": case, "observed": observed}) + "\n")
    assert observed == MEMBERSHIP_CASES[case]


def _owned_elf_symbol_names(path: Path) -> tuple[set[str], set[str]]:
    # Inspect the emitted bytes without a separately scheduled nm process.
    # Unlike archive resolution, exact closure includes weak undefined names.
    # Only symbol zero is omitted; parse_relocatable validates its null shape.
    symbols = elf.parse_relocatable(path.read_bytes()).symbols[1:]
    undefined = {
        symbol.name for symbol in symbols if symbol.section_index == elf.SHN_UNDEF
    }
    defined = {
        symbol.name for symbol in symbols
        if symbol.binding != elf.STB_LOCAL and symbol.section_index != elf.SHN_UNDEF
    }
    return undefined, defined


def test_current_owned_copy_has_exact_strict_closure(emitted_copy):
    _build, obj, index_obj, _executable = emitted_copy
    undefined, defined = _owned_elf_symbol_names(obj)
    assert undefined == (
        contract.RAW_FUNCTION_IMPORTS | contract.RAW_GLOBAL_IMPORTS
    )
    assert defined == contract.OWNED_SYMBOLS

    index_undefined, index_defined = _owned_elf_symbol_names(index_obj)
    assert index_undefined == {
        "calloc", "free", "pcc_allocator_object_side_slot", "pcc_allocator_granule_object_slot",
    }
    index_source = contract.STRICT_SOURCE.with_name("freestanding_gc_index_table.py")
    source_tree = ast.parse(index_source.read_text())
    expected_index_symbols = {
        node.args[0].value
        for node in ast.walk(source_tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id in {"c_abi_export", "define_global_i64", "define_global_ptr_null"}
        and node.args and isinstance(node.args[0], ast.Constant)
    }
    assert {
        "pcc_gc_relocation_member_count", "pcc_gc_relocation_member_note",
        "pcc_gc_relocation_member_forget", "pcc_gc_relocation_member_clear",
    } <= expected_index_symbols
    assert index_defined == expected_index_symbols


def _symbol_fixture():
    return elf.ElfObject(
        sections=(
            elf.ElfSection(".text", elf.SHT_PROGBITS, elf.SHF_ALLOC, 1, b"\xc3"),
            elf.ElfSection(".bss", elf.SHT_NOBITS, elf.SHF_ALLOC | elf.SHF_WRITE, 8,
                           mem_size=8),
        ),
        symbols=(
            elf.ElfSymbol.null(),
            elf.ElfSymbol("", 1, 0, 0, elf.STB_LOCAL, elf.STT_SECTION),
            elf.ElfSymbol("local_definition", 1, 0, 1, elf.STB_LOCAL, elf.STT_FUNC),
            elf.ElfSymbol("local_import", elf.SHN_UNDEF, 0, 0,
                          elf.STB_LOCAL, elf.STT_NOTYPE),
            elf.ElfSymbol("global_import", elf.SHN_UNDEF, 0, 0,
                          elf.STB_GLOBAL, elf.STT_NOTYPE),
            elf.ElfSymbol("weak_import", elf.SHN_UNDEF, 0, 0,
                          elf.STB_WEAK, elf.STT_NOTYPE),
            elf.ElfSymbol("global_definition", 1, 0, 1, elf.STB_GLOBAL, elf.STT_FUNC),
            elf.ElfSymbol("weak_definition", 1, 0, 1, elf.STB_WEAK, elf.STT_FUNC),
            elf.ElfSymbol("hidden_definition", 1, 0, 1, elf.STB_GLOBAL, elf.STT_FUNC,
                          elf.STV_HIDDEN),
            elf.ElfSymbol("absolute_definition", elf.SHN_ABS, 42, 0,
                          elf.STB_GLOBAL, elf.STT_NOTYPE),
            elf.ElfSymbol("bss_definition", 2, 0, 8, elf.STB_GLOBAL, elf.STT_OBJECT),
        ),
    )


def test_owned_symbol_names_preserve_exact_binding_and_section_semantics(tmp_path, monkeypatch):
    path = tmp_path / "symbols.o"
    path.write_bytes(elf.emit_relocatable(_symbol_fixture()))

    def forbid_process(*args, **kwargs):
        raise AssertionError("object-closure inspection must not start an external process")

    monkeypatch.setattr(subprocess, "Popen", forbid_process)
    assert _owned_elf_symbol_names(path) == (
        {"local_import", "global_import", "weak_import"},
        {"global_definition", "weak_definition", "hidden_definition",
         "absolute_definition", "bss_definition"},
    )


@pytest.mark.parametrize("binding", [elf.STB_LOCAL, elf.STB_GLOBAL, elf.STB_WEAK])
def test_owned_symbol_names_include_unreferenced_undefined_symbols(tmp_path, binding):
    original = _symbol_fixture()
    extra = elf.ElfSymbol("unexpected_import", elf.SHN_UNDEF, 0, 0,
                          binding, elf.STT_NOTYPE)
    symbols = list(original.symbols)
    symbols.insert(1 if binding == elf.STB_LOCAL else len(symbols), extra)
    path = tmp_path / "unexpected.o"
    path.write_bytes(elf.emit_relocatable(replace(original, symbols=tuple(symbols))))
    undefined, _defined = _owned_elf_symbol_names(path)
    assert undefined == {
        "local_import", "global_import", "weak_import", "unexpected_import",
    }


@pytest.mark.parametrize("payload", [b"", b"not an ELF object", b"\x7fELF" + b"\0" * 64])
def test_owned_symbol_names_reject_invalid_objects(tmp_path, payload):
    path = tmp_path / "invalid.o"
    path.write_bytes(payload)
    with pytest.raises(elf.ElfError):
        _owned_elf_symbol_names(path)


def test_owned_symbol_names_reject_truncated_symbol_table(tmp_path):
    encoded = elf.emit_relocatable(_symbol_fixture())
    header = elf._ELF_HEADER.unpack_from(encoded)
    section_table = header[6]
    damaged = bytearray(encoded)
    for offset in range(section_table, len(encoded), elf._SECTION_HEADER.size):
        section = list(elf._SECTION_HEADER.unpack_from(encoded, offset))
        if section[1] == elf.SHT_SYMTAB:
            section[5] -= 1
            elf._SECTION_HEADER.pack_into(damaged, offset, *section)
            break
    else:
        raise AssertionError("fixture is missing its symbol table")
    path = tmp_path / "truncated-symbols.o"
    path.write_bytes(damaged)
    with pytest.raises(elf.ElfError, match="symbol table has an invalid entry size"):
        _owned_elf_symbol_names(path)
