"""Slab-granule provenance map behavior and publication contract.

The production map uses 4 KiB keys: each 64 KiB allocator slab publishes
sixteen keys that all point to one stable ``{kind, stride, base}`` span.  The
threaded gate deliberately bypasses ``threading.Thread``: the pcc-Python
``py_threading`` compatibility layer is synchronous, so the supported
single-writer/lock-free-reader contract is exercised through the runtime's
real ``pcc_thread_start`` pthread ABI instead.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import textwrap
from typing import Iterator

import pytest

from pcc.frontends.python.pipeline import compile_python
from pcc.tools.runtime_archive_provenance import verify_runtime_archive_manifest
from tests.runtime_build_cache import (
    cached_pcc_python_runtime,
    cached_threaded_pcc_python_runtime,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"
ARCHIVE_NAME = "libpy_runtime_pcc_py.a"


def test_granule_cache_survives_raw_address_reuse(
    tmp_path: Path, pcc_runtime_archive: Path,
) -> None:
    """A retired raw span must not hide a new object at the same address.

    The external C driver is an ABI oracle for the self-emitted allocator.
    Rebinding one reserved mapping makes OS address reuse deterministic;
    clearing the positive cache exercises the normal span-cache miss path.
    """
    source = tmp_path / "span_reuse.c"
    output = tmp_path / "span_reuse"
    source.write_text(textwrap.dedent(r'''
        #define _GNU_SOURCE
        #include "py_internal.h"
        #include <sys/mman.h>
        #include <stdint.h>
        #include <stdio.h>
        #include <stdlib.h>

        extern int64_t pcc_allocator_granule_retire_slab_locked(void *);
        extern void pcc_allocator_lock_acquire(void);
        extern void pcc_allocator_lock_release(void);
        extern int64_t pcc_allocator_exact_object_cache[8192];

        int main(void) {
            if (pcc_gc_set_backend(atoi(getenv("PCC_GC_BACKEND"))) != 0) return 10;
            void *base = mmap(NULL, 65536, PROT_READ | PROT_WRITE,
                              MAP_PRIVATE | MAP_ANON, -1, 0);
            if (base == MAP_FAILED) return 1;
            PyObject *object = (PyObject *)((char *)base + 48);
            if (pcc_gc_granule_register_slab(base, 2, 112) != 1) return 2;
            if (pcc_gc_granule_is_object_start(object) != -1) return 3;
            pcc_allocator_lock_acquire();
            int64_t retired = pcc_allocator_granule_retire_slab_locked(base);
            pcc_allocator_lock_release();
            if (retired != 1) return 4;
            if (pcc_gc_granule_register_slab(base, 1, 112) != 1) return 5;
            /* GC_STATE_RESERVED in freestanding_allocator.py. */
            *(int64_t *)base = INT64_C(5783538902897647429);
            ((PyObjectHeader *)object)->refcount = 1;
            ((PyObjectHeader *)object)->type_tag = PY_TYPE_INT;
            if (pcc_gc_granule_object_publish(object) != 1) return 6;
            for (int slot = 0; slot < 8192; slot++) {
                __atomic_store_n(&pcc_allocator_exact_object_cache[slot],
                                 0, __ATOMIC_RELAXED);
            }
            int64_t granule = pcc_gc_granule_is_object_start(object);
            int64_t managed = pcc_gc_pointer_is_managed(object);
            printf("granule=%lld managed=%lld\n",
                   (long long)granule, (long long)managed);
            return granule == 1 && managed == 1 ? 0 : 9;
        }
    '''), encoding="utf-8")
    built = subprocess.run(
        [os.environ.get("CC", "cc"), "-std=c11", "-pthread",
         f"-I{RUNTIME_DIR / 'include'}", f"-I{RUNTIME_DIR / 'src'}",
         str(source), str(pcc_runtime_archive), "-lm", "-o", str(output)],
        capture_output=True, text=True, timeout=60,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    for backend in range(5):
        ran = subprocess.run(
            [str(output)], capture_output=True, text=True, timeout=10,
            env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
        )
        assert ran.returncode == 0, f"GC{backend}: {ran.stdout}{ran.stderr}"
        assert ran.stdout == "granule=1 managed=1\n"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_snapshot(root: Path) -> dict[str, tuple[int, int, int, int, str]]:
    """Capture bytes and publication identity for the runtime artifact bundle."""

    snapshot: dict[str, tuple[int, int, int, int, str]] = {}
    for path in sorted(root.glob(ARCHIVE_NAME + "*")):
        if not path.is_file():
            continue
        stat = path.stat()
        snapshot[path.name] = (
            stat.st_ino,
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_ctime_ns,
            _sha256(path),
        )
    return snapshot


def _archive_members(archive: Path) -> set[str]:
    result = subprocess.run(
        ["ar", "-t", str(archive)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return set(result.stdout.splitlines())


@pytest.fixture(scope="session")
def granule_runtime_archives() -> Iterator[dict[str, Path]]:
    """Build/reuse immutable default and pthread runtime variants."""

    shared_before = _artifact_snapshot(RUNTIME_DIR)
    saved_threads = os.environ.pop("PCC_WITH_THREADS", None)
    try:
        default_root = cached_pcc_python_runtime()
    finally:
        if saved_threads is not None:
            os.environ["PCC_WITH_THREADS"] = saved_threads
    threaded_root = cached_threaded_pcc_python_runtime()
    assert _artifact_snapshot(RUNTIME_DIR) == shared_before

    archives = {
        "default": default_root / ARCHIVE_NAME,
        "threaded": threaded_root / ARCHIVE_NAME,
    }
    for archive in archives.values():
        assert archive.is_file()
        assert RUNTIME_DIR not in archive.parents
        verify_runtime_archive_manifest(archive, runtime_root=archive.parent)

    default_members = _archive_members(archives["default"])
    threaded_members = _archive_members(archives["threaded"])
    # Every runtime is threaded: both fixtures link the one pthread kernel.
    assert "freestanding_thread_kernel_pthread.o" in default_members
    assert "freestanding_thread_kernel_pthread.o" in threaded_members
    assert "freestanding_thread_kernel.o" not in default_members | threaded_members

    isolated_before = {
        name: _artifact_snapshot(archive.parent)
        for name, archive in archives.items()
    }
    yield archives
    assert _artifact_snapshot(RUNTIME_DIR) == shared_before
    for name, archive in archives.items():
        assert _artifact_snapshot(archive.parent) == isolated_before[name]


def _run_python_probe(
    tmp_path: Path,
    name: str,
    body: str,
    runtime_archive: Path,
) -> list[str]:
    """Compile once and require the requested backend to run for GC0..4."""

    src = tmp_path / f"{name}.py"
    exe = tmp_path / f"{name}.out"
    src.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
        runtime_archive=str(runtime_archive),
    )
    outputs: list[list[str]] = []
    for backend in range(5):
        environment = dict(os.environ, PCC_GC_BACKEND=str(backend))
        done = subprocess.run(
            [str(exe)],
            capture_output=True,
            text=True,
            timeout=180,
            env=environment,
        )
        assert done.returncode == 0, (
            f"backend {backend}:\nstdout:\n{done.stdout}\nstderr:\n{done.stderr}"
        )
        tokens = done.stdout.split()
        assert tokens[:2] == ["backend", str(backend)], (
            f"requested backend {backend}, probe reported {tokens[:2]}"
        )
        outputs.append(tokens[2:])
    for backend in range(1, 5):
        assert outputs[backend] == outputs[0], f"backend {backend} diverged"
    return outputs[0]


def test_granule_object_raw_preflight_and_exact_set_fallback(
    tmp_path: Path,
    granule_runtime_archives: dict[str, Path],
) -> None:
    out = _run_python_probe(
        tmp_path,
        "granule_contract",
        r'''
        from pcc.extern import c_int32, c_int64, c_ptr, c_void, extern, c_obj, c_rawptr
        from pcc.unsafe import (
            free,
            malloc,
            null,
            ptr_add,
            ptr_diff,
            store_i32,
            store_i64,
        )

        alloc_obj = extern("pcc_allocator_alloc_object", (c_int64,), c_rawptr)
        gc_alloc = extern("pcc_gc_alloc", (c_int64, c_int32, c_int32), c_obj)
        gc_free = extern("pcc_gc_free_object_memory", (c_ptr,), c_void)
        gc_backend = extern("pcc_gc_backend", (), c_int64)
        granule_span = extern("pcc_gc_granule_span", (c_ptr,), c_rawptr)
        is_obj = extern("pcc_gc_granule_is_object_start", (c_ptr,), c_int64)
        publish_obj = extern(
            "pcc_gc_granule_object_publish", (c_ptr,), c_int64
        )
        retire_obj = extern(
            "pcc_gc_granule_object_retire", (c_ptr,), c_int64
        )
        kind = extern("pcc_gc_granule_kind", (c_ptr,), c_int64)
        reg_slab = extern(
            "pcc_gc_granule_register_slab", (c_ptr, c_int64, c_int64), c_int64
        )
        pointer_is_managed = extern(
            "pcc_gc_pointer_is_managed", (c_ptr,), c_int64
        )
        pointer_register = extern(
            "pcc_gc_pointer_register", (c_ptr,), c_int64
        )
        pointer_unregister = extern(
            "pcc_gc_pointer_unregister", (c_ptr,), c_int64
        )
        note_object_freeing = extern(
            "pcc_gc_note_object_freeing", (c_ptr,), c_void
        )
        py_object_free = extern("PyObject_Free", (c_ptr,), c_void)
        exact_contains = extern(
            "pcc_gc_managed_pointer_index_contains", (c_ptr,), c_int64
        )

        def aligned_4k(p):
            addr = ptr_diff(p, null())
            return ptr_add(p, ((addr + 4095) // 4096) * 4096 - addr)

        def main() -> None:
            print("backend", gc_backend())

            # Allocation reserves an object-family cell but cannot publish it
            # before the complete object header exists.
            a = alloc_obj(56)
            b = alloc_obj(56)
            print(is_obj(a), is_obj(b), kind(a))
            print(is_obj(ptr_add(a, 8)))

            store_i64(a, 0, 1)
            store_i32(a, 8, 3)
            store_i32(a, 12, 0)
            print(
                exact_contains(a),
                pointer_is_managed(a),
                publish_obj(a),
                is_obj(a),
                pointer_is_managed(a),
                exact_contains(a),
            )
            print(pointer_register(a), exact_contains(a))
            print(retire_obj(a), is_obj(a), pointer_is_managed(a))
            free(b)
            print(is_obj(b))

            # A freed object cell stays in the object-family list.  A raw
            # allocation in the SAME size class cannot steal it, while the
            # next object allocation reuses it deterministically (locked LIFO).
            raw_same = malloc(56)
            raw_stole_object = 0
            if ptr_diff(raw_same, b) == 0:
                raw_stole_object = 1
            print(raw_stole_object, kind(raw_same), is_obj(raw_same))
            reused = alloc_obj(56)
            object_reused = 0
            if ptr_diff(reused, b) == 0:
                object_reused = 1
            print(object_reused, kind(reused), is_obj(reused))

            # The public registration path must publish the completed header
            # without inserting an ordinary object-family cell into the exact
            # per-object index; unregister retires it back to non-live.
            store_i64(reused, 0, 1)
            store_i32(reused, 8, 3)
            store_i32(reused, 12, 0)
            print(
                pointer_register(reused),
                is_obj(reused),
                pointer_is_managed(reused),
                exact_contains(reused),
            )
            print(
                pointer_unregister(reused),
                is_obj(reused),
                pointer_is_managed(reused),
                exact_contains(reused),
            )

            # A conservative freeing note may insert exact provenance while
            # an initialized object-family cell is still RESERVED.  Public
            # unregister must remove that exact key even though the prior
            # marker was not LIVE, before allocator reuse can expose the
            # same address to a different object.
            conservative = alloc_obj(56)
            store_i64(conservative, 0, 1)
            store_i32(conservative, 8, 3)
            store_i32(conservative, 12, 0)
            note_object_freeing(conservative)
            print(is_obj(conservative), exact_contains(conservative))
            print(
                pointer_unregister(conservative),
                is_obj(conservative),
                exact_contains(conservative),
            )
            free(conservative)
            conservative_reuse = alloc_obj(56)
            reused_conservative_address = 0
            if ptr_diff(conservative_reuse, conservative) == 0:
                reused_conservative_address = 1
            print(
                reused_conservative_address,
                is_obj(conservative_reuse),
                exact_contains(conservative_reuse),
            )
            free(conservative_reuse)

            # The managed branch of PyObject_Free must also quarantine an
            # invalid marker.  Conservative exact provenance keeps the old
            # address managed, but must not authorize allocator reuse or be
            # removed when marker retirement fails closed.
            managed_corrupt = alloc_obj(72)
            store_i64(managed_corrupt, 0, 1)
            store_i32(managed_corrupt, 8, 3)
            store_i32(managed_corrupt, 12, 0)
            note_object_freeing(managed_corrupt)
            print(
                is_obj(managed_corrupt),
                exact_contains(managed_corrupt),
                pointer_is_managed(managed_corrupt),
            )
            store_i64(managed_corrupt, -48, 123)
            py_object_free(managed_corrupt)
            after_managed_corrupt = alloc_obj(72)
            managed_corrupt_was_quarantined = 0
            if ptr_diff(after_managed_corrupt, managed_corrupt) != 0:
                managed_corrupt_was_quarantined = 1
            print(
                managed_corrupt_was_quarantined,
                is_obj(after_managed_corrupt),
                exact_contains(after_managed_corrupt),
                exact_contains(managed_corrupt),
                pointer_is_managed(managed_corrupt),
            )
            free(after_managed_corrupt)

            # The unmanaged branch must likewise fail closed instead of
            # recycling a corrupted RESERVED object-family cell.
            corrupt = alloc_obj(56)
            store_i64(corrupt, -48, 123)
            print(
                pointer_unregister(corrupt),
                is_obj(corrupt),
                exact_contains(corrupt),
            )
            py_object_free(corrupt)
            after_corrupt = alloc_obj(56)
            corrupt_was_quarantined = 0
            if ptr_diff(after_corrupt, corrupt) != 0:
                corrupt_was_quarantined = 1
            print(
                corrupt_was_quarantined,
                is_obj(after_corrupt),
                exact_contains(after_corrupt),
            )
            free(after_corrupt)

            raw = malloc(100)
            print(kind(raw), is_obj(raw))

            # Validation and duplicate detection happen before publishing any
            # of a slab's sixteen keys.
            storage = malloc(200000)
            # Keep fifteen complete pages before base so a late-overlap
            # registration has fifteen absent keys and collides only on its
            # sixteenth (last) candidate key.
            base = ptr_add(aligned_4k(storage), 15 * 4096)
            print(reg_slab(null(), 2, 64))
            print(reg_slab(ptr_add(base, 8), 2, 64))
            print(reg_slab(base, 0, 64))
            print(reg_slab(base, 1, 48))
            misses = 0
            page = 0
            while page < 16:
                probe = ptr_add(base, page * 4096)
                if kind(probe) == 0 and ptr_diff(granule_span(probe), null()) == 0:
                    misses = misses + 1
                page = page + 1
            print(misses)

            print(reg_slab(base, 2, 64))
            first_span = granule_span(base)
            covered = 0
            same_span = 0
            page = 0
            while page < 16:
                probe = ptr_add(base, page * 4096 + 4095)
                if kind(probe) == 2:
                    covered = covered + 1
                if ptr_diff(granule_span(probe), first_span) == 0:
                    same_span = same_span + 1
                page = page + 1
            print(covered, same_span, kind(ptr_add(base, 65536)))

            # A visible key may never be rebound to another descriptor.
            print(reg_slab(base, 1, 64))
            print(kind(base), ptr_diff(granule_span(base), first_span))

            # Preflight must inspect all sixteen keys before binding any.  The
            # first fifteen keys here are absent; only candidate key 16 is the
            # already-published first key of the prior slab.
            late_overlap = ptr_add(base, -15 * 4096)
            print(reg_slab(late_overlap, 2, 64))
            late_unpublished = 0
            page = 0
            while page < 15:
                probe = ptr_add(late_overlap, page * 4096)
                if kind(probe) == 0 and ptr_diff(granule_span(probe), null()) == 0:
                    late_unpublished = late_unpublished + 1
                page = page + 1
            print(late_unpublished, kind(ptr_add(late_overlap, 15 * 4096)))

            # A directly registered object larger than the 16 KiB slab
            # ceiling is granule-unknown and therefore stays on the exact
            # provenance path under every requested GC backend.  This avoids
            # conflating exact provenance with the later tracing-backend
            # transfer into the object index.
            large = alloc_obj(20000)
            store_i64(large, 0, 1)
            store_i32(large, 8, 3)
            store_i32(large, 12, 0)
            print(kind(large), pointer_register(large))
            print(
                pointer_is_managed(large),
                exact_contains(large),
                is_obj(large),
            )
            print(pointer_unregister(large), pointer_is_managed(large))
            free(large)

            # S1 authority remains the exact foreign-pointer set.  A raw,
            # granule-unknown pointer becomes managed only while registered.
            foreign = malloc(200000)
            print(kind(foreign), is_obj(foreign), pointer_is_managed(foreign))
            print(
                pointer_register(foreign),
                pointer_is_managed(foreign),
                exact_contains(foreign),
            )
            print(
                pointer_unregister(foreign),
                pointer_is_managed(foreign),
                exact_contains(foreign),
            )
            free(foreign)
            free(raw_same)
            free(reused)
            free(raw)
            free(a)

        main()
        ''',
        granule_runtime_archives["default"],
    )
    assert out == [
        "-1", "-1", "1", "-1",
        "0", "0", "1", "1", "1", "0",
        "0", "0", "1", "-1", "0", "-1",
        "0", "2", "-1", "1", "1", "-1",
        "0", "1", "1", "0", "0", "-1", "0", "0",
        "-1", "1", "0", "-1", "0", "1", "-1", "0",
        "-1", "1", "1", "1", "-1", "0", "1", "1",
        "-1", "-1", "0", "1", "-1", "0",
        "2", "-1",
        "-1", "-1", "-1", "-1", "16",
        "1", "16", "16", "0",
        "-1", "2", "0",
        "-1", "15", "2",
        "0", "1", "1", "1", "-1", "1", "0",
        "0", "-1", "0", "1", "1", "1", "1", "0", "0",
    ]


def test_granule_grow_preserves_all_sixteen_keys_per_slab(
    tmp_path: Path,
    granule_runtime_archives: dict[str, Path],
) -> None:
    # 600 registrations publish 9600 keys, forcing repeated table growth.
    out = _run_python_probe(
        tmp_path,
        "granule_grow",
        r'''
        from pcc.extern import extern, c_int64, c_ptr, c_rawptr
        from pcc.unsafe import malloc, null, ptr_add, ptr_diff

        gc_backend = extern("pcc_gc_backend", (), c_int64)
        granule_span = extern("pcc_gc_granule_span", (c_ptr,), c_rawptr)
        kind = extern("pcc_gc_granule_kind", (c_ptr,), c_int64)
        reg_slab = extern(
            "pcc_gc_granule_register_slab", (c_ptr, c_int64, c_int64), c_int64
        )

        def aligned_4k(p):
            addr = ptr_diff(p, null())
            return ptr_add(p, ((addr + 4095) // 4096) * 4096 - addr)

        def main() -> None:
            print("backend", gc_backend())
            ptrs = []
            failures = 0
            i = 0
            while i < 600:
                base = aligned_4k(malloc(81920))
                ptrs.append(base)
                if reg_slab(base, 2, 64) != 1:
                    failures = failures + 1
                i = i + 1

            covered = 0
            same_span = 0
            i = 0
            while i < 600:
                first_span = granule_span(ptrs[i])
                page = 0
                while page < 16:
                    probe = ptr_add(ptrs[i], page * 4096 + 2048)
                    if kind(probe) == 2:
                        covered = covered + 1
                    if ptr_diff(granule_span(probe), first_span) == 0:
                        same_span = same_span + 1
                    page = page + 1
                i = i + 1
            print(failures, covered, same_span)

        main()
        ''',
        granule_runtime_archives["default"],
    )
    assert out == ["0", "9600", "9600"]


def test_gc_alloc_fallback_tails_preserve_publication_order_by_runtime_mode() -> None:
    """Lock semantic order without conflating C calloc and pcc-Py slabs."""

    c_tokens = [
        "h = (PyObjectHeader *)pcc_gc_try_minor_alloc(size)",
        "pcc_gc_backend4_try_zpage_alloc(size, flags)",
        "if (h == NULL)",
        "calloc(1, (size_t)size)",
        "h->refcount = 1",
        "h->type_tag = type_tag",
        "h->flags = stored_flags",
        "pcc_debug_note_alloc_size(h, size)",
        "pcc_gc_pointer_register((PyObject *)h)",
        "pcc_gc_note_object_allocated_sized((PyObject *)h, size)",
    ]

    py_source = (RUNTIME_DIR / "py" / "py_obj.py").read_text(encoding="utf-8")
    py_alloc = py_source.split('@c_abi_export("pcc_gc_alloc")', 1)[1].split(
        '@c_abi_export("pcc_gc_retain")', 1
    )[0]
    py_tokens = [
        "obj = pcc_gc_try_minor_alloc(size)",
        "pcc_gc_backend4_try_zpage_alloc(size, flags)",
        "if ptr_is_null(obj) != 0:",
        "obj = pcc_allocator_alloc_object(size)",
        "memset(obj, 0, size)",
        "store_i64(obj, PYOBJECTHEADER_REFCOUNT_OFFSET, 1)",
        "store_i32(obj, PYOBJECTHEADER_TYPE_TAG_OFFSET, type_tag)",
        "store_i32(obj, PYOBJECTHEADER_FLAGS_OFFSET, stored_flags)",
        "pcc_debug_note_alloc_size(obj, size)",
        "pcc_gc_pointer_register(obj)",
        "pcc_gc_note_object_allocated_sized(obj, size)",
    ]
    py_positions = [py_alloc.index(token) for token in py_tokens]
    assert py_positions == sorted(py_positions)
    py_fallback_alloc = py_alloc.index("obj = pcc_allocator_alloc_object(size)")
    py_header_publish = py_alloc.index(
        "store_i64(obj, PYOBJECTHEADER_REFCOUNT_OFFSET, 1)"
    )
    for flag_transition in (
        "(stored_flags & ~65536) | 262144",
        "(stored_flags & ~4096) | 262144",
    ):
        assert py_fallback_alloc < py_alloc.index(flag_transition) < py_header_publish


def _build_threaded_object_publication_harness(
    tmp_path: Path,
    runtime_archive: Path,
) -> Path:
    """Build a pthread proof that LIVE acquire exposes the complete header."""

    src = tmp_path / "granule_object_publication_pthreads.c"
    exe = tmp_path / "granule_object_publication_pthreads.out"
    src.write_text(
        textwrap.dedent(
            r'''
            #include "py_internal.h"
            #include <sched.h>
            #include <stdint.h>
            #include <stdio.h>
            #include <stdlib.h>

            enum { READER_COUNT = 3 };

            typedef struct {
                PyObjectHeader header;
                int64_t published_epoch;
                int64_t published_inverse;
            } PublicationObject;

            extern void *pcc_allocator_alloc_object(int64_t size);
            extern int64_t pcc_gc_managed_pointer_index_contains(PyObject *obj);

            static PublicationObject *object = NULL;
            static int64_t ready_count = 0;
            static int64_t start_flag = 0;
            static int64_t abort_flag = 0;
            static int64_t readers_done = 0;
            static int64_t positive_reads = 0;
            static int64_t errors = 0;

            static void add_error(void) {
                __atomic_add_fetch(&errors, 1, __ATOMIC_ACQ_REL);
            }

            static void wait_for_start(void) {
                __atomic_add_fetch(&ready_count, 1, __ATOMIC_ACQ_REL);
                while (__atomic_load_n(&start_flag, __ATOMIC_ACQUIRE) == 0) {
                    sched_yield();
                }
            }

            static void *reader_main(void *unused) {
                (void)unused;
                wait_for_start();
                for (;;) {
                    if (pcc_gc_pointer_is_managed((PyObject *)object) == 1) {
                        break;
                    }
                    if (__atomic_load_n(&abort_flag, __ATOMIC_ACQUIRE) != 0) {
                        return NULL;
                    }
                    sched_yield();
                }

                /* There is deliberately no phase flag or post-header writer
                 * handshake here.  The positive managed lookup acquires the
                 * LIVE marker published after these header stores. */
                int64_t refcount = __atomic_load_n(
                    &object->header.refcount, __ATOMIC_RELAXED
                );
                int32_t type_tag = __atomic_load_n(
                    &object->header.type_tag, __ATOMIC_RELAXED
                );
                int32_t flags = __atomic_load_n(
                    &object->header.flags, __ATOMIC_RELAXED
                );
                int64_t epoch = __atomic_load_n(
                    &object->published_epoch, __ATOMIC_RELAXED
                );
                int64_t inverse = __atomic_load_n(
                    &object->published_inverse, __ATOMIC_RELAXED
                );
                if (
                    refcount != 1
                    || type_tag != PY_TYPE_INT
                    || flags != 0
                    || epoch != 0x12345678
                    || inverse != ~((int64_t)0x12345678)
                    || pcc_gc_granule_is_object_start(object) != 1
                    || pcc_gc_managed_pointer_index_contains(
                        (PyObject *)object
                    ) != 0
                ) {
                    add_error();
                }
                __atomic_add_fetch(&positive_reads, 1, __ATOMIC_ACQ_REL);
                __atomic_add_fetch(&readers_done, 1, __ATOMIC_RELEASE);
                return NULL;
            }

            static void *writer_main(void *unused) {
                (void)unused;
                wait_for_start();
                __atomic_store_n(
                    &object->header.refcount, 1, __ATOMIC_RELAXED
                );
                __atomic_store_n(
                    &object->header.type_tag, PY_TYPE_INT, __ATOMIC_RELAXED
                );
                __atomic_store_n(&object->header.flags, 0, __ATOMIC_RELAXED);
                __atomic_store_n(
                    &object->published_epoch, 0x12345678, __ATOMIC_RELAXED
                );
                __atomic_store_n(
                    &object->published_inverse,
                    ~((int64_t)0x12345678),
                    __ATOMIC_RELAXED
                );
                if (pcc_gc_granule_object_publish(object) != 1) {
                    add_error();
                    __atomic_store_n(&abort_flag, 1, __ATOMIC_RELEASE);
                    return NULL;
                }
                while (
                    __atomic_load_n(&readers_done, __ATOMIC_ACQUIRE)
                    != READER_COUNT
                ) {
                    sched_yield();
                }
                if (
                    pcc_gc_granule_object_retire(object) != 1
                    || pcc_gc_granule_is_object_start(object) != -1
                    || pcc_gc_managed_pointer_index_contains(
                        (PyObject *)object
                    ) != 0
                ) {
                    add_error();
                }
                free(object);
                return NULL;
            }

            int main(void) {
                if (pcc_threads_enabled() != 1) return 2;
                object = (PublicationObject *)pcc_allocator_alloc_object(
                    sizeof(PublicationObject)
                );
                if (object == NULL) return 3;
                if (
                    pcc_gc_granule_is_object_start(object) != -1
                    || pcc_gc_pointer_is_managed((PyObject *)object) != 0
                    || pcc_gc_managed_pointer_index_contains(
                        (PyObject *)object
                    ) != 0
                ) {
                    return 4;
                }

                PccThreadHandle *readers[READER_COUNT] = {0};
                PccThreadHandle *writer = NULL;
                for (int index = 0; index < READER_COUNT; index++) {
                    if (pcc_thread_start(&readers[index], reader_main, NULL) != 0) {
                        return 5;
                    }
                }
                if (pcc_thread_start(&writer, writer_main, NULL) != 0) return 6;
                while (
                    __atomic_load_n(&ready_count, __ATOMIC_ACQUIRE)
                    != READER_COUNT + 1
                ) {
                    sched_yield();
                }
                /* This is published before the writer performs any header
                 * store, so it cannot make the completed header visible to
                 * readers.  LIVE release/acquire is the only such edge. */
                __atomic_store_n(&start_flag, 1, __ATOMIC_RELEASE);

                void *thread_result = NULL;
                if (pcc_thread_join(writer, &thread_result) != 0) return 7;
                if (thread_result != NULL) return 8;
                for (int index = 0; index < READER_COUNT; index++) {
                    thread_result = NULL;
                    if (pcc_thread_join(readers[index], &thread_result) != 0) {
                        return 9;
                    }
                    if (thread_result != NULL) return 10;
                }
                printf(
                    "backend %lld errors %lld positive_reads %lld\n",
                    (long long)pcc_gc_backend(),
                    (long long)__atomic_load_n(&errors, __ATOMIC_ACQUIRE),
                    (long long)__atomic_load_n(
                        &positive_reads, __ATOMIC_ACQUIRE
                    )
                );
                return errors == 0 && positive_reads == READER_COUNT ? 0 : 11;
            }
            '''
        ).lstrip(),
        encoding="utf-8",
    )
    cc = os.environ.get("CC", "cc")
    build = subprocess.run(
        [
            cc,
            "-DPCC_WITH_THREADS=1",
            "-std=c11",
            "-pthread",
            f"-I{runtime_archive.parent / 'include'}",
            f"-I{runtime_archive.parent / 'src'}",
            str(src),
            str(runtime_archive),
            "-lm",
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    return exe


def test_granule_live_publication_exposes_complete_header_to_real_pthreads(
    tmp_path: Path,
    granule_runtime_archives: dict[str, Path],
) -> None:
    exe = _build_threaded_object_publication_harness(
        tmp_path, granule_runtime_archives["threaded"]
    )
    for backend in range(5):
        done = subprocess.run(
            [str(exe)],
            capture_output=True,
            text=True,
            timeout=180,
            env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
        )
        assert done.returncode == 0, (
            f"backend {backend}:\nstdout:\n{done.stdout}\nstderr:\n{done.stderr}"
        )
        assert done.stdout == (
            f"backend {backend} errors 0 positive_reads 3\n"
        )


def _build_threaded_object_lifecycle_harness(
    tmp_path: Path,
    runtime_archive: Path,
) -> Path:
    """Build a pthread marker/provenance reserve/publish/retire/reuse probe."""

    src = tmp_path / "granule_object_lifecycle_pthreads.c"
    exe = tmp_path / "granule_object_lifecycle_pthreads.out"
    src.write_text(
        textwrap.dedent(
            r'''
            #include "py_internal.h"
            #include <sched.h>
            #include <stdint.h>
            #include <stdio.h>
            #include <stdlib.h>

            enum {
                READER_COUNT = 3,
                LIFECYCLE_COUNT = 256,
                PHASE_RESERVED = 1,
                PHASE_LIVE = 2,
                PHASE_RETIRED = 3,
                PHASE_FREED = 4
            };

            typedef struct { PyObjectHeader header; } LifecycleObject;

            extern void *pcc_allocator_alloc_object(int64_t size);
            extern int64_t pcc_gc_managed_pointer_index_contains(PyObject *obj);

            static _Alignas(4096) unsigned char permanent_negative[4096];
            static LifecycleObject *current_object = NULL;
            static int64_t ready_count = 0;
            static int64_t start_flag = 0;
            static int64_t done_flag = 0;
            /* A stable phase bounds one complete reader call window.  This
             * deliberately does not claim overlap with the exact internal
             * magic publication instruction. */
            static int64_t lifecycle_phase = 0;
            static int64_t reserved_seen = 0;
            static int64_t live_seen = 0;
            static int64_t retired_seen = 0;
            static int64_t freed_seen = 0;
            static int64_t live_windows = 0;
            static int64_t negative_windows = 0;
            static int64_t errors = 0;
            static int64_t reuse_count = 0;

            static void add_error(void) {
                __atomic_add_fetch(&errors, 1, __ATOMIC_ACQ_REL);
            }

            static void update_max(int64_t *slot, int64_t value) {
                int64_t observed = __atomic_load_n(slot, __ATOMIC_ACQUIRE);
                while (observed < value) {
                    if (__atomic_compare_exchange_n(
                            slot, &observed, value, 0,
                            __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE
                        )) {
                        return;
                    }
                }
            }

            static void wait_until_seen(int64_t *slot, int64_t epoch) {
                while (__atomic_load_n(slot, __ATOMIC_ACQUIRE) < epoch) {
                    sched_yield();
                }
            }

            static void wait_for_start(void) {
                __atomic_add_fetch(&ready_count, 1, __ATOMIC_ACQ_REL);
                while (__atomic_load_n(&start_flag, __ATOMIC_ACQUIRE) == 0) {
                    sched_yield();
                }
            }

            static void *reader_main(void *unused) {
                (void)unused;
                wait_for_start();
                while (__atomic_load_n(&done_flag, __ATOMIC_ACQUIRE) == 0) {
                    int64_t before = __atomic_load_n(
                        &lifecycle_phase, __ATOMIC_SEQ_CST
                    );
                    LifecycleObject *obj = __atomic_load_n(
                        &current_object, __ATOMIC_ACQUIRE
                    );
                    int64_t state = before & 7;
                    if (obj == NULL || state < PHASE_RESERVED || state > PHASE_FREED) {
                        sched_yield();
                        continue;
                    }

                    int64_t managed = pcc_gc_pointer_is_managed((PyObject *)obj);
                    int64_t absent = pcc_gc_pointer_is_managed(
                        (PyObject *)permanent_negative
                    );
                    int64_t exact = pcc_gc_managed_pointer_index_contains(
                        (PyObject *)obj
                    );
                    int64_t marker = pcc_gc_granule_is_object_start(obj);
                    int64_t after = __atomic_load_n(
                        &lifecycle_phase, __ATOMIC_SEQ_CST
                    );
                    if (before != after) continue;

                    int64_t epoch = before / 8 + 1;
                    if (absent != 0 || exact != 0) add_error();
                    if (state == PHASE_LIVE) {
                        if (managed != 1 || marker != 1) {
                            add_error();
                        }
                        __atomic_add_fetch(
                            &live_windows, 1, __ATOMIC_ACQ_REL
                        );
                        update_max(&live_seen, epoch);
                    } else {
                        if (managed != 0 || marker != -1) add_error();
                        __atomic_add_fetch(
                            &negative_windows, 1, __ATOMIC_ACQ_REL
                        );
                        if (state == PHASE_RESERVED) {
                            update_max(&reserved_seen, epoch);
                        } else if (state == PHASE_RETIRED) {
                            update_max(&retired_seen, epoch);
                        } else {
                            update_max(&freed_seen, epoch);
                        }
                    }
                }
                return NULL;
            }

            static void *writer_main(void *unused) {
                (void)unused;
                LifecycleObject *prior = NULL;
                wait_for_start();
                for (int64_t index = 0; index < LIFECYCLE_COUNT; index++) {
                    int64_t epoch = index + 1;
                    /* Hide the reserve operation itself from readers.  The
                     * next stable RESERVED phase starts only after the
                     * allocator has returned the RESERVED cell. */
                    __atomic_store_n(
                        &lifecycle_phase, index * 8, __ATOMIC_SEQ_CST
                    );
                    LifecycleObject *obj = (LifecycleObject *)
                        pcc_allocator_alloc_object(sizeof(LifecycleObject));
                    if (obj == NULL) {
                        add_error();
                        break;
                    }
                    if (prior != NULL) {
                        if (obj != prior) {
                            add_error();
                        } else {
                            reuse_count++;
                        }
                    }
                    __atomic_store_n(&current_object, obj, __ATOMIC_RELEASE);
                    __atomic_store_n(
                        &lifecycle_phase,
                        index * 8 + PHASE_RESERVED,
                        __ATOMIC_SEQ_CST
                    );
                    wait_until_seen(&reserved_seen, epoch);

                    __atomic_store_n(&obj->header.refcount, 1, __ATOMIC_RELAXED);
                    __atomic_store_n(
                        &obj->header.type_tag, PY_TYPE_INT, __ATOMIC_RELAXED
                    );
                    __atomic_store_n(&obj->header.flags, 0, __ATOMIC_RELAXED);
                    __atomic_store_n(
                        &lifecycle_phase, index * 8, __ATOMIC_SEQ_CST
                    );
                    if (pcc_gc_granule_object_publish(obj) != 1) add_error();
                    __atomic_store_n(
                        &lifecycle_phase,
                        index * 8 + PHASE_LIVE,
                        __ATOMIC_SEQ_CST
                    );
                    wait_until_seen(&live_seen, epoch);

                    __atomic_store_n(
                        &lifecycle_phase, index * 8, __ATOMIC_SEQ_CST
                    );
                    if (pcc_gc_granule_object_retire(obj) != 1) add_error();
                    __atomic_store_n(
                        &lifecycle_phase,
                        index * 8 + PHASE_RETIRED,
                        __ATOMIC_SEQ_CST
                    );
                    wait_until_seen(&retired_seen, epoch);

                    __atomic_store_n(
                        &lifecycle_phase, index * 8, __ATOMIC_SEQ_CST
                    );
                    free(obj);
                    __atomic_store_n(
                        &lifecycle_phase,
                        index * 8 + PHASE_FREED,
                        __ATOMIC_SEQ_CST
                    );
                    wait_until_seen(&freed_seen, epoch);
                    prior = obj;
                }
                __atomic_store_n(&done_flag, 1, __ATOMIC_RELEASE);
                return NULL;
            }

            int main(void) {
                int64_t backend = pcc_gc_backend();
                if (pcc_threads_enabled() != 1) return 2;
                if (
                    pcc_gc_pointer_is_managed((PyObject *)permanent_negative) != 0
                    || pcc_gc_granule_is_object_start(permanent_negative) != -1
                ) {
                    return 3;
                }

                PccThreadHandle *readers[READER_COUNT] = {0};
                PccThreadHandle *writer = NULL;
                for (int index = 0; index < READER_COUNT; index++) {
                    if (pcc_thread_start(&readers[index], reader_main, NULL) != 0) {
                        return 4;
                    }
                }
                if (pcc_thread_start(&writer, writer_main, NULL) != 0) return 5;
                while (
                    __atomic_load_n(&ready_count, __ATOMIC_ACQUIRE)
                    != READER_COUNT + 1
                ) {
                    sched_yield();
                }
                __atomic_store_n(&start_flag, 1, __ATOMIC_RELEASE);

                void *thread_result = NULL;
                if (pcc_thread_join(writer, &thread_result) != 0) return 6;
                if (thread_result != NULL) return 7;
                for (int index = 0; index < READER_COUNT; index++) {
                    thread_result = NULL;
                    if (pcc_thread_join(readers[index], &thread_result) != 0) {
                        return 8;
                    }
                    if (thread_result != NULL) return 9;
                }

                printf("backend %lld\n", (long long)backend);
                printf(
                    "errors %lld reuse %lld live_windows %lld "
                    "negative_windows %lld\n",
                    (long long)__atomic_load_n(&errors, __ATOMIC_ACQUIRE),
                    (long long)reuse_count,
                    (long long)__atomic_load_n(&live_windows, __ATOMIC_ACQUIRE),
                    (long long)__atomic_load_n(
                        &negative_windows, __ATOMIC_ACQUIRE
                    )
                );
                return errors == 0
                        && reuse_count == LIFECYCLE_COUNT - 1
                        && live_windows >= LIFECYCLE_COUNT
                        && negative_windows >= LIFECYCLE_COUNT * 3
                    ? 0
                    : 10;
            }
            '''
        ).lstrip(),
        encoding="utf-8",
    )
    cc = os.environ.get("CC", "cc")
    build = subprocess.run(
        [
            cc,
            "-DPCC_WITH_THREADS=1",
            "-std=c11",
            "-pthread",
            f"-I{runtime_archive.parent / 'include'}",
            f"-I{runtime_archive.parent / 'src'}",
            str(src),
            str(runtime_archive),
            "-lm",
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    return exe


def test_granule_object_lifecycle_races_real_pthread_readers(
    tmp_path: Path,
    granule_runtime_archives: dict[str, Path],
) -> None:
    exe = _build_threaded_object_lifecycle_harness(
        tmp_path, granule_runtime_archives["threaded"]
    )
    for backend in range(5):
        environment = dict(os.environ, PCC_GC_BACKEND=str(backend))
        done = subprocess.run(
            [str(exe)],
            capture_output=True,
            text=True,
            timeout=180,
            env=environment,
        )
        assert done.returncode == 0, (
            f"backend {backend}:\nstdout:\n{done.stdout}\nstderr:\n{done.stderr}"
        )
        tokens = done.stdout.split()
        assert tokens[:2] == ["backend", str(backend)]
        values = dict(zip(tokens[2::2], tokens[3::2], strict=True))
        assert values["errors"] == "0"
        assert values["reuse"] == "255"
        assert int(values["live_windows"]) >= 256
        assert int(values["negative_windows"]) >= 768


def _build_gc3_minor_forwarded_source_retirement_harness(
    tmp_path: Path,
    runtime_archive: Path,
) -> Path:
    """Build a GC3 minor-arena STR oldification and retirement probe."""

    src = tmp_path / "granule_gc3_minor_forwarded_source_retirement.c"
    exe = tmp_path / "granule_gc3_minor_forwarded_source_retirement.out"
    src.write_text(
        textwrap.dedent(
            r'''
            #include "py_runtime.h"
            #include "py_internal.h"
            #include <stdint.h>
            #include <stdio.h>

            extern int64_t pcc_gc_managed_pointer_index_contains(PyObject *obj);
            extern int64_t pcc_gc_object_known_size(PyObject *obj);

            int main(void) {
                if (
                    pcc_gc_set_backend(PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR)
                    != 0
                ) {
                    return 2;
                }
                pcc_gc_telemetry_reset();

                /* STR is one of the scalar tags that production GC3 can
                 * oldify.  It comes from the real minor arena, whose backing
                 * block is a raw-family allocator slab: kind=2 and no LIVE
                 * object-cell marker.  The tracing object index, not exact
                 * provenance, makes this address managed. */
                PyObject *source = py_str_new("x", 1);
                if (source == NULL) return 3;
                int64_t origin_kind = pcc_gc_granule_kind(source);
                int64_t origin_live = pcc_gc_granule_is_object_start(source);
                int64_t origin_managed = pcc_gc_pointer_is_managed(source);
                int64_t origin_exact = pcc_gc_managed_pointer_index_contains(
                    source
                );
                int64_t origin_size = pcc_gc_object_known_size(source);
                int32_t origin_flags = ((PyObjectHeader *)source)->flags;
                PyStrObject *source_str = (PyStrObject *)source;
                int64_t source_payload = (
                    source_str->h.type_tag == PY_TYPE_STR
                    && source_str->byte_len == 1
                    && source_str->data[0] == 'x'
                    && source_str->data[1] == '\0'
                ) ? 1 : 0;
                if (
                    origin_kind != 2
                    || origin_live != -1
                    || origin_managed != 1
                    || origin_exact != 0
                    || origin_size <= 0
                    || (origin_flags & PY_FLAG_GC_MINOR_ARENA) == 0
                    || (origin_flags & PY_FLAG_GC_MALLOC_ALLOC) != 0
                    || source_payload != 1
                ) {
                    return 4;
                }

                (void)pcc_gc_step(1024);
                PyObject *target = pcc_gc_note_relocation_read(source);
                if (target == NULL || target == source) return 5;
                int64_t active_entries = pcc_gc_backend4_forwarding_entries();
                int64_t active_managed = pcc_gc_pointer_is_managed(source);
                int64_t active_exact = pcc_gc_managed_pointer_index_contains(
                    source
                );
                PyStrObject *target_str = (PyStrObject *)target;
                int64_t active_payload = (
                    target_str->h.type_tag == PY_TYPE_STR
                    && target_str->byte_len == 1
                    && target_str->data[0] == 'x'
                    && target_str->data[1] == '\0'
                ) ? 1 : 0;
                if (
                    active_entries != 1
                    || active_managed != 1
                    || active_exact != 0
                    || active_payload != 1
                ) {
                    return 6;
                }
                int64_t blocked_switch = pcc_gc_set_backend(
                    PCC_GC_KIND_REFCOUNT_CYCLE
                );
                PyObject *after_gc0_target = pcc_gc_note_relocation_read(
                    source
                );
                int64_t blocked_switch_gc4 = pcc_gc_set_backend(
                    PCC_GC_KIND_COLORED_RELOCATING
                );
                int64_t blocked_backend = pcc_gc_backend();
                int64_t blocked_entries = pcc_gc_backend4_forwarding_entries();
                int64_t blocked_managed = pcc_gc_pointer_is_managed(source);
                PyObject *blocked_target = pcc_gc_note_relocation_read(source);
                if (
                    blocked_switch != -1
                    || blocked_switch_gc4 != -1
                    || blocked_backend != PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
                    || blocked_entries != 1
                    || blocked_managed != 1
                    || after_gc0_target != target
                    || blocked_target != target
                ) {
                    return 7;
                }

                py_incref(target);
                pcc_gc_release(source);
                int64_t retired_entries = pcc_gc_backend4_forwarding_entries();
                int64_t stale_live = pcc_gc_granule_is_object_start(source);
                int64_t stale_managed = pcc_gc_pointer_is_managed(source);
                int64_t stale_exact = pcc_gc_managed_pointer_index_contains(
                    source
                );
                int64_t stale_size = pcc_gc_object_known_size(source);
                int64_t target_refcount = ((PyObjectHeader *)target)->refcount;
                int32_t target_type = ((PyObjectHeader *)target)->type_tag;
                int64_t retired_payload = (
                    target_str->byte_len == 1
                    && target_str->data[0] == 'x'
                    && target_str->data[1] == '\0'
                ) ? 1 : 0;
                int64_t completed_switch = pcc_gc_set_backend(
                    PCC_GC_KIND_REFCOUNT_CYCLE
                );
                int64_t completed_backend = pcc_gc_backend();
                printf(
                    "origin %lld %lld %lld %lld known %d minor %d malloc %d "
                    "payload %lld moved %d active %lld %lld %lld %lld "
                    "blocked %lld %lld %lld %lld %lld "
                    "retired %lld %lld %lld %lld %lld target %lld %d %lld "
                    "switched %lld %lld\n",
                    (long long)origin_kind,
                    (long long)origin_live,
                    (long long)origin_managed,
                    (long long)origin_exact,
                    origin_size > 0,
                    (origin_flags & PY_FLAG_GC_MINOR_ARENA) != 0,
                    (origin_flags & PY_FLAG_GC_MALLOC_ALLOC) != 0,
                    (long long)source_payload,
                    target != source,
                    (long long)active_entries,
                    (long long)active_managed,
                    (long long)active_exact,
                    (long long)active_payload,
                    (long long)blocked_switch,
                    (long long)blocked_switch_gc4,
                    (long long)blocked_backend,
                    (long long)blocked_entries,
                    (long long)blocked_managed,
                    (long long)retired_entries,
                    (long long)stale_live,
                    (long long)stale_managed,
                    (long long)stale_exact,
                    (long long)stale_size,
                    (long long)target_refcount,
                    target_type,
                    (long long)retired_payload,
                    (long long)completed_switch,
                    (long long)completed_backend
                );

                pcc_gc_release(target);
                return retired_entries == 0
                        && stale_live == -1
                        && stale_managed == 0
                        && stale_exact == 0
                        && stale_size == 0
                        && target_refcount == 1
                        && target_type == PY_TYPE_STR
                        && retired_payload == 1
                        && completed_switch == 0
                        && completed_backend == PCC_GC_KIND_REFCOUNT_CYCLE
                    ? 0
                    : 8;
            }
            '''
        ).lstrip(),
        encoding="utf-8",
    )
    cc = os.environ.get("CC", "cc")
    build = subprocess.run(
        [
            cc,
            "-std=c11",
            f"-I{runtime_archive.parent / 'include'}",
            f"-I{runtime_archive.parent / 'src'}",
            str(src),
            str(runtime_archive),
            "-pthread",
            "-lm",
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    return exe


def test_granule_gc3_minor_forwarded_source_is_stale_after_retirement(
    tmp_path: Path,
    granule_runtime_archives: dict[str, Path],
) -> None:
    exe = _build_gc3_minor_forwarded_source_retirement_harness(
        tmp_path, granule_runtime_archives["default"]
    )
    done = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=180,
        env=dict(
            os.environ,
            PCC_GC_BACKEND="3",
            PCC_GC_MINOR_HEAP_SIZE="1024",
            PCC_GC_MINOR_ALLOC_MAX="128",
        ),
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout == (
        "origin 2 -1 1 0 known 1 minor 1 malloc 0 payload 1 "
        "moved 1 active 1 1 0 1 blocked -1 -1 3 1 1 "
        "retired 0 -1 0 0 0 target 1 4 1 switched 0 0\n"
    )


def _build_gc4_downstream_fallback_tail_harness(
    tmp_path: Path,
    runtime_archive: Path,
) -> Path:
    """Build a production-equivalent tail probe after a zpage miss."""

    src = tmp_path / "granule_gc4_downstream_fallback_tail.c"
    exe = tmp_path / "granule_gc4_downstream_fallback_tail.out"
    src.write_text(
        textwrap.dedent(
            r'''
            #include "py_internal.h"
            #include <stdint.h>
            #include <stdio.h>
            #include <stdlib.h>
            #include <string.h>

            extern void *pcc_allocator_alloc_object(int64_t size);
            extern void pcc_debug_note_alloc_size(void *ptr, int64_t size);
            extern int64_t pcc_gc_managed_pointer_index_contains(PyObject *obj);
            extern int64_t pcc_gc_object_known_size(PyObject *obj);

            int main(void) {
                if (
                    pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0
                ) {
                    return 2;
                }

                /* Scheme B starts exactly after a hypothetical zpage NULL.
                 * The remainder is the production pcc-Python fallback tail:
                 * allocation accounting, object-family allocation, zeroing,
                 * complete header, debug size, publication, then tracking.
                 * This does not claim a real OOM/fault-injected zpage miss. */
                int64_t object_size = (int64_t)sizeof(PyListObject);
                pcc_gc_note_alloc(object_size);
                PyListObject *source = (PyListObject *)
                    pcc_allocator_alloc_object(object_size);
                if (source == NULL) return 3;
                memset(source, 0, (size_t)object_size);
                source->h.refcount = 1;
                source->h.type_tag = PY_TYPE_LIST;
                source->h.flags = PY_FLAG_GC_MALLOC_ALLOC;
                pcc_debug_note_alloc_size(source, object_size);
                if (pcc_gc_pointer_register((PyObject *)source) != 0) return 4;
                pcc_gc_note_object_allocated_sized(
                    (PyObject *)source, object_size
                );

                int64_t source_kind = pcc_gc_granule_kind(source);
                int64_t source_live = pcc_gc_granule_is_object_start(source);
                int64_t source_managed = pcc_gc_pointer_is_managed(
                    (PyObject *)source
                );
                int64_t source_exact = pcc_gc_managed_pointer_index_contains(
                    (PyObject *)source
                );
                int64_t known_size = pcc_gc_object_known_size(
                    (PyObject *)source
                );
                int32_t source_flags = source->h.flags;
                if (
                    source_kind != 1
                    || source_live != 1
                    || source_managed != 1
                    || source_exact != 0
                    || known_size != object_size
                    || (source_flags & PY_FLAG_GC_MALLOC_ALLOC) == 0
                    || (source_flags & PY_FLAG_GC_ZPAGE_ALLOC) != 0
                    || source->length != 0
                    || source->capacity != 0
                    || source->items != NULL
                ) {
                    return 5;
                }

                /* Finish the legal empty-LIST constructor payload, including
                 * the same external-span registration used by py_list_new,
                 * then populate through the real list API. */
                source->capacity = 4;
                source->items = (PyObject **)malloc(4 * sizeof(PyObject *));
                if (source->items == NULL) return 6;
                memset(source->items, 0, 4 * sizeof(PyObject *));
                (void)pcc_gc_backend4_zpage_register_owner_payload_span(
                    (PyObject *)source,
                    source->items,
                    4 * (int64_t)sizeof(PyObject *)
                );
                py_gc_track((PyObject *)source);
                PyObject *child = py_str_new("child", 5);
                if (child == NULL) return 7;
                py_list_append((PyObject *)source, child);
                int64_t child_before_copy = ((PyObjectHeader *)child)->refcount;
                if (
                    source->length != 1
                    || source->capacity < 1
                    || source->items == NULL
                    || source->items[0] != child
                    || child_before_copy != 2
                ) {
                    return 8;
                }

                PyObject *root = NULL;
                pcc_gc_scheduler_root_register(&root);
                pcc_gc_store_root(&root, (PyObject *)source);
                pcc_gc_release((PyObject *)source);

                pcc_gc_reset_relocation_set();
                int64_t selected = pcc_gc_select_relocation_set(1);
                if (
                    selected != 1
                    || pcc_gc_relocation_set_contains((PyObject *)source) != 1
                ) {
                    return 9;
                }
                int64_t moved = pcc_gc_backend4_evacuation_page_drain(1);
                if (moved != 1) return 10;
                int64_t quarantined_live = pcc_gc_granule_is_object_start(
                    source
                );
                int64_t quarantined_managed = pcc_gc_pointer_is_managed(
                    (PyObject *)source
                );
                int64_t quarantined_exact =
                    pcc_gc_managed_pointer_index_contains((PyObject *)source);
                int64_t quarantined_size = pcc_gc_object_known_size(
                    (PyObject *)source
                );
                if (
                    quarantined_live != 1
                    || quarantined_managed != 1
                    || quarantined_exact != 0
                    || quarantined_size <= 0
                ) {
                    return 11;
                }
                PyObject *first_target = pcc_gc_note_relocation_read(
                    (PyObject *)source
                );
                if (
                    first_target == NULL
                    || first_target == (PyObject *)source
                    || pcc_gc_backend4_forwarding_entries() != 1
                ) {
                    return 12;
                }
                PyListObject *moved_list = (PyListObject *)first_target;
                int64_t first_target_refcount = moved_list->h.refcount;
                int64_t child_after_copy = ((PyObjectHeader *)child)->refcount;
                if (
                    moved_list->h.type_tag != PY_TYPE_LIST
                    || moved_list->length != 1
                    || moved_list->capacity < 1
                    || moved_list->items == NULL
                    || moved_list->items[0] != child
                    || first_target_refcount != 2
                    || child_after_copy != 3
                ) {
                    return 13;
                }

                /* The drain performs the first real remap epoch.  Three
                 * scheduler steps then exercise retirement and two idle
                 * epochs without calling an unlocked helper from the test. */
                (void)pcc_gc_step(256);
                (void)pcc_gc_step(256);
                (void)pcc_gc_step(256);
                PyObject *final_root = root;
                int64_t healed = (
                    final_root != NULL
                    && final_root != (PyObject *)source
                ) ? 1 : 0;
                int64_t forwarding = pcc_gc_backend4_forwarding_entries();
                int64_t old_live = pcc_gc_granule_is_object_start(source);
                int64_t old_managed = pcc_gc_pointer_is_managed(
                    (PyObject *)source
                );
                int64_t old_exact = pcc_gc_managed_pointer_index_contains(
                    (PyObject *)source
                );
                int64_t old_size = pcc_gc_object_known_size(
                    (PyObject *)source
                );
                int64_t final_managed = pcc_gc_pointer_is_managed(final_root);
                int64_t final_exact = pcc_gc_managed_pointer_index_contains(
                    final_root
                );
                int64_t payload_ok = 0;
                int64_t final_root_refcount = 0;
                if (final_root != NULL) {
                    PyListObject *final_list = (PyListObject *)final_root;
                    final_root_refcount = final_list->h.refcount;
                    if (
                        final_list->h.type_tag == PY_TYPE_LIST
                        && final_list->length == 1
                        && final_list->capacity >= 1
                        && final_list->items != NULL
                        && final_list->items[0] == child
                    ) {
                        payload_ok = 1;
                    }
                }
                int64_t no_old = pcc_gc_backend4_verify_no_old_addresses();
                int64_t child_after_quarantine =
                    ((PyObjectHeader *)child)->refcount;
                int64_t ok = healed == 1
                    && forwarding == 0
                    && old_live == -1
                    && old_managed == 0
                    && old_exact == 0
                    && old_size == 0
                    && final_managed == 1
                    && final_exact == 0
                    && payload_ok == 1
                    && no_old == 1;
                pcc_gc_store_root(&root, NULL);
                pcc_gc_scheduler_root_unregister(&root);
                int64_t child_after_root_cleanup =
                    ((PyObjectHeader *)child)->refcount;
                int64_t counts_ok = first_target_refcount == 2
                    && final_root_refcount == 1
                    && child_before_copy == 2
                    && child_after_copy == 3
                    && child_after_quarantine == 2
                    && child_after_root_cleanup == 1;
                pcc_gc_release(child);
                printf(
                    "source %lld %lld %lld %lld size %lld malloc %d zpage %d "
                    "selected %lld moved %lld healed %lld forwarding %lld "
                    "quarantine %lld %lld %lld %lld "
                    "old %lld %lld %lld %lld final %lld %lld payload %lld "
                    "refs %lld %lld child %lld %lld %lld %lld counts %lld "
                    "no_old %lld\n",
                    (long long)source_kind,
                    (long long)source_live,
                    (long long)source_managed,
                    (long long)source_exact,
                    (long long)known_size,
                    (source_flags & PY_FLAG_GC_MALLOC_ALLOC) != 0,
                    (source_flags & PY_FLAG_GC_ZPAGE_ALLOC) != 0,
                    (long long)selected,
                    (long long)moved,
                    (long long)healed,
                    (long long)forwarding,
                    (long long)quarantined_live,
                    (long long)quarantined_managed,
                    (long long)quarantined_exact,
                    (long long)quarantined_size,
                    (long long)old_live,
                    (long long)old_managed,
                    (long long)old_exact,
                    (long long)old_size,
                    (long long)final_managed,
                    (long long)final_exact,
                    (long long)payload_ok,
                    (long long)first_target_refcount,
                    (long long)final_root_refcount,
                    (long long)child_before_copy,
                    (long long)child_after_copy,
                    (long long)child_after_quarantine,
                    (long long)child_after_root_cleanup,
                    (long long)counts_ok,
                    (long long)no_old
                );
                return ok && counts_ok ? 0 : 14;
            }
            '''
        ).lstrip(),
        encoding="utf-8",
    )
    cc = os.environ.get("CC", "cc")
    build = subprocess.run(
        [
            cc,
            "-std=c11",
            f"-I{runtime_archive.parent / 'include'}",
            f"-I{runtime_archive.parent / 'src'}",
            str(src),
            str(runtime_archive),
            "-pthread",
            "-lm",
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    return exe


def test_granule_gc4_downstream_fallback_tail_retires_object_slab_source(
    tmp_path: Path,
    granule_runtime_archives: dict[str, Path],
) -> None:
    # Production-equivalent from the zpage-NULL branch onward.  This proves
    # downstream publication/tracking/moving semantics, not an actual zpage
    # OOM or injected allocation failure, and it makes no C-oracle claim.
    exe = _build_gc4_downstream_fallback_tail_harness(
        tmp_path, granule_runtime_archives["default"]
    )
    done = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=180,
        env=dict(os.environ, PCC_GC_BACKEND="4"),
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout == (
        "source 1 1 1 0 size 40 malloc 1 zpage 0 "
        "selected 1 moved 1 healed 1 forwarding 0 quarantine 1 1 0 40 "
        "old -1 0 0 0 final 1 0 payload 1 refs 2 1 "
        "child 2 3 2 1 counts 1 no_old 1\n"
    )


def _build_threaded_granule_harness(
    tmp_path: Path,
    runtime_archive: Path,
) -> Path:
    src = tmp_path / "granule_pthread_readers.c"
    exe = tmp_path / "granule_pthread_readers.out"
    src.write_text(
        textwrap.dedent(
            r'''
            #include "py_internal.h"
            #include <sched.h>
            #include <stdint.h>
            #include <stdio.h>
            #include <stdlib.h>

            enum { READER_COUNT = 3, SLAB_COUNT = 600, GRANULES = 16 };

            extern int64_t pcc_allocator_granule_count;
            extern void *pcc_allocator_granule_table;
            extern int64_t pcc_allocator_granule_radix_node_count;
            extern int64_t pcc_allocator_metadata_mapped;
            extern int64_t pcc_allocator_live_requested_bytes(void);
            extern int64_t pcc_allocator_live_usable_bytes(void);

            static void *slabs[SLAB_COUNT];
            static void *spans[SLAB_COUNT];
            static _Alignas(4096) unsigned char sentinel_page[4096];
            static int64_t ready_count = 0;
            static int64_t start_flag = 0;
            /* Even = no registration call active; odd = one exact call window.
             * The odd value encodes the zero-based TOTAL slab registration
             * ordinal, including runtime slabs created before this writer. */
            static int64_t registration_epoch = 0;
            static int64_t starting_slab_ordinal = 0;
            static int64_t initial_slab_ordinal = 0;
            static int64_t writer_done = 0;
            static int64_t published_count = 0;
            static int64_t errors = 0;
            static int64_t observations = 0;
            static int64_t ordinary_overlaps = 0;
            static int64_t grow_overlaps = 0;
            static int64_t ordinary_negative_overlaps = 0;
            static int64_t grow_negative_overlaps = 0;
            static int64_t starting_metadata_bytes = 0;
            static int64_t bootstrap_capacity_delta = 0;
            static int64_t bootstrap_metadata_delta = 0;
            static int64_t registration_metadata_bytes = 0;
            static int64_t expected_registration_metadata_bytes = 0;

            static int is_known_grow_index(int64_t index) {
                return index == 8 || index == 16 || index == 32
                    || index == 64 || index == 128 || index == 256
                    || index == 512;
            }

            static int64_t expected_grow_bytes(int64_t index) {
                if (index == 8 || index == 16 || index == 32) return 65536;
                if (index == 64) return 131072;
                if (index == 128) return 196608;
                if (index == 256) return 327680;
                if (index == 512) return 589824;
                return 0;
            }

            static int64_t expected_metadata_total(
                int64_t table_cap,
                int64_t slab_count,
                int64_t radix_node_count
            ) {
                int64_t total =
                    ((slab_count + 2047) / 2048) * 65536
                    + radix_node_count * 32768;
                for (int64_t cap = 256; cap <= table_cap; cap *= 2) {
                    int64_t bytes = 64 + cap * 16;
                    total += (bytes + 65535) & ~65535LL;
                }
                return total;
            }

            static int check_slab(int index) {
                char *base = (char *)slabs[index];
                void *expected_span = spans[index];
                if (base == NULL || expected_span == NULL) return 1;
                for (int page = 0; page < GRANULES; page++) {
                    void *probe = base + page * 4096 + (page * 251 % 4096);
                    if (pcc_gc_granule_kind(probe) != 2) return 2;
                    if (pcc_gc_granule_span(probe) != expected_span) return 3;
                    if (pcc_gc_granule_is_object_start(probe) != -1) return 4;
                }
                return 0;
            }

            static void wait_for_start(void) {
                __atomic_add_fetch(&ready_count, 1, __ATOMIC_ACQ_REL);
                while (__atomic_load_n(&start_flag, __ATOMIC_ACQUIRE) == 0) {
                    sched_yield();
                }
            }

            static void *reader_main(void *arg) {
                intptr_t cursor = (intptr_t)arg;
                wait_for_start();
                while (__atomic_load_n(&writer_done, __ATOMIC_ACQUIRE) == 0) {
                    int64_t limit = __atomic_load_n(
                        &published_count, __ATOMIC_ACQUIRE
                    );
                    if (limit == 0) {
                        sched_yield();
                        continue;
                    }
                    cursor = (cursor + 17) % limit;
                    char *base = (char *)slabs[cursor];
                    void *expected_span = spans[cursor];
                    int page = (int)((cursor * 251) % GRANULES);
                    void *probe = base + page * 4096 + (page * 251 % 4096);

                    /* Classify a stress observation only when all lookups see
                     * the SAME odd public-call epoch.  This does not instrument
                     * the exact internal table-pointer publication instant. */
                    int64_t before = __atomic_load_n(
                        &registration_epoch, __ATOMIC_SEQ_CST
                    );
                    void *observed_span = pcc_gc_granule_span(probe);
                    void *sentinel_span = pcc_gc_granule_span(sentinel_page);
                    int64_t sentinel_kind = pcc_gc_granule_kind(sentinel_page);
                    int64_t after = __atomic_load_n(
                        &registration_epoch, __ATOMIC_SEQ_CST
                    );
                    if (observed_span != expected_span) {
                        __atomic_add_fetch(&errors, 1, __ATOMIC_ACQ_REL);
                    }
                    __atomic_add_fetch(&observations, 1, __ATOMIC_ACQ_REL);
                    if (before == after && (before & 1) != 0) {
                        int64_t registration_index = (before - 1) / 2;
                        if (is_known_grow_index(registration_index)) {
                            __atomic_add_fetch(
                                &grow_overlaps, 1, __ATOMIC_ACQ_REL
                            );
                            if (sentinel_span == NULL && sentinel_kind == 0) {
                                __atomic_add_fetch(
                                    &grow_negative_overlaps,
                                    1,
                                    __ATOMIC_ACQ_REL
                                );
                            }
                        } else {
                            __atomic_add_fetch(
                                &ordinary_overlaps, 1, __ATOMIC_ACQ_REL
                            );
                            if (sentinel_span == NULL && sentinel_kind == 0) {
                                __atomic_add_fetch(
                                    &ordinary_negative_overlaps,
                                    1,
                                    __ATOMIC_ACQ_REL
                                );
                            }
                        }
                    }
                    if (sentinel_span != NULL || sentinel_kind != 0) {
                        __atomic_add_fetch(&errors, 1, __ATOMIC_ACQ_REL);
                    }
                }
                for (int index = 0; index < SLAB_COUNT; index++) {
                    if (check_slab(index) != 0) {
                        __atomic_add_fetch(&errors, 1, __ATOMIC_ACQ_REL);
                    }
                }
                return NULL;
            }

            static void *writer_main(void *unused) {
                (void)unused;
                wait_for_start();
                for (int index = 0; index < SLAB_COUNT; index++) {
                    void *allocation = malloc(131072);
                    if (allocation == NULL) {
                        __atomic_add_fetch(&errors, 1, __ATOMIC_ACQ_REL);
                        break;
                    }
                    uintptr_t address = (uintptr_t)allocation;
                    void *base = (void *)((address + 4095u) & ~(uintptr_t)4095u);
                    int64_t total_index = initial_slab_ordinal + index;
                    int64_t capacity_before = pcc_os_heap_capacity_bytes();
                    int64_t metadata_before = __atomic_load_n(
                        &pcc_allocator_metadata_mapped, __ATOMIC_ACQUIRE
                    );
                    int64_t radix_nodes_before = __atomic_load_n(
                        &pcc_allocator_granule_radix_node_count,
                        __ATOMIC_ACQUIRE
                    );
                    int64_t requested_before =
                        pcc_allocator_live_requested_bytes();
                    int64_t usable_before = pcc_allocator_live_usable_bytes();

                    /* The public registration ABI owns allocator-lock
                     * serialization; callers must not take the lock twice.
                     * No yield or unrelated work may widen this odd epoch. */
                    __atomic_store_n(
                        &registration_epoch, total_index * 2 + 1,
                        __ATOMIC_SEQ_CST
                    );
                    int64_t registration_result =
                        pcc_gc_granule_register_slab(base, 2, 64);
                    __atomic_store_n(
                        &registration_epoch, total_index * 2 + 2,
                        __ATOMIC_SEQ_CST
                    );
                    if (registration_result != 1) {
                        __atomic_add_fetch(&errors, 1, __ATOMIC_ACQ_REL);
                        break;
                    }

                    int64_t capacity_after = pcc_os_heap_capacity_bytes();
                    int64_t metadata_delta = __atomic_load_n(
                        &pcc_allocator_metadata_mapped, __ATOMIC_ACQUIRE
                    ) - metadata_before;
                    int64_t radix_nodes_after = __atomic_load_n(
                        &pcc_allocator_granule_radix_node_count,
                        __ATOMIC_ACQUIRE
                    );
                    int64_t radix_delta =
                        (radix_nodes_after - radix_nodes_before) * 32768;
                    int64_t expected_delta =
                        expected_grow_bytes(total_index) + radix_delta;
                    expected_registration_metadata_bytes += radix_delta;
                    registration_metadata_bytes += metadata_delta;
                    if (
                        metadata_delta != expected_delta
                        || capacity_after - capacity_before != metadata_delta
                        || pcc_allocator_live_requested_bytes()
                            != requested_before
                        || pcc_allocator_live_usable_bytes() != usable_before
                    ) {
                        __atomic_add_fetch(&errors, 1, __ATOMIC_ACQ_REL);
                    }
                    void *span = pcc_gc_granule_span(base);
                    if (span == NULL) {
                        __atomic_add_fetch(&errors, 1, __ATOMIC_ACQ_REL);
                        break;
                    }
                    slabs[index] = base;
                    spans[index] = span;
                    __atomic_store_n(
                        &published_count, index + 1, __ATOMIC_RELEASE
                    );
                }
                __atomic_store_n(&writer_done, 1, __ATOMIC_RELEASE);
                return NULL;
            }

            int main(void) {
                if (pcc_threads_enabled() != 1) return 2;
                int64_t backend = pcc_gc_backend();
                if (
                    pcc_gc_granule_span(sentinel_page) != NULL
                    || pcc_gc_granule_kind(sentinel_page) != 0
                ) {
                    __atomic_add_fetch(&errors, 1, __ATOMIC_ACQ_REL);
                }
                int64_t starting_keys = pcc_allocator_granule_count;
                int64_t starting_capacity = pcc_allocator_granule_table == NULL
                    ? 0
                    : *(int64_t *)pcc_allocator_granule_table;
                starting_slab_ordinal = starting_keys / GRANULES;
                int64_t starting_radix_nodes = __atomic_load_n(
                    &pcc_allocator_granule_radix_node_count,
                    __ATOMIC_ACQUIRE
                );
                starting_metadata_bytes = __atomic_load_n(
                    &pcc_allocator_metadata_mapped, __ATOMIC_ACQUIRE
                );
                int64_t capacity_before_threads = pcc_os_heap_capacity_bytes();
                if (
                    starting_capacity < 256
                    || starting_keys <= 0
                    || starting_keys % GRANULES != 0
                    || starting_metadata_bytes != expected_metadata_total(
                        starting_capacity,
                        starting_slab_ordinal,
                        starting_radix_nodes
                    )
                ) {
                    __atomic_add_fetch(&errors, 1, __ATOMIC_ACQ_REL);
                }
                PccThreadHandle *readers[READER_COUNT] = {0};
                PccThreadHandle *writer = NULL;
                for (intptr_t index = 0; index < READER_COUNT; index++) {
                    if (
                        pcc_thread_start(&readers[index], reader_main, (void *)index)
                        != 0
                    ) {
                        return 3;
                    }
                }
                if (pcc_thread_start(&writer, writer_main, NULL) != 0) return 4;
                while (
                    __atomic_load_n(&ready_count, __ATOMIC_ACQUIRE)
                    != READER_COUNT + 1
                ) {
                    sched_yield();
                }

                int64_t initial_keys = pcc_allocator_granule_count;
                int64_t initial_capacity = pcc_allocator_granule_table == NULL
                    ? 0
                    : *(int64_t *)pcc_allocator_granule_table;
                if (
                    initial_capacity != 256
                    || initial_keys <= 0
                    || initial_keys % GRANULES != 0
                ) {
                    __atomic_add_fetch(&errors, 1, __ATOMIC_ACQ_REL);
                }
                initial_slab_ordinal = initial_keys / GRANULES;
                int64_t initial_radix_nodes = __atomic_load_n(
                    &pcc_allocator_granule_radix_node_count,
                    __ATOMIC_ACQUIRE
                );
                bootstrap_capacity_delta =
                    pcc_os_heap_capacity_bytes() - capacity_before_threads;
                bootstrap_metadata_delta = __atomic_load_n(
                    &pcc_allocator_metadata_mapped, __ATOMIC_ACQUIRE
                ) - starting_metadata_bytes;
                int64_t new_runtime_slabs =
                    initial_slab_ordinal - starting_slab_ordinal;
                int64_t expected_bootstrap_metadata =
                    expected_metadata_total(
                        initial_capacity,
                        initial_slab_ordinal,
                        initial_radix_nodes
                    )
                    - starting_metadata_bytes;
                int64_t expected_bootstrap_capacity =
                    new_runtime_slabs * 65536 + bootstrap_metadata_delta;
                if (
                    new_runtime_slabs < 0
                    || bootstrap_metadata_delta != expected_bootstrap_metadata
                    || bootstrap_capacity_delta != expected_bootstrap_capacity
                ) {
                    __atomic_add_fetch(&errors, 1, __ATOMIC_ACQ_REL);
                }
                for (
                    int64_t index = initial_slab_ordinal;
                    index < initial_slab_ordinal + SLAB_COUNT;
                    index++
                ) {
                    expected_registration_metadata_bytes +=
                        expected_grow_bytes(index);
                }
                __atomic_store_n(&start_flag, 1, __ATOMIC_RELEASE);

                void *thread_result = NULL;
                if (pcc_thread_join(writer, &thread_result) != 0) return 5;
                if (thread_result != NULL) return 6;
                for (int index = 0; index < READER_COUNT; index++) {
                    thread_result = NULL;
                    if (
                        pcc_thread_join(readers[index], &thread_result) != 0
                    ) {
                        return 7;
                    }
                    if (thread_result != NULL) return 8;
                }

                int64_t final_keys = 0;
                for (int index = 0; index < SLAB_COUNT; index++) {
                    if (check_slab(index) != 0) errors++;
                    for (int page = 0; page < GRANULES; page++) {
                        if (
                            pcc_gc_granule_span(
                                (char *)slabs[index] + page * 4096
                            ) == spans[index]
                        ) {
                            final_keys++;
                        }
                    }
                }

                printf("backend %lld\n", (long long)backend);
                printf("threads %lld\n", (long long)pcc_threads_enabled());
                printf(
                    "starting_cap %lld starting_slabs %lld starting_metadata %lld\n",
                    (long long)starting_capacity,
                    (long long)starting_slab_ordinal,
                    (long long)starting_metadata_bytes
                );
                printf(
                    "initial_cap %lld initial_slabs %lld bootstrap_capacity %lld "
                    "bootstrap_metadata %lld\n",
                    (long long)initial_capacity,
                    (long long)initial_slab_ordinal,
                    (long long)bootstrap_capacity_delta,
                    (long long)bootstrap_metadata_delta
                );
                printf(
                    "published %lld errors %lld final_keys %lld\n",
                    (long long)__atomic_load_n(
                        &published_count, __ATOMIC_ACQUIRE
                    ),
                    (long long)__atomic_load_n(&errors, __ATOMIC_ACQUIRE),
                    (long long)final_keys
                );
                printf(
                    "observations %lld ordinary_overlap %lld grow_overlap %lld\n",
                    (long long)__atomic_load_n(
                        &observations, __ATOMIC_ACQUIRE
                    ),
                    (long long)__atomic_load_n(
                        &ordinary_overlaps, __ATOMIC_ACQUIRE
                    ),
                    (long long)__atomic_load_n(
                        &grow_overlaps, __ATOMIC_ACQUIRE
                    )
                );
                printf(
                    "ordinary_negative_overlap %lld "
                    "grow_negative_overlap %lld\n",
                    (long long)__atomic_load_n(
                        &ordinary_negative_overlaps, __ATOMIC_ACQUIRE
                    ),
                    (long long)__atomic_load_n(
                        &grow_negative_overlaps, __ATOMIC_ACQUIRE
                    )
                );
                printf(
                    "metadata_bytes %lld expected_metadata %lld\n",
                    (long long)registration_metadata_bytes,
                    (long long)expected_registration_metadata_bytes
                );
                return errors == 0
                        && final_keys == SLAB_COUNT * GRANULES
                        && ordinary_overlaps > 0
                        && grow_overlaps > 0
                        && ordinary_negative_overlaps > 0
                        && grow_negative_overlaps > 0
                        && registration_metadata_bytes
                            == expected_registration_metadata_bytes
                    ? 0
                    : 9;
            }
            '''
        ).lstrip(),
        encoding="utf-8",
    )
    cc = os.environ.get("CC", "cc")
    build = subprocess.run(
        [
            cc,
            "-DPCC_WITH_THREADS=1",
            "-std=c11",
            "-pthread",
            f"-I{runtime_archive.parent / 'include'}",
            f"-I{runtime_archive.parent / 'src'}",
            str(src),
            str(runtime_archive),
            "-lm",
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    return exe


def test_granule_single_writer_races_real_pthread_readers_through_grow(
    tmp_path: Path,
    granule_runtime_archives: dict[str, Path],
) -> None:
    exe = _build_threaded_granule_harness(
        tmp_path, granule_runtime_archives["threaded"]
    )
    for backend in range(5):
        environment = dict(os.environ, PCC_GC_BACKEND=str(backend))
        done = subprocess.run(
            [str(exe)],
            capture_output=True,
            text=True,
            timeout=180,
            env=environment,
        )
        assert done.returncode == 0, (
            f"backend {backend}:\nstdout:\n{done.stdout}\nstderr:\n{done.stderr}"
        )
        tokens = done.stdout.split()
        assert tokens[:2] == ["backend", str(backend)]
        values = dict(zip(tokens[2::2], tokens[3::2], strict=True))
        assert values["threads"] == "1"
        assert values["starting_cap"] == "256"
        assert int(values["starting_slabs"]) > 0
        assert int(values["starting_metadata"]) >= 2 * 65536
        assert values["initial_cap"] == "256"
        assert int(values["initial_slabs"]) >= int(values["starting_slabs"])
        assert int(values["bootstrap_capacity"]) >= 0
        assert int(values["bootstrap_metadata"]) >= 0
        assert int(values["bootstrap_capacity"]) == (
            (
                int(values["initial_slabs"])
                - int(values["starting_slabs"])
            )
            * 65536
            + int(values["bootstrap_metadata"])
        )
        assert values["published"] == "600"
        assert values["errors"] == "0"
        assert values["final_keys"] == "9600"
        assert int(values["observations"]) > 0
        assert int(values["ordinary_overlap"]) > 0
        assert int(values["grow_overlap"]) > 0
        assert int(values["ordinary_negative_overlap"]) > 0
        assert int(values["grow_negative_overlap"]) > 0
        assert int(values["metadata_bytes"]) > 0
        assert values["metadata_bytes"] == values["expected_metadata"]
