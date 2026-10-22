from __future__ import annotations

import os
import re
import subprocess
import textwrap
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
RUNTIME = REPO / "pcc" / "runtime"


def test_runtime_has_one_exact_managed_pointer_provenance_decision():
    header = (RUNTIME / "include" / "py_runtime.h").read_text(encoding="utf-8")
    port = (RUNTIME / "py" / "py_gc_backend.py").read_text(encoding="utf-8")
    py_index = (RUNTIME / "py" / "freestanding_gc_index_table.py").read_text(
        encoding="utf-8"
    )

    for symbol in (
        "pcc_gc_pointer_is_managed",
        "pcc_gc_pointer_register",
        "pcc_gc_pointer_unregister",
    ):
        assert symbol in header
        assert f'@c_abi_export("{symbol}")' in port
    for symbol in (
        "pcc_gc_managed_pointer_index_contains",
        "pcc_gc_managed_pointer_index_insert",
        "pcc_gc_managed_pointer_index_remove",
    ):
        assert f'@c_abi_export("{symbol}")' in py_index

    # These were mutually inconsistent guesses in the runtime ports.  An
    # address ceiling, alignment test, or magic low-address cutoff is not
    # provenance and must not creep back into the semantic boundary.
    candidate_ports = (
        "py_tuple.py",
        "py_list_set_slice.py",
        "py_class.py",
        "py_dict.py",
        "py_pickle_copy_runtime.py",
        "py_obj.py",
        "py_list.py",
        "py_tuple_slice.py",
        "py_protocol_runtime.py",
        "freestanding_gc_barrier_dispatcher.py",
        "freestanding_gc_generational_promotion.py",
        "py_gc_backend.py",
        "freestanding_runtime_debug.py",
    )
    sources = [RUNTIME / "py" / name for name in candidate_ports]
    forbidden = re.compile(
        r"140737488355328|281474976710656|17592186044416|"
        r"35184372088832|bits\s*<\s*(?:2048|4096)|"
        r"\(bits\s*&\s*(?:3|7)\)|1ULL\s*<<\s*(?:44|47|48)"
    )
    violations: list[str] = []
    for path in sources:
        for line_no, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            if forbidden.search(line):
                violations.append(f"{path.name}:{line_no}: {line.strip()}")
    assert not violations, "address guesses remain:\n  " + "\n  ".join(violations)


def test_manual_object_publication_paths_register_before_header_consumers():
    py_obj = (RUNTIME / "py" / "py_obj.py").read_text(encoding="utf-8")
    assert py_obj.index("pcc_gc_pointer_register(obj)") < py_obj.index(
        "pcc_gc_note_object_allocated_sized(obj, size)"
    )

    for path in (RUNTIME / "py" / "py_iter.py", RUNTIME / "py" / "py_int_ops.py"):
        source = path.read_text(encoding="utf-8")
        assert "pcc_gc_alloc" in source

    py_int = (RUNTIME / "py" / "py_int_core.py").read_text(encoding="utf-8")
    for source in (py_int,):
        assert "pcc_gc_pointer_register" in source
        assert "PY_FLAG_GC_MALLOC_ALLOC" in source


def _harness_source() -> str:
    return textwrap.dedent(
        r"""
        #ifndef _GNU_SOURCE
        #define _GNU_SOURCE 1
        #endif
        #include "py_runtime.h"
        #include <stdint.h>
        #include <stdio.h>
        #include <stdlib.h>
        #include <sys/mman.h>

        #ifndef MAP_ANON
        #define MAP_ANON MAP_ANONYMOUS
        #endif

        extern int64_t pcc_gc_managed_pointer_index_contains(PyObject *);
        extern void *pcc_gc_object_index_find(PyObject *);
        extern void *pcc_gc_forwarding_index_find(PyObject *);
        extern void *pcc_gc_forwarding_target_index_find(PyObject *);

        static int raw_callback(void) { return 17; }

        static int expect(const char *name, int64_t got, int64_t want) {
            if (got == want) return 0;
            fprintf(stderr, "%s:%lld!=%lld\n", name,
                    (long long)got, (long long)want);
            return 1;
        }

        static int expect_stale(const char *name, PyObject *obj) {
            int64_t got = pcc_gc_pointer_is_managed(obj);
            if (got == 0) return 0;
            fprintf(stderr,
                    "%s:managed=%lld aux=%lld object=%d from=%d target=%d\n",
                    name, (long long)got,
                    (long long)pcc_gc_managed_pointer_index_contains(obj),
                    pcc_gc_object_index_find(obj) != NULL,
                    pcc_gc_forwarding_index_find(obj) != NULL,
                    pcc_gc_forwarding_target_index_find(obj) != NULL);
            return 1;
        }

        int main(void) {
            py_gc_init();
            int failed = 0;
            const char *literal = "raw-c-string";
            void *guard = mmap(NULL, 4096, PROT_NONE,
                               MAP_PRIVATE | MAP_ANON, -1, 0);
            if (guard == MAP_FAILED) return 90;

            failed |= expect("null", pcc_gc_pointer_is_managed(NULL), 0);
            failed |= expect("tagged", pcc_gc_pointer_is_managed(
                py_int_from_i64(7)), 0);
            failed |= expect("cstr", pcc_gc_pointer_is_managed(
                (PyObject *)(uintptr_t)literal), 0);
            failed |= expect("function", pcc_gc_pointer_is_managed(
                (PyObject *)(uintptr_t)(void *)&raw_callback), 0);
            /* This page is unreadable: a header sniff would fault here. */
            failed |= expect("guard", pcc_gc_pointer_is_managed(
                (PyObject *)guard), 0);

            failed |= expect("none", pcc_gc_pointer_is_managed(py_None), 1);
            failed |= expect("notimplemented", pcc_gc_pointer_is_managed(
                py_NotImplemented), 1);
            failed |= expect("true", pcc_gc_pointer_is_managed(py_True), 1);
            failed |= expect("false", pcc_gc_pointer_is_managed(py_False), 1);

            PyObject *list = py_list_new(0);
            PyObject *text = py_str_new("managed", 7);
            PyObject *big = py_int_from_i64(INT64_MAX);
            if (list == NULL || text == NULL || big == NULL) return 91;
            failed |= expect("list", pcc_gc_pointer_is_managed(list), 1);
            failed |= expect("str", pcc_gc_pointer_is_managed(text), 1);
            failed |= expect("bigint", pcc_gc_pointer_is_managed(big), 1);

            uintptr_t old_list = (uintptr_t)list;
            uintptr_t old_text = (uintptr_t)text;
            uintptr_t old_big = (uintptr_t)big;
            py_decref(list);
            py_decref(text);
            py_decref(big);
            /* GC4 intentionally keeps old forwarding shells managed until
             * its two-epoch remap quarantine has healed every slot.  Drive
             * that public lifecycle to quiescence before testing stale
             * provenance; the other backends make these collects no-ops or
             * ordinary bounded collections. */
            (void)pcc_gc_collect(0);
            (void)pcc_gc_collect(0);
            failed |= expect_stale("stale-list", (PyObject *)old_list);
            failed |= expect_stale("stale-str", (PyObject *)old_text);
            failed |= expect_stale("stale-bigint", (PyObject *)old_big);

            munmap(guard, 4096);
            if (failed != 0) return 92;
            puts("pointer-provenance:ok");
            return 0;
        }
        """
    )


def _link_harness(tmp_path: Path, archive: Path, label: str) -> Path:
    source = tmp_path / f"pointer_provenance_{label}.c"
    executable = tmp_path / f"pointer_provenance_{label}"
    source.write_text(_harness_source(), encoding="utf-8")
    result = subprocess.run(
        [
            "clang",
            "-std=c11",
            f"-I{RUNTIME / 'include'}",
            str(source),
            str(archive),
            "-pthread",
            "-lm",
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return executable


def _assert_all_backends(executable: Path) -> None:
    for backend in range(5):
        env = {**os.environ, "PCC_GC_BACKEND": str(backend)}
        result = subprocess.run(
            [str(executable)],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 0, (
            f"GC{backend}:\n{result.stdout}{result.stderr}"
        )
        assert result.stdout == "pointer-provenance:ok\n"


def test_pointer_provenance_pcc_python_runtime_gc0_through_gc4(
    tmp_path: Path, pcc_runtime_archive: Path
):
    _assert_all_backends(
        _link_harness(tmp_path, pcc_runtime_archive, "pcc_python")
    )
