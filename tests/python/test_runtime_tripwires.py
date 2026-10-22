from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).absolute().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"


# The pcc-Python runtime's tripwires are always armed: no special build.
@pytest.fixture(scope="module")
def armed_runtime(pcc_runtime_archive: Path) -> Path:
    return pcc_runtime_archive


@pytest.fixture(scope="module")
def armed_runtime_threaded(threaded_pcc_runtime_archive: Path) -> Path:
    return threaded_pcc_runtime_archive


def _compile_probe(tmp_path: Path, archive: Path, name: str, source: str) -> Path:
    src = tmp_path / f"{name}.c"
    exe = tmp_path / name
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    build = subprocess.run(
        [
            os.environ.get("CC", "cc"),
            "-std=c11",
            f"-I{RUNTIME_DIR / 'include'}",
            f"-I{RUNTIME_DIR / 'src'}",
            str(src),
            str(archive),
            "-lm",
            "-pthread",
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    return exe


def test_tripwire_source_covers_named_runtime_boundaries():
    internal = (RUNTIME_DIR / "src" / "py_internal.h").read_text(encoding="utf-8")

    assert "#ifdef PCC_RUNTIME_TRIPWIRES" in internal
    assert "#define PCC_RT_TRIPWIRE(cond, msg) ((void)0)" in internal


def test_armed_tripwires_accept_valid_roots_zpage_forwarding_and_native_handle(
    tmp_path: Path,
    armed_runtime: Path,
):
    exe = _compile_probe(
        tmp_path,
        armed_runtime,
        "tripwire_valid_probe",
        r'''
        #include "py_runtime.h"
        #include "py_internal.h"

        static int release_hits = 0;

        static void release_foreign(void *ptr) {
            if (ptr == (void *)(uintptr_t)0x2000) release_hits++;
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            py_cpy_handle_set_release_fn(release_foreign);
            PyObject *handle = py_cpy_handle_new((void *)(uintptr_t)0x2000);
            if (handle == 0) return 3;
            py_decref(handle);
            if (release_hits != 1) return 4;

            /* A zero-length list-shaped object is non-leaf, so backend 4
             * places it on a zpage and the relocation selector can choose it. */
            PyObject *root = pcc_gc_alloc(64, PY_TYPE_LIST, 0);
            if (root == 0) return 5;
            pcc_gc_scheduler_root_register(&root);
            if (pcc_gc_scheduler_root_count() != 1) return 6;

            int32_t frame_map[2] = {1, 0};
            pcc_gc_register_continuation_root(frame_map, &root);
            if (pcc_gc_continuation_root_slot_count() != 1) return 7;

            if (pcc_gc_select_relocation_set(8) <= 0) return 8;
            if (!pcc_gc_relocation_set_contains(root)) return 9;
            PyObject *old = root;
            PyObject *moved = pcc_gc_relocate_copy(old, 64);
            if (moved == 0 || moved == old) return 10;
            if (pcc_gc_note_relocation_read(old) != moved) return 11;

            pcc_gc_unregister_continuation_root(&root);
            pcc_gc_scheduler_root_unregister(&root);
            return 0;
        }
        ''',
    )
    result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_armed_tripwire_fault_injection_aborts_with_runtime_log(
    tmp_path: Path,
    armed_runtime: Path,
):
    exe = _compile_probe(
        tmp_path,
        armed_runtime,
        "tripwire_fault_probe",
        r'''
        #include "py_runtime.h"
        #include <stdint.h>

        int main(void) {
            PyObject *handle = py_cpy_handle_new((void *)(uintptr_t)0x1000);
            if (handle == 0) return 2;
            /* Deliberately omit py_cpy_handle_set_release_fn(). */
            py_decref(handle);
            return 3;
        }
        ''',
    )
    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "PCC_LOG": "runtime"},
    )
    assert result.returncode != 0
    assert "TRIPWIRE" in result.stderr
    assert "owned foreign reference has no release hook" in result.stderr


def test_armed_deferred_graph_tripwire_aborts_after_outer_unlock(
    tmp_path: Path,
    armed_runtime: Path,
):
    exe = _compile_probe(
        tmp_path,
        armed_runtime,
        "deferred_graph_tripwire_fault_probe",
        r'''
        #include "py_runtime.h"
        #include "py_internal.h"

        int main(void) {
            if (pcc_gc_set_backend(
                    PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
                ) != 0) return 2;
            PyObject *obj = pcc_gc_alloc(4096, PY_TYPE_LIST, 0);
            if (obj == 0) return 3;
            py_header_flags_or(py_header(obj), PY_FLAG_GC_OLD);
            (void)pcc_gc_step(1);
            return 4;
        }
        ''',
    )
    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "PCC_LOG": "runtime"},
    )
    assert result.returncode != 0
    assert "TRIPWIRE" in result.stderr
    assert "promoting a YOUNG object already marked OLD" in result.stderr


def test_armed_cpy_violation_defers_until_unlock_under_graph_lock(
    tmp_path: Path,
    armed_runtime_threaded: Path,
):
    """A graph-lock owner that violates the native-handle move contract must
    not enter the fatal log sink while locked: the violation reports only at
    the outer unlock, so reaching the unlock proves the deferral happened."""
    exe = _compile_probe(
        tmp_path,
        armed_runtime_threaded,
        "cpy_mixed_deferred_probe",
        r'''
        #include "py_runtime.h"
        #include "py_internal.h"
        #include <stdio.h>

        int main(void) {
            pcc_py_gc_minor_graph_lock();
            pcc_cpy_handle_move_owned_ref(NULL, NULL);
            /* Reaching this marker means no fatal log sink ran while the
             * thread owned the graph lock. */
            fprintf(stderr, "LOCK-HOLDER-CONTINUED\n");
            pcc_py_gc_minor_graph_unlock();
            return 7;  /* must never be reached: finish aborts */
        }
        ''',
    )
    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "PCC_LOG": "runtime"},
    )
    assert result.returncode != 0, result.stdout + result.stderr
    stderr = result.stderr
    assert "LOCK-HOLDER-CONTINUED" in stderr, stderr
    assert "invalid native-handle move" in stderr, stderr
    assert stderr.index("LOCK-HOLDER-CONTINUED") < stderr.index("TRIPWIRE")


def test_armed_cpy_violation_aborts_immediately_without_lock(
    tmp_path: Path,
    armed_runtime: Path,
):
    exe = _compile_probe(
        tmp_path,
        armed_runtime,
        "cpy_mixed_immediate_probe",
        r'''
        #include "py_runtime.h"
        #include "py_internal.h"

        int main(void) {
            pcc_cpy_handle_move_owned_ref(NULL, NULL);
            return 7;  /* must never be reached: immediate abort */
        }
        ''',
    )
    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "PCC_LOG": "runtime"},
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "invalid native-handle move" in result.stderr


LOCKED_DEFER_SITE_MESSAGES = (
    "forwarded-source payload retirement failed before target teardown",
    "source side-table commit failed after payload detachment",
    "forwarded-source granule retirement invariant violated",
    "forwarded-source payload retirement failed before normal teardown",
    "pcc_gc_backend4_note_forwarding_removed_on_page_unlocked: forwarding count underflow / duplicate removal",
)
# Sites inside a symbol exported by py_runtime.h cannot assume the caller
# holds the graph lock, so they route by lock ownership instead: deferred for
# an owner, still immediately fatal for an unlocked caller.
LOCKED_MIXED_SITE_MESSAGES = (
    "source side-table commit detached count mismatch",
)


def _site_window(source: str, message: str) -> str:
    """Return the call expression surrounding one invariant message."""
    assert source.count(message) == 1, message
    end = source.index(message) + len(message)
    start = max(0, source.rindex("(", 0, source.index(message)) - 200)
    return source[start:end]


def test_cpy_handle_move_checks_defer_under_graph_lock():
    # All three checks use the bailing owner macro: a violation must not
    # fall through to the move and drop an owned foreign reference.

    internal = (RUNTIME_DIR / "src" / "py_internal.h").read_text(
        encoding="utf-8"
    )
    owner_macro = internal.split(
        "#define PCC_GC_OWNER_TRIPWIRE(cond, msg)", 1
    )[1].split("#else", 1)[0]
    assert "pcc_gc_tripwire_defer_or_fail(" in owner_macro
    assert "return;" in owner_macro


def test_strict_port_routes_locked_fatal_logs_through_deferred_slot():
    substrate = (
        RUNTIME_DIR / "py" / "freestanding_runtime_high_substrate.py"
    ).read_text(encoding="utf-8")
    # Both exports pin exact ABI widths, so the strict port and the C oracle
    # cannot drift on the line argument or the seam's return type.
    defer_export = substrate.split("def pcc_py_gc_defer_tripwire(", 1)[0]
    assert (
        '"pcc_py_gc_defer_tripwire", "void", ("ptr", "ptr", "i32")'
        in defer_export
    )
    seam_export = substrate.split("def pcc_gc_tripwire_defer_or_fail(", 1)[0]
    assert (
        '"pcc_gc_tripwire_defer_or_fail", "i32", ("ptr", "ptr", "i32")'
        in seam_export
    )
    internal = (RUNTIME_DIR / "src" / "py_internal.h").read_text(
        encoding="utf-8"
    )
    seam_decl = internal.split(
        "int pcc_gc_tripwire_defer_or_fail(", 1
    )[1].split(");", 1)[0]
    assert "int32_t line" in seam_decl
    unlock = substrate.split("def pcc_py_gc_minor_graph_unlock()", 1)[1].split(
        "@c_abi_export", 1
    )[0]
    # Exactly two finish sites: the threads-off exit and the outermost
    # threaded release, with the report strictly after the physical store.
    assert unlock.count("_finish_deferred_tripwire()") == 2
    release = unlock.index('"release",')
    assert unlock.index("_finish_deferred_tripwire()", release) > release
    shallow = unlock.split("if depth <= 0:", 1)[1].split("return", 1)[0]
    assert "_finish_deferred_tripwire" not in shallow

    for name, calls in (
        ("freestanding_gc_forwarding_retirement.py", 3),
        ("freestanding_gc_relocation_payload.py", 1),
        ("py_gc_backend.py", 1),
    ):
        body = (RUNTIME_DIR / "py" / name).read_text(encoding="utf-8")
        assert 'extern(\n    "pcc_runtime_tripwire_fail"' not in body, name
        assert body.count("pcc_py_gc_defer_tripwire(") == calls, name

    # Deallocation runs outside the graph lock and keeps immediate fatals.
    dealloc = (RUNTIME_DIR / "py" / "py_obj_dealloc.py").read_text(
        encoding="utf-8"
    )
    assert 'extern(\n    "pcc_runtime_tripwire_fail"' in dealloc


