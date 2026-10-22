from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

from tests.runtime_build_cache import cached_pcc_python_runtime


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "AGENTS.md").exists():
            return parent
    raise RuntimeError("AGENTS.md not found walking up from test file")


REPO_ROOT = _repo_root()
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"


def _cc() -> str:
    return os.environ.get("CC", "cc")


def _build_runtime(tmp_path: Path) -> Path:
    del tmp_path
    return cached_pcc_python_runtime()


def _compile_and_run(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
    work_runtime = _build_runtime(tmp_path)
    src = tmp_path / "backend3_barrier_probe.c"
    exe = tmp_path / "backend3_barrier_probe.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    build = subprocess.run(
        [
            _cc(),
            "-std=c11",
            f"-I{work_runtime / 'include'}",
            f"-I{work_runtime / 'src'}",
            str(src),
            str(work_runtime / "libpy_runtime_pcc_py.a"),
            "-lm",
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    return subprocess.run([str(exe)], capture_output=True, text=True, timeout=60)


def test_backend3_list_extend_old_to_young_remembers_owner(tmp_path):
    """py_list_extend must record each grown-slot store via the write barrier
    (NULL-init + pcc_gc_store_ptr) so an OLD list gaining YOUNG elements is
    entered into the generational remembered set.

    Load-bearing: extend is the ONLY store of these elements, so its barrier
    is the sole reason the OLD destination list carries PY_FLAG_GC_REMEMBERED.
    Without the barrier (a raw store) the flag would be clear and the minor
    collector would never scan the list, so the young elements reachable only
    through it would be lost. The REMEMBERED assertion is the barrier
    discriminator; the pre/post-cycle content checks prove no element is lost.
    Elements are young *lists* (non-leaf, hence graph-tracked and
    barrier-eligible) carrying a unique tagged-int payload.
    """
    proc = _compile_and_run(
        tmp_path,
        '''
        #include "py_internal.h"
        #include <stdio.h>

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR) != 0)
                return 2;

            PyObject *dst_root = 0;
            pcc_gc_scheduler_root_register(&dst_root);

            PyObject *dst = py_list_new(0);
            if (dst == 0) return 3;
            pcc_gc_store_root(&dst_root, dst);
            {
                PyObjectHeader *dh = (PyObjectHeader *)dst;
                dh->flags =
                    (dh->flags & ~(PY_FLAG_GC_YOUNG | PY_FLAG_GC_MINOR_ARENA))
                    | PY_FLAG_GC_OLD;
            }

            enum { N = 6 };
            PyObject *src = py_list_new(0);
            if (src == 0) return 4;
            for (int i = 0; i < N; i++) {
                PyObject *elem = py_list_new(1);
                if (elem == 0) return 5;
                py_list_append(elem, py_int_from_i64(1000 + i));
                py_list_append(src, elem);
                pcc_gc_release(elem);
            }

            py_list_extend(dst, src);

            /* Barrier-attributable: the extend recorded the OLD list into the
             * remembered set (owner granularity). Zero without the barrier. */
            if ((((PyObjectHeader *)dst)->flags & PY_FLAG_GC_REMEMBERED) == 0)
                return 10;

            if (py_list_len(dst) != N) return 11;
            for (int i = 0; i < N; i++) {
                PyObject *got = py_list_get(dst, i);
                if (got == 0) return 12;
                PyObject *inner = py_list_get(got, 0);
                int of = 0;
                int64_t v = py_int_to_i64(inner, &of);
                pcc_gc_release(inner);
                pcc_gc_release(got);
                if (of || v != 1000 + i) return 13;
            }

            /* Drop the young source so the elements are reachable only via the
             * OLD list, then run minor cycles. The remembered-set entry from
             * the extend barrier is what promotes and preserves them; draining
             * clears the owner's REMEMBERED flag. */
            pcc_gc_release(src);
            for (int r = 0;
                 r < 32
                 && (((PyObjectHeader *)pcc_gc_load_ptr(0, &dst_root))->flags
                     & PY_FLAG_GC_REMEMBERED) != 0;
                 r++) {
                (void)pcc_gc_step(256);
            }
            dst = pcc_gc_load_ptr(0, &dst_root);
            if (dst == 0) return 14;

            if (py_list_len(dst) != N) return 15;
            for (int i = 0; i < N; i++) {
                PyObject *got = py_list_get(dst, i);
                if (got == 0) return 16;
                PyObject *inner = py_list_get(got, 0);
                int of = 0;
                int64_t v = py_int_to_i64(inner, &of);
                pcc_gc_release(inner);
                pcc_gc_release(got);
                if (of || v != 1000 + i) return 17;
            }

            pcc_gc_store_root(&dst_root, 0);
            pcc_gc_scheduler_root_unregister(&dst_root);
            printf("backend3-list-extend-barrier-ok\\n");
            return 0;
        }
        ''',
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend3-list-extend-barrier-ok"


def test_backend3_set_rehash_old_to_young_preserves_keys(tmp_path):
    """End-to-end regression for py_set_rehash under generational pressure:
    an OLD set that gains YOUNG keys and rehashes several times must lose no
    key across the moves + a minor collection.

    Scope note (honest): backend #3's remembered set is OWNER granularity and
    pcc_gc_backend3_remember_owner_unlocked is idempotent, so py_set_add's
    insert store_ptr already remembers the set; the rehash-specific
    pcc_gc_note_slot_write_barrier is therefore redundant with the add-time
    barrier ON THIS BACKEND (unlike backend #4, where it re-tracks the moved
    slot). This test cannot isolate the rehash barrier on #3; the REMEMBERED
    assertion catches the absence of the old->young barrier in the set path as
    a whole, and the membership/length checks guard set-rehash-under-minor-GC
    correctness. Keys are young *tuples* (hashable and non-leaf/tracked).
    """
    proc = _compile_and_run(
        tmp_path,
        '''
        #include "py_internal.h"
        #include <stdio.h>

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR) != 0)
                return 2;

            PyObject *set_root = 0;
            pcc_gc_scheduler_root_register(&set_root);

            PyObject *set = py_set_new();
            if (set == 0) return 3;
            pcc_gc_store_root(&set_root, set);
            {
                PyObjectHeader *sh = (PyObjectHeader *)set;
                sh->flags =
                    (sh->flags & ~(PY_FLAG_GC_YOUNG | PY_FLAG_GC_MINOR_ARENA))
                    | PY_FLAG_GC_OLD;
            }

            /* Add 12 young tuple keys to an OLD set: capacity 8 -> 16 -> 32,
             * i.e. at least two rehashes that move live keys into fresh
             * arrays while the set holds young keys. */
            enum { N = 12 };
            for (int i = 0; i < N; i++) {
                PyObject *k = py_tuple_new(1);
                if (k == 0) return 4;
                py_tuple_set_item(k, 0, py_int_from_i64(3000 + i));
                py_set_add(set, k);
                pcc_gc_release(k);
            }

            /* old->young barrier fired in the set path at least once. */
            if ((((PyObjectHeader *)set)->flags & PY_FLAG_GC_REMEMBERED) == 0)
                return 10;
            /* A rehash provably happened (table grew past the initial 8). */
            if (((PySetObject *)set)->capacity <= 8) return 11;

            /* No key lost across the rehashes: membership + length. */
            if (py_set_len(set) != N) return 12;
            for (int i = 0; i < N; i++) {
                PyObject *probe = py_tuple_new(1);
                if (probe == 0) return 13;
                py_tuple_set_item(probe, 0, py_int_from_i64(3000 + i));
                int64_t found = py_set_contains(set, probe);
                pcc_gc_release(probe);
                if (found != 1) return 14;
            }

            /* Run minor cycles; then re-check every key still resolves. */
            for (int r = 0;
                 r < 32
                 && (((PyObjectHeader *)pcc_gc_load_ptr(0, &set_root))->flags
                     & PY_FLAG_GC_REMEMBERED) != 0;
                 r++) {
                (void)pcc_gc_step(256);
            }
            set = pcc_gc_load_ptr(0, &set_root);
            if (set == 0) return 15;
            if (py_set_len(set) != N) return 16;
            for (int i = 0; i < N; i++) {
                PyObject *probe = py_tuple_new(1);
                if (probe == 0) return 17;
                py_tuple_set_item(probe, 0, py_int_from_i64(3000 + i));
                int64_t found = py_set_contains(set, probe);
                pcc_gc_release(probe);
                if (found != 1) return 18;
            }

            pcc_gc_store_root(&set_root, 0);
            pcc_gc_scheduler_root_unregister(&set_root);
            printf("backend3-set-rehash-preserved-ok\\n");
            return 0;
        }
        ''',
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend3-set-rehash-preserved-ok"
