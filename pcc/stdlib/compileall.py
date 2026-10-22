"""Honest CPython-bytecode boundary for pcc-native build tools.

pcc produces native artifacts and does not own CPython bytecode or the versioned
``.pyc`` marshal format.  ``compile_file`` therefore never fabricates a cache
file or reports success for a Python source.  It returns ``False`` (the public
compileall failure signal), which lets callers such as Meson's install helper
choose whether missing optional bytecode is fatal while preserving source files.
Missing and non-source inputs retain CPython's no-work ``True`` result.
Directory traversal is bounded and aggregates those results.
"""
from __future__ import annotations

import os
import sys


_MAX_DEPTH = 128
_MAX_FILES = 100000


def _is_python_source(path):
    return str(path).endswith(".py")


def compile_file(
    fullname,
    ddir=None,
    force=False,
    rx=None,
    quiet=0,
    legacy=False,
    optimize=-1,
    invalidation_mode=None,
    stripdir=None,
    prependdir=None,
    limit_sl_dest=None,
    hardlink_dupes=False,
):
    path = str(fullname)
    if rx is not None:
        raise NotImplementedError("compileall exclusion regexes are not owned")
    if invalidation_mode is not None:
        raise NotImplementedError(
            "compileall invalidation modes require CPython bytecode"
        )
    if hardlink_dupes:
        raise NotImplementedError(
            "compileall hard-linked bytecode caches are not runtime-owned"
        )
    if limit_sl_dest is not None:
        raise NotImplementedError(
            "compileall symlink destination policies are not runtime-owned"
        )
    if not os.path.isfile(path):
        # CPython treats a missing or non-regular input as no work, not a
        # failed compilation.  There is no bytecode claim to qualify here.
        return True
    if not _is_python_source(path):
        return True
    if int(quiet) < 1:
        print("pcc compileall: CPython .pyc emission is unavailable for " + path)
    # ``False`` is intentional.  No file was compiled, regardless of force,
    # optimize, ddir or path-rewriting options.
    return False


def _compile_dir(current, maxlevels, quiet, legacy, optimize, state):
    depth = state[0]
    if depth > _MAX_DEPTH:
        raise RuntimeError("compileall directory depth exceeds 128")
    try:
        names = sorted(os.listdir(current))
    except OSError:
        if int(quiet) < 2:
            print("Can't list " + current)
        return True
    result = True
    for name in names:
        if name == "__pycache__":
            continue
        path = os.path.join(current, name)
        state[1] += 1
        if state[1] > _MAX_FILES:
            raise RuntimeError("compileall file count exceeds 100000")
        if os.path.isdir(path):
            if maxlevels > 0:
                state[0] = depth + 1
                child_levels = maxlevels - 1
                if not _compile_dir(
                    path, child_levels, quiet, legacy, optimize, state
                ):
                    result = False
                state[0] = depth
            continue
        if _is_python_source(path):
            if not compile_file(
                path,
                force=True,
                quiet=quiet,
                legacy=legacy,
                optimize=optimize,
            ):
                result = False
    return result


def compile_dir(
    dir,
    maxlevels=None,
    ddir=None,
    force=False,
    rx=None,
    quiet=0,
    legacy=False,
    optimize=-1,
    workers=1,
    invalidation_mode=None,
    stripdir=None,
    prependdir=None,
    limit_sl_dest=None,
    hardlink_dupes=False,
):
    root = str(dir)
    if (
        rx is not None
        or invalidation_mode is not None
        or hardlink_dupes
        or limit_sl_dest is not None
    ):
        raise NotImplementedError(
            "advanced compileall bytecode policies are not runtime-owned"
        )
    if int(workers) not in (0, 1):
        raise NotImplementedError("parallel compileall workers are not owned")
    if not os.path.isdir(root):
        if int(quiet) < 2:
            print("Can't list " + root)
        # CPython treats an unlistable directory as an empty traversal; this
        # differs intentionally from compile_file()'s missing-file signal.
        return True
    levels = _MAX_DEPTH if maxlevels is None else int(maxlevels)
    return _compile_dir(root, levels, quiet, legacy, optimize, [0, 0])


def compile_path(
    skip_curdir=True,
    maxlevels=0,
    force=False,
    quiet=0,
    legacy=False,
    optimize=-1,
    invalidation_mode=None,
):
    result = True
    for entry in sys.path:
        if skip_curdir and entry in ("", "."):
            continue
        if not compile_dir(
            entry,
            maxlevels=maxlevels,
            force=force,
            quiet=quiet,
            legacy=legacy,
            optimize=optimize,
            invalidation_mode=invalidation_mode,
        ):
            result = False
    return result


def main():
    raise NotImplementedError(
        "compileall command-line parsing is not runtime-owned"
    )


__all__ = ["compile_dir", "compile_file", "compile_path"]
