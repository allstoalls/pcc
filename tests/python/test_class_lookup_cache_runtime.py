from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path


from tests.runtime_build_cache import cached_threaded_pcc_python_runtime


REPO = Path(__file__).absolute().parents[2]
RUNTIME = REPO / "pcc" / "runtime"


def _compile_and_run(
    tmp_path: Path,
    name: str,
    source: str,
    runtime_archive: Path,
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    src = tmp_path / f"{name}.c"
    exe = tmp_path / name
    src.write_text(textwrap.dedent(source), encoding="utf-8")
    command = [
        os.environ.get("CC", "cc"),
        "-std=c11",
        "-pthread",
        f"-I{RUNTIME / 'include'}",
        f"-I{RUNTIME / 'src'}",
        str(src),
        str(runtime_archive),
        "-lm",
        "-o",
        str(exe),
    ]
    if sys.platform.startswith("linux"):
        command.insert(-2, "-ldl")
    built = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert built.returncode == 0, built.stdout + built.stderr
    return subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


def test_class_lookup_uses_relocation_safe_linear_walk() -> None:
    internal_header = (RUNTIME / "src" / "py_internal.h").read_text(
        encoding="utf-8"
    )
    py_source = (RUNTIME / "py" / "py_class.py").read_text(encoding="utf-8")
    substrate_source = (RUNTIME / "py" / "py_substrate.py").read_text(
        encoding="utf-8"
    )
    py_gc_source = (
        RUNTIME / "py" / "freestanding_gc_relocation_copy.py"
    ).read_text(encoding="utf-8")
    normalized_header = " ".join(internal_header.replace("*", " ").split())

    assert "_class_lookup_cache_block" not in py_source
    assert "_class_lookup_cache_slot" not in py_source
    assert "pcc_class_lookup_cache" not in substrate_source
    assert "while i < n_mro_i32:" in py_source
    assert "while j < n_methods_i32:" in py_source
    assert "stored_hash == name_hash" in py_source
    assert "_strs_eq(m_name, name) != 0" in py_source
    assert "pcc_gc_load_ptr(cls, ptr_add(mro, i * 8))" in py_source
    assert "pcc_gc_note_relocation_read(load_ptr(method_slot, 0))" in py_source
    assert "if tag == PY_TYPE_CLASS:" in py_gc_source
    assert 'global_addr("py_class_attr_cache_epoch")' in py_gc_source
    assert "must remain immutable" in normalized_header
    assert "must not race with lookup" in normalized_header


def test_class_lookup_preserves_shadowing_and_delete_epoch(
    tmp_path: Path,
    pcc_runtime_archive: Path,
) -> None:
    source = r'''
        #include "py_internal.h"
        #include <stdint.h>
        #include <string.h>

        extern int32_t py_class_attr_cache_epoch;
        static const char run_name[] = "run";

        int main(void) {
            if (pcc_gc_set_backend(CACHE_BACKEND) != 0) return 1;
            PyClassObject *base = py_class_new("Base", NULL, 0, NULL, 0);
            if (base == NULL) return 2;
            PyClassObject *bases[1] = {base};
            PyClassObject *child = py_class_new("Child", bases, 1, NULL, 0);
            if (child == NULL) return 3;
            PyObject *base_func = py_int_from_i64(11);
            PyObject *child_func = py_int_from_i64(22);
            py_class_add_method(base, run_name, base_func);

            if (py_class_lookup(child, run_name) != base_func) return 4;
            if (py_class_lookup(child, run_name) != base_func) return 5;

            py_class_add_method(child, run_name, child_func);
            if (py_class_lookup(child, run_name) != child_func) return 6;
            if (py_class_lookup(child, run_name) != child_func) return 7;

            PyClassObject *reuse = py_class_new("Reuse", NULL, 0, NULL, 0);
            if (reuse == NULL) return 8;
            PyObject *alpha_func = py_int_from_i64(33);
            PyObject *beta_func = py_int_from_i64(44);
            py_class_add_method(reuse, "alpha", alpha_func);
            py_class_add_method(reuse, "beta", beta_func);
            char mutable_name[16] = "alpha";
            if (py_class_lookup(reuse, mutable_name) != alpha_func) return 9;
            memcpy(mutable_name, "beta", 5);
            if (py_class_lookup(reuse, mutable_name) != beta_func) return 10;

            PyObject *attr = py_int_from_i64(55);
            if (py_class_setattr(reuse, "temporary", attr) != 0) return 11;
            int32_t before_delete = __atomic_load_n(
                &py_class_attr_cache_epoch, __ATOMIC_ACQUIRE
            );
            if (py_class_delattr(reuse, "temporary") != 0) return 12;
            int32_t after_delete = __atomic_load_n(
                &py_class_attr_cache_epoch, __ATOMIC_ACQUIRE
            );
            if (after_delete == before_delete) return 13;
            /* Equal FNV-1a length/hash; full-name comparison still decides. */
            PyObject *collision_a = py_int_from_i64(66);
            PyObject *collision_b = py_int_from_i64(77);
            py_class_add_method(reuse, "jahaxiff", collision_a);
            py_class_add_method(reuse, "vzekpekw", collision_b);
            if (py_class_lookup(reuse, "jahaxiff") != collision_a) return 14;
            if (py_class_lookup(reuse, "vzekpekw") != collision_b) return 15;
            return 0;
        }
        '''
    for backend in range(5):
        backend_source = source.replace("CACHE_BACKEND", str(backend))
        for runtime_name, archive in (
            ("pcc_py", pcc_runtime_archive),
        ):
            result = _compile_and_run(
                tmp_path,
                f"class_lookup_backend{backend}_{runtime_name}",
                backend_source,
                archive,
            )
            assert result.returncode == 0, (
                f"backend={backend} runtime={runtime_name}: "
                + result.stdout
                + result.stderr
            )


def test_exposing_mutable_class_attrs_invalidates_field_cache_epoch(
    tmp_path: Path, pcc_runtime_archive: Path,
) -> None:
    source = r'''
        #include "py_internal.h"
        #include <stdint.h>

        extern int32_t py_class_attr_cache_epoch;

        int main(void) {
            if (pcc_gc_set_backend(CACHE_BACKEND) != 0) return 1;
            PyClassObject *cls = py_class_new("Mutable", NULL, 0, NULL, 0);
            if (cls == NULL) return 2;
            int32_t before = __atomic_load_n(
                &py_class_attr_cache_epoch, __ATOMIC_ACQUIRE
            );
            /* Every class owns its __doc__ entry from creation; reading the
               namespace without asking for the mutable dict exposes nothing. */
            if (py_class_attrs_dict(cls, 0) == NULL) return 3;
            if (__atomic_load_n(&py_class_attr_cache_epoch, __ATOMIC_ACQUIRE)
                != before) return 8;
            PyObject *attrs = py_class_attrs_dict(cls, 1);
            if (attrs == NULL) return 4;
            int32_t exposed = __atomic_load_n(
                &py_class_attr_cache_epoch, __ATOMIC_ACQUIRE
            );
            if (exposed != before + 1) return 5;
            if (py_class_attrs_dict(cls, 1) != attrs) return 6;
            if (__atomic_load_n(&py_class_attr_cache_epoch, __ATOMIC_ACQUIRE)
                != exposed) return 7;
            return 0;
        }
    '''
    for backend in range(5):
        for runtime_name, archive in (
            ("pcc_py", pcc_runtime_archive),
        ):
            result = _compile_and_run(
                tmp_path,
                f"class_attrs_expose_backend{backend}_{runtime_name}",
                source.replace("CACHE_BACKEND", str(backend)), archive,
            )
            assert result.returncode == 0, (
                f"backend={backend} runtime={runtime_name}: "
                + result.stdout + result.stderr
            )


def test_instance_field_cache_reloads_values_after_class_dict_exposure(
    tmp_path: Path, pcc_runtime_archive: Path,
) -> None:
    source = r'''
        #include "py_internal.h"
        #include <stdint.h>

        extern int32_t py_class_attr_cache_epoch;

        int main(void) {
            if (pcc_gc_set_backend(CACHE_BACKEND) != 0) return 1;
            const char *fields[] = {"value"};
            PyClassObject *cls = py_class_new("Item", NULL, 0, fields, 1);
            if (cls == NULL) return 2;
            PyInstanceObject *inst = py_instance_new(cls);
            if (inst == NULL) return 3;
            PyObject *first = py_int_from_i64(42);
            py_instance_set_field(inst, 0, first);
            py_decref(first);
            PyObject *read = py_obj_getattr((PyObject *)inst, "value");
            if (read == NULL || py_int_value_i64(read) != 42) return 4;
            py_decref(read);
            PyObject *second = py_int_from_i64(43);
            py_instance_set_field(inst, 0, second);
            py_decref(second);
            read = py_obj_getattr((PyObject *)inst, "value");
            if (read == NULL || py_int_value_i64(read) != 43) return 6;
            py_decref(read);
            int32_t cached_epoch = __atomic_load_n(
                &py_class_attr_cache_epoch, __ATOMIC_ACQUIRE
            );
            if (py_class_attrs_dict(cls, 1) == NULL) return 7;
            if (__atomic_load_n(&py_class_attr_cache_epoch, __ATOMIC_ACQUIRE)
                != cached_epoch + 1) return 9;
            read = py_obj_getattr((PyObject *)inst, "value");
            if (read == NULL || py_int_value_i64(read) != 43) return 8;
            py_decref(read);
            return 0;
        }
    '''
    for backend in range(5):
        for runtime_name, archive in (
            ("pcc_py", pcc_runtime_archive),
        ):
            result = _compile_and_run(
                tmp_path,
                f"callsite_field_backend{backend}_{runtime_name}",
                source.replace("CACHE_BACKEND", str(backend)), archive,
            )
            assert result.returncode == 0, (
                f"backend={backend} runtime={runtime_name}: "
                + result.stdout + result.stderr
            )


def test_user_instance_getattr_keeps_field_and_class_attrs(
    tmp_path: Path, pcc_runtime_archive: Path,
) -> None:
    source = r'''
        #include "py_internal.h"

        int main(void) {
            if (pcc_gc_set_backend(CACHE_BACKEND) != 0) return 1;
            const char *fields[] = {"value"};
            PyClassObject *cls = py_class_new("Item", NULL, 0, fields, 1);
            if (cls == NULL) return 2;
            PyInstanceObject *inst = (PyInstanceObject *)py_instance_new(cls);
            if (inst == NULL) return 3;
            PyObject *value = py_int_from_i64(42);
            py_instance_set_field(inst, 0, value);
            py_decref(value);
            for (int i = 0; i < 32; i++) {
                PyObject *got = py_obj_getattr((PyObject *)inst, "value");
                if (got == NULL || py_int_value_i64(got) != 42) return 4;
                py_decref(got);
            }
            PyObject *extra = py_int_from_i64(77);
            if (py_class_setattr(cls, "extra", extra) != 0) return 5;
            py_decref(extra);
            PyObject *got = py_obj_getattr((PyObject *)inst, "extra");
            if (got == NULL || py_int_value_i64(got) != 77) return 6;
            py_decref(got);
            if (py_class_delattr(cls, "extra") != 0) return 7;
            got = py_obj_getattr((PyObject *)inst, "value");
            if (got == NULL || py_int_value_i64(got) != 42) return 8;
            py_decref(got);
            return 0;
        }
    '''
    for backend in range(5):
        for runtime_name, archive in (
            ("pcc_py", pcc_runtime_archive),
        ):
            result = _compile_and_run(
                tmp_path,
                f"early_getattr_backend{backend}_{runtime_name}",
                source.replace("CACHE_BACKEND", str(backend)), archive,
            )
            assert result.returncode == 0, (
                f"backend={backend} runtime={runtime_name}: "
                + result.stdout + result.stderr
            )


def test_class_lookup_reloads_relocated_method_and_class(
    tmp_path: Path,
    pcc_runtime_archive: Path,
) -> None:
    source = r'''
        #include "py_internal.h"
        #include <stdint.h>
        #include <stdlib.h>

        extern int32_t py_class_attr_cache_epoch;

        static int force_minor_refill(void) {
            int64_t before = pcc_gc_telemetry(PCC_GC_COUNTER_MINOR_COLLECTIONS);
            for (int i = 0; i < 64; i++) {
                PyObject *filler = py_str_new("filler", 6);
                if (filler == NULL) return 0;
                pcc_gc_release(filler);
            }
            return pcc_gc_telemetry(PCC_GC_COUNTER_MINOR_COLLECTIONS) > before;
        }

        int main(void) {
            if (pcc_gc_set_backend(RELOC_BACKEND) != 0) return 1;
            PyObject *class_root = NULL;
            pcc_gc_scheduler_root_register(&class_root);

            PyClassObject *cls = py_class_new("Reloc", NULL, 0, NULL, 0);
            if (cls == NULL) return 2;
            pcc_gc_store_root(&class_root, (PyObject *)cls);

            if (RELOC_BACKEND == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR) {
                if (!force_minor_refill()) return 3;
            }
            cls = (PyClassObject *)pcc_gc_load_ptr(NULL, &class_root);
            if (cls == NULL) return 5;
            if (RELOC_BACKEND == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR) {
                if ((cls->h.flags & PY_FLAG_GC_OLD) == 0 ||
                    (cls->h.flags & PY_FLAG_GC_YOUNG) != 0 ||
                    (cls->h.flags & PY_FLAG_GC_MINOR_ARENA) == 0) return 17;
                pcc_gc_pin((PyObject *)cls);
            }

            PyObject *method = NULL;
            static const char run_name[] = "run";
            if (RELOC_BACKEND == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR) {
                method = py_str_new("method", 6);
            } else {
                method = py_list_new(0);
            }
            if (method == NULL) return 6;
            py_class_add_method(cls, run_name, method);
            if (py_class_lookup(cls, run_name) != method) return 7;
            PyObject *old_method = method;

            if (RELOC_BACKEND == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR) {
                if (!force_minor_refill()) return 8;
            } else {
                pcc_gc_reset_relocation_set();
                if (pcc_gc_select_relocation_set(65536) <= 0) return 9;
                if (pcc_gc_relocation_set_contains(old_method) != 1) return 9;
                if (pcc_gc_relocation_set_contains((PyObject *)cls) != 1) {
                    return 9;
                }
                PyObject *moved_method = pcc_gc_relocate_copy(
                    old_method, (int64_t)sizeof(PyListObject)
                );
                if (moved_method == NULL) return 9;
                pcc_gc_release(moved_method);
            }
            PyObject *forwarded = pcc_gc_note_relocation_read(old_method);
            if (forwarded == NULL || forwarded == old_method) return 10;
            if (py_class_lookup(cls, run_name) != forwarded) return 11;
            if (cls->methods[0].func != forwarded) return 12;

            if (RELOC_BACKEND == PCC_GC_KIND_COLORED_RELOCATING) {
                int32_t before_class_move = __atomic_load_n(
                    &py_class_attr_cache_epoch, __ATOMIC_ACQUIRE
                );
                PyClassObject *old_cls = cls;
                PyObject *moved_cls = pcc_gc_relocate_copy(
                    (PyObject *)old_cls, (int64_t)sizeof(PyClassObject)
                );
                if (moved_cls == NULL) return 13;
                pcc_gc_release(moved_cls);
                cls = (PyClassObject *)pcc_gc_load_ptr(NULL, &class_root);
                if (cls == NULL || cls == old_cls) return 14;
                int32_t after_class_move = __atomic_load_n(
                    &py_class_attr_cache_epoch, __ATOMIC_ACQUIRE
                );
                if (after_class_move == before_class_move) return 15;
                if (py_class_lookup(cls, run_name) != forwarded) return 16;
            }

            if (RELOC_BACKEND == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR) {
                pcc_gc_unpin((PyObject *)cls);
            }
            pcc_gc_store_root(&class_root, NULL);
            pcc_gc_scheduler_root_unregister(&class_root);
            pcc_gc_release(forwarded);
            pcc_gc_release((PyObject *)cls);
            return 0;
        }
        '''
    env = os.environ.copy()
    env.update(
        {
            "PCC_GC_MINOR_HEAP_SIZE": "256",
            "PCC_GC_MINOR_ALLOC_MAX": "128",
        }
    )
    for backend in (3, 4):
        backend_source = source.replace("RELOC_BACKEND", str(backend))
        for runtime_name, archive in (
            ("pcc_py", pcc_runtime_archive),
        ):
            result = _compile_and_run(
                tmp_path,
                f"class_lookup_relocation_backend{backend}_{runtime_name}",
                backend_source,
                archive,
                env=env,
            )
            assert result.returncode == 0, (
                f"backend={backend} runtime={runtime_name}: "
                + result.stdout
                + result.stderr
            )


def test_class_lookup_concurrent_reads_are_stable_for_immutable_classes(
    tmp_path: Path,
) -> None:
    source = r'''
        #include "py_internal.h"
        #include <pthread.h>
        #include <stdint.h>

        typedef struct {
            PyClassObject *cls;
            const char *name;
            PyObject *expected;
        } Lookup;

        typedef struct {
            Lookup *lookup;
            int *start;
            int *failed;
        } WorkerArgs;

        static void *lookup_worker(void *opaque) {
            WorkerArgs *args = (WorkerArgs *)opaque;
            while (__atomic_load_n(args->start, __ATOMIC_ACQUIRE) == 0) {}
            for (int i = 0; i < 200000; i++) {
                PyObject *got = py_class_lookup(
                    args->lookup->cls,
                    args->lookup->name
                );
                if (got != args->lookup->expected) {
                    __atomic_store_n(args->failed, 1, __ATOMIC_RELEASE);
                    return NULL;
                }
            }
            return NULL;
        }

        int main(void) {
            static const char left_name[] = "left";
            static const char right_name[] = "right";
            PyClassObject *base = py_class_new("Base", NULL, 0, NULL, 0);
            if (base == NULL) return 2;
            PyClassObject *bases[1] = {base};
            PyClassObject *child = py_class_new("Child", bases, 1, NULL, 0);
            if (child == NULL) return 3;
            PyObject *left_func = py_int_from_i64(11);
            PyObject *right_func = py_int_from_i64(22);
            py_class_add_method(base, left_name, left_func);
            py_class_add_method(child, right_name, right_func);
            Lookup left = {child, left_name, left_func};
            Lookup right = {child, right_name, right_func};

            int start = 0;
            int failed = 0;
            WorkerArgs left_args = {&left, &start, &failed};
            WorkerArgs right_args = {&right, &start, &failed};
            pthread_t left_thread;
            pthread_t right_thread;
            if (pthread_create(
                    &left_thread, NULL, lookup_worker, &left_args
                ) != 0) return 5;
            if (pthread_create(
                    &right_thread, NULL, lookup_worker, &right_args
                ) != 0) return 6;
            __atomic_store_n(&start, 1, __ATOMIC_RELEASE);
            if (pthread_join(left_thread, NULL) != 0) return 7;
            if (pthread_join(right_thread, NULL) != 0) return 8;
            return __atomic_load_n(&failed, __ATOMIC_ACQUIRE) == 0 ? 0 : 9;
        }
        '''
    threaded_pcc_py_archive = (
        cached_threaded_pcc_python_runtime() / "libpy_runtime_pcc_py.a"
    )
    for runtime_name, archive in (
        ("pcc_py", threaded_pcc_py_archive),
    ):
        result = _compile_and_run(
            tmp_path,
            f"class_lookup_concurrent_reads_{runtime_name}",
            source,
            archive,
        )
        assert result.returncode == 0, (
            runtime_name + ": " + result.stdout + result.stderr
        )


def test_instance_field_cache_concurrent_reads_keep_name_and_index_together(
    tmp_path: Path,
    threaded_pcc_runtime_archive: Path,
) -> None:
    """The external C driver exercises the self-emitted runtime's field ABI."""
    source = r'''
        #include "py_internal.h"
        #include <pthread.h>
        #include <stdint.h>

        /* Equal low address bits force both names into the same cache slot. */
        _Alignas(64) static const char names[2][64] = {"left", "right"};
        static int start;
        static int failed;
        typedef struct {
            PyObject *instance;
            PyObject **values;
            int first;
        } Lookup;

        static void *worker(void *opaque) {
            Lookup *arg = (Lookup *)opaque;
            while (!__atomic_load_n(&start, __ATOMIC_ACQUIRE)) {}
            for (int i = 0; i < 200000; i++) {
                int field = (i + arg->first) & 1;
                PyObject *got = py_instance_getattr(arg->instance, names[field]);
                if (got != arg->values[field]) {
                    __atomic_store_n(&failed, 1, __ATOMIC_RELEASE);
                    return NULL;
                }
                py_decref(got);
            }
            return NULL;
        }

        int main(void) {
            const char *fields[2] = {names[0], names[1]};
            if (pcc_gc_set_backend(0) != 0) return 12;
            PyClassObject *cls = py_class_new("Fields", NULL, 0, fields, 2);
            if (!cls) return 2;
            PyObject *instance = py_instance_new(cls);
            if (!instance) return 3;
            /* Heap objects make both threads exercise atomic retain/release;
             * small tagged integers would bypass refcounting entirely. */
            PyObject *left = py_str_new("left-value", 10);
            PyObject *right = py_str_new("right-value", 11);
            if (!left || !right) return 10;
            py_instance_set_field(instance, 0, left);
            py_instance_set_field(instance, 1, right);
            PyObject *values[2] = {left, right};
            int64_t left_refs = ((PyObjectHeader *)left)->refcount;
            int64_t right_refs = ((PyObjectHeader *)right)->refcount;
            Lookup args[2] = {{instance, values, 0}, {instance, values, 1}};
            pthread_t threads[2];
            if (pthread_create(&threads[0], NULL, worker, &args[0])) return 4;
            if (pthread_create(&threads[1], NULL, worker, &args[1])) return 5;
            __atomic_store_n(&start, 1, __ATOMIC_RELEASE);
            if (pthread_join(threads[0], NULL)) return 6;
            if (pthread_join(threads[1], NULL)) return 7;
            if (((PyObjectHeader *)left)->refcount != left_refs ||
                ((PyObjectHeader *)right)->refcount != right_refs) return 11;
            return __atomic_load_n(&failed, __ATOMIC_ACQUIRE) ? 9 : 0;
        }
    '''
    result = _compile_and_run(
        tmp_path, "instance_field_cache_threads", source,
        threaded_pcc_runtime_archive,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_instance_field_cache_does_not_retain_a_borrowed_name_buffer(
    tmp_path: Path,
    pcc_runtime_archive: Path,
) -> None:
    source = r'''
        #include "py_internal.h"
        #include <string.h>

        int main(void) {
            const char *fields[2] = {"left", "right"};
            PyClassObject *cls = py_class_new("Fields", NULL, 0, fields, 2);
            if (!cls) return 2;
            PyObject *instance = py_instance_new(cls);
            if (!instance) return 3;
            PyObject *left = py_int_from_i64(11);
            PyObject *right = py_int_from_i64(22);
            py_instance_set_field(instance, 0, left);
            py_instance_set_field(instance, 1, right);
            char name[16] = "left";
            PyObject *got = py_instance_getattr(instance, name);
            if (got != left) return 4;
            py_decref(got);
            strcpy(name, "right");
            got = py_instance_getattr(instance, name);
            if (got != right) return 5;
            py_decref(got);
            return 0;
        }
    '''
    result = _compile_and_run(
        tmp_path, "instance_field_cache_borrowed_name", source,
        pcc_runtime_archive,
    )
    assert result.returncode == 0, result.stdout + result.stderr
