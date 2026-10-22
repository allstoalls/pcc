"""Mode normalization and compatibility guards for the Python pipeline."""

from __future__ import annotations

import os
import sys
from typing import Optional

PY_LIBPYTHON_MODE_ENV = "PCC_PYTHON_LIBPYTHON"
IR_SCAFFOLD_MODE_ENV = "PCC_IR_SCAFFOLD"
GPU_BACKEND_ENV = "PCC_GPU_BACKEND"
DEFAULT_GPU_BACKEND = "none"
KNOWN_GPU_BACKENDS = ("metal", "none")
SELF_BACKEND_PUBLISH_SYNC_ENV = "PCC_SELF_BACKEND_PUBLISH_SYNC"


class PyPipelineError(RuntimeError):
    """Raised when the Python pipeline fails in a user-visible way.

    The message is ALSO stored in ``pcc_message``: under pcc1 this
    exception has been observed with ``str()==""``, ``args is None`` and
    no ``__cause__`` (2026-08-27 stage2 failures), so the diagnostic
    formatter needs a storage path independent of the args machinery.
    Eleven probe shapes failed to reproduce the loss outside a real
    stage build; until the mechanism is found, this attribute makes the
    next real failure self-diagnosing instead of printing "compile
    failed" with no text.
    """

    def __init__(self, message: str = "") -> None:
        # Preserve the exact CPython args shape: no-arg construction must
        # keep args == (), not ('',).
        #
        # ``super().__init__`` rather than ``RuntimeError.__init__(self, ...)``:
        # pcc's runtime does not expose ``__init__`` on builtin exception type
        # objects, so the unbound form raised ``AttributeError: __init__``
        # while *formatting* a link failure. That masked the real error and
        # made every installed-pcc1 compile fail
        # (tests/integration/test_installed_pcc1_repl_debug_profile.py).
        # The runtime gap is separate work; this call is the only site of the
        # unbound form in the tree.
        self.pcc_message = str(message)
        if message:
            super().__init__(message)
        else:
            super().__init__()


def failed_process_detail(exc) -> str:
    """Describe a failed child process without trusting its attributes.

    A compiled pcc1 stage has been observed losing ``returncode`` on a caught
    ``CalledProcessError``; ``f"... (exit {exc.returncode})"`` then raised
    ``AttributeError('returncode')`` while *reporting* the failure, replacing
    every real link error with the single word "returncode". Read each field
    behind its own guard so the underlying message always survives.

    This lives beside ``PyPipelineError`` because every "report a child process
    failure" site needs it, not only the self-backend linker: an unguarded read
    on any of them erases the diagnosis it was meant to deliver.
    """
    parts = [type(exc).__name__]
    try:
        parts.append("exit=" + str(exc.returncode))
    except Exception:
        parts.append("exit=<unreadable>")
    try:
        message = str(exc)
    except Exception:
        message = ""
    if message:
        parts.append(message)
    return "; ".join(parts)


def normalize_gpu_backend_name(value: Optional[str]) -> str:
    if value is None:
        return DEFAULT_GPU_BACKEND
    candidate = str(value or "").strip().lower()
    if not candidate:
        return DEFAULT_GPU_BACKEND
    if candidate in ("off", "disabled", "host", "cpu"):
        return "none"
    return candidate


def resolve_gpu_backend_kind(requested: Optional[str]) -> str:
    env_raw = os.environ.get(GPU_BACKEND_ENV)
    kind = normalize_gpu_backend_name(
        requested if requested is not None else env_raw
    )
    if kind not in KNOWN_GPU_BACKENDS:
        known = ", ".join(sorted(KNOWN_GPU_BACKENDS))
        raise ValueError(
            f"unknown gpu backend {kind!r}; expected one of: {known}"
        )
    return kind


def self_backend_publish_sync_enabled() -> bool:
    value = os.environ.get(SELF_BACKEND_PUBLISH_SYNC_ENV, "").strip().lower()
    return value not in ("0", "false", "no", "off")


def normalize_native_backend_name(value: Optional[str]) -> str:
    if value is None:
        value = os.environ.get("PCC_BACKEND")
    candidate = str(value or "").strip().lower()
    if not candidate:
        return "self"
    return candidate


# An explicit setting of either route variable selects the route as given.
_DIRECT_INDEXED_ROUTE_SELECTORS = (
    "PCC_DIRECT_INDEXED_KERNEL_CAPTURE",
    "PCC_DIRECT_INDEXED_KERNEL_EMIT",
)
DIRECT_INDEXED_ROUTE_ENV = _DIRECT_INDEXED_ROUTE_SELECTORS + (
    # One inline post-call error edge and a shared traceback landing per
    # (function, target) instead of call.cont/err.frame blocks per call.
    # Defaulted with the route; an explicit value of its own is kept.
    "PCC_DIRECT_INLINE_ERROR_EDGE_CAPTURE",
)


def select_direct_indexed_route(
    *,
    backend: Optional[str],
    emit_llvm_only: bool,
    python_library: bool,
    target_triple: Optional[str],
    gpu_backend: Optional[str],
    libpython_mode: Optional[str],
) -> list:
    """Default a host executable compile to the direct indexed route.

    The frontend then hands the self backend its indexed kernel instead of
    LLVM text that the emit workers parse back into the same kernel. Text
    stays the route for LLVM/library output, cross targets, GPU host lowering
    and libpython modes. An explicit ``PCC_DIRECT_INDEXED_KERNEL_*`` setting
    always wins; ``0`` selects the text route for debugging. Returns the
    variables set here, for ``restore_direct_indexed_route``.
    """
    for name in _DIRECT_INDEXED_ROUTE_SELECTORS:
        if name in os.environ:
            return []
    if emit_llvm_only or python_library or target_triple is not None:
        return []
    if normalize_native_backend_name(backend) != "self":
        return []
    gpu = normalize_gpu_backend_name(
        gpu_backend if gpu_backend is not None else os.environ.get(GPU_BACKEND_ENV)
    )
    if gpu != "none":
        return []
    try:
        if resolve_libpython_mode(libpython_mode) != "off":
            return []
    except PyPipelineError:
        # The compile itself reports the invalid mode.
        return []
    # Python IR passes still run on LLVM text; with them on, a direct
    # worker would render and reparse its own text without the split emit
    # lanes. Until the passes read the indexed kernel, keep that case on text.
    from pcc.frontends.python.pipeline_pass_config import resolve_python_ir_pass_names

    if resolve_python_ir_pass_names():
        return []
    selected = [name for name in DIRECT_INDEXED_ROUTE_ENV if name not in os.environ]
    for name in selected:
        os.environ[name] = "1"
    return selected


def restore_direct_indexed_route(names) -> None:
    for name in names:
        os.environ.pop(name, None)


def resolve_native_backend(backend: Optional[str]) -> str:
    kind = normalize_native_backend_name(backend)
    if kind != "self":
        raise PyPipelineError(
            "unknown backend " f"{kind!r}; expected self"
        )
    return kind


def native_backend_kind(backend) -> str:
    kind = str(getattr(backend, "kind", backend) or "")
    if kind != "self":
        raise PyPipelineError(
            "Python native emission backend "
            f"{kind!r} is not supported; expected self"
        )
    return kind


def resolve_libpython_mode(mode: Optional[str]) -> str:
    raw = mode
    if raw is None:
        raw = os.environ.get(PY_LIBPYTHON_MODE_ENV, "")
    normalized = str(raw or "").strip().lower()
    if not normalized:
        return "off"
    if normalized == "auto":
        return "auto"
    if normalized in ("on", "true", "yes", "1"):
        return "on"
    if normalized in ("off", "false", "no", "0"):
        return "off"
    raise PyPipelineError(
        "invalid libpython mode " f"{raw!r}; expected auto, on, or off"
    )


def resolve_ir_scaffold_mode(mode: Optional[str]) -> str:
    """Resolve the ``--ir-scaffold`` mode to a canonical value."""

    raw = mode
    if raw is None:
        raw = os.environ.get(IR_SCAFFOLD_MODE_ENV, "")
    normalized = str(raw or "").strip().lower()
    if not normalized or normalized == "auto":
        return "on"
    if normalized in ("on", "true", "yes", "1"):
        return "on"
    if normalized in ("off", "false", "no", "0"):
        return "off"
    raise PyPipelineError(
        "invalid ir scaffold mode " f"{raw!r}; expected off, on, or auto"
    )


def finalize_libpython_mode(
    *,
    detected: bool,
    mode: str,
    context: str,
    reasons: list[str],
) -> bool:
    if mode == "on":
        return True
    if mode == "off" and detected:
        suffix = ""
        if reasons:
            suffix = " (" + "; ".join(reasons) + ")"
        if bool(os.environ.get("PCC_DEBUG_LIBPYTHON_GATE_BYPASS")):
            sys.stderr.write("[libpython_gate_bypass] " + context + suffix + "\n")
            return detected
        raise PyPipelineError(
            "Python pipeline requires libpython fallback for "
            + context
            + suffix
            + "; rerun with --python-libpython=auto/on or "
            + "PCC_PYTHON_LIBPYTHON=auto/on"
        )
    return detected


def reject_mixed_extension_object_models(
    *,
    needs_libpython: bool,
    needs_native_extension_exports: bool,
) -> None:
    if needs_libpython and needs_native_extension_exports:
        raise PyPipelineError(
            "pcc-native extension imports cannot be combined with libpython mode; "
            "use --python-libpython=off or a CPython-ABI extension"
        )
