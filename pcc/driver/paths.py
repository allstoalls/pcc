"""Filesystem layout discovery for the Python frontend pipeline.

This module is deliberately independent from parsing, code generation, cache
state, and backend selection.  A source checkout, an installed wheel, and a
native bootstrap executable expose different stable anchors; the helpers below
turn those anchors into one pcc package directory without importing the rest of
the compilation pipeline.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Optional


def join_strings(parts: list[str], sep: str) -> str:
    if not parts:
        return ""
    out = parts[0]
    index = 1
    while index < len(parts):
        out += sep + parts[index]
        index += 1
    return out


def join_dotted_parts(parts: list[str]) -> str:
    return join_strings(parts, ".")


def first_string(items: list[str]) -> str:
    return items[0]


def module_name_from_src(src_path: str) -> str:
    base = str(os.path.basename(src_path))
    if base.endswith(".py"):
        base = base[:-3]
    if not base:
        return "<module>"

    abs_path = str(os.path.abspath(src_path))
    parent_dir = str(os.path.dirname(abs_path))
    if base == "__main__":
        init_py = str(os.path.join(parent_dir, "__init__.py"))
        package_name = str(os.path.basename(parent_dir))
        if package_name and os.path.isfile(init_py):
            return join_dotted_parts([package_name, "__main__"])

    package_parts: list[str] = []
    current_dir = parent_dir
    while current_dir:
        init_py = str(os.path.join(current_dir, "__init__.py"))
        if not os.path.isfile(init_py):
            break
        package_parts.append(str(os.path.basename(current_dir)))
        parent = str(os.path.dirname(current_dir))
        if parent == current_dir:
            break
        current_dir = parent

    if not package_parts:
        return base

    ordered_parts: list[str] = []
    index = len(package_parts) - 1
    while index >= 0:
        ordered_parts.append(package_parts[index])
        index -= 1
    if base == "__init__":
        return join_dotted_parts(ordered_parts)
    return join_dotted_parts(ordered_parts + [base])


def module_root_from_src(src_path: str, module_name: str) -> str:
    abs_path = str(os.path.abspath(src_path))
    current_dir = str(os.path.dirname(abs_path))
    parts = module_name.split(".")
    levels = (
        len(parts)
        if os.path.basename(abs_path) == "__init__.py"
        else max(0, len(parts) - 1)
    )
    index = 0
    while index < levels:
        parent = str(os.path.dirname(current_dir))
        if parent == current_dir:
            break
        current_dir = parent
        index += 1
    return current_dir


def package_parts_for_module(src_path: str, module_name: str) -> list[str]:
    parts = module_name.split(".")
    if os.path.basename(src_path) == "__init__.py":
        return parts
    return parts[:-1]


# Import resolution lists the same few package directories thousands of times
# per compile: 7,200 ``os.listdir`` calls over 20 directories for the pcc
# compiler's own closure.  pcc1 lists a directory by spawning ``ls``, so those
# calls were about 40 s of the native Stage2 coordinator.  A listing is reused
# only while its directory's mtime is unchanged; adding, removing or renaming
# an entry updates that mtime, so a package installed mid-process is still
# found.  A directory changed within the last two seconds is listed afresh
# every time: a second change inside one timestamp tick (whole seconds on
# HFS+) would leave the mtime equal.
_DIRECTORY_NAMES_CACHE: dict = {}
_DIRECTORY_NAMES_CACHE_MAX_ENTRIES = 4096
_DIRECTORY_NAMES_QUIET_SECONDS = 2.0


def directory_entry_names(directory: str) -> tuple:
    """Return ``os.listdir(directory)`` as a tuple, reused while unchanged.

    Raises ``OSError`` exactly like ``os.listdir``; a failure is not cached.
    """
    stamp = os.path.getmtime(directory)
    cached = _DIRECTORY_NAMES_CACHE.get(directory)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    names = tuple(os.listdir(directory))
    if time.time() - stamp < _DIRECTORY_NAMES_QUIET_SECONDS:
        return names
    if len(_DIRECTORY_NAMES_CACHE) >= _DIRECTORY_NAMES_CACHE_MAX_ENTRIES:
        _DIRECTORY_NAMES_CACHE.clear()
    _DIRECTORY_NAMES_CACHE[directory] = (stamp, names)
    return names


def path_component_matches_case(path: str, expected_name: str) -> bool:
    """Return whether *path*'s last component really is ``expected_name``.

    macOS and Windows filesystems are case-insensitive by default, so
    ``os.path.isfile`` answers yes for ``pkg/App.py`` when only ``pkg/app.py``
    exists.  ``from pkg import App`` then resolved the class name ``App`` as a
    submodule and compiled ``app.py`` twice under two module names, which the
    linker rejected with undefined ``__pcc_py_module_top_pkg_App``.  Compare
    against the directory listing so module identity stays case-sensitive on
    every platform.
    """
    directory = str(os.path.dirname(path))
    if not directory:
        directory = "."
    try:
        names = directory_entry_names(directory)
    except OSError:
        return True
    for name in names:
        if name == expected_name:
            return True
    return False


def resolve_module_src(root_dir: str, dotted_name: str) -> Optional[str]:
    parts = dotted_name.split(".")
    leaf = parts[len(parts) - 1]
    py_path = str(os.path.join(root_dir, *parts)) + ".py"
    if os.path.isfile(py_path) and path_component_matches_case(
        py_path, leaf + ".py"
    ):
        return py_path
    package_dir = str(os.path.join(root_dir, *parts))
    init_path = str(os.path.join(package_dir, "__init__.py"))
    if os.path.isfile(init_path) and path_component_matches_case(
        package_dir, leaf
    ):
        return init_path
    return None


def runtime_dir_has_runtime_files(path: str) -> bool:
    if not os.path.isdir(path):
        return False
    include_h = os.path.isfile(os.path.join(path, "include", "py_runtime.h"))
    makefile = os.path.isfile(os.path.join(path, "Makefile"))
    maybe_lib = os.path.isfile(os.path.join(path, "libpy_runtime_pcc_py.a"))
    return include_h or makefile or maybe_lib


def bootstrap_append_unique_path(paths: list[str], path: Optional[str]) -> None:
    if path is None:
        return
    path = str(path or "").strip()
    if not path:
        return
    path = os.path.abspath(path)
    for existing in paths:
        if existing == path:
            return
    paths.append(path)


def bootstrap_append_pcc_dir_candidate(
    paths: list[str], path: Optional[str]
) -> None:
    if path is None:
        return
    path = str(path or "").strip()
    if not path:
        return
    path = os.path.abspath(path)
    bootstrap_append_unique_path(paths, path)
    bootstrap_append_unique_path(paths, os.path.join(path, "pcc"))
    name = os.path.basename(path)
    if name in ("py_frontend", "py_runtime", "py_stdlib", "stdlib", "backend"):
        bootstrap_append_unique_path(paths, os.path.dirname(path))


def bootstrap_append_pcc_dir_ancestors(
    paths: list[str], path: Optional[str]
) -> None:
    if path is None:
        return
    path = str(path or "").strip()
    if not path:
        return
    cur = os.path.abspath(path)
    if os.path.isfile(cur):
        cur = os.path.dirname(cur)
    while cur:
        bootstrap_append_pcc_dir_candidate(paths, cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent


def bootstrap_append_install_prefix_candidates(
    paths: list[str], prefix: Optional[str]
) -> None:
    """Add possible installed ``pcc`` package directories under *prefix*."""

    if prefix is None:
        return
    prefix = str(prefix or "").strip()
    if not prefix:
        return
    prefix = os.path.abspath(prefix)
    direct_roots = [
        os.path.join(prefix, "Lib", "site-packages"),
        os.path.join(prefix, "lib", "site-packages"),
        os.path.join(prefix, "lib64", "site-packages"),
    ]
    for site_root in direct_roots:
        bootstrap_append_pcc_dir_candidate(paths, os.path.join(site_root, "pcc"))
    for lib_name in ("lib", "lib64"):
        lib_root = os.path.join(prefix, lib_name)
        if not os.path.isdir(lib_root):
            continue
        try:
            names = sorted(os.listdir(lib_root))
        except OSError:
            names = []
        for name in names:
            site_root = os.path.join(lib_root, name, "site-packages")
            bootstrap_append_pcc_dir_candidate(
                paths, os.path.join(site_root, "pcc")
            )


def pcc_dir_has_source_files(path: str) -> bool:
    return (
        os.path.isfile(os.path.join(path, "__init__.py"))
        and os.path.isfile(os.path.join(path, "backend", "self_backend_dispatch.py"))
        and (
            os.path.isfile(os.path.join(path, "stdlib", "__init__.py"))
            or runtime_dir_has_runtime_files(os.path.join(path, "runtime"))
        )
    )


def resolve_pcc_dir_from_environment(pipeline_file: str) -> str:
    """Resolve the pcc package root from explicit and installed anchors."""

    raw_pipeline_dir = os.path.dirname(os.path.abspath(pipeline_file))
    raw_pcc_dir = os.path.dirname(os.path.dirname(raw_pipeline_dir))
    candidates: list[str] = []
    bootstrap_append_pcc_dir_candidate(
        candidates, os.environ.get("PCC_SOURCE_ROOT")
    )
    bootstrap_append_pcc_dir_candidate(candidates, os.environ.get("PCC_REPO_ROOT"))
    bootstrap_append_pcc_dir_candidate(
        candidates, os.environ.get("PCC_PY_STDLIB_ROOT")
    )
    bootstrap_append_install_prefix_candidates(
        candidates, os.environ.get("VIRTUAL_ENV")
    )
    bootstrap_append_pcc_dir_candidate(candidates, raw_pcc_dir)
    bootstrap_append_pcc_dir_candidate(candidates, raw_pipeline_dir)
    bootstrap_append_pcc_dir_ancestors(candidates, pipeline_file)
    try:
        if len(sys.argv) > 0:
            argv_prefix = os.path.dirname(
                os.path.dirname(os.path.abspath(sys.argv[0]))
            )
            bootstrap_append_install_prefix_candidates(candidates, argv_prefix)
            bootstrap_append_pcc_dir_ancestors(candidates, sys.argv[0])
    except Exception:
        pass
    try:
        executable_prefix = os.path.dirname(
            os.path.dirname(os.path.abspath(sys.executable))
        )
        bootstrap_append_install_prefix_candidates(candidates, executable_prefix)
        bootstrap_append_pcc_dir_ancestors(candidates, sys.executable)
    except Exception:
        pass
    for candidate in candidates:
        if pcc_dir_has_source_files(candidate):
            return candidate
    return raw_pcc_dir


def resolve_runtime_paths(
    pipeline_file: str,
) -> tuple[str, str, str, tuple[str, str, str, str, str]]:
    """Return ``(pcc_dir, pipeline_dir, runtime_dir, candidates)``."""

    pcc_dir = str(resolve_pcc_dir_from_environment(pipeline_file))
    pipeline_candidate = str(os.path.join(pcc_dir, "frontends", "python"))
    pipeline_dir = (
        pipeline_candidate
        if os.path.isdir(pipeline_candidate)
        else str(os.path.dirname(os.path.abspath(pipeline_file)))
    )
    candidates = (
        str(os.path.join(pcc_dir, "pcc", "runtime")),
        str(os.path.join(pcc_dir, "runtime")),
        str(os.path.join(pipeline_dir, "runtime")),
        str(os.path.join(pipeline_dir, "pcc", "runtime")),
        str(os.path.join(os.getcwd(), "pcc", "runtime")),
    )
    runtime_dir = str(os.path.join(pcc_dir, "runtime"))
    for candidate in candidates:
        if runtime_dir_has_runtime_files(candidate):
            runtime_dir = candidate
            break
    return pcc_dir, pipeline_dir, runtime_dir, candidates


def pcc_source_root_for_host_subprocess(pcc_dir: str) -> str:
    pcc_dir = str(pcc_dir)
    if os.path.basename(pcc_dir) == "pcc":
        return os.path.dirname(pcc_dir)
    return pcc_dir


def host_python_command(pcc_source_root: str, cwd: str) -> str:
    configured = str(os.environ.get("PCC_HOST_PYTHON", "") or "").strip()
    if configured:
        return configured
    roots = [str(pcc_source_root), str(cwd)]
    seen: set[str] = set()
    for root in roots:
        root = os.path.abspath(root)
        if root in seen:
            continue
        seen.add(root)
        python3 = str(os.path.join(root, ".venv", "bin", "python3"))
        if os.path.isfile(python3):
            return python3
        python = str(os.path.join(root, ".venv", "bin", "python"))
        if os.path.isfile(python):
            return python
    return "python3"


__all__ = [
    "bootstrap_append_install_prefix_candidates",
    "bootstrap_append_pcc_dir_ancestors",
    "bootstrap_append_pcc_dir_candidate",
    "bootstrap_append_unique_path",
    "pcc_dir_has_source_files",
    "pcc_source_root_for_host_subprocess",
    "host_python_command",
    "resolve_pcc_dir_from_environment",
    "resolve_runtime_paths",
    "runtime_dir_has_runtime_files",
]
