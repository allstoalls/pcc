from __future__ import annotations

import ast
import os
import re
import subprocess
import textwrap
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
RUNTIME = REPO / "pcc" / "py_runtime"


def test_reserved_descriptor_tags_never_enter_instance_layout_dispatch():
    """Only tag 11 and allocated class tags (104+) have ``inst->cls``."""
    c_allowed = {
        (
            "pcc_threads.c",
            "|| tag >= PY_TYPE_USER",
        ),
        (
            "py_obj.c",
            "|| tag >= PY_TYPE_USER",
        ),
        (
            "py_tuple.c",
            "if (tag >= PY_TYPE_USER) return 1;",
        ),
    }
    c_seen: list[tuple[str, str]] = []
    c_violations: list[str] = []
    comparison = re.compile(r"(?:>=|<)\s*PY_TYPE_USER\b")
    for path in sorted((RUNTIME / "src").glob("*.c")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if comparison.search(line):
                occurrence = (path.name, line.strip())
                if occurrence in c_allowed:
                    c_seen.append(occurrence)
                else:
                    c_violations.append(f"{path.name}:{lineno}: {line.strip()}")

    py_allowed = {
        # These preparation helpers share the refcount header-validity check;
        # accepting a descriptor header does not access an instance layout.
        ("py_obj.py", "_py_incref_prepare"),
        ("py_obj.py", "_py_decref_prepare"),
        ("py_obj.py", "py_incref"),
        ("py_obj.py", "py_decref"),
        ("py_tuple.py", "_tuple_item_can_participate_in_cycle"),
    }
    py_seen: list[tuple[str, str]] = []
    py_violations: list[str] = []
    for path in sorted((RUNTIME / "py").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for function in (
            node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        ):
            for node in ast.walk(function):
                if not isinstance(node, ast.Compare):
                    continue
                if any(
                    isinstance(child, ast.Name) and child.id == "PY_TYPE_USER"
                    for child in ast.walk(node)
                ):
                    occurrence = (path.name, function.name)
                    if occurrence in py_allowed:
                        py_seen.append(occurrence)
                    else:
                        py_violations.append(
                            f"{path.name}:{node.lineno}: {function.name}"
                        )

    assert not c_violations, "instance layout still starts at tag 100:\n  " + "\n  ".join(c_violations)
    assert not py_violations, "port instance layout still starts at tag 100:\n  " + "\n  ".join(py_violations)
    assert set(c_seen) == c_allowed
    assert set(py_seen) == py_allowed


def test_descriptor_dealloc_is_a_signature_exact_freestanding_boundary():
    from pcc.py_frontend.pipeline_freestanding import (
        freestanding_allowed_external_symbols,
    )

    exact = (
        "from pcc.extern import c_ptr, c_void, extern\n"
        "py_descriptor_dealloc = extern("
        "\"py_descriptor_dealloc\", (c_ptr,), c_void)\n"
    )
    wrong = exact.replace("(c_ptr,), c_void", "(c_ptr,), c_ptr")
    assert freestanding_allowed_external_symbols(exact) == {
        "py_descriptor_dealloc"
    }
    assert freestanding_allowed_external_symbols(wrong) == set()


def test_every_refcount_and_gc_dispatch_owns_descriptor_deallocation():
    c_dispatchers = (
        "py_obj.c",
        "py_obj_dealloc.c",
        "py_obj_gc.c",
        "py_gc_backend.c",
    )
    for filename in c_dispatchers:
        source = (RUNTIME / "src" / filename).read_text(encoding="utf-8")
        for tag in (
            "PY_TYPE_PROPERTY",
            "PY_TYPE_CLASSMETHOD",
            "PY_TYPE_STATICMETHOD",
        ):
            assert f"case {tag}:" in source, filename + " omits " + tag
        assert "py_descriptor_dealloc" in source

    py_dispatchers = (
        "py_obj_dealloc.py",
        "freestanding_gc_backend0_collector.py",
        "freestanding_gc_tracing_sweep_collector.py",
    )
    for filename in py_dispatchers:
        source = (RUNTIME / "py" / filename).read_text(encoding="utf-8")
        for tag in ("property", "classmethod", "staticmethod"):
            assert tag in source.lower(), filename + " omits " + tag
        assert "py_descriptor_dealloc" in source

    c_class = (RUNTIME / "src" / "py_class.c").read_text(encoding="utf-8")
    c_release = c_class.split("static void descriptor_release_slot", 1)[1]
    c_release = c_release.split("void py_descriptor_dealloc", 1)[0]
    assert c_release.index("*slot = NULL") < c_release.index("py_decref(value)")

    py_class = (RUNTIME / "py" / "py_class.py").read_text(encoding="utf-8")
    py_release = py_class.split("def _descriptor_release_slot", 1)[1]
    py_release = py_release.split('@c_abi_export("py_descriptor_dealloc")', 1)[0]
    assert py_release.index("store_ptr(owner, offset, null())") < py_release.index("py_decref(value)")


def _descriptor_harness() -> str:
    return textwrap.dedent(
        r"""
        #include "py_internal.h"
        #include <stdint.h>
        #include <stdio.h>

        static int64_t refcount(PyObject *o) {
            return ((PyObjectHeader *)o)->refcount;
        }

        int main(void) {
            py_gc_init();
    PyObject *child = py_list_new(0);
            if (child == NULL || refcount(child) != 1) return 10;

            PyObject *property = py_property_new(child, NULL, NULL);
            if (property == NULL || py_type_of(property) != PY_TYPE_PROPERTY) return 11;
            if (refcount(child) != 2) return 12;
            py_decref(property);
            if (refcount(child) != 1) return 13;

            PyObject *classmethod = py_classmethod_new(child);
            if (classmethod == NULL || py_type_of(classmethod) != PY_TYPE_CLASSMETHOD) return 14;
            if (refcount(child) != 2) return 15;
            py_decref(classmethod);
            if (refcount(child) != 1) return 16;

            PyStaticMethodObject *staticmethod = (PyStaticMethodObject *)pcc_gc_alloc(
                (int64_t)sizeof(PyStaticMethodObject), PY_TYPE_STATICMETHOD, 0
            );
            if (staticmethod == NULL) return 17;
            staticmethod->func = NULL;
            pcc_gc_store_ptr((PyObject *)staticmethod, &staticmethod->func, child);
            py_gc_track((PyObject *)staticmethod);
            if (refcount(child) != 2) return 18;
            py_decref((PyObject *)staticmethod);
            if (refcount(child) != 1) return 19;

            py_decref(child);
            puts("descriptor-dealloc:ok");
            return 0;
        }
        """
    ).lstrip()


def _link_descriptor_harness(tmp_path: Path, archive: Path, name: str) -> Path:
    source = tmp_path / (name + ".c")
    executable = tmp_path / name
    source.write_text(_descriptor_harness(), encoding="utf-8")
    result = subprocess.run(
        [
            os.environ.get("CC", "cc"),
            "-std=c11",
            f"-I{RUNTIME / 'include'}",
            f"-I{RUNTIME / 'src'}",
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


def _assert_descriptor_harness(tmp_path: Path, archive: Path, label: str) -> None:
    executable = _link_descriptor_harness(tmp_path, archive, label)
    result = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "descriptor-dealloc:ok\n"


def test_descriptor_dealloc_releases_owned_slots_in_c_runtime(
    tmp_path: Path,
    c_runtime_archive: Path,
):
    _assert_descriptor_harness(tmp_path, c_runtime_archive, "c")


def test_descriptor_dealloc_releases_owned_slots_in_pcc_python_runtime(
    tmp_path: Path,
    pcc_py_runtime_archive: Path,
):
    _assert_descriptor_harness(tmp_path, pcc_py_runtime_archive, "pcc_python")
