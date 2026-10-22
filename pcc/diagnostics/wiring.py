"""Runtime wiring for roadmap features that already have core modules.

This module connects existing observability, pass-explain, cache-explain and
tail-call utilities to the real compiler entry points.  It deliberately avoids
the earlier "catalog only" pattern: every installer below monkey-patches a
currently-used function in ``cli_core.py``, ``pipeline.py``, ``project.py`` or
``ir_pass_pipeline.py``.

The install path is guarded by ``PCC_DISABLE_ROADMAP_DEEPWIRE`` so bootstrap
bisecting can turn it off without removing the code.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import functools
import hashlib
import json
import os
import sys
import time
from typing import Any, Optional

from pcc.diagnostics.compile_observability import ObservabilityOptions, ObservedCompileError, observed_compile, parse_observability_cli_option
from pcc.diagnostics.profile_events import ProfileRecorder, write_profile_json

_INSTALLED = False
_LAST_CLI_OBSERVABILITY: dict[str, Any] = {}


def install() -> None:
    """Install all deep runtime wires. Safe to call multiple times."""
    global _INSTALLED
    if _INSTALLED:
        return
    if _truthy(os.environ.get("PCC_DISABLE_ROADMAP_DEEPWIRE")):
        _INSTALLED = True
        return
    _install_cli_core()
    _install_pipeline_profile()
    _install_ir_pass_explain_and_tailcall()
    _install_project_cache_explain()
    _INSTALLED = True


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _install_cli_core() -> None:
    try:
        from pcc.driver import cli_core
    except Exception:
        return
    if getattr(cli_core, "_pcc_deepwire_installed", False):
        return

    help_extra = (
        "  --diagnostic-format FMT   text (default), json, or sarif for hard errors.\n"
        "  --profile-json PATH       Write compiler phase/profile JSON.\n"
        "  --explain-fallback        Include native/fallback routing details.\n"
        "  --passes=explain          Explain selected/skipped pass decisions.\n"
        "  --explain-cache[=PATH]    Explain C project/source cache keys.\n"
    )
    if "--diagnostic-format" not in getattr(cli_core, "_HELP_TEXT", ""):
        cli_core._HELP_TEXT = cli_core._HELP_TEXT.replace(
            "  --verbose                 Print Python pipeline timing.\n",
            "  --verbose                 Print Python pipeline timing.\n" + help_extra,
        )

    original_parse = cli_core.parse_cli_args
    original_execute = cli_core.execute_cli

    @functools.wraps(original_parse)
    def parse_cli_args_deepwire(argv=None):
        raw = list(cli_core._normalized_sys_argv() if argv is None else argv)
        filtered: list[str] = []
        opts: dict[str, Any] = {}
        i = 0
        while i < len(raw):
            arg = (raw[i] or "") + ""
            try:
                parsed = parse_observability_cli_option(arg, raw, i)
            except ValueError as exc:
                return None, 2, str(exc)
            if parsed is not None:
                key, value, next_i = parsed
                opts[key] = True if key == "explain_fallback" else value
                i = next_i
                continue
            if arg == "--passes=explain" or arg == "--passes-explain":
                opts["passes_explain"] = "1"
                i += 1
                continue
            if arg.startswith("--explain-cache="):
                opts["explain_cache"] = arg.split("=", 1)[1] or "-"
                i += 1
                continue
            if arg == "--explain-cache":
                # Bare form is a flag that defaults to stdout; do not consume
                # the next arg (which is typically the input PATH).  Use the
                # ``--explain-cache=PATH`` form to write to a file.
                opts["explain_cache"] = "-"
                i += 1
                continue
            filtered.append(arg)
            i += 1
        _LAST_CLI_OBSERVABILITY.clear()
        _LAST_CLI_OBSERVABILITY.update(opts)
        # Eagerly publish observability options to the process environment so
        # ``cli_main``'s inline ``.py`` path (which calls ``_observed_compile``
        # directly without going through ``execute_cli``) sees the same env
        # state as the ``execute_cli_deepwire`` wrapper would set later.
        env_updates = _env_from_cli_opts(opts)
        for key, value in env_updates.items():
            if value is None or value == "":
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return original_parse(filtered)

    @functools.wraps(original_execute)
    def execute_cli_deepwire(*args, **kwargs):
        opts = dict(_LAST_CLI_OBSERVABILITY)
        env_updates = _env_from_cli_opts(opts)
        with _temporary_env(env_updates):
            if opts.get("diagnostic_format") or opts.get("profile_json") or opts.get("explain_fallback"):
                observed_opts = ObservabilityOptions(
                    diagnostic_format=str(opts.get("diagnostic_format") or "text"),
                    profile_json=opts.get("profile_json"),
                    explain_fallback=bool(opts.get("explain_fallback")),
                    phase="cli-core",
                    entry="pcc.driver.cli_core",
                )
                try:
                    return observed_compile(
                        original_execute,
                        *args,
                        options=observed_opts,
                        metadata={"path": kwargs.get("path", "")},
                        **kwargs,
                    )
                except ObservedCompileError as exc:
                    sys.stderr.write(exc.formatted)
                    if not exc.formatted.endswith("\n"):
                        sys.stderr.write("\n")
                    return 1
            return original_execute(*args, **kwargs)

    cli_core.parse_cli_args = parse_cli_args_deepwire
    cli_core.execute_cli = execute_cli_deepwire
    cli_core._pcc_deepwire_installed = True


def _env_from_cli_opts(opts: dict[str, Any]) -> dict[str, Optional[str]]:
    env: dict[str, Optional[str]] = {}
    if opts.get("profile_json"):
        env["PCC_PROFILE_JSON"] = str(opts["profile_json"])
    if opts.get("diagnostic_format"):
        env["PCC_DIAGNOSTIC_FORMAT"] = str(opts["diagnostic_format"])
    if opts.get("explain_fallback"):
        env["PCC_EXPLAIN_FALLBACK"] = "1"
    if opts.get("passes_explain"):
        env["PCC_PASSES_EXPLAIN"] = "1"
    if opts.get("explain_cache"):
        env["PCC_EXPLAIN_CACHE"] = str(opts["explain_cache"])
    return env


@contextmanager
def _temporary_env(updates: dict[str, Optional[str]]):
    old: dict[str, Optional[str]] = {}
    try:
        for key, value in updates.items():
            old[key] = os.environ.get(key)
            if value is None or value == "":
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _install_pipeline_profile() -> None:
    try:
        from pcc.frontends.python import pipeline
    except Exception:
        return
    original = getattr(pipeline, "compile_python", None)
    if original is None or getattr(original, "_pcc_profiled", False):
        return

    @functools.wraps(original)
    def compile_python_profiled(*args, **kwargs):
        profile_path = kwargs.pop("profile_json", None) or os.environ.get("PCC_PROFILE_JSON")
        if not profile_path:
            return original(*args, **kwargs)
        recorder = ProfileRecorder()
        recorder.set_metadata("entry", "pcc.frontends.python.pipeline.compile_python")
        if args:
            recorder.set_metadata("input", str(args[0]))
        for key in ("emit_llvm_only", "libpython_mode", "ir_scaffold_mode", "backend", "python_library"):
            if key in kwargs:
                recorder.set_metadata(key, kwargs[key])
        with ExitStack() as stack:
            _patch_pipeline_phase_functions(stack, recorder)
            try:
                with recorder.phase("pipeline.total"):
                    return original(*args, **kwargs)
            finally:
                write_profile_json(str(profile_path), recorder)

    compile_python_profiled._pcc_profiled = True
    pipeline.compile_python = compile_python_profiled


def _patch_pipeline_phase_functions(stack: ExitStack, recorder: ProfileRecorder) -> None:
    def patch_attr(obj: object, name: str, phase: str) -> None:
        if not hasattr(obj, name):
            return
        fn = getattr(obj, name)
        if not callable(fn) or getattr(fn, "_pcc_phase_wrapped", False):
            return

        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            with recorder.phase(phase):
                return fn(*args, **kwargs)

        wrapped._pcc_phase_wrapped = True
        setattr(obj, name, wrapped)
        stack.callback(setattr, obj, name, fn)

    try:
        from pcc.frontends.python import py_lift
        patch_attr(py_lift, "parse_and_lift", "parse")
    except Exception:
        pass
    try:
        from pcc.frontends.python import type_infer
        patch_attr(type_infer, "infer_module", "type_infer")
    except Exception:
        pass
    try:
        from pcc.frontends.python.codegen import layer1
        orig_generate = layer1.L1CodeGen.generate

        @functools.wraps(orig_generate)
        def generate_wrapped(self, *args, **kwargs):
            with recorder.phase("codegen.layer1"):
                return orig_generate(self, *args, **kwargs)

        if not getattr(orig_generate, "_pcc_phase_wrapped", False):
            generate_wrapped._pcc_phase_wrapped = True
            layer1.L1CodeGen.generate = generate_wrapped
            stack.callback(setattr, layer1.L1CodeGen, "generate", orig_generate)
    except Exception:
        pass
    try:
        from pcc.frontends.python import ir_pass_pipeline
        patch_attr(ir_pass_pipeline, "run_python_ir_pass_pipeline", "ir_passes")
    except Exception:
        pass
    try:
        from pcc.frontends.python import pipeline as pipeline_mod
        for name, phase in (
            ("_collect_relative_module_closure", "source_closure"),
            ("_collect_multi_source_relative_closure", "source_closure"),
            ("_filter_ir_scaffold_closure", "source_closure"),
            ("_build_runtime_archive", "runtime_archive"),
            ("_link_with_self_backend", "self_backend"),
        ):
            patch_attr(pipeline_mod, name, phase)
    except Exception:
        pass
    import subprocess
    for name in ("run", "check_call", "check_output"):
        patch_attr(subprocess, name, "subprocess")


def _install_ir_pass_explain_and_tailcall() -> None:
    try:
        from pcc.frontends.python import ir_pass_pipeline
    except Exception:
        return
    original = getattr(ir_pass_pipeline, "run_python_ir_pass_pipeline", None)
    if original is None or getattr(original, "_pcc_deepwire_pass", False):
        return

    @functools.wraps(original)
    def run_pipeline_deepwire(ir_text: str, *, pass_names, module_name: str):
        pass_list = [str(p).strip() for p in pass_names if str(p).strip()]
        tail_requested = _truthy(os.environ.get("PCC_ENABLE_TAILCALL_REWRITE")) or any(
            p in {"tailcall", "tco", "tailcall-ir"} for p in pass_list
        )
        remaining = tuple(p for p in pass_list if p not in {"tailcall", "tco", "tailcall-ir"})
        current = ir_text
        tail_result = None
        if tail_requested:
            from pcc.ir.optimization.tailcall_ir import rewrite_simple_void_self_tailcalls
            tail_result = rewrite_simple_void_self_tailcalls(current)
            current = tail_result.ir_text
            _write_optional_report(os.environ.get("PCC_TAILCALL_REPORT"), tail_result.report_json())
        if _truthy(os.environ.get("PCC_PASSES_EXPLAIN")):
            _emit_pass_explain(
                ir_pass_pipeline, current, remaining, module_name,
                tail_requested=tail_requested, tail_result=tail_result,
            )
        if not remaining:
            return current
        return original(current, pass_names=remaining, module_name=module_name)

    run_pipeline_deepwire._pcc_deepwire_pass = True
    ir_pass_pipeline.run_python_ir_pass_pipeline = run_pipeline_deepwire


def _emit_pass_explain(ir_pass_pipeline: object, ir_text: str, pass_names: tuple[str, ...], module_name: str, *, tail_requested: bool = False, tail_result: object | None = None) -> None:
    from pcc.diagnostics.pass_explain import PassDecision, format_pass_explain
    try:
        expanded = tuple(ir_pass_pipeline._expand_pass_names(pass_names, len(ir_text)))
    except Exception:
        expanded = tuple(pass_names)
    decisions: list[PassDecision] = []
    if tail_requested:
        candidates = tuple(getattr(tail_result, "candidates", ()) or ())
        if candidates:
            reason = "; ".join(
                f"{getattr(c, 'function', '<unknown>')}: {getattr(c, 'reason', '')}"
                for c in candidates
            )
        else:
            reason = "tailcall pass requested; no self-tail-call candidates"
        decisions.append(PassDecision(
            name="tailcall",
            ran=bool(getattr(tail_result, "rewritten", False)),
            reason=reason,
        ))
    for name in expanded:
        reason = "selected"
        ran = True
        try:
            skip = ir_pass_pipeline._skip_status_for_pass(name, len(ir_text))
            if skip is not None:
                ran = False
                reason = skip[0]
        except Exception:
            pass
        decisions.append(PassDecision(name=name, ran=ran, reason=reason))
    fmt = "json" if os.environ.get("PCC_PASSES_EXPLAIN_FORMAT") == "json" else "text"
    text = format_pass_explain(decisions, fmt=fmt)
    target = os.environ.get("PCC_PASSES_EXPLAIN_PATH")
    if target:
        _write_optional_report(target, text)
    else:
        sys.stderr.write(f"[pcc.diagnostics.pass_explain] module={module_name}\n{text}\n")


def _install_project_cache_explain() -> None:
    try:
        from pcc.driver import project
    except Exception:
        return
    if getattr(project, "_pcc_diagnostics_cache_explain_installed", False):
        return

    def wrap(name: str) -> None:
        if not hasattr(project, name):
            return
        original = getattr(project, name)
        if not callable(original):
            return

        @functools.wraps(original)
        def wrapped(*args, **kwargs):
            started = time.perf_counter()
            result = original(*args, **kwargs)
            if os.environ.get("PCC_EXPLAIN_CACHE"):
                _emit_cache_explain(name, args, kwargs, result, time.perf_counter() - started)
            return result

        setattr(project, name, wrapped)

    for name in ("collect_project", "collect_translation_units", "collect_cpp_args"):
        wrap(name)
    project._pcc_diagnostics_cache_explain_installed = True


def _emit_cache_explain(name: str, args: tuple[Any, ...], kwargs: dict[str, Any], result: Any, elapsed: float) -> None:
    input_path = str(args[0]) if args else str(kwargs.get("path", ""))
    payload = {
        "schema": "pcc.diagnostics.cache_explain.v1",
        "event": name,
        "input": input_path,
        "input_hash": _path_hash(input_path),
        "elapsed_ms": round(elapsed * 1000.0, 3),
        "result_kind": type(result).__name__,
    }
    text = json.dumps(payload, sort_keys=True)
    target = os.environ.get("PCC_EXPLAIN_CACHE")
    if target and target != "-":
        _write_optional_report(target, text, append=True)
    else:
        sys.stderr.write(text + "\n")


def _path_hash(path: str) -> str:
    h = hashlib.sha256()
    if os.path.isfile(path):
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    if os.path.isdir(path):
        for root, _dirs, files in os.walk(path):
            for filename in sorted(files):
                if not filename.endswith((".c", ".h", ".py")):
                    continue
                full = os.path.join(root, filename)
                h.update(os.path.relpath(full, path).encode("utf-8", "surrogateescape"))
                with open(full, "rb") as f:
                    h.update(f.read())
        return h.hexdigest()
    return ""


def _write_optional_report(path: Optional[str], text: str, *, append: bool = False) -> None:
    if not path or path == "-":
        sys.stderr.write(text)
        if not text.endswith("\n"):
            sys.stderr.write("\n")
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    mode = "a" if append else "w"
    with open(path, mode, encoding="utf-8") as f:
        f.write(text)
        if not text.endswith("\n"):
            f.write("\n")
