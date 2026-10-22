"""Fail-closed :mod:`runpy` boundary for ahead-of-time pcc programs.

CPython's ``run_module`` and ``run_path`` discover code at runtime, allocate a
fresh globals dictionary, set the special execution attributes, and execute
that code again.  pcc's compiled-module registry deliberately has a narrower
contract: it initializes each linked module once and returns its live module
object.  Treating an import as ``runpy`` execution would therefore get
``__name__``, ``sys.argv[0]``, fresh-global, and repeat-execution semantics
wrong.

The public names live here so build-tool dependency closure does not consult a
host interpreter merely to import :mod:`runpy`.  Calls fail closed until the
compiler owns a registered re-execution entry point (and, for ``run_path``, a
dynamic-source compilation boundary).
"""
from __future__ import annotations


__all__ = ["run_module", "run_path"]


def _unsupported(operation):
    raise NotImplementedError(
        operation
        + " requires compiler-owned fresh-namespace code re-execution; "
        + "the compiled-module import registry only supports one-time initialization"
    )


def run_module(
    mod_name,
    init_globals=None,
    run_name=None,
    alter_sys=False,
):
    """Execute a module in a fresh namespace.

    No import-equivalent shortcut is provided: even with default arguments,
    CPython executes the module anew and returns a new globals dictionary.
    """
    _unsupported("runpy.run_module")


def run_path(path_name, init_globals=None, run_name=None):
    """Execute code selected by a filesystem path at runtime."""
    _unsupported("runpy.run_path")


def _run_module_as_main(mod_name, alter_argv=True):
    """Private CPython helper used by some launchers; keep the same boundary."""
    _unsupported("runpy._run_module_as_main")
