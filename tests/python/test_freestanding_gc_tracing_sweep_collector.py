from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from pcc.frontends.python import pipeline
from pcc.frontends.python.codegen.runtime_abi import FREESTANDING_GC_RUNTIME_GLOBALS


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"
STRICT_SOURCE = (
    RUNTIME_DIR / "py" / "freestanding_gc_tracing_sweep_collector.py"
)
MANAGED_SOURCE = RUNTIME_DIR / "py" / "py_gc_backend.py"
MAKEFILE = RUNTIME_DIR / "Makefile"

OWNED_SYMBOLS = {
    "pcc_gc_tracing_finalize_unreachable",
    "pcc_gc_tracing_has_sweep_candidate",
    "pcc_gc_tracing_recheck_reachability_after_finalizers",
    "pcc_gc_tracing_sweep_unreachable",
}
RAW_FUNCTION_IMPORTS = {
    "pcc_gc_object_is_address_pinned",
    "pcc_capi_dealloc_cext_object",
    "pcc_capi_is_cext_type_tag",
    "pcc_gc_drain_all_gray_unlocked",
    "pcc_gc_forwarding_index_find",
    "pcc_gc_free_watch_begin",
    "pcc_gc_free_watch_end",
    "pcc_gc_note_object_freeing",
    "pcc_gc_object_node_is_active",
    "pcc_gc_seed_roots",
    "pcc_gc_tracing_clear_unreachable",
    "pcc_py_gc_minor_graph_lock",
    "pcc_py_gc_minor_graph_unlock",
    "pcc_refcount_forget",
    "py_class_dealloc",
    "py_descriptor_dealloc",
    "py_dealloc_continuation",
    "py_dealloc_coroutine",
    "py_dealloc_dict",
    "py_dealloc_exc",
    "py_dealloc_file",
    "py_dealloc_float",
    "py_dealloc_func",
    "py_dealloc_gen",
    "py_dealloc_generic",
    "py_dealloc_int",
    "py_dealloc_iter",
    "py_dealloc_list",
    "py_dealloc_memoryview",
    "py_dealloc_set",
    "py_dealloc_str",
    "py_dealloc_task",
    "py_dealloc_thread_condition",
    "py_dealloc_thread_event",
    "py_dealloc_thread_lock",
    "py_dealloc_thread_rlock",
    "py_dealloc_thread_semaphore",
    "py_dealloc_thread_thread",
    "py_dealloc_tuple",
    "py_dealloc_virtual_thread",
    "py_dealloc_vthread_channel",
    "py_dealloc_weakref",
    "py_gc_untrack",
    "py_instance_dealloc",
    "py_user_del_dispatch",
}
RAW_GLOBAL_IMPORTS = {
    "pcc_gc_backend_selected",
    "pcc_gc_object_head",
}


def _exported_symbols(source: str) -> set[str]:
    return set(re.findall(r'@c_abi_export\("([^"]+)"\)', source))


def _compile_object(tmp_path: Path, emitter: str) -> Path:
    llvm_ir = tmp_path / ("freestanding_gc_tracing_sweep_" + emitter + ".ll")
    pipeline.compile_python(
        str(STRICT_SOURCE),
        str(llvm_ir),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    source = llvm_ir
    if emitter == "self":
        from pcc.backend.self_backend_dispatch import emit_self_asm

        source = tmp_path / "freestanding_gc_tracing_sweep.s"
        source.write_text(
            emit_self_asm(llvm_ir.read_text(encoding="utf-8")),
            encoding="utf-8",
        )
    obj = tmp_path / ("freestanding_gc_tracing_sweep_" + emitter + ".o")
    result = subprocess.run(
        ["clang", "-c", str(source), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return obj


def test_tracing_sweep_collector_has_one_strict_source_owner():
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    managed = MANAGED_SOURCE.read_text(encoding="utf-8")
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert "__pcc_freestanding__ = True" in strict
    assert _exported_symbols(strict) == OWNED_SYMBOLS
    assert _exported_symbols(managed).isdisjoint(OWNED_SYMBOLS)
    assert "freestanding_gc_tracing_sweep_collector" in makefile

    for name in ("_has_sweep_candidate", "_sweep_unreachable"):
        assert f"{name} = extern(" in managed
        assert f"def {name}(" not in managed
    for name in (
        "_finish_delayed_zpage_freeing_note",
        "_finalize_unreachable",
        "_recheck_reachability_after_finalizers",
    ):
        assert f"def {name}(" not in managed


def test_tracing_sweep_preserves_pep442_and_two_pass_order():
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    sweep = strict.split(
        '@c_abi_export("pcc_gc_tracing_sweep_unreachable")', 1
    )[1]

    recheck = "pcc_gc_tracing_recheck_reachability_after_finalizers()"
    assert sweep.count(recheck) == 2
    assert sweep.index("if budget <= 0:") < sweep.index(recheck)
    assert sweep.index(recheck) < sweep.index("py_user_del_dispatch(obj)")
    assert sweep.index("py_user_del_dispatch(obj)") < sweep.rindex(recheck)
    assert sweep.rindex(recheck) < sweep.index(
        "pcc_gc_tracing_clear_unreachable(obj)"
    )
    assert sweep.index("pcc_gc_tracing_clear_unreachable(obj)") < sweep.index(
        "pcc_gc_tracing_finalize_unreachable(obj)"
    )
    assert "(flags & 16384) == 0 and pcc_gc_object_is_address_pinned(obj) == 0" in sweep
    assert "pcc_capi_is_cext_type_tag(tag) == 0" in sweep

    finalize = strict.split(
        '@c_abi_export("pcc_gc_tracing_finalize_unreachable")', 1
    )[1].split("\n@c_abi_export", 1)[0]
    assert finalize.index("store_i32(obj, 12, flags | 524288)") < finalize.index(
        "pcc_gc_note_object_freeing(obj)"
    )
    assert "backend == 4 and (flags & 65536) != 0" in finalize
    assert finalize.index("pcc_capi_dealloc_cext_object(obj, tag) == 0") < (
        finalize.index(
            'if tag >= abi_constant("object.type.user_class_start"):'
        )
    )


@pytest.mark.parametrize("emitter", ["llvm", "self"])
def test_tracing_sweep_object_has_exact_raw_closure(tmp_path: Path, emitter: str):
    obj = _compile_object(tmp_path, emitter)
    assert RAW_GLOBAL_IMPORTS <= FREESTANDING_GC_RUNTIME_GLOBALS
    undefined_result = subprocess.run(
        ["nm", "-u", str(obj)], capture_output=True, text=True, timeout=30
    )
    assert undefined_result.returncode == 0, (
        undefined_result.stdout + undefined_result.stderr
    )
    undefined = {
        line.split()[-1].lstrip("_")
        for line in undefined_result.stdout.splitlines()
        if line.strip()
    }
    assert undefined == RAW_FUNCTION_IMPORTS | RAW_GLOBAL_IMPORTS

    symbols_result = subprocess.run(
        ["nm", "-g", str(obj)], capture_output=True, text=True, timeout=30
    )
    assert symbols_result.returncode == 0, symbols_result.stdout + symbols_result.stderr
    defined = {
        line.split()[-1].lstrip("_")
        for line in symbols_result.stdout.splitlines()
        if line.strip() and " U " not in line
    }
    assert defined == OWNED_SYMBOLS


def test_production_archive_has_one_tracing_sweep_owner(
    pcc_runtime_archive: Path,
):
    symbols_result = subprocess.run(
        ["nm", "-A", "-g", str(pcc_runtime_archive)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert symbols_result.returncode == 0, symbols_result.stdout + symbols_result.stderr
    for symbol in OWNED_SYMBOLS:
        owners = [
            line
            for line in symbols_result.stdout.splitlines()
            if line.strip()
            and line.split()[-1].lstrip("_") == symbol
            and " U " not in line
        ]
        assert len(owners) == 1, (symbol, owners)
        assert ":freestanding_gc_tracing_sweep_collector.o:" in owners[0]
        assert ":py_gc_backend.o:" not in owners[0]
