#!/usr/bin/env python3
"""scripts/pcc_multi.py — multi-file Python compile entry point.

Wraps :func:`pcc.frontends.python.pipeline.compile_python_multi` with a
small argparse-based CLI so the three-stage bootstrap
(``scripts/bootstrap.py``) and other callers can build a single
native executable from several ``.py`` sources without going
through the click-based ``pcc`` entry point.

Usage::

    python3 scripts/pcc_multi.py \\
        --entry pkg.main              \\
        --out pcc1                    \\
        pkg/main.py=pkg.main          \\
        pkg/util.py=pkg.util          \\
        pkg/lib.py=pkg.lib

Each positional argument is either a bare ``.py`` path (module
name inferred from the filename stem) or ``path=module.name`` to
set an explicit dotted module name — required when the stem
collides (``__init__.py``, ``__main__.py``) or when the file
uses relative imports.

The CLI tolerates ``--emit-llvm`` (write combined LLVM IR
instead of an executable) and ``--verbose`` for pipeline timing.
"""
from __future__ import annotations

import sys

from pcc.extern import extern, c_int

_USAGE = (
    "Usage:\n"
    "  pcc_multi --entry MODULE --out PATH "
    "[--backend self] [--python-libpython off|auto|on] "
    "[--ir-scaffold on|off|auto] [--emit-llvm] [-v|--verbose] "
    "SRC [SRC ...]\n"
    "\n"
    "Each SRC is either <path> or <path>=<module.name>.\n"
)

_exit_c: "extern" = extern("exit", (c_int,), )


def _write_text(text: str, *, to_stderr: bool = False) -> None:
    if to_stderr:
        try:
            import sys
            sys.stderr.write(text)
            return
        except Exception:
            pass
    print(text, end="")


def _exit_process(code: int) -> None:
    try:
        _exit_c(code)
        return
    except Exception:
        raise SystemExit(code)


def _pipeline_error_text(exc) -> str:
    message = str(exc)
    if len(message) != 0:
        return message
    diagnostic = repr(exc)
    if len(diagnostic) != 0:
        return diagnostic
    return "PyPipelineError"


def _write_pipeline_error_debug(exc) -> None:
    import os

    enabled = str(os.environ.get("PCC_DEBUG_BOOTSTRAP_TRACE", "") or "")
    if enabled.strip().lower() not in ("1", "true", "yes", "on"):
        return
    _write_text(
        "pcc_multi debug: exception_type=" + str(type(exc).__name__) + "\n",
        to_stderr=True,
    )
    _write_text(
        "pcc_multi debug: exception_args="
        + repr(getattr(exc, "args", None))
        + "\n",
        to_stderr=True,
    )
    tb = getattr(exc, "__traceback__", None)
    index = 0
    while tb is not None and index < 16:
        frame = getattr(tb, "tb_frame", None)
        code = getattr(frame, "f_code", None) if frame is not None else None
        if code is None:
            _write_text(
                "pcc_multi debug: tb[" + str(index) + "]=<frame-missing>\n",
                to_stderr=True,
            )
        else:
            _write_text(
                "pcc_multi debug: tb["
                + str(index)
                + "]="
                + str(code.co_filename)
                + ":"
                + str(tb.tb_lineno)
                + " "
                + str(code.co_name)
                + "\n",
                to_stderr=True,
            )
        tb = getattr(tb, "tb_next", None)
        index += 1


def _parse_src_arg(spec: str):
    """Return ``(path, module_name_or_None)`` for one positional."""
    i = 0
    while i < len(spec):
        if spec[i] == "=":
            return spec[:i], spec[i + 1:]
        i += 1
    return spec, None


def _infer_module_name(path: str) -> str:
    start = 0
    i = len(path) - 1
    while i >= 0:
        ch = path[i]
        if ch == "/" or ch == "\\":
            start = i + 1
            break
        i -= 1
    base = path[start:]
    if len(base) >= 3 and base[-3:] == ".py":
        return base[:-3]
    return base


def _parse_python_libpython(value: str) -> str:
    lowered = (value or "").strip().lower()
    if lowered not in ("auto", "on", "off"):
        raise ValueError(
            "invalid --python-libpython "
            f"{value!r}; expected auto, on, or off"
        )
    return lowered


def _parse_ir_scaffold(value: str) -> str:
    lowered = (value or "").strip().lower()
    if lowered not in ("auto", "on", "off"):
        raise ValueError(
            "invalid --ir-scaffold "
            f"{value!r}; expected on, off, or auto"
        )
    return lowered


def _parse_cli(argv=None):
    argv = _normalized_argv(argv)
    entry = None
    out = None
    emit_llvm = False
    verbose = False
    backend = None
    python_libpython = None
    ir_scaffold = None
    sources: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            i += 1
            while i < len(argv):
                sources.append(argv[i])
                i += 1
            break
        if arg == "--help" or arg == "-h":
            return None, 0, None
        if arg == "--emit-llvm":
            emit_llvm = True
            i += 1
            continue
        if arg == "--verbose" or arg == "-v":
            verbose = True
            i += 1
            continue
        if arg == "--entry" or arg == "--out":
            if i + 1 >= len(argv):
                return None, 2, f"{arg} requires a value"
            value = argv[i + 1]
            if arg == "--entry":
                entry = value
            else:
                out = value
            i += 2
            continue
        if arg == "--backend":
            if i + 1 >= len(argv):
                return None, 2, "--backend requires a value"
            backend = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--backend="):
            backend = arg[len("--backend="):]
            i += 1
            continue
        if arg == "--python-libpython" or arg == "--ir-scaffold":
            if i + 1 >= len(argv):
                return None, 2, f"{arg} requires a value"
            try:
                if arg == "--python-libpython":
                    python_libpython = _parse_python_libpython(argv[i + 1])
                else:
                    ir_scaffold = _parse_ir_scaffold(argv[i + 1])
            except ValueError as exc:
                return None, 2, str(exc)
            i += 2
            continue
        if arg.startswith("--python-libpython="):
            try:
                python_libpython = _parse_python_libpython(
                    arg[len("--python-libpython="):]
                )
            except ValueError as exc:
                return None, 2, str(exc)
            i += 1
            continue
        if arg.startswith("--ir-scaffold="):
            try:
                ir_scaffold = _parse_ir_scaffold(
                    arg[len("--ir-scaffold="):]
                )
            except ValueError as exc:
                return None, 2, str(exc)
            i += 1
            continue
        if len(arg) > 0 and arg[0] == "-":
            return None, 2, f"unknown option: {arg}"
        sources.append(arg)
        i += 1

    if entry is None:
        return None, 2, "missing required option --entry"
    if out is None:
        return None, 2, "missing required option --out"
    if len(sources) == 0:
        return None, 2, "at least one source is required"
    return (
        entry,
        out,
        emit_llvm,
        verbose,
        backend,
        python_libpython,
        ir_scaffold,
        sources,
    ), 0, None


def _normalized_argv(argv=None):
    out: list[str] = []
    if argv is None:
        i = 1
        while i < len(sys.argv):
            out.append((sys.argv[i] or "") + "")
            i += 1
        return out
    i = 0
    while i < len(argv):
        out.append((argv[i] or "") + "")
        i += 1
    return out


def main(argv=None) -> int:
    argv = _normalized_argv(argv)
    entry_module = None
    out_path = None
    emit_llvm = False
    verbose = False
    backend = None
    python_libpython = None
    ir_scaffold = None
    source_specs: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            i += 1
            while i < len(argv):
                source_specs.append(argv[i])
                i += 1
            break
        if arg == "--help" or arg == "-h":
            _write_text(_USAGE)
            return 0
        if arg == "--emit-llvm":
            emit_llvm = True
            i += 1
            continue
        if arg == "--verbose" or arg == "-v":
            verbose = True
            i += 1
            continue
        if arg == "--entry" or arg == "--out":
            if i + 1 >= len(argv):
                _write_text(
                    "pcc_multi: " + arg + " requires a value\n",
                    to_stderr=True,
                )
                _write_text(_USAGE, to_stderr=True)
                return 2
            value = argv[i + 1]
            if arg == "--entry":
                entry_module = value
            else:
                out_path = value
            i += 2
            continue
        if arg == "--backend":
            if i + 1 >= len(argv):
                _write_text(
                    "pcc_multi: --backend requires a value\n",
                    to_stderr=True,
                )
                _write_text(_USAGE, to_stderr=True)
                return 2
            backend = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--backend="):
            backend = arg[len("--backend="):]
            i += 1
            continue
        if arg == "--python-libpython" or arg == "--ir-scaffold":
            if i + 1 >= len(argv):
                _write_text(
                    "pcc_multi: " + arg + " requires a value\n",
                    to_stderr=True,
                )
                _write_text(_USAGE, to_stderr=True)
                return 2
            try:
                if arg == "--python-libpython":
                    python_libpython = _parse_python_libpython(argv[i + 1])
                else:
                    ir_scaffold = _parse_ir_scaffold(argv[i + 1])
            except ValueError as exc:
                _write_text("pcc_multi: " + str(exc) + "\n", to_stderr=True)
                _write_text(_USAGE, to_stderr=True)
                return 2
            i += 2
            continue
        if arg.startswith("--python-libpython="):
            try:
                python_libpython = _parse_python_libpython(
                    arg[len("--python-libpython="):]
                )
            except ValueError as exc:
                _write_text("pcc_multi: " + str(exc) + "\n", to_stderr=True)
                _write_text(_USAGE, to_stderr=True)
                return 2
            i += 1
            continue
        if arg.startswith("--ir-scaffold="):
            try:
                ir_scaffold = _parse_ir_scaffold(
                    arg[len("--ir-scaffold="):]
                )
            except ValueError as exc:
                _write_text("pcc_multi: " + str(exc) + "\n", to_stderr=True)
                _write_text(_USAGE, to_stderr=True)
                return 2
            i += 1
            continue
        if len(arg) > 0 and arg[0] == "-":
            _write_text(
                "pcc_multi: unknown option: " + arg + "\n",
                to_stderr=True,
            )
            _write_text(_USAGE, to_stderr=True)
            return 2
        source_specs.append(arg)
        i += 1

    if entry_module is None:
        _write_text("pcc_multi: missing required option --entry\n", to_stderr=True)
        _write_text(_USAGE, to_stderr=True)
        return 2
    if out_path is None:
        _write_text("pcc_multi: missing required option --out\n", to_stderr=True)
        _write_text(_USAGE, to_stderr=True)
        return 2
    if len(source_specs) == 0:
        _write_text("pcc_multi: at least one source is required\n", to_stderr=True)
        _write_text(_USAGE, to_stderr=True)
        return 2

    src_paths = []
    module_names = []
    for spec in source_specs:
        path, mod = _parse_src_arg(spec)
        path = (path or "") + ""
        src_paths.append(path)
        if mod is None:
            module_names.append(_infer_module_name(path))
        else:
            module_names.append((mod or "") + "")

    from pcc.frontends.python.pipeline import (
        compile_python_multi,
        PyPipelineError,
    )
    try:
        if backend is None:
            compile_python_multi(
                src_paths,
                out_path,
                verbose=verbose,
                emit_llvm_only=emit_llvm,
                entry_module=entry_module,
                module_names=module_names,
                libpython_mode=python_libpython,
                ir_scaffold_mode=ir_scaffold,
            )
        else:
            compile_python_multi(
                src_paths,
                out_path,
                verbose=verbose,
                emit_llvm_only=emit_llvm,
                entry_module=entry_module,
                module_names=module_names,
                backend=backend,
                libpython_mode=python_libpython,
                ir_scaffold_mode=ir_scaffold,
            )
    except PyPipelineError as e:
        _write_pipeline_error_debug(e)
        _write_text(
            "pcc_multi: " + _pipeline_error_text(e) + "\n",
            to_stderr=True,
        )
        return 1
    return 0


if __name__ == "__main__":
    code = main()
    if code != 0:
        _exit_process(code)
