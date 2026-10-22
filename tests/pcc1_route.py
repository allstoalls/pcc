"""Route the test suite's compiles through a native pcc.

``PCC_TEST_COMPILER=/path/to/pcc1`` makes every in-process
``pipeline.compile_python`` call in the session compile with that binary
instead of host CPython pcc, and ``pcc.driver.cli_launcher`` (``uv run pcc``,
``.venv/bin/pcc``) exec it.  The whole suite then checks the native compiler
the same way it checks the host one.

A call the native CLI cannot express (libpython on/auto, the LLVM backend,
``recursive_stdlib``, ``profile=``, ``target_triple``, ``link_args``, multi-module
compiles) runs on the host.  Every compile -- routed or not -- is appended to
``PCC_TEST_COMPILER_LOG`` (a per-session default is created when it is unset),
and the session summary counts native compiles against host fallbacks by
reason, so the host-only surface is measured rather than silently skipped.
``PCC_TEST_COMPILER_STRICT=1`` fails the session on any host fallback.

``PCC_RUNTIME_ARCHIVE`` is required: pcc1 does not rebuild its own runtime.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys


LOG_ENV = "PCC_TEST_COMPILER_LOG"
STRICT_ENV = "PCC_TEST_COMPILER_STRICT"


def _record(route: str, kind: str, detail: str) -> None:
    path = os.environ.get(LOG_ENV, "")
    if not path:
        raise RuntimeError(f"{LOG_ENV} is unset; pcc1_route.install() sets it")
    record = {
        "route": route,
        "kind": kind,
        "detail": detail,
        "test": os.environ.get("PYTEST_CURRENT_TEST", ""),
    }
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def log_routed(kind: str) -> None:
    _record("native", kind, "")


def log_unrouted(kind: str, detail: str) -> None:
    _record("host", kind, detail)


def summarize(path: str) -> dict:
    """Count compiles per route, and host fallbacks per (kind, reason)."""
    counts = {"native": 0, "host": 0}
    reasons: dict[str, int] = {}
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                route = record.get("route", "host")
                counts[route] = counts.get(route, 0) + 1
                if route == "host":
                    key = record.get("kind", "?") + ": " + record.get("detail", "")
                    reasons[key] = reasons.get(key, 0) + 1
    return {"counts": counts, "host_reasons": reasons}


def native_env(runtime_archive=None) -> dict:
    """Environment for a native compile: no host Python, cc, or auto-build."""
    env = dict(os.environ)
    env.pop("LC_ALL", None)
    if runtime_archive:
        env["PCC_RUNTIME_ARCHIVE"] = str(runtime_archive)
    env.update(
        PCC_HOST_PYTHON="/usr/bin/false",
        PCC_RUNTIME_CC="/usr/bin/false",
        PCC_NO_AUTO_PCC1="1",
    )
    return env


def install() -> None:
    compiler = os.environ["PCC_TEST_COMPILER"]
    if not os.path.isfile(compiler):
        raise RuntimeError(f"PCC_TEST_COMPILER is not a file: {compiler}")
    if not os.environ.get("PCC_RUNTIME_ARCHIVE"):
        raise RuntimeError("PCC_TEST_COMPILER needs PCC_RUNTIME_ARCHIVE")
    if not os.environ.get(LOG_ENV):
        # Set in the controlling process before xdist starts its workers, so
        # every worker appends to the one session log.
        import tempfile

        os.environ[LOG_ENV] = os.path.join(
            tempfile.gettempdir(), f"pcc-test-compiler-{os.getpid()}.jsonl"
        )

    from pcc.frontends.python import pipeline

    host_compile = pipeline.compile_python
    host_multi = pipeline.compile_python_multi

    def compile_python(
        src_path,
        out_path,
        *,
        verbose=False,
        emit_llvm_only=False,
        libpython_mode=None,
        ir_scaffold_mode=None,
        backend=None,
        gpu_backend=None,
        target_triple=None,
        recursive_stdlib=False,
        python_library=False,
        runtime_archive=None,
        link_args=(),
        profile=None,
    ):
        reason = ""
        if libpython_mode not in (None, "off"):
            reason = "libpython_mode=" + str(libpython_mode)
        elif backend not in (None, "self"):
            reason = "backend=" + str(backend)
        elif recursive_stdlib:
            reason = "recursive_stdlib"
        elif profile is not None:
            reason = "profile"
        elif gpu_backend is not None:
            reason = "gpu_backend=" + str(gpu_backend)
        elif target_triple is not None:
            reason = "target_triple=" + str(target_triple)
        elif link_args:
            reason = "link_args"
        if reason:
            log_unrouted("compile_python", reason)
            return host_compile(
                src_path,
                out_path,
                verbose=verbose,
                emit_llvm_only=emit_llvm_only,
                libpython_mode=libpython_mode,
                ir_scaffold_mode=ir_scaffold_mode,
                backend=backend,
                gpu_backend=gpu_backend,
                target_triple=target_triple,
                recursive_stdlib=recursive_stdlib,
                python_library=python_library,
                runtime_archive=runtime_archive,
                link_args=link_args,
                profile=profile,
            )
        argv = [
            compiler,
            "--backend",
            "self",
            "--python-libpython",
            "off",
            "--ir-scaffold",
            ir_scaffold_mode or "on",
        ]
        if python_library:
            argv.append("--python-library")
        if emit_llvm_only:
            argv += ["--emit-llvm", str(out_path), str(src_path)]
        else:
            argv += [str(src_path), "-o", str(out_path)]
        completed = subprocess.run(
            argv,
            env=native_env(runtime_archive),
            capture_output=True,
            text=True,
            timeout=900,
        )
        if verbose:
            sys.stderr.write(completed.stderr)
        if completed.returncode != 0:
            raise pipeline.PyPipelineError(
                f"{compiler} exited {completed.returncode}:\n"
                + completed.stdout[-4000:]
                + completed.stderr[-8000:]
            )
        log_routed("compile_python")
        return None

    def compile_python_multi(*args, **kwargs):
        log_unrouted("compile_python_multi", "no native multi-module CLI")
        return host_multi(*args, **kwargs)

    pipeline.compile_python = compile_python
    pipeline.compile_python_multi = compile_python_multi
