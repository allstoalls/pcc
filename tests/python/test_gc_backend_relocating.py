from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

from pcc.tools.runtime_archive_provenance import (
    PRODUCTION_POLICY,
    verify_runtime_archive_manifest,
)
from tests.runtime_build_cache import cached_c_runtime, cached_threaded_pcc_python_runtime

REPO_ROOT = Path(__file__).absolute().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "py_runtime"

_PCC_PY_RUNTIME_BUILD_CACHE: Path | None = None


def _compile_probe(tmp_path, source: str):
    from pcc.py_frontend.pipeline import compile_python

    src = tmp_path / "probe.py"
    exe = tmp_path / "probe.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    compile_python(str(src), str(exe), ir_scaffold_mode="on", libpython_mode="off")
    return exe


def _run_backend_four(exe):
    env = os.environ.copy()
    env["PCC_GC_BACKEND"] = "4"
    return subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
    )


def _cc() -> str:
    return os.environ.get("CC", "cc")


def _build_runtime(tmp_path: Path) -> Path:
    del tmp_path
    return cached_c_runtime()


def _build_pcc_py_runtime(tmp_path: Path) -> Path:
    global _PCC_PY_RUNTIME_BUILD_CACHE
    explicit = os.environ.get("PCC_RUNTIME_ARCHIVE")
    if explicit:
        archive = Path(explicit).resolve()
        assert archive.name == "libpy_runtime_pcc_py.a"
        manifest = verify_runtime_archive_manifest(
            archive,
            runtime_root=RUNTIME_DIR,
        )
        assert manifest["policy"] == PRODUCTION_POLICY
        assert all(
            record["source_kind"] == "pcc-python"
            and record["producer_kind"] == "pcc-python-library-ir-to-obj"
            and record["uses_host_cc"] is False
            for record in manifest["members"]
        )
        return archive.parent
    if (
        _PCC_PY_RUNTIME_BUILD_CACHE is not None
        and (_PCC_PY_RUNTIME_BUILD_CACHE / "libpy_runtime_pcc_py.a").is_file()
    ):
        return _PCC_PY_RUNTIME_BUILD_CACHE
    _PCC_PY_RUNTIME_BUILD_CACHE = cached_threaded_pcc_python_runtime()
    return _PCC_PY_RUNTIME_BUILD_CACHE


def _assert_pcc_python_memoryview_owned_buffer_follows_relocation(
    tmp_path: Path,
    work_runtime: Path,
):
    src = tmp_path / "pcc_python_memoryview_relocation_probe.c"
    exe = tmp_path / "pcc_python_memoryview_relocation_probe.out"
    src.write_text(
        textwrap.dedent(
            """
            #include "py_runtime.h"
            #include "py_internal.h"
            #include <stdint.h>

            typedef struct {
                void *buf;
                PyObject *obj;
                int64_t len;
                int64_t itemsize;
                int32_t readonly;
                int32_t ndim;
                char *format;
                int64_t *shape;
                int64_t *strides;
                int64_t *suboffsets;
                void *internal;
            } ProbePyBuffer;

            typedef struct {
                PyObjectHeader h;
                PyObject *base;
                ProbePyBuffer *owned_buffer;
            } ProbeMemoryViewObject;

            extern ProbePyBuffer *pcc_PyMemoryView_GET_BUFFER(PyObject *obj);
            extern int64_t pcc_gc_object_known_size(PyObject *obj);
            extern int64_t pcc_gc_backend4_relocation_set_add(PyObject *obj);
            extern void *pcc_gc_backend4_relocation_set_find(PyObject *obj);
            extern void pcc_gc_backend4_remap_referents(PyObject *obj);

            static int check_memoryview_relocation(void) {
                PyObject *base_root = 0;
                PyObject *inner_root = 0;
                PyObject *outer_root = 0;
                pcc_gc_scheduler_root_register(&base_root);
                pcc_gc_scheduler_root_register(&inner_root);
                pcc_gc_scheduler_root_register(&outer_root);
                PyObject *base = py_bytes_new("abc", 3);
                if (base == 0) return 10;
                pcc_gc_store_root(&base_root, base);
                PyObject *inner = py_memoryview_new(base);
                if (inner == 0) return 11;
                pcc_gc_store_root(&inner_root, inner);
                base = pcc_gc_load_ptr(0, &base_root);
                inner = pcc_gc_load_ptr(0, &inner_root);
                PyObject *outer = py_memoryview_new(inner);
                if (outer == 0) return 12;
                pcc_gc_store_root(&outer_root, outer);
                base = pcc_gc_load_ptr(0, &base_root);
                inner = pcc_gc_load_ptr(0, &inner_root);
                outer = pcc_gc_load_ptr(0, &outer_root);
                if (base == 0 || inner == 0 || outer == 0) return 13;
                pcc_gc_release(base);
                pcc_gc_release(inner);
                pcc_gc_release(outer);
                base = pcc_gc_load_ptr(0, &base_root);
                inner = pcc_gc_load_ptr(0, &inner_root);
                outer = pcc_gc_load_ptr(0, &outer_root);
                if (pcc_gc_object_known_size(inner) <= 0) return 14;
                if (pcc_gc_object_known_size(outer) <= 0) return 15;

                ProbePyBuffer *buffer = pcc_PyMemoryView_GET_BUFFER(outer);
                if (buffer == 0 || buffer->obj != inner) return 16;
                if (buffer->buf != (void *)((char *)base + 24)) return 17;

                ProbeMemoryViewObject *old_outer =
                    (ProbeMemoryViewObject *)outer;

                pcc_gc_reset_relocation_set();
                /* The set prepends and production drains head-first: adding
                 * outer then inner exercises the real inner->outer order. */
                if (pcc_gc_backend4_relocation_set_add(outer) != 1) return 18;
                if (pcc_gc_backend4_relocation_set_add(inner) != 1) return 19;
                if (pcc_gc_backend4_relocation_set_find(inner) == 0) return 20;
                int64_t inner_size = pcc_gc_object_known_size(inner);
                if (inner_size <= 0) return 21;
                PyObject *moved_inner = pcc_gc_relocate_copy(inner, inner_size);
                if (moved_inner == 0) return 22;
                if (moved_inner == inner) return 23;

                pcc_gc_backend4_remap_referents(outer);
                if (old_outer->base != moved_inner) return 24;
                if (buffer->obj != moved_inner) return 25;
                if (buffer->buf != (void *)((char *)base + 24)) return 26;

                int64_t outer_size = pcc_gc_object_known_size(outer);
                if (outer_size <= 0) return 27;
                PyObject *moved_outer = pcc_gc_relocate_copy(
                    outer, outer_size
                );
                if (moved_outer == 0) return 28;
                if (moved_outer == outer) return 29;
                ProbeMemoryViewObject *new_outer =
                    (ProbeMemoryViewObject *)moved_outer;
                if (old_outer->owned_buffer != 0) return 30;
                if (new_outer->owned_buffer != buffer) return 31;
                if (new_outer->base != moved_inner) return 32;
                if (buffer->obj != moved_inner) return 33;
                if (buffer->buf != (void *)((char *)base + 24)) return 34;
                if (buffer->len != 3 || buffer->shape == 0) return 35;
                if (*buffer->shape != 3 || buffer->strides == 0) return 36;
                if (*buffer->strides != 1) return 37;
                return 1;
            }

            int main(void) {
                if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) {
                    return 2;
                }
                int result = check_memoryview_relocation();
                return result == 1 ? 0 : result;
            }
            """
        ).lstrip(),
        encoding="utf-8",
    )
    build = subprocess.run(
        [
            _cc(),
            "-std=c11",
            f"-I{work_runtime / 'include'}",
            f"-I{work_runtime / 'src'}",
            str(src),
            str(work_runtime / "libpy_runtime_pcc_py.a"),
            "-lpthread",
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stderr

    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"exit={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )


def _assert_backend_four_task_and_scheduler_queue_follow_forwarding(
    tmp_path: Path,
    work_runtime: Path,
    archive_name: str,
    *,
    extra_link_args: list[str] | None = None,
):
    src = tmp_path / f"{archive_name}_task_queue_forwarding_probe.c"
    exe = tmp_path / f"{archive_name}_task_queue_forwarding_probe.out"
    src.write_text(
        textwrap.dedent(
            """
            #include "py_runtime.h"
            #include "py_internal.h"
            #include <stdint.h>
            #include <stdio.h>
            #include <stdlib.h>
            extern void pcc_gc_publish_initialized(PyObject *obj);

            typedef struct {
                PyObjectHeader h;
                int64_t length;
                int64_t capacity;
                PyObject **items;
            } ProbeListObject;

            static PyObject *new_reloc_payload(void) {
                ProbeListObject *obj = (
                    ProbeListObject *
                )pcc_gc_alloc(64, PY_TYPE_LIST, 0);
                if (obj == 0) return 0;
                obj->length = 0;
                obj->capacity = 0;
                obj->items = 0;
                /* pcc_gc_alloc sets PY_FLAG_GC_FRESH_ALLOC for container tags
                 * and the relocation-set add refuses anything carrying it, so
                 * a raw payload that never publishes can never be forwarded. */
                pcc_gc_publish_initialized((PyObject *)obj);
                return (PyObject *)obj;
            }

            static int move_newest_simple_object(void) {
                pcc_gc_telemetry_reset();
                int64_t total = 0;
                for (int i = 0; i < 4; i++) {
                    total += pcc_gc_step(16);
                    if (
                        pcc_gc_telemetry(PCC_GC_COUNTER_RELOCATION_FORWARDS) >= 1
                    ) {
                        return total >= 1;
                    }
                }
                return 0;
            }

            static int check_task_result_slot(void) {
                PyObject *coro = py_coroutine_new_native("task-coro", 0, 0, 0);
                if (coro == 0) return 0;
                PyObject *task = py_task_new(coro);
                if (task == 0) return 0;
                pcc_gc_release(coro);

                PyObject *result = new_reloc_payload();
                if (result == 0) return 0;
                int64_t stable_id = pcc_gc_object_id(result);
                py_task_set_result(task, result);
                if (!move_newest_simple_object()) return 0;

                PyObject *forwarded = pcc_gc_note_relocation_read(result);
                PyObject *got = py_task_get_result(task);
                PyObject *resolved_task = pcc_gc_note_relocation_read(task);
                PyTaskObject *t = (PyTaskObject *)resolved_task;
                int ok = forwarded != 0
                    && forwarded != result
                    && got == forwarded
                    && t->result == forwarded
                    && pcc_gc_object_id(got) == stable_id;
                pcc_gc_release(got);
                py_task_set_result(task, py_None);
                pcc_gc_release(result);
                pcc_gc_release(task);
                return ok;
            }

            static int check_scheduler_queue_pop_slot(void) {
                PccGcSchedulerQueue *queue = pcc_gc_scheduler_queue_new();
                if (queue == 0) return 0;
                PyObject *child = new_reloc_payload();
                if (child == 0) return 0;
                int64_t stable_id = pcc_gc_object_id(child);
                if (pcc_gc_scheduler_queue_push(queue, child) != 0) return 0;
                if (!move_newest_simple_object()) return 0;

                PyObject *forwarded = pcc_gc_note_relocation_read(child);
                PyObject *out = 0;
                pcc_gc_scheduler_root_register(&out);
                int64_t popped = pcc_gc_scheduler_queue_pop_into(queue, &out);
                int ok = popped == 1
                    && forwarded != 0
                    && forwarded != child
                    && out == forwarded
                    && pcc_gc_object_id(out) == stable_id;
                pcc_gc_store_root(&out, 0);
                pcc_gc_scheduler_root_unregister(&out);
                pcc_gc_release(child);
                pcc_gc_scheduler_queue_free(queue);
                return ok;
            }

            static int check_scheduler_queue_free_slot(void) {
                PccGcSchedulerQueue *queue = pcc_gc_scheduler_queue_new();
                if (queue == 0) return 0;
                PyObject *child = new_reloc_payload();
                if (child == 0) return 0;
                if (pcc_gc_scheduler_queue_push(queue, child) != 0) return 0;
                if (!move_newest_simple_object()) return 0;
                if (
                    pcc_gc_telemetry(
                        PCC_GC_COUNTER_RELOCATION_BARRIER_FORWARDS
                    ) != 0
                ) {
                    return 0;
                }
                pcc_gc_scheduler_queue_free(queue);
                int ok = pcc_gc_telemetry(
                    PCC_GC_COUNTER_RELOCATION_BARRIER_FORWARDS
                ) >= 1;
                pcc_gc_release(child);
                return ok;
            }

            int main(void) {
                if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) {
                    return 2;
                }
                printf("%d\\n", check_task_result_slot());
                printf("%d\\n", check_scheduler_queue_pop_slot());
                printf("%d\\n", check_scheduler_queue_free_slot());
                return 0;
            }
            """
        ).lstrip()
    , encoding="utf-8")
    link = [
        _cc(),
        "-std=c11",
        f"-I{work_runtime / 'include'}",
        f"-I{work_runtime / 'src'}",
        str(src),
        str(work_runtime / archive_name),
    ]
    if extra_link_args:
        link.extend(extra_link_args)
    link.extend(["-o", str(exe)])
    build = subprocess.run(link, capture_output=True, text=True, timeout=30)
    assert build.returncode == 0, build.stderr

    result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == ["1", "1", "1"]


def _assert_backend_four_list_relocation_copies_owned_items(
    tmp_path: Path,
    work_runtime: Path,
    archive_name: str,
    *,
    extra_link_args: list[str] | None = None,
):
    src = tmp_path / f"{archive_name}_list_relocation_probe.c"
    exe = tmp_path / f"{archive_name}_list_relocation_probe.out"
    src.write_text(
        textwrap.dedent(
            """
            #include "py_runtime.h"
            #include "py_internal.h"
            #include <stdint.h>
            #include <stdio.h>

            static int check_list_relocation(void) {
                PyObject *slot = 0;
                pcc_gc_scheduler_root_register(&slot);

                PyObject *child = py_str_new("list-child", 10);
                if (child == 0) return 0;
                PyObject *list = py_list_new(0);
                if (list == 0) return 0;
                py_list_append(list, child);
                pcc_gc_release(child);

                int64_t list_id = pcc_gc_object_id(list);
                pcc_gc_store_root(&slot, list);
                pcc_gc_release(list);

                pcc_gc_reset_relocation_set();
                (void)pcc_gc_select_relocation_set(16);
                if (pcc_gc_relocation_set_contains(slot) != 1) return 0;

                PyListObject *old_list = (PyListObject *)slot;
                PyObject **old_items = old_list->items;
                PyObject *moved = pcc_gc_relocate_copy(slot, sizeof(PyListObject));
                if (moved == 0 || moved == slot) return 0;

                PyListObject *new_list = (PyListObject *)moved;
                int copied_payload = new_list->items != 0
                    && new_list->items != old_items
                    && new_list->length == 1;
                PyObject *loaded = pcc_gc_load_ptr(0, &slot);
                PyObject *got = py_list_get(loaded, 0);
                PyObject *expected = py_str_new("list-child", 10);
                int ok = copied_payload
                    && loaded == moved
                    && pcc_gc_object_id(loaded) == list_id
                    && got != 0
                    && expected != 0
                    && py_obj_eq(got, expected);

                pcc_gc_release(got);
                pcc_gc_release(expected);
                pcc_gc_store_root(&slot, 0);
                pcc_gc_scheduler_root_unregister(&slot);
                pcc_gc_release(moved);
                return ok;
            }

            int main(void) {
                if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) {
                    return 2;
                }
                printf("%d\\n", check_list_relocation());
                return 0;
            }
            """
        ).lstrip()
    , encoding="utf-8")
    link = [
        _cc(),
        "-std=c11",
        f"-I{work_runtime / 'include'}",
        f"-I{work_runtime / 'src'}",
        str(src),
        str(work_runtime / archive_name),
    ]
    if extra_link_args:
        link.extend(extra_link_args)
    link.extend(["-o", str(exe)])
    build = subprocess.run(link, capture_output=True, text=True, timeout=30)
    assert build.returncode == 0, build.stderr

    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == ["1"]


def _assert_backend_four_tuple_relocation_retain_owned_items(
    tmp_path: Path,
    work_runtime: Path,
    archive_name: str,
    *,
    extra_link_args: list[str] | None = None,
):
    src = tmp_path / f"{archive_name}_tuple_relocation_probe.c"
    exe = tmp_path / f"{archive_name}_tuple_relocation_probe.out"
    src.write_text(
        textwrap.dedent(
            """
            #include "py_runtime.h"
            #include "py_internal.h"
            #include <stdint.h>
            #include <stdio.h>

            static int check_tuple_relocation(void) {
                PyObject *slot = 0;
                pcc_gc_scheduler_root_register(&slot);

                PyObject *child = py_str_new("tuple-child", 11);
                if (child == 0) return 0;
                PyObject *tuple = py_tuple_new(1);
                if (tuple == 0) return 0;
                py_tuple_set_item(tuple, 0, child);
                pcc_gc_release(child);

                int64_t tuple_id = pcc_gc_object_id(tuple);
                pcc_gc_store_root(&slot, tuple);
                pcc_gc_release(tuple);

                pcc_gc_reset_relocation_set();
                (void)pcc_gc_select_relocation_set(16);
                if (pcc_gc_relocation_set_contains(slot) != 1) return 0;

                PyObject *moved = pcc_gc_relocate_copy(
                    slot,
                    (int64_t)(sizeof(PyTupleObject) + sizeof(PyObject *))
                );
                if (moved == 0 || moved == slot) return 0;

                int child_owned = py_header(child)->refcount >= 2;
                if (!child_owned) return 0;

                PyObject *loaded = pcc_gc_load_ptr(0, &slot);
                PyObject *got = py_tuple_get(loaded, 0);
                PyObject *expected = py_str_new("tuple-child", 11);
                int ok = loaded == moved
                    && pcc_gc_object_id(loaded) == tuple_id
                    && got != 0
                    && expected != 0
                    && py_obj_eq(got, expected);

                pcc_gc_release(got);
                pcc_gc_release(expected);
                pcc_gc_store_root(&slot, 0);
                pcc_gc_scheduler_root_unregister(&slot);
                pcc_gc_release(moved);
                return ok;
            }

            int main(void) {
                if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) {
                    return 2;
                }
                printf("%d\\n", check_tuple_relocation());
                return 0;
            }
            """
        ).lstrip()
    , encoding="utf-8")
    link = [
        _cc(),
        "-std=c11",
        f"-I{work_runtime / 'include'}",
        f"-I{work_runtime / 'src'}",
        str(src),
        str(work_runtime / archive_name),
    ]
    if extra_link_args:
        link.extend(extra_link_args)
    link.extend(["-o", str(exe)])
    build = subprocess.run(link, capture_output=True, text=True, timeout=30)
    assert build.returncode == 0, build.stderr

    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == ["1"]


def _assert_backend_four_task_relocation_retains_state_slots(
    tmp_path: Path,
    work_runtime: Path,
    archive_name: str,
    *,
    extra_link_args: list[str] | None = None,
):
    src = tmp_path / f"{archive_name}_task_relocation_probe.c"
    exe = tmp_path / f"{archive_name}_task_relocation_probe.out"
    src.write_text(
        textwrap.dedent(
            """
            #include "py_runtime.h"
            #include "py_internal.h"
            #include <stdint.h>
            #include <stdio.h>

            static int check_task_relocation(void) {
                PyObject *slot = 0;
                pcc_gc_scheduler_root_register(&slot);

                PyObject *coro = py_coroutine_new_native("task-coro", 0, 0, 0);
                if (coro == 0) return 0;
                PyObject *task = py_task_new(coro);
                if (task == 0) return 0;
                int64_t coro_id = pcc_gc_object_id(coro);
                pcc_gc_release(coro);

                PyObject *result = py_str_new("task-result", 11);
                PyObject *waiter = py_str_new("task-waiter", 11);
                if (result == 0 || waiter == 0) return 0;
                py_task_set_result(task, result);
                py_task_set_waiter(task, waiter);
                pcc_gc_release(result);
                pcc_gc_release(waiter);

                int64_t task_id = pcc_gc_object_id(task);
                pcc_gc_store_root(&slot, task);
                pcc_gc_release(task);

                pcc_gc_reset_relocation_set();
                (void)pcc_gc_select_relocation_set(64);
                if (pcc_gc_relocation_set_contains(slot) != 1) return 0;

                PyTaskObject *old_task = (PyTaskObject *)slot;
                PyObject *old_coro = old_task->coro;
                PyObject *old_result = old_task->result;
                PyObject *old_waiter = old_task->waiter;
                PyObject *moved = pcc_gc_relocate_copy(slot, sizeof(PyTaskObject));
                if (moved == 0 || moved == slot) return 0;

                int slots_owned =
                    py_header(old_coro)->refcount >= 2
                    && py_header(old_result)->refcount >= 2
                    && py_header(old_waiter)->refcount >= 2;
                if (!slots_owned) return 0;

                PyObject *loaded = pcc_gc_load_ptr(0, &slot);
                PyObject *got_coro = py_task_get_coro(loaded);
                PyObject *got_result = py_task_get_result(loaded);
                PyObject *got_waiter = py_task_get_waiter(loaded);
                PyObject *expected_result = py_str_new("task-result", 11);
                PyObject *expected_waiter = py_str_new("task-waiter", 11);

                int ok = loaded == moved
                    && pcc_gc_object_id(loaded) == task_id
                    && pcc_gc_object_id(got_coro) == coro_id
                    && got_result != 0
                    && got_waiter != 0
                    && expected_result != 0
                    && expected_waiter != 0
                    && py_obj_eq(got_result, expected_result)
                    && py_obj_eq(got_waiter, expected_waiter);

                pcc_gc_release(got_coro);
                pcc_gc_release(got_result);
                pcc_gc_release(got_waiter);
                pcc_gc_release(expected_result);
                pcc_gc_release(expected_waiter);
                py_task_set_result(loaded, py_None);
                py_task_set_waiter(loaded, 0);
                pcc_gc_store_root(&slot, 0);
                pcc_gc_scheduler_root_unregister(&slot);
                pcc_gc_release(moved);
                return ok;
            }

            int main(void) {
                if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) {
                    return 2;
                }
                printf("%d\\n", check_task_relocation());
                return 0;
            }
            """
        ).lstrip()
    , encoding="utf-8")
    link = [
        _cc(),
        "-std=c11",
        f"-I{work_runtime / 'include'}",
        f"-I{work_runtime / 'src'}",
        str(src),
        str(work_runtime / archive_name),
    ]
    if extra_link_args:
        link.extend(extra_link_args)
    link.extend(["-o", str(exe)])
    build = subprocess.run(link, capture_output=True, text=True, timeout=30)
    assert build.returncode == 0, build.stderr

    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == ["1"]


def _assert_backend_four_set_relocation_retains_owned_entries(
    tmp_path: Path,
    work_runtime: Path,
    archive_name: str,
    *,
    extra_link_args: list[str] | None = None,
):
    src = tmp_path / f"{archive_name}_set_relocation_probe.c"
    exe = tmp_path / f"{archive_name}_set_relocation_probe.out"
    src.write_text(
        textwrap.dedent(
            """
            #include "py_runtime.h"
            #include "py_internal.h"
            #include <stdint.h>
            #include <stdio.h>

            static int check_set_relocation(void) {
                PyObject *slot = 0;
                pcc_gc_scheduler_root_register(&slot);

                PyObject *set = py_set_new();
                PyObject *a = py_str_new("set-a", 5);
                PyObject *b = py_str_new("set-b", 5);
                PyObject *dead = py_str_new("set-dead", 8);
                if (set == 0 || a == 0 || b == 0 || dead == 0) return 0;

                py_set_add(set, a);
                py_set_add(set, b);
                py_set_add(set, dead);
                if (py_set_len(set) != 3) return 0;
                if (py_set_remove(set, dead) != 0) return 0;
                pcc_gc_release(dead);

                int64_t set_id = pcc_gc_object_id(set);
                pcc_gc_release(a);
                pcc_gc_release(b);
                pcc_gc_store_root(&slot, set);
                pcc_gc_release(set);

                pcc_gc_reset_relocation_set();
                (void)pcc_gc_select_relocation_set(64);
                if (pcc_gc_relocation_set_contains(slot) != 1) return 0;

                PySetObject *old_set = (PySetObject *)slot;
                SetEntry *old_entries = old_set->entries;
                PyObject *old_a = 0;
                PyObject *old_b = 0;
                int64_t tombstones = 0;
                for (int64_t i = 0; i < old_set->capacity; i++) {
                    PyObject *key = old_entries[i].key;
                    if (key == py_set_dummy) {
                        tombstones++;
                    } else if (key != 0) {
                        if (old_a == 0) {
                            old_a = key;
                        } else {
                            old_b = key;
                        }
                    }
                }
                if (old_a == 0 || old_b == 0 || tombstones < 1) return 0;

                PyObject *moved = pcc_gc_relocate_copy(slot, sizeof(PySetObject));
                if (moved == 0 || moved == slot) return 0;

                PySetObject *moved_set = (PySetObject *)moved;
                int entries_owned =
                    moved_set->entries != 0
                    && moved_set->entries != old_entries
                    && moved_set->capacity == old_set->capacity
                    && moved_set->fill == old_set->fill
                    && py_header(old_a)->refcount >= 2
                    && py_header(old_b)->refcount >= 2;
                if (!entries_owned) return 0;

                PyObject *loaded = pcc_gc_load_ptr(0, &slot);
                PyObject *expected_a = py_str_new("set-a", 5);
                PyObject *expected_b = py_str_new("set-b", 5);
                PyObject *expected_dead = py_str_new("set-dead", 8);

                int ok = loaded == moved
                    && pcc_gc_object_id(loaded) == set_id
                    && py_set_len(loaded) == 2
                    && expected_a != 0
                    && expected_b != 0
                    && expected_dead != 0
                    && py_set_contains(loaded, expected_a) == 1
                    && py_set_contains(loaded, expected_b) == 1
                    && py_set_contains(loaded, expected_dead) == 0;

                pcc_gc_release(expected_a);
                pcc_gc_release(expected_b);
                pcc_gc_release(expected_dead);
                pcc_gc_store_root(&slot, 0);
                pcc_gc_scheduler_root_unregister(&slot);
                pcc_gc_release(moved);
                return ok;
            }

            int main(void) {
                if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) {
                    return 2;
                }
                printf("%d\\n", check_set_relocation());
                return 0;
            }
            """
        ).lstrip()
    , encoding="utf-8")
    link = [
        _cc(),
        "-std=c11",
        f"-I{work_runtime / 'include'}",
        f"-I{work_runtime / 'src'}",
        str(src),
        str(work_runtime / archive_name),
    ]
    if extra_link_args:
        link.extend(extra_link_args)
    link.extend(["-o", str(exe)])
    build = subprocess.run(link, capture_output=True, text=True, timeout=30)
    assert build.returncode == 0, build.stderr

    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == ["1"]


def _assert_backend_four_dict_relocation_retains_owned_tables(
    tmp_path: Path,
    work_runtime: Path,
    archive_name: str,
    *,
    extra_link_args: list[str] | None = None,
):
    src = tmp_path / f"{archive_name}_dict_relocation_probe.c"
    exe = tmp_path / f"{archive_name}_dict_relocation_probe.out"
    src.write_text(
        textwrap.dedent(
            """
            #include "py_runtime.h"
            #include "py_internal.h"
            #include <stdint.h>
            #include <stdio.h>

            static int check_dict_relocation(void) {
                PyObject *slot = 0;
                pcc_gc_scheduler_root_register(&slot);

                PyObject *dict = py_dict_new();
                PyObject *k1 = py_str_new("dict-k1", 7);
                PyObject *v1 = py_str_new("dict-v1", 7);
                PyObject *k2 = py_str_new("dict-k2", 7);
                PyObject *v2 = py_str_new("dict-v2", 7);
                PyObject *dead_k = py_str_new("dict-dead", 9);
                PyObject *dead_v = py_str_new("dict-dead-v", 11);
                if (
                    dict == 0 || k1 == 0 || v1 == 0 || k2 == 0 || v2 == 0
                    || dead_k == 0 || dead_v == 0
                ) {
                    return 0;
                }

                py_dict_set(dict, k1, v1);
                py_dict_set(dict, k2, v2);
                py_dict_set(dict, dead_k, dead_v);
                if (py_dict_len(dict) != 3) return 0;
                if (py_dict_del(dict, dead_k) != 0) return 0;

                pcc_gc_release(k1);
                pcc_gc_release(v1);
                pcc_gc_release(k2);
                pcc_gc_release(v2);
                pcc_gc_release(dead_k);
                pcc_gc_release(dead_v);

                int64_t dict_id = pcc_gc_object_id(dict);
                pcc_gc_store_root(&slot, dict);
                pcc_gc_release(dict);

                pcc_gc_reset_relocation_set();
                (void)pcc_gc_select_relocation_set(64);
                if (pcc_gc_relocation_set_contains(slot) != 1) return 0;

                PyDictObject *old_dict = (PyDictObject *)slot;
                int64_t *old_indices = old_dict->indices;
                DictEntry *old_entries = old_dict->entries;
                PyObject *old_k1 = 0;
                PyObject *old_v1 = 0;
                PyObject *old_k2 = 0;
                PyObject *old_v2 = 0;
                int64_t tombstones = 0;
                for (int64_t i = 0; i < old_dict->capacity; i++) {
                    if (old_indices[i] == PY_DICT_TOMBSTONE) tombstones++;
                }
                for (int64_t i = 0; i < old_dict->entries_used; i++) {
                    DictEntry *e = &old_entries[i];
                    if (e->key == 0) continue;
                    if (old_k1 == 0) {
                        old_k1 = e->key;
                        old_v1 = e->value;
                    } else {
                        old_k2 = e->key;
                        old_v2 = e->value;
                    }
                }
                if (
                    old_k1 == 0 || old_v1 == 0 || old_k2 == 0 || old_v2 == 0
                    || tombstones < 1
                ) {
                    return 0;
                }

                PyObject *moved = pcc_gc_relocate_copy(slot, sizeof(PyDictObject));
                if (moved == 0 || moved == slot) return 0;

                PyDictObject *moved_dict = (PyDictObject *)moved;
                int tables_owned =
                    moved_dict->indices != 0
                    && moved_dict->entries != 0
                    && moved_dict->indices != old_indices
                    && moved_dict->entries != old_entries
                    && moved_dict->capacity == old_dict->capacity
                    && moved_dict->size == old_dict->size
                    && moved_dict->entries_used == old_dict->entries_used
                    && py_header(old_k1)->refcount >= 2
                    && py_header(old_v1)->refcount >= 2
                    && py_header(old_k2)->refcount >= 2
                    && py_header(old_v2)->refcount >= 2;
                if (!tables_owned) return 0;

                PyObject *loaded = pcc_gc_load_ptr(0, &slot);
                PyObject *expect_k1 = py_str_new("dict-k1", 7);
                PyObject *expect_v1 = py_str_new("dict-v1", 7);
                PyObject *expect_k2 = py_str_new("dict-k2", 7);
                PyObject *expect_v2 = py_str_new("dict-v2", 7);
                PyObject *expect_dead = py_str_new("dict-dead", 9);
                PyObject *got_v1 = py_dict_get(loaded, expect_k1);
                PyObject *got_v2 = py_dict_get(loaded, expect_k2);

                int ok = loaded == moved
                    && pcc_gc_object_id(loaded) == dict_id
                    && py_dict_len(loaded) == 2
                    && expect_k1 != 0
                    && expect_v1 != 0
                    && expect_k2 != 0
                    && expect_v2 != 0
                    && expect_dead != 0
                    && got_v1 != 0
                    && got_v2 != 0
                    && py_obj_eq(got_v1, expect_v1)
                    && py_obj_eq(got_v2, expect_v2)
                    && py_dict_contains(loaded, expect_dead) == 0;

                pcc_gc_release(expect_k1);
                pcc_gc_release(expect_v1);
                pcc_gc_release(expect_k2);
                pcc_gc_release(expect_v2);
                pcc_gc_release(expect_dead);
                pcc_gc_release(got_v1);
                pcc_gc_release(got_v2);
                pcc_gc_store_root(&slot, 0);
                pcc_gc_scheduler_root_unregister(&slot);
                pcc_gc_release(moved);
                return ok;
            }

            int main(void) {
                if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) {
                    return 2;
                }
                printf("%d\\n", check_dict_relocation());
                return 0;
            }
            """
        ).lstrip()
    , encoding="utf-8")
    link = [
        _cc(),
        "-std=c11",
        f"-I{work_runtime / 'include'}",
        f"-I{work_runtime / 'src'}",
        str(src),
        str(work_runtime / archive_name),
    ]
    if extra_link_args:
        link.extend(extra_link_args)
    link.extend(["-o", str(exe)])
    build = subprocess.run(link, capture_output=True, text=True, timeout=30)
    assert build.returncode == 0, build.stderr

    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == ["1"]


def _assert_backend_four_instance_relocation_retains_owned_fields(
    tmp_path: Path,
    work_runtime: Path,
    archive_name: str,
    *,
    extra_link_args: list[str] | None = None,
):
    src = tmp_path / f"{archive_name}_instance_relocation_probe.c"
    exe = tmp_path / f"{archive_name}_instance_relocation_probe.out"
    src.write_text(
        textwrap.dedent(
            """
            #include "py_runtime.h"
            #include "py_internal.h"
            #include <stdint.h>
            #include <stdio.h>

            static int check_instance_relocation(void) {
                PyObject *slot = 0;
                pcc_gc_scheduler_root_register(&slot);

                const char *fields[1] = {"field"};
                PyClassObject *cls = py_class_new(
                    "RelocInstance", 0, 0, fields, 1
                );
                if (cls == 0) return 0;
                PyObject *inst = py_instance_new(cls);
                PyObject *field_value = py_str_new("field-value", 11);
                PyObject *dyn_value = py_str_new("dyn-value", 9);
                if (inst == 0 || field_value == 0 || dyn_value == 0) return 0;

                py_instance_set_field((PyInstanceObject *)inst, 0, field_value);
                if (
                    py_instance_setattr(
                        (PyInstanceObject *)inst, "dyn", dyn_value
                    ) != 0
                ) {
                    return 0;
                }
                pcc_gc_release(field_value);
                pcc_gc_release(dyn_value);

                int64_t inst_id = pcc_gc_object_id(inst);
                pcc_gc_store_root(&slot, inst);
                pcc_gc_release(inst);

                pcc_gc_reset_relocation_set();
                (void)pcc_gc_select_relocation_set(64);
                if (pcc_gc_relocation_set_contains(slot) != 1) return 0;

                PyInstanceObject *old_inst = (PyInstanceObject *)slot;
                PyObject *old_field = old_inst->fields[0];
                PyObject *old_dyn = old_inst->fields[1];
                if (old_field == 0 || old_dyn == 0) return 0;

                PyObject *moved = pcc_gc_relocate_copy(
                    slot, cls->instance_size
                );
                if (moved == 0 || moved == slot) return 0;

                PyInstanceObject *moved_inst = (PyInstanceObject *)moved;
                int fields_owned =
                    moved_inst->cls == cls
                    && moved_inst->fields[0] == old_field
                    && moved_inst->fields[1] == old_dyn
                    && py_header(old_field)->refcount >= 2
                    && py_header(old_dyn)->refcount >= 2;
                if (!fields_owned) return 0;

                PyObject *loaded = pcc_gc_load_ptr(0, &slot);
                PyObject *got_field = py_instance_get_field(
                    (PyInstanceObject *)loaded, 0
                );
                PyObject *got_dyn = py_instance_getattr(
                    (PyInstanceObject *)loaded, "dyn"
                );
                PyObject *expect_field = py_str_new("field-value", 11);
                PyObject *expect_dyn = py_str_new("dyn-value", 9);

                int ok = loaded == moved
                    && pcc_gc_object_id(loaded) == inst_id
                    && py_isinstance(loaded, cls) == 1
                    && got_field != 0
                    && got_dyn != 0
                    && expect_field != 0
                    && expect_dyn != 0
                    && py_obj_eq(got_field, expect_field)
                    && py_obj_eq(got_dyn, expect_dyn);

                pcc_gc_release(got_field);
                pcc_gc_release(got_dyn);
                pcc_gc_release(expect_field);
                pcc_gc_release(expect_dyn);
                pcc_gc_store_root(&slot, 0);
                pcc_gc_scheduler_root_unregister(&slot);
                pcc_gc_release(moved);
                pcc_gc_release((PyObject *)cls);
                pcc_gc_reset_relocation_set();
                return ok;
            }

            int main(void) {
                if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) {
                    return 2;
                }
                printf("%d\\n", check_instance_relocation());
                return 0;
            }
            """
        ).lstrip()
    , encoding="utf-8")
    link = [
        _cc(),
        "-std=c11",
        f"-I{work_runtime / 'include'}",
        f"-I{work_runtime / 'src'}",
        str(src),
        str(work_runtime / archive_name),
    ]
    if extra_link_args:
        link.extend(extra_link_args)
    link.extend(["-o", str(exe)])
    build = subprocess.run(link, capture_output=True, text=True, timeout=30)
    assert build.returncode == 0, build.stderr

    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == ["1"]


def _assert_backend_four_targets_wait_for_phase_reset(
    tmp_path: Path,
    work_runtime: Path,
    archive_name: str,
    *,
    extra_link_args: list[str] | None = None,
):
    src = tmp_path / f"{archive_name}_relocation_phase_probe.c"
    exe = tmp_path / f"{archive_name}_relocation_phase_probe.out"
    src.write_text(
        textwrap.dedent(
            """
            #include "py_runtime.h"
            #include "py_internal.h"
            #include <stdint.h>
            #include <stdio.h>
            #include <stdlib.h>
            extern void pcc_gc_publish_initialized(PyObject *obj);

            typedef struct {
                PyObjectHeader h;
                int64_t length;
                int64_t capacity;
                PyObject **items;
            } ProbeListObject;

            static PyObject *new_reloc_payload(void) {
                ProbeListObject *obj = (
                    ProbeListObject *
                )pcc_gc_alloc(64, PY_TYPE_LIST, 0);
                if (obj == 0) return 0;
                obj->length = 0;
                obj->capacity = 0;
                obj->items = 0;
                /* pcc_gc_alloc sets PY_FLAG_GC_FRESH_ALLOC for container tags
                 * and the relocation-set add refuses anything carrying it, so
                 * a raw payload that never publishes can never be forwarded. */
                pcc_gc_publish_initialized((PyObject *)obj);
                return (PyObject *)obj;
            }

            static int check_relocation_phase_progress(void) {
                PyObject *slot = 0;
                pcc_gc_scheduler_root_register(&slot);

                PyObject *old = new_reloc_payload();
                if (old == 0) return 0;
                pcc_gc_store_root(&slot, old);
                int64_t old_id = pcc_gc_object_id(old);
                if (old_id <= 0) return 0;

                pcc_gc_telemetry_reset();
                if (pcc_gc_step(2) <= 0) return 0;
                PyObject *moved = pcc_gc_load_ptr(0, &slot);
                if (moved == 0 || moved == old) return 0;
                if (pcc_gc_object_id(moved) != old_id) return 0;

                pcc_gc_release(old);
                int64_t forwards_before = pcc_gc_telemetry(
                    PCC_GC_COUNTER_RELOCATION_FORWARDS
                );
                int64_t work_after_free = 0;
                for (int i = 0; i < 12; i++) {
                    work_after_free += pcc_gc_step(2);
                }
                int64_t forwards_after = pcc_gc_telemetry(
                    PCC_GC_COUNTER_RELOCATION_FORWARDS
                );
                int same_phase_slot_stable = pcc_gc_load_ptr(0, &slot) == moved;
                (void)work_after_free;
                int same_phase_no_relocation =
                    forwards_after == forwards_before
                    && same_phase_slot_stable;

                pcc_gc_reset_relocation_set();
                int64_t selected_next_phase = pcc_gc_select_relocation_set(1);
                int next_phase_contains =
                    pcc_gc_relocation_set_contains(moved) == 1;
                int next_phase_can_select =
                    selected_next_phase == 1
                    && next_phase_contains;

                pcc_gc_store_root(&slot, 0);
                pcc_gc_scheduler_root_unregister(&slot);
                pcc_gc_reset_relocation_set();
                return same_phase_no_relocation && next_phase_can_select;
            }

            int main(void) {
                if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) {
                    return 2;
                }
                printf("%d\\n", check_relocation_phase_progress());
                return 0;
            }
            """
        ).lstrip()
    , encoding="utf-8")
    link = [
        _cc(),
        "-std=c11",
        f"-I{work_runtime / 'include'}",
        f"-I{work_runtime / 'src'}",
        str(src),
        str(work_runtime / archive_name),
    ]
    if extra_link_args:
        link.extend(extra_link_args)
    link.extend(["-o", str(exe)])
    build = subprocess.run(link, capture_output=True, text=True, timeout=30)
    assert build.returncode == 0, build.stderr

    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == ["1"]


def test_colored_relocating_task_and_scheduler_queue_follow_forwarding(tmp_path):
    work_runtime = _build_runtime(tmp_path)
    _assert_backend_four_task_and_scheduler_queue_follow_forwarding(
        tmp_path,
        work_runtime,
        "libpy_runtime.a",
    )


def test_pcc_python_colored_relocating_task_and_scheduler_queue_follow_forwarding(
    tmp_path,
):
    work_runtime = _build_pcc_py_runtime(tmp_path)
    _assert_backend_four_task_and_scheduler_queue_follow_forwarding(
        tmp_path,
        work_runtime,
        "libpy_runtime_pcc_py.a",
        extra_link_args=["-pthread"],
    )


def test_pcc_python_memoryview_owned_buffer_follows_relocation(tmp_path):
    work_runtime = _build_pcc_py_runtime(tmp_path)
    _assert_pcc_python_memoryview_owned_buffer_follows_relocation(
        tmp_path,
        work_runtime,
    )


def test_colored_relocating_list_copy_owns_item_array(tmp_path):
    work_runtime = _build_runtime(tmp_path)
    _assert_backend_four_list_relocation_copies_owned_items(
        tmp_path,
        work_runtime,
        "libpy_runtime.a",
    )


def test_pcc_python_colored_relocating_list_copy_owns_item_array(tmp_path):
    work_runtime = _build_pcc_py_runtime(tmp_path)
    _assert_backend_four_list_relocation_copies_owned_items(
        tmp_path,
        work_runtime,
        "libpy_runtime_pcc_py.a",
        extra_link_args=["-lpthread"],
    )


def test_colored_relocating_tuple_copy_retains_owned_items(tmp_path):
    work_runtime = _build_runtime(tmp_path)
    _assert_backend_four_tuple_relocation_retain_owned_items(
        tmp_path,
        work_runtime,
        "libpy_runtime.a",
    )


def test_pcc_python_colored_relocating_tuple_copy_retains_owned_items(tmp_path):
    work_runtime = _build_pcc_py_runtime(tmp_path)
    _assert_backend_four_tuple_relocation_retain_owned_items(
        tmp_path,
        work_runtime,
        "libpy_runtime_pcc_py.a",
        extra_link_args=["-lpthread"],
    )


def test_colored_relocating_task_copy_retains_state_slots(tmp_path):
    work_runtime = _build_runtime(tmp_path)
    _assert_backend_four_task_relocation_retains_state_slots(
        tmp_path,
        work_runtime,
        "libpy_runtime.a",
    )


def test_pcc_python_colored_relocating_task_copy_retains_state_slots(tmp_path):
    work_runtime = _build_pcc_py_runtime(tmp_path)
    _assert_backend_four_task_relocation_retains_state_slots(
        tmp_path,
        work_runtime,
        "libpy_runtime_pcc_py.a",
        extra_link_args=["-lpthread"],
    )


def test_colored_relocating_set_copy_retains_owned_entries(tmp_path):
    work_runtime = _build_runtime(tmp_path)
    _assert_backend_four_set_relocation_retains_owned_entries(
        tmp_path,
        work_runtime,
        "libpy_runtime.a",
    )


def test_pcc_python_colored_relocating_set_copy_retains_owned_entries(tmp_path):
    work_runtime = _build_pcc_py_runtime(tmp_path)
    _assert_backend_four_set_relocation_retains_owned_entries(
        tmp_path,
        work_runtime,
        "libpy_runtime_pcc_py.a",
        extra_link_args=["-lpthread"],
    )


def test_colored_relocating_dict_copy_retains_owned_tables(tmp_path):
    work_runtime = _build_runtime(tmp_path)
    _assert_backend_four_dict_relocation_retains_owned_tables(
        tmp_path,
        work_runtime,
        "libpy_runtime.a",
    )


def test_pcc_python_colored_relocating_dict_copy_retains_owned_tables(tmp_path):
    work_runtime = _build_pcc_py_runtime(tmp_path)
    _assert_backend_four_dict_relocation_retains_owned_tables(
        tmp_path,
        work_runtime,
        "libpy_runtime_pcc_py.a",
        extra_link_args=["-lpthread"],
    )


def test_colored_relocating_instance_copy_retains_owned_fields(tmp_path):
    work_runtime = _build_runtime(tmp_path)
    _assert_backend_four_instance_relocation_retains_owned_fields(
        tmp_path,
        work_runtime,
        "libpy_runtime.a",
    )


def test_pcc_python_colored_relocating_instance_copy_retains_owned_fields(tmp_path):
    work_runtime = _build_pcc_py_runtime(tmp_path)
    _assert_backend_four_instance_relocation_retains_owned_fields(
        tmp_path,
        work_runtime,
        "libpy_runtime_pcc_py.a",
        extra_link_args=["-lpthread"],
    )


def test_colored_relocating_targets_wait_for_phase_reset(tmp_path):
    work_runtime = _build_runtime(tmp_path)
    _assert_backend_four_targets_wait_for_phase_reset(
        tmp_path,
        work_runtime,
        "libpy_runtime.a",
    )


def test_pcc_python_colored_relocating_targets_wait_for_phase_reset(tmp_path):
    work_runtime = _build_pcc_py_runtime(tmp_path)
    _assert_backend_four_targets_wait_for_phase_reset(
        tmp_path,
        work_runtime,
        "libpy_runtime_pcc_py.a",
        extra_link_args=["-lpthread"],
    )


def test_colored_relocating_load_barrier_follows_forwarding_entry(tmp_path):
    exe = _compile_probe(
        tmp_path,
        """
        from pcc.extern import extern, c_int32, c_int64, c_ptr, c_void, c_obj
        from pcc.unsafe import free, malloc, null, ptr_eq, store_ptr

        pcc_gc_alloc = extern("pcc_gc_alloc", (c_int64, c_int32, c_int32), c_obj)
        pcc_gc_release = extern("pcc_gc_release", (c_ptr,), c_void)
        pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_obj)
        pcc_gc_store_ptr = extern("pcc_gc_store_ptr", (c_ptr, c_ptr, c_ptr), c_void)
        pcc_gc_install_forwarding = extern("pcc_gc_install_forwarding", (c_ptr, c_ptr), c_int64)
        pcc_gc_telemetry = extern("pcc_gc_telemetry", (c_int64,), c_int64)
        pcc_gc_telemetry_reset = extern("pcc_gc_telemetry_reset", (), c_void)

        def main() -> None:
            pcc_gc_telemetry_reset()
            old = pcc_gc_alloc(64, 5, 0)
            new = pcc_gc_alloc(64, 5, 0)
            slot = malloc(8)
            store_ptr(slot, 0, null())
            pcc_gc_store_ptr(null(), slot, old)
            print(pcc_gc_install_forwarding(old, new))
            print(pcc_gc_telemetry(15))
            loaded = pcc_gc_load_ptr(null(), slot)
            print(ptr_eq(loaded, new))
            print(pcc_gc_telemetry(16))
            pcc_gc_store_ptr(null(), slot, null())
            pcc_gc_release(old)
            pcc_gc_release(new)
            free(slot)

        if __name__ == "__main__":
            main()
        """,
    )
    result = _run_backend_four(exe)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == ["0", "1", "True", "1"]


def test_colored_relocating_rejects_forwarding_for_pinned_objects(tmp_path):
    exe = _compile_probe(
        tmp_path,
        """
        from pcc.extern import extern, c_int32, c_int64, c_ptr, c_void, c_obj

        pcc_gc_alloc = extern("pcc_gc_alloc", (c_int64, c_int32, c_int32), c_obj)
        pcc_gc_release = extern("pcc_gc_release", (c_ptr,), c_void)
        pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)
        pcc_gc_unpin = extern("pcc_gc_unpin", (c_ptr,), c_void)
        pcc_gc_install_forwarding = extern("pcc_gc_install_forwarding", (c_ptr, c_ptr), c_int64)
        pcc_gc_telemetry = extern("pcc_gc_telemetry", (c_int64,), c_int64)
        pcc_gc_telemetry_reset = extern("pcc_gc_telemetry_reset", (), c_void)

        def main() -> None:
            pcc_gc_telemetry_reset()
            old = pcc_gc_alloc(64, 5, 0)
            new = pcc_gc_alloc(64, 5, 0)
            pcc_gc_pin(old)
            print(pcc_gc_install_forwarding(old, new))
            print(pcc_gc_telemetry(17))
            pcc_gc_unpin(old)
            pcc_gc_release(old)
            pcc_gc_release(new)

        if __name__ == "__main__":
            main()
        """,
    )
    result = _run_backend_four(exe)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == ["-2", "1"]


def test_colored_relocating_stable_id_survives_forwarding(tmp_path):
    exe = _compile_probe(
        tmp_path,
        """
        from pcc.extern import extern, c_int32, c_int64, c_ptr, c_void, c_obj
        from pcc.unsafe import free, malloc, null, ptr_eq, store_ptr

        pcc_gc_alloc = extern("pcc_gc_alloc", (c_int64, c_int32, c_int32), c_obj)
        pcc_gc_release = extern("pcc_gc_release", (c_ptr,), c_void)
        pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_obj)
        pcc_gc_store_ptr = extern("pcc_gc_store_ptr", (c_ptr, c_ptr, c_ptr), c_void)
        pcc_gc_install_forwarding = extern("pcc_gc_install_forwarding", (c_ptr, c_ptr), c_int64)
        pcc_gc_object_id = extern("pcc_gc_object_id", (c_ptr,), c_int64)

        def main() -> None:
            old = pcc_gc_alloc(64, 5, 0)
            new = pcc_gc_alloc(64, 5, 0)
            old_id = pcc_gc_object_id(old)
            print(old_id > 0)
            print(pcc_gc_install_forwarding(old, new))
            print(pcc_gc_object_id(old) == old_id)
            print(pcc_gc_object_id(new) == old_id)
            slot = malloc(8)
            store_ptr(slot, 0, null())
            pcc_gc_store_ptr(null(), slot, old)
            loaded = pcc_gc_load_ptr(null(), slot)
            print(ptr_eq(loaded, new))
            print(pcc_gc_object_id(loaded) == old_id)
            pcc_gc_store_ptr(null(), slot, null())
            pcc_gc_release(old)
            pcc_gc_release(new)
            free(slot)

        if __name__ == "__main__":
            main()
        """,
    )
    result = _run_backend_four(exe)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == [
        "True",
        "0",
        "True",
        "True",
        "True",
        "True",
    ]


def test_colored_relocating_selects_unpinned_relocation_set(tmp_path):
    exe = _compile_probe(
        tmp_path,
        """
        from pcc.extern import extern, c_int32, c_int64, c_ptr, c_void, c_obj

        pcc_gc_alloc = extern("pcc_gc_alloc", (c_int64, c_int32, c_int32), c_obj)
        pcc_gc_release = extern("pcc_gc_release", (c_ptr,), c_void)
        pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)
        pcc_gc_unpin = extern("pcc_gc_unpin", (c_ptr,), c_void)
        pcc_gc_reset_relocation_set = extern("pcc_gc_reset_relocation_set", (), c_void)
        pcc_gc_select_relocation_set = extern("pcc_gc_select_relocation_set", (c_int64,), c_int64)
        pcc_gc_relocation_set_contains = extern("pcc_gc_relocation_set_contains", (c_ptr,), c_int64)
        pcc_gc_relocation_set_size = extern("pcc_gc_relocation_set_size", (), c_int64)
        pcc_gc_publish_initialized = extern("pcc_gc_publish_initialized", (c_ptr,), c_void)

        def main() -> None:
            # The compiled runtime can own legitimate relocatable startup
            # objects.  Measure that live baseline instead of assuming an
            # otherwise-empty heap, then prove the three allocations below
            # contribute exactly the two unpinned candidates.
            pcc_gc_reset_relocation_set()
            baseline = pcc_gc_select_relocation_set(1000)
            pcc_gc_reset_relocation_set()
            a = pcc_gc_alloc(64, 5, 0)
            b = pcc_gc_alloc(64, 5, 0)
            c = pcc_gc_alloc(64, 5, 0)
            # pcc_gc_alloc hands back a half-initialized object carrying
            # PY_FLAG_GC_FRESH_ALLOC, and the selector refuses a fresh owner
            # outright -- relocating a half-built object would copy garbage
            # slots.  pcc_gc_publish_initialized is the contract's release
            # valve; the C probes in this file already call it right after
            # allocating.  Without it all three stay invisible to the
            # relocation set and this probe measures the flag, not pinning.
            pcc_gc_publish_initialized(a)
            pcc_gc_publish_initialized(b)
            pcc_gc_publish_initialized(c)
            pcc_gc_pin(b)
            pcc_gc_reset_relocation_set()
            print(pcc_gc_select_relocation_set(1000) - baseline)
            print(pcc_gc_relocation_set_size() - baseline)
            print(pcc_gc_relocation_set_contains(a))
            print(pcc_gc_relocation_set_contains(b))
            print(pcc_gc_relocation_set_contains(c))
            print(pcc_gc_select_relocation_set(1000))
            pcc_gc_reset_relocation_set()
            print(pcc_gc_relocation_set_size())
            print(pcc_gc_relocation_set_contains(a))
            pcc_gc_unpin(b)
            pcc_gc_release(a)
            pcc_gc_release(b)
            pcc_gc_release(c)

        if __name__ == "__main__":
            main()
        """,
    )
    result = _run_backend_four(exe)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == [
        "2",
        "2",
        "1",
        "0",
        "1",
        "0",
        "0",
        "0",
    ]


def test_colored_relocating_copy_forwards_selected_payload_object(tmp_path):
    exe = _compile_probe(
        tmp_path,
        """
        from pcc.extern import extern, c_int32, c_int64, c_ptr, c_void, c_obj
        from pcc.unsafe import malloc, null, ptr_eq, ptr_is_null, store_ptr, free

        pcc_gc_alloc = extern("pcc_gc_alloc", (c_int64, c_int32, c_int32), c_obj)
        pcc_gc_release = extern("pcc_gc_release", (c_ptr,), c_void)
        pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_obj)
        pcc_gc_store_ptr = extern("pcc_gc_store_ptr", (c_ptr, c_ptr, c_ptr), c_void)
        pcc_gc_object_id = extern("pcc_gc_object_id", (c_ptr,), c_int64)
        pcc_gc_reset_relocation_set = extern("pcc_gc_reset_relocation_set", (), c_void)
        pcc_gc_select_relocation_set = extern("pcc_gc_select_relocation_set", (c_int64,), c_int64)
        pcc_gc_relocate_copy = extern("pcc_gc_relocate_copy", (c_ptr, c_int64), c_obj)
        pcc_gc_relocation_set_contains = extern("pcc_gc_relocation_set_contains", (c_ptr,), c_int64)
        pcc_gc_publish_initialized = extern("pcc_gc_publish_initialized", (c_ptr,), c_void)

        def main() -> None:
            old = pcc_gc_alloc(64, 5, 0)
            # A raw pcc_gc_alloc result carries PY_FLAG_GC_FRESH_ALLOC and the
            # selector refuses a fresh owner outright: relocating a
            # half-initialized object would copy garbage slots.  Publishing is
            # the contract's release valve, and the C probes earlier in this
            # file already call it right after allocating.
            pcc_gc_publish_initialized(old)
            old_id = pcc_gc_object_id(old)
            pcc_gc_reset_relocation_set()
            # Selection is per page, chosen by fragmentation score, so a
            # budget of 1 selects one object from the best-scoring page --
            # which need not be the page old landed on.  Give the budget room
            # and assert containment directly.
            pcc_gc_select_relocation_set(1000)
            print(pcc_gc_relocation_set_contains(old))
            moved = pcc_gc_relocate_copy(old, 64)
            print(ptr_is_null(moved))
            print(ptr_eq(old, moved))
            print(pcc_gc_object_id(moved) == old_id)
            slot = malloc(8)
            store_ptr(slot, 0, null())
            pcc_gc_store_ptr(null(), slot, old)
            loaded = pcc_gc_load_ptr(null(), slot)
            print(ptr_eq(loaded, moved))
            print(pcc_gc_object_id(loaded) == old_id)
            pcc_gc_store_ptr(null(), slot, null())
            pcc_gc_release(old)
            pcc_gc_release(moved)
            free(slot)

        if __name__ == "__main__":
            main()
        """,
    )
    result = _run_backend_four(exe)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == [
        "1",
        "False",
        "True",
        "True",
        "True",
        "True",
    ]


def test_colored_relocating_copy_consumes_relocation_entry(tmp_path):
    exe = _compile_probe(
        tmp_path,
        """
        from pcc.extern import extern, c_int32, c_int64, c_ptr, c_void, c_obj
        from pcc.unsafe import ptr_is_null

        pcc_gc_alloc = extern("pcc_gc_alloc", (c_int64, c_int32, c_int32), c_obj)
        pcc_gc_release = extern("pcc_gc_release", (c_ptr,), c_void)
        pcc_gc_reset_relocation_set = extern("pcc_gc_reset_relocation_set", (), c_void)
        pcc_gc_select_relocation_set = extern("pcc_gc_select_relocation_set", (c_int64,), c_int64)
        pcc_gc_relocation_set_contains = extern("pcc_gc_relocation_set_contains", (c_ptr,), c_int64)
        pcc_gc_relocate_copy = extern("pcc_gc_relocate_copy", (c_ptr, c_int64), c_obj)
        pcc_gc_publish_initialized = extern("pcc_gc_publish_initialized", (c_ptr,), c_void)

        def main() -> None:
            old = pcc_gc_alloc(64, 5, 0)
            # A raw pcc_gc_alloc result carries PY_FLAG_GC_FRESH_ALLOC and the
            # selector refuses a fresh owner outright: relocating a
            # half-initialized object would copy garbage slots.  Publishing is
            # the contract's release valve, and the C probes earlier in this
            # file already call it right after allocating.
            pcc_gc_publish_initialized(old)
            pcc_gc_reset_relocation_set()
            pcc_gc_select_relocation_set(1000)
            moved = pcc_gc_relocate_copy(old, 64)
            print(ptr_is_null(moved))
            print(pcc_gc_relocation_set_contains(old))
            moved_again = pcc_gc_relocate_copy(old, 64)
            print(ptr_is_null(moved_again))
            if ptr_is_null(moved_again) == 0:
                pcc_gc_release(moved_again)
            pcc_gc_release(moved)
            pcc_gc_release(old)

        if __name__ == "__main__":
            main()
        """,
    )
    result = _run_backend_four(exe)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == ["False", "0", "True"]


def test_colored_relocating_step_forwards_selected_payload_object(tmp_path):
    exe = _compile_probe(
        tmp_path,
        """
        from pcc.extern import extern, c_int32, c_int64, c_ptr, c_void, c_obj
        from pcc.unsafe import free, load_ptr, malloc, null, ptr_eq, store_ptr

        pcc_gc_alloc = extern("pcc_gc_alloc", (c_int64, c_int32, c_int32), c_obj)
        pcc_gc_release = extern("pcc_gc_release", (c_ptr,), c_void)
        pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_obj)
        pcc_gc_store_ptr = extern("pcc_gc_store_ptr", (c_ptr, c_ptr, c_ptr), c_void)
        pcc_gc_object_id = extern("pcc_gc_object_id", (c_ptr,), c_int64)
        pcc_gc_reset_relocation_set = extern("pcc_gc_reset_relocation_set", (), c_void)
        pcc_gc_select_relocation_set = extern("pcc_gc_select_relocation_set", (c_int64,), c_int64)
        pcc_gc_relocation_set_contains = extern("pcc_gc_relocation_set_contains", (c_ptr,), c_int64)
        pcc_gc_step = extern("pcc_gc_step", (c_int64,), c_int64)
        pcc_gc_telemetry = extern("pcc_gc_telemetry", (c_int64,), c_int64)
        pcc_gc_telemetry_reset = extern("pcc_gc_telemetry_reset", (), c_void)
        pcc_gc_publish_initialized = extern("pcc_gc_publish_initialized", (c_ptr,), c_void)

        def main() -> None:
            old = pcc_gc_alloc(64, 5, 0)
            # A raw pcc_gc_alloc result carries PY_FLAG_GC_FRESH_ALLOC and the
            # selector refuses a fresh owner outright: relocating a
            # half-initialized object would copy garbage slots.  Publishing is
            # the contract's release valve, and the C probes earlier in this
            # file already call it right after allocating.
            pcc_gc_publish_initialized(old)
            old_id = pcc_gc_object_id(old)
            slot = malloc(8)
            store_ptr(slot, 0, null())
            pcc_gc_store_ptr(null(), slot, old)
            raw_slot = malloc(8)
            # Keep the original address outside the registered root/slot
            # surface so the post-step assertion proves an actual move.
            store_ptr(raw_slot, 0, old)
            pcc_gc_reset_relocation_set()
            print(pcc_gc_select_relocation_set(1000) > 0)
            print(pcc_gc_relocation_set_contains(old))
            pcc_gc_telemetry_reset()
            print(pcc_gc_step(1000) > 0)
            print(pcc_gc_telemetry(15) >= 1)
            loaded = pcc_gc_load_ptr(null(), slot)
            print(ptr_eq(loaded, load_ptr(raw_slot, 0)))
            print(pcc_gc_object_id(loaded) == old_id)
            print(pcc_gc_telemetry(16) >= 1)
            pcc_gc_store_ptr(null(), slot, null())
            pcc_gc_release(old)
            free(raw_slot)
            free(slot)

        if __name__ == "__main__":
            main()
        """,
    )
    result = _run_backend_four(exe)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == [
        "True",
        "1",
        "True",
        "True",
        "False",
        "True",
        "True",
    ]
