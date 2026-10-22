from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from tests.runtime_build_cache import (
    cached_threaded_pcc_python_runtime,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"


def _compile_probe(tmp_path: Path, kind: str, backend: int) -> Path:
    runtime = (
        cached_threaded_pcc_python_runtime()
    )
    archive = runtime / (
        "libpy_runtime_pcc_py.a"
    )
    source = tmp_path / f"resurrection_metadata_{kind}_{backend}.c"
    executable = tmp_path / f"resurrection_metadata_{kind}_{backend}"
    source.write_text(
        textwrap.dedent(
            f"""
            #include "py_internal.h"
            #include <stdint.h>

            static PyObject *resurrected_root;
            static int64_t finalizer_calls;

            static void probe_del(PyObject *self) {{
                finalizer_calls++;
                if (finalizer_calls == 1) {{
                    py_incref(self);
                    resurrected_root = self;
                }}
            }}

            int main(void) {{
                if (pcc_gc_set_backend({backend}) != 0) return 2;
                void *root_handle = pcc_gc_scheduler_root_register_handle(
                    &resurrected_root
                );
                if (root_handle == NULL) return 3;
                PyClassObject *cls = py_class_new(
                    "ResurrectionMetadata", NULL, 0, NULL, 0
                );
                if (cls == NULL) return 4;
                py_class_add_method(
                    cls, "__del__", (PyObject *)(uintptr_t)probe_del
                );
                PyObject *instance = py_instance_new(cls);
                if (instance == NULL) return 5;
                py_decref(instance);
                PyObject *live = pcc_gc_load_ptr(NULL, &resurrected_root);
                if (live == NULL || finalizer_calls != 1) return 6;
                if (py_header(live)->refcount != 1) return 7;
                if ((py_header(live)->flags & PY_FLAG_GC_DEALLOCATING) != 0) {{
                    return 8;
                }}
                if (pcc_gc_pointer_is_managed(live) != 1) return 9;
                if (pcc_gc_object_index_find(live) == NULL) return 10;
                if ({backend} == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR) {{
                    if ((py_header(live)->flags & (PY_FLAG_GC_YOUNG | PY_FLAG_GC_OLD)) == 0) {{
                        return 11;
                    }}
                }} else {{
                    pcc_gc_reset_relocation_set();
                    if (pcc_gc_backend4_relocation_set_add(live) != 1) {{
                        return 12;
                    }}
                    pcc_gc_reset_relocation_set();
                }}

                PyObject *last = live;
                resurrected_root = NULL;
                py_decref(last);
                if (finalizer_calls != 1) return 13;
                if (pcc_gc_object_index_find(last) != NULL) return 14;
                pcc_gc_scheduler_root_unregister_handle(root_handle);
                py_decref((PyObject *)cls);
                return 0;
            }}
            """
        ).lstrip(),
        encoding="utf-8",
    )
    build = subprocess.run(
        [
            os.environ.get("CC", "cc"),
            "-std=c11",
            "-pthread",
            f"-I{runtime / 'include'}",
            f"-I{runtime / 'src'}",
            str(source),
            str(archive),
            "-lm",
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    return executable


def test_last_decref_resurrection_delays_metadata_removal_in_both_mirrors():

    py_obj = (RUNTIME_DIR / "py" / "py_obj.py").read_text(encoding="utf-8")
    py_finish = py_obj.split("def _py_decref_finish(prepared) -> None:", 1)[
        1
    ].split('@c_abi_export("py_decref")', 1)[0]
    assert "delay_instance_metadata" in py_finish
    assert "delay_zpage_freeing_note == 0 and delay_instance_metadata == 0" in py_finish

    py_class = (RUNTIME_DIR / "py" / "py_class.py").read_text(encoding="utf-8")
    py_dealloc = py_class.split("def py_instance_dealloc(o) -> None:", 1)[
        1
    ].split('@c_abi_export("py_dataclass_replace")', 1)[0]
    metadata = py_dealloc.index("metadata_valid: int = 1")
    clear = py_dealloc.index("flags & ~524288")
    assert metadata < clear
    assert py_dealloc.index("pcc_gc_note_object_freeing(o)") > clear


@pytest.mark.parametrize("backend", [3, 4], ids=["gc3", "gc4"])
@pytest.mark.parametrize("kind", ["pcc_python"])
def test_last_decref_resurrection_restores_live_metadata_and_frees_once(
    tmp_path: Path,
    kind: str,
    backend: int,
) -> None:
    executable = _compile_probe(tmp_path, kind, backend)
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, (
        f"{kind}/gc{backend} resurrection metadata returned {run.returncode}: "
        + run.stdout
        + run.stderr
    )
