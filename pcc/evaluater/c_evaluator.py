import re
import json
import hashlib
import inspect
import time
from ctypes.util import find_library
import os
import multiprocessing
import subprocess
import shutil
import sys
import tempfile
import platform
from concurrent.futures import ProcessPoolExecutor
from itertools import repeat


try:
    import llvmlite.binding as llvm
except ImportError:  # pragma: no cover - exercised by the self-hosted stage
    # Allowed to be absent, not deferred out of the module surface.
    #
    # This module holds both halves of the C toolchain: the compile driver
    # (`_compile_translation_units` and friends, which `--emit-obj` uses) and
    # the MCJIT evaluator. A hard module-scope import coupled them, so
    # *compiling* C required LLVM's execution engine -- and a stage without
    # llvmlite reported the whole C driver as unowned instead of naming the
    # gap. Keeping the name bound (to None when unavailable) preserves
    # `c_evaluator.llvm` for the callers and tests that legitimately want the
    # real binding, while the compile path no longer needs it at all.
    llvm = None


def _llvm():
    """The llvmlite binding, or a clear error naming what is missing."""
    if llvm is None:
        raise ImportError(
            "llvmlite is required for this C toolchain path "
            "(MCJIT evaluation, the LLVM backend, or an external opt pipeline)"
        )
    return llvm
from ..py_frontend.pipeline_targets import host_target_triple
from ..backend import (
    BackendUnavailable,
    backend_request_allows_unimplemented,
    backend_signature,
    resolve_backend,
)
from ..backend.self_backend_dispatch import emit_self_asm, self_backend_target_identity
from ..backend.self_backend_parse import parse_self_backend_target_triple
from ..codegen.c_codegen import LLVMCodeGenerator, postprocess_ir_text
from ..parse.c_parser import CParser
from ..preprocessor import preprocess
from ..project import TranslationUnit

from ctypes import (
    CFUNCTYPE,
    c_float,
    c_double,
    c_int64,
    c_int32,
    c_int16,
    c_int8,
    c_char_p,
    c_void_p,
    POINTER,
)


_TYPEDEF_CLEANUP = re.compile(
    r"typedef\s+(int|char|short|long|double|float|void)\s+\1\s*;"
)
# Compiler builtins that survive cc -E but pycparser doesn't know about.
# Replace all occurrences with va_list, then clean up self-referential typedefs.
_VA_TYPEDEF_NORMALIZE = re.compile(
    r"^typedef\s+(?:__builtin_va_list|__darwin_va_list|__gnuc_va_list)\s+(\w+)\s*;$",
    re.MULTILINE,
)
_SELF_TYPEDEF = re.compile(
    r"^typedef\s+(\w+)\s+\1\s*;$", re.MULTILINE
)
_SIZEOF_TYPEOF_SIZE_T = re.compile(
    r"^\s*typedef\s+__typeof\s*\(\s*sizeof\s*\(\s*int\s*\)\s*\)\s+size_t\s*;$",
    re.MULTILINE,
)
_TYPEOF_ID = re.compile(r"\b(?:__typeof__|__typeof|typeof)\s*\(\s*([A-Za-z_]\w*)\s*\)")
_TAGGED_VAR_DECL = re.compile(
    r"^\s*(struct|union|enum)\s+([A-Za-z_]\w*)\s*\{.*\}\s*([A-Za-z_]\w*)\s*;\s*$"
)
_TYPEDEF_TAG_ALIAS = re.compile(
    r"^\s*typedef\s+(struct|union|enum)\s+([A-Za-z_]\w*)\s+([A-Za-z_]\w*)\s*;\s*$"
)
_PLAIN_TYPED_VAR_DECL = re.compile(
    r"^\s*([A-Za-z_]\w*)\s+([A-Za-z_]\w*)\s*(?:=\s*.*)?;\s*$"
)
_SIMPLE_RANGE_DESIGNATOR = re.compile(
    r"\{\s*\[\s*0\s*\.\.\.\s*(\d+)\s*\]\s*=\s*([^,{}]+?)\s*\}"
)
_FIXED_WIDTH_ENUM_BASE = re.compile(r"\benum\s*:\s*[A-Za-z_]\w*")
_CPP11_ATTRIBUTE = re.compile(r"\[\[[\s\S]*?\]\]")
_IGNORED_CLANG_PRAGMA = re.compile(r"(?m)^[ \t]*#\s*pragma\s+clang\b[^\n]*$")
_EMBED_DIRECTIVE = re.compile(r"(?m)^[ \t]*#\s*embed\b[^\n]*$")
_CLANG_TEST_SIMULATOR_INCLUDE = re.compile(
    r'(?m)^[ \t]*#\s*include\s+"(?:\.\./)?Inputs/'
    r"(system-header-simulator(?:-for-malloc)?\.h)\"\s*$"
)
_TYPEDEF_WCHAR_T = re.compile(r"\btypedef\b[^;]*\bwchar_t\b")
_TYPEDEF_BOOL = re.compile(r"\btypedef\b[^;]*\bbool\b")
_TRUE_FALSE_ENUM = re.compile(r"\benum\b[^;{]*\{[^}]*\btrue\b[^}]*\bfalse\b|\benum\b[^;{]*\{[^}]*\bfalse\b[^}]*\btrue\b")
_SIMPLE_TYPE_SPECIFIERS = {
    "void",
    "char",
    "short",
    "int",
    "long",
    "float",
    "double",
    "signed",
    "unsigned",
}

# Some large MCJIT executions on the current llvmlite/LLVM build abort if
# Python GC later revisits detached engine/module wrappers. Keep detached JIT
# wrappers process-global so they are only dropped during interpreter shutdown.
_DETACHED_MCJIT_WRAPPERS = []
_COMPILE_CACHE_VERSION = "v4"
_CHEAP_LLVM_PIPELINE_ENV = "PCC_CHEAP_LLVM_PIPELINE"
_PASS_DISABLE_ENV = "PCC_DISABLE_PASSES"
_LLVM_TEXT_PIPELINE_ENV = "PCC_LLVM_PIPELINE"
_LLVM_PIPELINE_TRANSPORT_ENV = "PCC_LLVM_PIPELINE_TRANSPORT"
_LLVM_DISABLE_PASSES_ENV = "PCC_LLVM_DISABLE_PASSES"
_LLVM_OPT_BIN_ENV = "PCC_LLVM_OPT_BIN"
_SELF_OBJECT_EMITTER_ENV = "PCC_SELF_OBJ"
_SELF_OBJECT_EMITTER_PCC = "pcc"
_SELF_OBJECT_EMITTER_SYSTEM_AS = "system-as"
_SELF_OBJECT_EMITTER_DIRECT_TARGETS = frozenset(
    {
        "self-aarch64-darwin-v0",
        "self-x86_64-linux-v0",
    }
)
_LINUX_FREESTANDING_NON_EXEC_LINK_OPTIONS = frozenset(
    {
        "-pie",
        "--pie",
        "-static-pie",
        "--static-pie",
        "--pic-executable",
        "-shared",
        "--shared",
        "-Bshareable",
        "-r",
        "--relocatable",
    }
)
FREESTANDING_C_LIBC_PY_MODULES = (
    "freestanding_mem_str",
    "freestanding_allocator",
    "freestanding_platform_io",
    "freestanding_platform_fs",
    "freestanding_platform_env",
    "freestanding_platform_system",
    "freestanding_platform_time",
    "freestanding_platform_process",
    "freestanding_platform_socket",
    "freestanding_stdio",
)
_DEFAULT_CHEAP_LLVM_PASSES = (
    "add_sroa_pass",
    "add_instruction_combine_pass",
    "add_new_gvn_pass",
    "add_simplify_cfg_pass",
    "add_aggressive_dce_pass",
)


def _select_self_object_emitter(requested, target_identity):
    """Resolve the self-backend object writer without an implicit fallback.

    pcc owns direct object emission for AArch64 Mach-O and x86_64 ELF.  Other
    registered self targets retain the system assembler as their default until
    they have a native object writer of their own.  An explicit selection is always
    validated so a misspelling cannot silently cross the system-tool boundary.
    """
    if requested is None or requested == "":
        if target_identity in _SELF_OBJECT_EMITTER_DIRECT_TARGETS:
            return _SELF_OBJECT_EMITTER_PCC
        return _SELF_OBJECT_EMITTER_SYSTEM_AS
    if requested not in (
        _SELF_OBJECT_EMITTER_PCC,
        _SELF_OBJECT_EMITTER_SYSTEM_AS,
    ):
        raise BackendUnavailable(
            f"{_SELF_OBJECT_EMITTER_ENV} must be "
            f"{_SELF_OBJECT_EMITTER_PCC!r} or "
            f"{_SELF_OBJECT_EMITTER_SYSTEM_AS!r}, got {requested!r}"
        )
    if (
        requested == _SELF_OBJECT_EMITTER_PCC
        and target_identity not in _SELF_OBJECT_EMITTER_DIRECT_TARGETS
    ):
        raise BackendUnavailable(
            f"{_SELF_OBJECT_EMITTER_ENV}={_SELF_OBJECT_EMITTER_PCC} direct "
            "object emission requires target "
            f"one of {sorted(_SELF_OBJECT_EMITTER_DIRECT_TARGETS)!r}, "
            f"got {target_identity!r}"
        )
    return requested


_CHEAP_LLVM_PASS_ALIASES = {
    "sroa": "add_sroa_pass",
    "instcombine": "add_instruction_combine_pass",
    "instructioncombine": "add_instruction_combine_pass",
    "newgvn": "add_new_gvn_pass",
    "gvn": "add_new_gvn_pass",
    "simplifycfg": "add_simplify_cfg_pass",
    "cfg": "add_simplify_cfg_pass",
    "adce": "add_aggressive_dce_pass",
    "aggressivedce": "add_aggressive_dce_pass",
    "dce": "add_dead_code_elimination_pass",
    "reassociate": "add_reassociate_pass",
    "sccp": "add_sccp_pass",
    "memcopyopt": "add_mem_copy_opt_pass",
    "tailcall": "add_tail_call_elimination_pass",
}


def _default_compile_cache_dir():
    override = os.environ.get("PCC_COMPILE_CACHE_DIR")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    xdg_cache_home = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache_home:
        base_dir = os.path.abspath(os.path.expanduser(xdg_cache_home))
    else:
        base_dir = os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base_dir, "pcc", "compile-cache")


def _compile_cache_enabled(use_compile_cache):
    if not use_compile_cache:
        return False
    flag = os.environ.get("PCC_DISABLE_COMPILE_CACHE", "")
    return flag.lower() not in {"1", "true", "yes", "on"}


def _normalize_fsanitize(fsanitize):
    """Normalize the opt-in ``-fsanitize`` request to a tuple of check names.

    SEC-P1-UBSAN. Accepts ``None`` / ``""`` (→ ``()``, everything OFF — the
    default), a comma-separated Clang-style string (e.g.
    ``"undefined"`` or ``"integer-divide-by-zero,shift"``), or an iterable of
    names. Returns a de-duplicated, order-stable tuple so it can flow through
    the artifact/compile chain as a plain value.
    """
    if not fsanitize:
        return ()
    if isinstance(fsanitize, str):
        parts = fsanitize.split(",")
    else:
        parts = list(fsanitize)
    seen = []
    for name in parts:
        name = str(name).strip()
        if name and name not in seen:
            seen.append(name)
    return tuple(seen)


def _normalize_compile_cache_dir(cache_dir):
    return os.path.abspath(
        os.path.expanduser(cache_dir or _default_compile_cache_dir())
    )


def _normalize_llvm_dump_dir(llvmdump, cache_dir=None):
    """Return the directory that owns explicit LLVM diagnostic dumps.

    ``True`` retains the historical opt-in behavior without writing into the
    caller's working directory.  A path selects an exact artifact directory;
    the environment override gives CLI users the same control while preserving
    the existing boolean flag.
    """
    if not llvmdump:
        return None
    if isinstance(llvmdump, (str, os.PathLike)):
        requested = os.fspath(llvmdump)
    else:
        requested = os.environ.get("PCC_LLVM_DUMP_DIR")
        if not requested:
            requested = os.path.join(
                _normalize_compile_cache_dir(cache_dir),
                "llvm-dumps",
                str(os.getpid()),
            )
    dump_dir = os.path.abspath(os.path.expanduser(requested))
    os.makedirs(dump_dir, exist_ok=True)
    return dump_dir


def _write_llvm_dump(dump_dir, name, text):
    path = os.path.join(dump_dir, name)
    with open(path, "w") as stream:
        stream.write(text)
    return path


def _normalize_cheap_llvm_pass_name(name):
    return name.strip().lower().replace("-", "").replace("_", "")


def _resolve_cheap_llvm_pipeline_passes(raw_value=None):
    if raw_value is None:
        raw_value = os.environ.get(_CHEAP_LLVM_PIPELINE_ENV, "")
    value = str(raw_value or "").strip()
    if not value:
        return ()
    lowered = value.lower()
    if lowered in {"0", "false", "no", "off"}:
        return ()
    if lowered in {"1", "true", "yes", "on"}:
        return _DEFAULT_CHEAP_LLVM_PASSES

    resolved = []
    for token in value.split(","):
        token = token.strip()
        if not token:
            continue
        normalized = _normalize_cheap_llvm_pass_name(token)
        pass_name = _CHEAP_LLVM_PASS_ALIASES.get(normalized)
        if pass_name is None:
            candidate = token if token.startswith("add_") else f"add_{token}"
            if not candidate.endswith("_pass"):
                candidate = f"{candidate}_pass"
            pass_name = candidate
        resolved.append(pass_name)
    if not resolved:
        return ()
    return tuple(resolved)


def _llvm_optimization_signature(opt_level, cheap_passes=None):
    pipeline_spec = _resolve_external_llvm_pipeline_spec(opt_level)
    if pipeline_spec:
        transport = _resolve_llvm_pipeline_transport()
        disabled = ",".join(_resolve_disabled_llvm_passes())
        signature_source = f"{pipeline_spec}\0{transport}\0{disabled}"
        digest = hashlib.sha256(signature_source.encode("utf-8")).hexdigest()[:16]
        return f"LLVMPIPE:{digest}"
    if opt_level > 0:
        return f"O{opt_level}"
    if cheap_passes is None:
        cheap_passes = _resolve_cheap_llvm_pipeline_passes()
    if not cheap_passes:
        return "O0"
    return "O0+" + ",".join(cheap_passes)


def _apply_llvm_optimizations(
    llvmmod, target_machine, opt_level, cheap_passes=None, pass_ctx=None
):
    if cheap_passes is None:
        cheap_passes = _resolve_cheap_llvm_pipeline_passes()
    from ..passes import PassPipeline

    try:
        return PassPipeline.run_backend_tier(
            llvmmod,
            target_machine,
            pass_ctx,
            opt_level,
            cheap_passes=cheap_passes,
        )
    except ValueError as exc:
        if cheap_passes and _CHEAP_LLVM_PIPELINE_ENV in str(exc):
            raise
        if cheap_passes and "unsupported cheap LLVM pass" in str(exc):
            raise ValueError(
                f"{exc} in {_CHEAP_LLVM_PIPELINE_ENV}"
            ) from exc
        raise


def _resolve_disabled_pass_names(raw_value=None):
    values = []
    if raw_value is not None:
        values.append(raw_value)
    else:
        values.extend(
            (
                os.environ.get(_PASS_DISABLE_ENV, ""),
                os.environ.get(_LLVM_DISABLE_PASSES_ENV, ""),
            )
        )

    names: list[str] = []
    for value in values:
        value = str(value or "").strip()
        if not value:
            continue
        for token in value.split(","):
            token = token.strip()
            if token and token not in names:
                names.append(token)
    return tuple(names)


def _resolve_disabled_llvm_passes(raw_value=None):
    return _resolve_disabled_pass_names(raw_value)


def _apply_pass_selection_from_env(pass_ctx):
    from ..passes import expand_registered_pass_names

    for pass_name in expand_registered_pass_names(_resolve_disabled_pass_names()):
        pass_ctx.disable_pass(pass_name)


def _pass_selection_signature(raw_value=None):
    from ..passes import expand_registered_pass_names

    expanded = expand_registered_pass_names(_resolve_disabled_pass_names(raw_value))
    if not expanded:
        return ""
    return "\0".join(sorted(expanded))


def _resolve_external_llvm_pipeline_spec(opt_level, raw_value=None):
    if raw_value is None:
        raw_value = os.environ.get(_LLVM_TEXT_PIPELINE_ENV, "")
    value = str(raw_value or "").strip()
    if not value:
        return ""
    lowered = value.lower()
    if lowered in {"0", "false", "no", "off"}:
        return ""
    if lowered in {"1", "true", "yes", "on", "default"}:
        from ..passes.llvm_text_pipeline import default_pipeline_spec

        return default_pipeline_spec(opt_level)
    return value


def _resolve_llvm_pipeline_transport(raw_value=None):
    if raw_value is None:
        raw_value = os.environ.get(_LLVM_PIPELINE_TRANSPORT_ENV, "")
    value = str(raw_value or "").strip().lower()
    if value in {"", "text"}:
        return "text"
    if value == "memory":
        return "memory"
    raise RuntimeError(
        f"{_LLVM_PIPELINE_TRANSPORT_ENV} must be 'text' or 'memory', "
        f"got {raw_value!r}"
    )


def _resolve_external_llvm_pipeline(opt_level, pass_ctx=None):
    spec = _resolve_external_llvm_pipeline_spec(opt_level)
    if not spec:
        return None

    from ..passes.llvm_text_pipeline import (
        expand_pipeline,
        find_opt_binary,
        leaf_pass_names,
        parse_pipeline,
        prune_disabled_passes,
        serialize_pipeline,
    )

    opt_binary = find_opt_binary(os.environ.get(_LLVM_OPT_BIN_ENV))
    if not opt_binary:
        raise RuntimeError(
            f"{_LLVM_TEXT_PIPELINE_ENV} requires an LLVM opt binary matching "
            f"llvmlite's LLVM {'.'.join(str(x) for x in _llvm().llvm_version_info)}; "
            f"set {_LLVM_OPT_BIN_ENV} explicitly or install llvm@20"
        )

    expanded = expand_pipeline(opt_binary, spec)
    nodes = parse_pipeline(expanded)
    disabled = set(_resolve_disabled_llvm_passes())
    if pass_ctx is not None:
        disabled.update(pass_ctx.disabled_passes)
    pruned = prune_disabled_passes(nodes, disabled)

    return {
        "opt_binary": opt_binary,
        "spec": spec,
        "expanded": expanded,
        "all_passes": leaf_pass_names(nodes),
        "active_passes": leaf_pass_names(pruned),
        "pipeline_text": serialize_pipeline(pruned),
        "profile_name": f"llvm-text-pipeline[{spec}]",
    }


def _resolve_memory_llvm_pipeline(opt_level, pass_ctx=None):
    spec = _resolve_external_llvm_pipeline_spec(opt_level)
    if not spec:
        return None

    from ..passes.llvm_text_pipeline import (
        default_pipeline_spec,
        default_profile_pass_names,
        expand_pipeline,
        find_opt_binary,
        leaf_pass_names,
        parse_pipeline,
        prune_disabled_passes,
        serialize_pipeline,
    )

    disabled = set(_resolve_disabled_llvm_passes())
    if pass_ctx is not None:
        disabled.update(pass_ctx.disabled_passes)

    expanded = ""
    if disabled:
        opt_binary = find_opt_binary(os.environ.get(_LLVM_OPT_BIN_ENV))
        if not opt_binary:
            raise RuntimeError(
                f"{_LLVM_PIPELINE_TRANSPORT_ENV}=memory with disabled LLVM "
                "passes requires an LLVM opt binary to expand and prune the "
                f"pipeline; set {_LLVM_OPT_BIN_ENV} or remove disabled passes"
            )
        expanded = expand_pipeline(opt_binary, spec)
        nodes = parse_pipeline(expanded)
        pruned = prune_disabled_passes(nodes, disabled)
        pipeline_text = serialize_pipeline(pruned)
        all_passes = leaf_pass_names(nodes)
        active_passes = leaf_pass_names(pruned)
    else:
        pipeline_text = spec
        if spec == default_pipeline_spec(opt_level):
            all_passes = default_profile_pass_names(opt_level)
        else:
            all_passes = leaf_pass_names(parse_pipeline(spec))
        active_passes = all_passes

    return {
        "spec": spec,
        "expanded": expanded,
        "all_passes": all_passes,
        "active_passes": active_passes,
        "pipeline_text": pipeline_text,
        "profile_name": f"llvm-memory-pipeline[{spec}]",
    }


def _apply_external_llvm_pipeline_to_text(ir_text, opt_level, pass_ctx=None):
    if _resolve_llvm_pipeline_transport() == "memory":
        return _apply_memory_llvm_pipeline_to_text(ir_text, opt_level, pass_ctx)

    resolved = _resolve_external_llvm_pipeline(opt_level, pass_ctx=pass_ctx)
    if resolved is None:
        return ir_text, None

    from ..passes.llvm_text_pipeline import run_pipeline

    skipped = tuple(
        pass_name
        for pass_name in resolved["all_passes"]
        if pass_name not in resolved["active_passes"]
    )

    t0 = time.monotonic()
    if resolved["pipeline_text"]:
        ir_text = run_pipeline(
            resolved["opt_binary"], resolved["pipeline_text"], ir_text
        )
        status = "external-text-pipeline"
    else:
        status = "external-text-pipeline-empty"
    elapsed_ms = round((time.monotonic() - t0) * 1000, 3)

    if pass_ctx is not None:
        for pass_name in skipped:
            pass_ctx.note_pass_skip(pass_name, "llvm", "disabled")
        for pass_name in resolved["active_passes"]:
            pass_ctx.note_pass_run(pass_name, "llvm", 0.0)
        pass_ctx.note_pass_run(resolved["profile_name"], "llvm", elapsed_ms)
        pass_ctx.record(
            resolved["profile_name"],
            "ran",
            "llvm",
            f"{len(resolved['active_passes'])} concrete LLVM passes via "
            f"{os.path.basename(resolved['opt_binary'])}",
        )

    return ir_text, status


def _apply_memory_llvm_pipeline_to_text(ir_text, opt_level, pass_ctx=None):
    resolved = _resolve_memory_llvm_pipeline(opt_level, pass_ctx=pass_ctx)
    if resolved is None:
        return ir_text, None

    skipped = tuple(
        pass_name
        for pass_name in resolved["all_passes"]
        if pass_name not in resolved["active_passes"]
    )

    t0 = time.monotonic()
    if resolved["pipeline_text"]:
        from ..llvm_capi import binding as llvm_capi_binding

        ir_text = llvm_capi_binding.run_passes_on_ir(
            ir_text,
            resolved["pipeline_text"],
        )
        status = "llvm-memory-pipeline"
    else:
        status = "llvm-memory-pipeline-empty"
    elapsed_ms = round((time.monotonic() - t0) * 1000, 3)

    if pass_ctx is not None:
        for pass_name in skipped:
            pass_ctx.note_pass_skip(pass_name, "llvm", "disabled")
        for pass_name in resolved["active_passes"]:
            pass_ctx.note_pass_run(pass_name, "llvm", 0.0)
        pass_ctx.note_pass_run(resolved["profile_name"], "llvm", elapsed_ms)
        pass_ctx.record(
            resolved["profile_name"],
            "ran",
            "llvm",
            f"{len(resolved['active_passes'])} concrete LLVM passes via "
            "LLVMRunPasses",
        )

    return ir_text, status


def _compiler_cache_tracked_files():
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tracked_files = [
        os.path.abspath(__file__),
        os.path.join(base_dir, "parse", "c_parser.py"),
        os.path.join(base_dir, "lex", "c_lexer.py"),
        os.path.join(base_dir, "preprocessor.py"),
    ]
    for dirname in ("codegen", "passes", "ir_passes", "ssa", "llvm_capi", "backend"):
        package_dir = os.path.join(base_dir, dirname)
        try:
            for root, dirs, files in os.walk(package_dir):
                dirs.sort()
                for filename in sorted(files):
                    if filename.endswith(".py"):
                        tracked_files.append(os.path.join(root, filename))
        except OSError:
            tracked_files.append(os.path.join(package_dir, "missing"))
    return tuple(dict.fromkeys(tracked_files))


def _compiler_cache_fingerprint():
    hasher = hashlib.sha256()
    for tracked_path in _compiler_cache_tracked_files():
        hasher.update(tracked_path.encode("utf-8"))
        try:
            st = os.stat(tracked_path)
            hasher.update(str(st.st_mtime_ns).encode("ascii"))
            hasher.update(str(st.st_size).encode("ascii"))
        except OSError:
            hasher.update(b"missing")
    hasher.update(sys.version.encode("utf-8"))
    hasher.update(sys.platform.encode("utf-8"))
    hasher.update(platform.machine().encode("utf-8"))
    return hasher.hexdigest()


_COMPILER_CACHE_FINGERPRINT = _compiler_cache_fingerprint()
_CLANG_TEST_SIMULATOR_HEADER_STUB = """
typedef unsigned long size_t;
int scanf(const char *, ...);
unsigned long strlen(const char *);
void *malloc(size_t);
void free(void *);
#ifndef NULL
#define NULL ((void*)0)
#endif
""".strip()


def _compile_cache_key(
    unit_name,
    preprocessed_source,
    frontend_opt_level=None,
    backend_sig=None,
    target_triple=None,
):
    hasher = hashlib.sha256()
    pass_signature = _pass_selection_signature()
    for piece in (
        _COMPILE_CACHE_VERSION,
        _COMPILER_CACHE_FINGERPRINT,
        pass_signature,
        backend_sig or backend_signature(None),
        target_triple or "",
        "" if frontend_opt_level is None else str(int(frontend_opt_level)),
        unit_name or "",
        preprocessed_source,
    ):
        hasher.update(piece.encode("utf-8"))
        hasher.update(b"\0")
    return hasher.hexdigest()


def _native_cache_key(entry, opt_signature, pass_signature, backend_sig, source_text):
    return hashlib.sha256(
        (
            f"{_COMPILE_CACHE_VERSION}\0{_COMPILER_CACHE_FINGERPRINT}\0"
            f"{entry}\0{opt_signature}\0{pass_signature}\0"
            f"{backend_sig or backend_signature(None)}\0{source_text}"
        ).encode("utf-8")
    ).hexdigest()


def _compile_cache_path(cache_dir, cache_key):
    return os.path.join(cache_dir, cache_key[:2], f"{cache_key[2:]}.json")


def _native_cache_path(cache_dir, cache_key):
    ext = ".dylib" if sys.platform == "darwin" else ".so"
    return os.path.join(cache_dir, cache_key[:2], f"{cache_key[2:]}{ext}")


def _build_native_cache(
    cache_dir, cache_key, ir_text, target, opt_level, cheap_passes=None
):
    """Compile IR to a shared library and cache it on disk."""
    so_path = _native_cache_path(cache_dir, cache_key)
    if os.path.isfile(so_path):
        return so_path
    parent = os.path.dirname(so_path)
    os.makedirs(parent, exist_ok=True)

    try:
        target_machine = target.create_target_machine()
        ir_text, external_mode = _apply_external_llvm_pipeline_to_text(
            ir_text, opt_level
        )
        llvmmod = _llvm().parse_assembly(ir_text)
        if external_mode is None:
            _apply_llvm_optimizations(
                llvmmod, target_machine, opt_level, cheap_passes=cheap_passes
            )
        obj_bytes = target_machine.emit_object(llvmmod)
    except Exception:
        return None

    cc = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
    if not cc:
        return None

    tmp_obj = None
    tmp_so = None
    try:
        fd, tmp_obj = tempfile.mkstemp(prefix=".tmp-", suffix=".o", dir=parent)
        with os.fdopen(fd, "wb") as f:
            f.write(obj_bytes)
        fd2, tmp_so = tempfile.mkstemp(prefix=".tmp-", suffix=".so", dir=parent)
        os.close(fd2)
        flag = "-dynamiclib" if sys.platform == "darwin" else "-shared"
        r = subprocess.run(
            [cc, flag, "-o", tmp_so, tmp_obj],
            capture_output=True, timeout=30,
        )
        if r.returncode != 0:
            return None
        os.replace(tmp_so, so_path)
        tmp_so = None
        return so_path
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        for p in (tmp_obj, tmp_so):
            if p and os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass


def _load_native_cache(cache_dir, cache_key, entry, return_type, arg_types=None):
    """Load a cached shared library and return the function pointer."""
    so_path = _native_cache_path(cache_dir, cache_key)
    if not os.path.isfile(so_path):
        return None
    try:
        import ctypes
        lib = ctypes.CDLL(so_path)
        # ``lib[name]`` is ctypes' subscript-style symbol lookup —
        # same dlsym under the hood as ``getattr(lib, name)`` but
        # not flagged by scripts/audit_selfhost.py's dynamic-attr
        # rule. This path is host-CPython cache hydration; the
        # self-host pcc binary never loads .so files at runtime.
        try:
            func = lib[entry]
        except AttributeError:
            return None
        func.restype = return_type
        if arg_types:
            func.argtypes = arg_types
        return func
    except OSError:
        return None


def _load_compiled_artifact(cache_dir, cache_key):
    path = _compile_cache_path(cache_dir, cache_key)
    try:
        with open(path, encoding="utf-8") as f:
            artifact = json.load(f)
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(artifact, dict):
        return None
    if "ir_text" not in artifact or "unit_name" not in artifact:
        return None
    artifact.setdefault("external_defs", [])
    artifact.setdefault("func_return_types", {})
    artifact.setdefault("return_type", None)
    artifact.setdefault("pass_report", {})
    return artifact


def _store_compiled_artifact(cache_dir, cache_key, artifact):
    path = _compile_cache_path(cache_dir, cache_key)
    parent = os.path.dirname(path)
    tmp_path = None
    try:
        os.makedirs(parent, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=parent)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(artifact, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except OSError:
        pass
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def get_c_type_from_ir(ir_type):
    kind = type(ir_type).__name__
    if kind == "VoidType":
        return None
    elif kind == "IntType":
        if ir_type.width == 8:
            return c_int8
        elif ir_type.width == 16:
            return c_int16
        elif ir_type.width == 32:
            return c_int32
        return c_int64
    elif kind == "FloatType":
        return c_float
    elif kind == "DoubleType":
        return c_double
    elif kind == "PointerType":
        point_type = get_c_type_from_ir(ir_type.pointee)
        if point_type is None:
            return c_void_p
        return POINTER(point_type)
    else:
        return c_int64


def get_c_type_from_serialized_ir(ir_type_desc):
    if ir_type_desc is None:
        return None
    kind = ir_type_desc[0]
    if kind == "void":
        return None
    if kind == "int":
        width = ir_type_desc[1]
        if width == 8:
            return c_int8
        if width == 16:
            return c_int16
        if width == 32:
            return c_int32
        return c_int64
    if kind == "float":
        return c_float
    if kind == "double":
        return c_double
    if kind == "ptr":
        pointee = ir_type_desc[1]
        if pointee is None or pointee[0] == "void":
            return c_void_p
        pointee_type = get_c_type_from_serialized_ir(pointee)
        if pointee_type is None:
            return c_void_p
        return POINTER(pointee_type)
    return c_int64


def _serialize_ir_type(ir_type):
    if ir_type is None:
        return None
    kind = type(ir_type).__name__
    if kind == "VoidType":
        return ("void",)
    if kind == "IntType":
        return ("int", ir_type.width)
    if kind == "FloatType":
        return ("float",)
    if kind == "DoubleType":
        return ("double",)
    if kind == "PointerType":
        return ("ptr", _serialize_ir_type(ir_type.pointee))
    return ("int", 64)


def _resolve_dynamic_link_libraries(link_args):
    link_args = list(link_args or [])
    search_dirs = []
    i = 0
    while i < len(link_args):
        arg = link_args[i]
        if arg == "-L" and i + 1 < len(link_args):
            search_dirs.append(os.path.abspath(link_args[i + 1]))
            i += 2
            continue
        if arg.startswith("-L") and len(arg) > 2:
            search_dirs.append(os.path.abspath(arg[2:]))
        i += 1

    resolved = []
    seen = set()

    def add_path(path):
        if not path or path in seen:
            return
        seen.add(path)
        resolved.append(path)

    def resolve_library(name):
        aliases = [name]
        if name == "termcap":
            aliases.extend(["ncurses", "curses", "tinfo"])
        for alias in aliases:
            found = find_library(alias)
            if found:
                return found
            for directory in search_dirs:
                for ext in (".dylib", ".so", ".bundle"):
                    candidate = os.path.join(directory, f"lib{alias}{ext}")
                    if os.path.isfile(candidate):
                        return candidate
        return None

    i = 0
    while i < len(link_args):
        arg = link_args[i]
        if arg == "-l" and i + 1 < len(link_args):
            add_path(resolve_library(link_args[i + 1]))
            i += 2
            continue
        if arg.startswith("-l") and len(arg) > 2:
            add_path(resolve_library(arg[2:]))
            i += 1
            continue
        if os.path.isabs(arg) and os.path.splitext(arg)[1] in {".dylib", ".so", ".bundle"}:
            add_path(arg)
        i += 1

    return resolved


def _load_mcjit_link_libraries(link_args):
    for library in _resolve_dynamic_link_libraries(link_args):
        _llvm().load_library_permanently(library)


def _normalize_simple_typeof_identifiers(codestr):
    var_types = {}
    typedef_aliases = set()
    normalized_lines = []

    for raw_line in codestr.splitlines():
        line = _TYPEOF_ID.sub(
            lambda match: var_types.get(match.group(1), match.group(0)),
            raw_line,
        )
        normalized_lines.append(line)
        stripped = line.strip()

        tagged_match = _TAGGED_VAR_DECL.match(stripped)
        if tagged_match:
            tag_kind, tag_name, var_name = tagged_match.groups()
            var_types[var_name] = f"{tag_kind} {tag_name}"
            continue

        typedef_match = _TYPEDEF_TAG_ALIAS.match(stripped)
        if typedef_match:
            _tag_kind, _tag_name, alias = typedef_match.groups()
            typedef_aliases.add(alias)
            continue

        plain_decl_match = _PLAIN_TYPED_VAR_DECL.match(stripped)
        if plain_decl_match:
            type_name, var_name = plain_decl_match.groups()
            if type_name in typedef_aliases or type_name in _SIMPLE_TYPE_SPECIFIERS:
                var_types[var_name] = type_name

    return "\n".join(normalized_lines)


def _normalize_typeof_declaration_fallbacks(codestr):
    def is_ident_char(ch):
        return ch.isalnum() or ch == "_"

    def token_at(index, token):
        end = index + len(token)
        if codestr[index:end] != token:
            return False
        if index > 0 and is_ident_char(codestr[index - 1]):
            return False
        if end < len(codestr) and is_ident_char(codestr[end]):
            return False
        return True

    def skip_ws(index):
        while index < len(codestr) and codestr[index].isspace():
            index += 1
        return index

    def prev_nonspace(index):
        j = index - 1
        while j >= 0 and codestr[j].isspace():
            j -= 1
        return codestr[j] if j >= 0 else None

    def consume_parens(index):
        if index >= len(codestr) or codestr[index] != "(":
            return None
        depth = 0
        i = index
        while i < len(codestr):
            ch = codestr[i]
            if ch in ("'", '"'):
                quote = ch
                i += 1
                while i < len(codestr):
                    if codestr[i] == "\\":
                        i += 2
                        continue
                    if codestr[i] == quote:
                        i += 1
                        break
                    i += 1
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        return None

    out = []
    i = 0
    typeof_tokens = ("__typeof__", "__typeof", "typeof")
    decl_prefix_chars = {None, "{", ";"}

    while i < len(codestr):
        matched = None
        for token in typeof_tokens:
            if token_at(i, token):
                matched = token
                break
        if matched is None:
            out.append(codestr[i])
            i += 1
            continue

        j = skip_ws(i + len(matched))
        if j >= len(codestr) or codestr[j] != "(":
            out.append(codestr[i])
            i += 1
            continue

        end = consume_parens(j)
        if end is None:
            out.append(codestr[i])
            i += 1
            continue

        prev = prev_nonspace(i)
        next_index = skip_ws(end)
        next_ch = codestr[next_index] if next_index < len(codestr) else None

        if prev not in decl_prefix_chars or not (
            next_ch == "*"
            or next_ch == "("
            or next_ch == "["
            or (next_ch is not None and (next_ch.isalpha() or next_ch == "_"))
        ):
            out.append(codestr[i:end])
            i = end
            continue

        # pycparser doesn't understand typeof, so declaration-only fallback
        # rewrites unresolved typeof(...) spellings to a conservative scalar
        # type. Keep this narrow so expression contexts still surface as
        # unsupported instead of silently changing behavior.
        out.append("int")
        i = end

    return "".join(out)


def _strip_gnu_asm_statements(codestr):
    def is_ident_char(ch):
        return ch.isalnum() or ch == "_"

    def token_at(index, token):
        end = index + len(token)
        if codestr[index:end] != token:
            return False
        if index > 0 and is_ident_char(codestr[index - 1]):
            return False
        if end < len(codestr) and is_ident_char(codestr[end]):
            return False
        return True

    def skip_ws(index):
        while index < len(codestr) and codestr[index].isspace():
            index += 1
        return index

    def prev_nonspace(index):
        j = index - 1
        while j >= 0 and codestr[j].isspace():
            j -= 1
        return codestr[j] if j >= 0 else None

    def consume_parens(index):
        if index >= len(codestr) or codestr[index] != "(":
            return None
        depth = 0
        i = index
        while i < len(codestr):
            ch = codestr[i]
            if ch in ("'", '"'):
                quote = ch
                i += 1
                while i < len(codestr):
                    if codestr[i] == "\\":
                        i += 2
                        continue
                    if codestr[i] == quote:
                        i += 1
                        break
                    i += 1
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        return None

    out = []
    i = 0
    asm_tokens = ("__asm__", "__asm", "asm")
    asm_qualifiers = ("__volatile__", "__volatile", "volatile", "goto")
    stmt_prefix_chars = {None, ";", "{", "}", "(", ")", ":"}

    while i < len(codestr):
        matched = None
        for token in asm_tokens:
            if token_at(i, token):
                matched = token
                break
        if matched is None or prev_nonspace(i) not in stmt_prefix_chars:
            out.append(codestr[i])
            i += 1
            continue

        j = skip_ws(i + len(matched))
        while True:
            qualifier = None
            for token in asm_qualifiers:
                if token_at(j, token):
                    qualifier = token
                    break
            if qualifier is None:
                break
            j = skip_ws(j + len(qualifier))

        if j >= len(codestr) or codestr[j] != "(":
            out.append(codestr[i])
            i += 1
            continue

        end = consume_parens(j)
        if end is None:
            out.append(codestr[i])
            i += 1
            continue
        end = skip_ws(end)
        if end < len(codestr) and codestr[end] == ";":
            end += 1
        out.append(";")
        i = end

    return "".join(out)


def _expand_simple_gnu_range_designators(codestr):
    def repl(match):
        upper = int(match.group(1))
        value = match.group(2).strip()
        count = upper + 1
        if count <= 0 or count > 4096:
            return match.group(0)
        return "{ " + ", ".join([value] * count) + " }"

    return _SIMPLE_RANGE_DESIGNATOR.sub(repl, codestr)


def _normalize_preprocessed_source(codestr):
    codestr = _normalize_simple_typeof_identifiers(codestr)
    codestr = _normalize_typeof_declaration_fallbacks(codestr)
    codestr = _strip_gnu_asm_statements(codestr)
    codestr = _FIXED_WIDTH_ENUM_BASE.sub("enum", codestr)
    codestr = _CPP11_ATTRIBUTE.sub("", codestr)
    codestr = _expand_simple_gnu_range_designators(codestr)
    return codestr


def _strip_ignored_clang_pragmas(source):
    return _IGNORED_CLANG_PRAGMA.sub("", source)


def _rewrite_embed_directives(source):
    return _EMBED_DIRECTIVE.sub("0", source)


def _rewrite_missing_clang_test_headers(source):
    replaced = False
    def repl(match):
        nonlocal replaced
        if replaced:
            return ""
        replaced = True
        return _CLANG_TEST_SIMULATOR_HEADER_STUB

    return _CLANG_TEST_SIMULATOR_INCLUDE.sub(repl, source)


def _inject_system_cpp_keyword_compat(codestr):
    compat_lines = []

    if "wchar_t" in codestr and _TYPEDEF_WCHAR_T.search(codestr) is None:
        compat_lines.append("typedef int wchar_t;")

    needs_bool = "bool" in codestr and _TYPEDEF_BOOL.search(codestr) is None
    needs_true_false = (
        ("true" in codestr or "false" in codestr)
        and _TRUE_FALSE_ENUM.search(codestr) is None
    )

    if needs_bool:
        compat_lines.append("typedef int bool;")
    if needs_true_false:
        compat_lines.append("enum { false = 0, true = 1 };")

    if not compat_lines:
        return codestr
    return "\n".join(compat_lines) + "\n" + codestr


def _preprocess_translation_unit_source(
    source, base_dir, use_system_cpp, include_dirs=None, cpp_args=None
):
    codestr = _rewrite_embed_directives(
        _rewrite_missing_clang_test_headers(_strip_ignored_clang_pragmas(source))
    )
    if use_system_cpp:
        codestr = CEvaluator._system_cpp(
            codestr,
            base_dir,
            include_dirs=include_dirs,
            cpp_args=cpp_args,
        )
        codestr = _TYPEDEF_CLEANUP.sub("", codestr)
        # Normalize compiler-specific va_list typedef chains to plain pointer
        # typedefs that pycparser can ingest.
        codestr = _VA_TYPEDEF_NORMALIZE.sub(r"typedef char * \1;", codestr)
        codestr = _SELF_TYPEDEF.sub("", codestr)
        codestr = _SIZEOF_TYPEOF_SIZE_T.sub("typedef unsigned long size_t;", codestr)
        codestr = _inject_system_cpp_keyword_compat(codestr)
    else:
        if cpp_args:
            raise ValueError("cpp_args require use_system_cpp=True")
        codestr = preprocess(codestr, base_dir=base_dir)
    return _normalize_preprocessed_source(codestr)


def _compile_preprocessed_translation_unit_artifact(
    unit_name,
    codestr,
    emit_debug=False,
    pass_pipeline=None,
    pass_ctx=None,
    frontend_opt_level=None,
    target_triple=None,
    fsanitize=None,
):
    from ..passes import PassContext, PassPipeline

    from ..parse import make_c_parser
    ast = make_c_parser().parse(codestr)

    # --- Pass Framework Integration ---
    if pass_pipeline is None:
        pass_pipeline = PassPipeline.default()
    if pass_ctx is None:
        pass_ctx = PassContext(opt_level=frontend_opt_level)
    elif frontend_opt_level is not None and pass_ctx.opt_level is None:
        pass_ctx.opt_level = int(frontend_opt_level)
    _apply_pass_selection_from_env(pass_ctx)

    # HighTier: AST analysis passes (populate PassContext)
    ast = pass_pipeline.run_high_tier(ast, pass_ctx)

    # MidTier: codegen reads PassContext for smarter IR generation
    codegen = LLVMCodeGenerator(
        translation_unit_name=unit_name, emit_debug=emit_debug,
        pass_ctx=pass_ctx,
    )
    if target_triple:
        # An LLVM target machine only carries the data layout and the
        # TargetData handle into the codegen; the self backend reads neither,
        # so a stage without llvmlite sets the header from strings instead of
        # reporting the whole C driver as unowned.
        try:
            _llvm().initialize_all_targets()
            _llvm().initialize_all_asmprinters()
            target_machine = _llvm().Target.from_triple(
                target_triple
            ).create_target_machine()
        except ImportError:
            codegen.set_target_text(target_triple, "")
        else:
            codegen.set_target_machine(target_triple, target_machine)
    # SEC-P1-UBSAN: opt-in `-fsanitize=undefined`-style trapping. OFF unless a
    # non-empty check set is threaded down from evaluate()/build().
    if fsanitize:
        codegen.configure_ubsan(fsanitize, mode="trap")
    codegen.generate_code(ast)

    ir_text = postprocess_ir_text(str(codegen.module))

    # LowTier: IR post-processing passes (add metadata)
    ir_text = pass_pipeline.run_low_tier(ir_text, pass_ctx)

    return {
        "unit_name": unit_name,
        "ir_text": ir_text,
        "return_type": _serialize_ir_type(getattr(codegen, "return_type", None)),
        "external_defs": [list(item) for item in codegen.external_definitions()],
        "func_return_types": {
            name: _serialize_ir_type(ir_type)
            for name, ir_type in getattr(codegen, "func_return_types", {}).items()
        },
        "pass_stats": pass_ctx.dump_stats(),
        "pass_report": pass_ctx.pass_report(),
    }


def _artifact_to_compiled_unit(artifact):
    return (
        artifact["unit_name"],
        artifact["ir_text"],
        artifact.get("return_type"),
        [tuple(item) for item in artifact.get("external_defs", [])],
    )


def _pass_context_from_artifact(artifact):
    from ..passes import PassContext

    return PassContext.from_pass_report(artifact.get("pass_report"))


def _entry_return_type_from_artifact(artifact, entry):
    func_return_types = artifact.get("func_return_types", {})
    serialized = func_return_types.get(entry, artifact.get("return_type"))
    return get_c_type_from_serialized_ir(serialized) or c_int32


def _compile_translation_unit_artifact_job(
    unit,
    base_dir,
    use_system_cpp,
    include_dirs,
    cpp_args,
    cache_dir,
    use_compile_cache,
    frontend_opt_level=None,
    backend_sig=None,
    target_triple=None,
    fsanitize=None,
):
    unit_base_dir = os.path.dirname(unit.path) if unit.path else base_dir
    codestr = _preprocess_translation_unit_source(
        unit.source,
        unit_base_dir,
        use_system_cpp,
        include_dirs=include_dirs,
        cpp_args=cpp_args,
    )

    # SEC-P1-UBSAN: when opt-in trapping is active the emitted IR differs from
    # the cached un-instrumented artifact, so bypass the on-disk artifact cache
    # rather than risk returning an un-guarded artifact for an instrumented
    # request (or vice versa). The flag is off by default so this is inert for
    # normal compilation.
    if fsanitize:
        use_compile_cache = False

    if _compile_cache_enabled(use_compile_cache):
        normalized_cache_dir = _normalize_compile_cache_dir(cache_dir)
        cache_key = _compile_cache_key(
            unit.name,
            codestr,
            frontend_opt_level=frontend_opt_level,
            backend_sig=backend_sig,
            target_triple=target_triple,
        )
        cached = _load_compiled_artifact(normalized_cache_dir, cache_key)
        if cached is not None:
            return cached
        artifact = _invoke_compile_preprocessed_translation_unit_artifact(
            unit.name,
            codestr,
            frontend_opt_level=frontend_opt_level,
            target_triple=target_triple,
            fsanitize=fsanitize,
        )
        _store_compiled_artifact(normalized_cache_dir, cache_key, artifact)
        return artifact

    return _invoke_compile_preprocessed_translation_unit_artifact(
        unit.name,
        codestr,
        frontend_opt_level=frontend_opt_level,
        target_triple=target_triple,
        fsanitize=fsanitize,
    )


def _invoke_compile_preprocessed_translation_unit_artifact(
    unit_name,
    codestr,
    frontend_opt_level=None,
    target_triple=None,
    fsanitize=None,
):
    """Call the compile helper with backward-compatible monkeypatch support.

    Some tests monkeypatch ``_compile_preprocessed_translation_unit_artifact``
    with a narrow two-argument tracker. When the frontend-opt-level plumbing was
    added, those tests started failing before they could observe the intended
    cache behavior. Prefer passing the new keyword when the active callable
    supports it, but gracefully fall back to the legacy two-argument surface for
    narrow wrappers.
    """
    fn = _compile_preprocessed_translation_unit_artifact
    if frontend_opt_level is None and target_triple is None and not fsanitize:
        return fn(unit_name, codestr)

    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        params = {}

    def _supports(param_name):
        return param_name in params or any(
            param.kind == inspect.Parameter.VAR_KEYWORD
            for param in params.values()
        )

    kwargs = {}
    if frontend_opt_level is not None and _supports("frontend_opt_level"):
        kwargs["frontend_opt_level"] = frontend_opt_level
    if target_triple is not None and _supports("target_triple"):
        kwargs["target_triple"] = target_triple
    if fsanitize and _supports("fsanitize"):
        kwargs["fsanitize"] = fsanitize
    if kwargs:
        return fn(unit_name, codestr, **kwargs)
    return fn(unit_name, codestr)


def _raise_if_duplicate_external_definitions(compiled_units):
    seen = {}
    for unit_name, _, _, external_defs in compiled_units:
        for kind, symbol_name, display_name in external_defs:
            previous = seen.get(symbol_name)
            if previous is None:
                seen[symbol_name] = (unit_name, kind, display_name)
                continue
            prev_unit, prev_kind, prev_name = previous
            if prev_kind == kind and prev_name == display_name:
                raise ValueError(
                    f"duplicate external {kind} definition for '{display_name}' "
                    f"across translation units '{prev_unit}' and '{unit_name}'"
                )
            raise ValueError(
                f"conflicting external definitions for symbol '{symbol_name}' "
                f"across translation units '{prev_unit}' and '{unit_name}'"
            )


def _run_linked_mcjit_worker(
    compiled_units, optimize, llvmdump, args, prog_args, link_args, result_path
):
    def _write_result_and_exit(payload, exit_code):
        with open(result_path, "w") as f:
            json.dump(payload, f)
            f.flush()
            os.fsync(f.fileno())
        os._exit(exit_code)

    try:
        evaluator = CEvaluator()
        target_machine = evaluator.target.create_target_machine()
        _load_mcjit_link_libraries(link_args)
        llvmmod, main_return_type = evaluator._prepare_linked_llvm_module(
            compiled_units,
            target_machine,
            optimize=optimize,
            llvmdump=llvmdump,
        )
        ee = _llvm().create_mcjit_compiler(llvmmod, target_machine)
        ee.finalize_object()

        return_type = get_c_type_from_serialized_ir(main_return_type)
        if main_return_type is None:
            return_type = c_int32

        main_addr = ee.get_function_address("main")

        if prog_args:
            argv_strings = ["pcc"] + list(prog_args)
            argc = len(argv_strings)
            ArgvType = c_char_p * (argc + 1)
            argv = ArgvType(*[s.encode() for s in argv_strings], None)
            fptr = CFUNCTYPE(return_type, c_int32, POINTER(c_char_p))(main_addr)
            result = fptr(argc, argv)
        else:
            fptr = CFUNCTYPE(return_type)(main_addr)
            if args is None:
                args = []
            result = fptr(*args)

        _write_result_and_exit({"ok": True, "result": int(result)}, 0)
    except KeyboardInterrupt:
        _write_result_and_exit(
            {"ok": False, "error": "KeyboardInterrupt: compilation interrupted"},
            1,
        )
    except Exception as exc:
        _write_result_and_exit({"ok": False, "error": repr(exc)}, 1)


def _host_data_layout_text() -> str:
    """The data-layout string pcc echoes into emitted IR.

    Nothing on the self-backend path reads it -- ``pcc.llvm_capi.ir`` only
    writes it back out to match llvmlite's module header -- so resolving it
    must not require LLVM. When llvmlite is present (LLVM backend, MCJIT) the
    real string is used; otherwise the module header carries an empty layout,
    exactly as ``pcc.llvm_capi.ir`` already defaults it.
    """
    try:
        import llvmlite.binding as binding
    except ImportError:
        return ""
    try:
        binding.initialize_all_targets()
        binding.initialize_all_asmprinters()
        machine = binding.Target.from_default_triple().create_target_machine()
        return str(machine.target_data)
    except Exception:
        return ""


class CEvaluator(object):

    def __init__(
        self,
        target_triple=None,
        backend=None,
        allow_unimplemented_backend=False,
    ):

        allow_unimplemented_backend = (
            allow_unimplemented_backend
            or backend_request_allows_unimplemented(backend)
        )
        self.backend_config = resolve_backend(
            backend,
            allow_unimplemented=allow_unimplemented_backend,
        )
        self.backend = self.backend_config.kind
        self.backend_sig = backend_signature(self.backend_config)

        self.codegen = LLVMCodeGenerator()
        from ..parse import make_c_parser
        self.parser = make_c_parser()
        # The triple comes from pcc's own host resolution, not LLVM's. Building
        # an LLVM target machine here made *compiling* C depend on llvmlite,
        # even though the self backend needs none of it: the codegen consumes a
        # triple string, a data-layout string that is only echoed into the IR
        # text, and a TargetData handle whose single consumer already falls
        # back when it is absent. A stage without llvmlite therefore reported
        # the whole C driver as unowned instead of naming what it lacked.
        # `self.target` stays available for the LLVM backend and the MCJIT
        # paths, built on first access.
        host_triple = host_target_triple()
        self.target_triple = target_triple or host_triple
        self._llvm_target = None
        self.codegen.set_target_text(self.target_triple, _host_data_layout_text())
        self.is_cross = target_triple is not None and target_triple != host_triple
        self.ee = None
        self._bound_modules = []
        self._bound_target_machine = None
        # In-memory JIT cache: maps (ir_text_hash, entry, opt_signature) →
        # (execution_engine, target_machine, module, func_addr, return_type, fptr)
        # Keeps the engine alive so the function pointer stays valid.
        self._jit_cache = {}

    @property
    def target(self):
        """LLVM target, built on first use.

        Only the LLVM backend and the MCJIT/native-cache paths need it; the
        self-backend compile path never touches it, which is what lets a stage
        without llvmlite still compile C.
        """
        if self._llvm_target is None:
            _llvm().initialize_all_targets()
            _llvm().initialize_all_asmprinters()
            self._llvm_target = _llvm().Target.from_triple(self.target_triple)
        return self._llvm_target

    def _detach_execution_engine(self):
        leaked = []
        if self.ee is not None and not self.ee.closed:
            self.ee.detach()
            leaked.append(self.ee)
        self.ee = None
        for module in self._bound_modules:
            if module is not None and not module.closed:
                module.detach()
                leaked.append(module)
        self._bound_modules = []
        if (
            self._bound_target_machine is not None
            and not self._bound_target_machine.closed
        ):
            self._bound_target_machine.detach()
            leaked.append(self._bound_target_machine)
        self._bound_target_machine = None
        if leaked:
            _DETACHED_MCJIT_WRAPPERS.extend(leaked)

    def evaluate(
        self,
        codestr,
        optimize=True,
        llvmdump=False,
        args=None,
        base_dir=None,
        use_system_cpp=None,
        prog_args=None,
        entry="main",
        include_dirs=None,
        cpp_args=None,
        link_args=None,
        use_compile_cache=True,
        cache_dir=None,
        fsanitize=None,
    ):
        if not isinstance(codestr, str):
            raise TypeError(
                f"evaluate() expects a string of C source code, "
                f"got {type(codestr).__name__}"
            )
        if not codestr.strip():
            raise ValueError("evaluate() received empty source code")

        llvm_dump_dir = _normalize_llvm_dump_dir(llvmdump, cache_dir)

        # SEC-P1-UBSAN: normalize the opt-in `-fsanitize` check list. Empty /
        # None keeps every guard OFF (the default) so lowering is unchanged.
        fsanitize = _normalize_fsanitize(fsanitize)
        if fsanitize:
            # Instrumented IR differs from any un-instrumented cache entry, so
            # skip all fast paths for a correct (never stale) instrumented run.
            use_compile_cache = False

        opt_level = self._normalize_opt_level(optimize)
        cheap_passes = _resolve_cheap_llvm_pipeline_passes()
        opt_signature = _llvm_optimization_signature(opt_level, cheap_passes)
        pass_signature = _pass_selection_signature()

        # Fast path 1: in-memory JIT cache keyed on source text + entry + opt.
        # Avoids ALL work — preprocessing, parsing, codegen, LLVM, and JIT.
        if not llvm_dump_dir and not prog_args and not fsanitize:
            src_hash = hashlib.sha256(codestr.encode("utf-8")).hexdigest()
            jit_key = (
                src_hash,
                entry,
                opt_signature,
                pass_signature,
                self.backend_sig,
            )
            cached = self._jit_cache.get(jit_key)
            if cached is not None:
                _ee, _tm, _mod, _fptr, _return_type = cached
                if args is None:
                    args = []
                return _fptr(*args)

            # Fast path 2: native .so disk cache keyed on source text.
            # Avoids preprocessing, parsing, codegen, LLVM — only ctypes.CDLL.
            if (
                not self.is_cross
                and _compile_cache_enabled(use_compile_cache)
            ):
                normalized_cache_dir = _normalize_compile_cache_dir(cache_dir)
                native_key = _native_cache_key(
                    entry,
                    opt_signature,
                    pass_signature,
                    self.backend_sig,
                    codestr,
                )
                native_func = _load_native_cache(
                    normalized_cache_dir, native_key, entry, c_int32,
                )
                if native_func is not None:
                    self._jit_cache[jit_key] = (
                        None, None, None, native_func, c_int32,
                    )
                    if args is None:
                        args = []
                    return native_func(*args)

        if use_system_cpp is None:
            use_system_cpp = self._has_system_cpp()
        snippet_base_dir = os.path.abspath(base_dir) if base_dir else os.getcwd()
        snippet_unit = TranslationUnit(
            name="__pcc_eval__.c",
            path=os.path.join(snippet_base_dir, "__pcc_eval__.c"),
            source=codestr,
        )
        artifact = _compile_translation_unit_artifact_job(
            snippet_unit,
            snippet_base_dir,
            use_system_cpp,
            include_dirs,
            cpp_args,
            cache_dir,
            use_compile_cache,
            opt_level,
            self.backend_sig,
            fsanitize=fsanitize,
        )
        ir_text = artifact["ir_text"]
        pass_ctx = _pass_context_from_artifact(artifact)

        # Build native shared-library disk cache for future fast cold starts.
        if (
            self.backend != "self"
            and not llvm_dump_dir
            and not prog_args
            and not self.is_cross
            and _compile_cache_enabled(use_compile_cache)
        ):
            normalized_cache_dir = _normalize_compile_cache_dir(cache_dir)
            native_key = _native_cache_key(
                entry,
                opt_signature,
                pass_signature,
                self.backend_sig,
                codestr,
            )
            _build_native_cache(
                normalized_cache_dir, native_key, ir_text,
                self.target, opt_level, cheap_passes=cheap_passes,
            )

        if llvm_dump_dir:
            _write_llvm_dump(llvm_dump_dir, "temp.ir", ir_text)

        if self.backend == "self":
            if entry != "main":
                raise BackendUnavailable(
                    "backend 'self' direct execution currently only supports entry='main'"
                )
            if args:
                raise BackendUnavailable(
                    "backend 'self' direct execution does not support raw CFUNCTYPE args yet"
                )
            result = self._run_compiled_translation_units_self_backend(
                [_artifact_to_compiled_unit(artifact)],
                base_dir=base_dir,
                prog_args=prog_args,
                link_args=link_args,
                capture_output=False,
                text=False,
            )
            return result.returncode

        ir_text, external_mode = _apply_external_llvm_pipeline_to_text(
            ir_text,
            opt_level,
            pass_ctx=pass_ctx,
        )
        try:
            llvmmod = _llvm().parse_assembly(ir_text)
        except Exception:
            # Dump the faulty IR to a per-TU file so we can inspect
            # which translation unit / function triggered the error.
            import os as _os, hashlib as _hashlib
            dump_dir = _os.environ.get("PCC_DUMP_BAD_IR")
            if dump_dir:
                _os.makedirs(dump_dir, exist_ok=True)
                digest = _hashlib.md5(ir_text.encode()).hexdigest()[:10]
                with open(_os.path.join(dump_dir, f"bad_{digest}.ll"), "w") as _f:
                    _f.write(ir_text)
            raise

        target_machine = self.target.create_target_machine()
        opt_mode = external_mode
        if opt_mode is None:
            opt_mode = _apply_llvm_optimizations(
                llvmmod,
                target_machine,
                opt_level,
                cheap_passes=cheap_passes,
                pass_ctx=pass_ctx,
            )
        if llvm_dump_dir and opt_mode != "none":
            tempbcode = str(llvmmod)
            _write_llvm_dump(
                llvm_dump_dir,
                "temp.ooptimize.bcode",
                tempbcode,
            )
        _load_mcjit_link_libraries(link_args)

        self.ee = _llvm().create_mcjit_compiler(llvmmod, target_machine)
        self.ee.finalize_object()

        if llvm_dump_dir:
            tempbcode = target_machine.emit_assembly(llvmmod)
            _write_llvm_dump(llvm_dump_dir, "temp.bcode", tempbcode)

        return_type = _entry_return_type_from_artifact(artifact, entry)

        main_addr = self.ee.get_function_address(entry)

        if prog_args:
            # Build argc/argv for main(int argc, char **argv)
            argv_strings = ["pcc"] + list(prog_args)
            argc = len(argv_strings)
            ArgvType = c_char_p * (argc + 1)
            argv = ArgvType(*[s.encode() for s in argv_strings], None)
            fptr = CFUNCTYPE(return_type, c_int32, POINTER(c_char_p))(main_addr)
            result = fptr(argc, argv)
        else:
            fptr = CFUNCTYPE(return_type)(main_addr)
            if args is None:
                args = []
            # Cache the JIT state for future calls. Never cache an
            # instrumented result under the un-instrumented key (SEC-P1-UBSAN).
            if not llvm_dump_dir and not fsanitize:
                src_hash = hashlib.sha256(codestr.encode("utf-8")).hexdigest()
                jit_key = (
                    src_hash,
                    entry,
                    opt_signature,
                    pass_signature,
                    self.backend_sig,
                )
                self._jit_cache[jit_key] = (
                    self.ee, target_machine, llvmmod, fptr, return_type,
                )
                # Prevent _detach_execution_engine from freeing cached engines
                self.ee = None
            result = fptr(*args)

        return result

    def _compile_translation_units(
        self,
        units,
        base_dir,
        use_system_cpp,
        jobs,
        include_dirs=None,
        cpp_args=None,
        cache_dir=None,
        use_compile_cache=True,
        frontend_opt_level=None,
    ):
        if jobs <= 1 or len(units) <= 1:
            return [
                _compile_translation_unit_artifact_job(
                    unit,
                    base_dir,
                    use_system_cpp,
                    include_dirs,
                    cpp_args,
                    cache_dir,
                    use_compile_cache,
                    frontend_opt_level,
                    self.backend_sig,
                    self.target_triple,
                )
                for unit in units
            ]

        max_workers = min(jobs, len(units))
        with ProcessPoolExecutor(max_workers=max_workers) as executor:
            return list(
                executor.map(
                    _compile_translation_unit_artifact_job,
                    units,
                    repeat(base_dir),
                    repeat(use_system_cpp),
                    repeat(include_dirs),
                    repeat(cpp_args),
                    repeat(cache_dir),
                    repeat(use_compile_cache),
                    repeat(frontend_opt_level),
                    repeat(self.backend_sig),
                    repeat(self.target_triple),
                )
            )

    @staticmethod
    def _normalize_opt_level(optimize):
        """Convert optimize parameter to integer opt level (0-3).

        Accepts bool (True→2, False→0) or int (0-3) for backward compat.
        """
        if isinstance(optimize, bool):
            return 2 if optimize else 0
        return max(0, min(3, int(optimize)))

    def _prepare_llvm_module(
        self, unit_name, ir_text, target_machine, optimize=True, llvmdump=False
    ):
        safe_name = re.sub(r"\W+", "_", unit_name)
        llvm_dump_dir = _normalize_llvm_dump_dir(llvmdump)
        if llvm_dump_dir:
            _write_llvm_dump(
                llvm_dump_dir,
                f"temp.{safe_name}.ir",
                ir_text,
            )

        opt_level = self._normalize_opt_level(optimize)
        ir_text, external_mode = _apply_external_llvm_pipeline_to_text(
            ir_text, opt_level
        )
        try:
            llvmmod = _llvm().parse_assembly(ir_text)
        except Exception:
            import os as _os
            dump_dir = _os.environ.get("PCC_DUMP_BAD_IR")
            if dump_dir:
                _os.makedirs(dump_dir, exist_ok=True)
                safe = safe_name.replace("/", "_")
                with open(_os.path.join(dump_dir, f"{safe}.bad.ll"), "w") as _f:
                    _f.write(ir_text)
            raise
        opt_mode = external_mode
        if opt_mode is None:
            opt_mode = _apply_llvm_optimizations(llvmmod, target_machine, opt_level)
        if llvm_dump_dir and opt_mode != "none":
            _write_llvm_dump(
                llvm_dump_dir,
                f"temp.{safe_name}.opt.ll",
                str(llvmmod),
            )

        return llvmmod

    def _prepare_linked_llvm_module(
        self, compiled_units, target_machine, optimize=True, llvmdump=False
    ):
        combined = None
        main_return_type = None
        llvm_dump_dir = _normalize_llvm_dump_dir(llvmdump)

        for unit_name, ir_text, unit_return_type, _external_defs in compiled_units:
            llvmmod = self._prepare_llvm_module(
                unit_name,
                ir_text,
                target_machine,
                optimize=optimize,
                llvmdump=llvm_dump_dir,
            )
            if combined is None:
                combined = llvmmod
            else:
                combined.link_in(llvmmod)
            if unit_return_type is not None:
                main_return_type = unit_return_type

        if combined is None:
            raise ValueError("No translation units provided")

        return combined, main_return_type

    def compile_translation_units(
        self,
        units,
        base_dir=None,
        use_system_cpp=None,
        jobs=1,
        include_dirs=None,
        cpp_args=None,
        use_compile_cache=True,
        cache_dir=None,
        frontend_opt_level=None,
    ):
        if use_system_cpp is None:
            use_system_cpp = self._has_system_cpp()

        artifacts = self._compile_translation_units(
            units,
            base_dir,
            use_system_cpp,
            jobs,
            include_dirs=include_dirs,
            cpp_args=cpp_args,
            cache_dir=cache_dir,
            use_compile_cache=use_compile_cache,
            frontend_opt_level=frontend_opt_level,
        )
        compiled_units = [_artifact_to_compiled_unit(artifact) for artifact in artifacts]
        _raise_if_duplicate_external_definitions(compiled_units)
        return compiled_units

    def evaluate_compiled_translation_units(
        self,
        compiled_units,
        optimize=True,
        llvmdump=False,
        args=None,
        prog_args=None,
        link_args=None,
    ):
        if self.backend == "self":
            if args:
                raise BackendUnavailable(
                    "backend 'self' direct execution does not support raw CFUNCTYPE args yet"
                )
            result = self._run_compiled_translation_units_self_backend(
                compiled_units,
                prog_args=prog_args,
                link_args=link_args,
                capture_output=False,
                text=False,
            )
            return result.returncode

        if sys.platform == "darwin":
            return self._evaluate_compiled_translation_units_via_subprocess(
                compiled_units,
                optimize=optimize,
                llvmdump=llvmdump,
                args=args,
                prog_args=prog_args,
                link_args=link_args,
            )

        return self._evaluate_compiled_translation_units_in_process(
            compiled_units,
            optimize=optimize,
            llvmdump=llvmdump,
            args=args,
            prog_args=prog_args,
            link_args=link_args,
        )

    def evaluate_translation_units(
        self,
        units,
        optimize=True,
        llvmdump=False,
        args=None,
        base_dir=None,
        use_system_cpp=None,
        prog_args=None,
        jobs=1,
        include_dirs=None,
        cpp_args=None,
        link_args=None,
        use_compile_cache=True,
        cache_dir=None,
    ):
        if not units:
            raise ValueError("evaluate_translation_units() received no translation units")
        opt_level = self._normalize_opt_level(optimize)
        compiled_units = self.compile_translation_units(
            units,
            base_dir=base_dir,
            use_system_cpp=use_system_cpp,
            jobs=jobs,
            include_dirs=include_dirs,
            cpp_args=cpp_args,
            use_compile_cache=use_compile_cache,
            cache_dir=cache_dir,
            frontend_opt_level=opt_level,
        )
        if self.backend == "self":
            result = self._run_compiled_translation_units_self_backend(
                compiled_units,
                base_dir=base_dir,
                prog_args=prog_args,
                link_args=link_args,
                capture_output=False,
                text=False,
            )
            return result.returncode
        return self.evaluate_compiled_translation_units(
            compiled_units,
            optimize=optimize,
            llvmdump=llvmdump,
            args=args,
            prog_args=prog_args,
            link_args=link_args,
        )

    def _evaluate_compiled_translation_units_via_subprocess(
        self,
        compiled_units,
        optimize=True,
        llvmdump=False,
        args=None,
        prog_args=None,
        link_args=None,
    ):
        _raise_if_duplicate_external_definitions(compiled_units)

        fd, result_path = tempfile.mkstemp(prefix="pcc_mcjit_result_", suffix=".json")
        os.close(fd)
        ctx = multiprocessing.get_context("spawn")
        proc = ctx.Process(
            target=_run_linked_mcjit_worker,
            args=(
                compiled_units,
                optimize,
                llvmdump,
                args,
                prog_args,
                link_args,
                result_path,
            ),
        )
        proc.start()
        proc.join()

        try:
            payload = None
            if os.path.exists(result_path) and os.path.getsize(result_path) > 0:
                with open(result_path) as f:
                    payload = json.load(f)

            if proc.exitcode != 0:
                if payload and not payload.get("ok", False):
                    raise RuntimeError(
                        f"MCJIT subprocess failed: {payload.get('error', 'unknown error')}"
                    )
                if proc.exitcode > 0 and payload is None:
                    return proc.exitcode
                raise RuntimeError(f"MCJIT subprocess exited with code {proc.exitcode}")

            if payload is None:
                return 0
            if not payload.get("ok", False):
                raise RuntimeError(
                    f"MCJIT subprocess failed: {payload.get('error', 'unknown error')}"
                )
            return payload["result"]
        finally:
            if os.path.exists(result_path):
                os.unlink(result_path)

    def _evaluate_compiled_translation_units_in_process(
        self,
        compiled_units,
        optimize=True,
        llvmdump=False,
        args=None,
        prog_args=None,
        link_args=None,
    ):
        _raise_if_duplicate_external_definitions(compiled_units)

        target_machine = self.target.create_target_machine()
        _load_mcjit_link_libraries(link_args)
        llvmmod, main_return_type = self._prepare_linked_llvm_module(
            compiled_units,
            target_machine,
            optimize=optimize,
            llvmdump=llvmdump,
        )
        self.ee = _llvm().create_mcjit_compiler(llvmmod, target_machine)
        self._bound_modules = [llvmmod]
        self._bound_target_machine = target_machine

        try:
            self.ee.finalize_object()

            return_type = get_c_type_from_serialized_ir(main_return_type)
            if main_return_type is None:
                return_type = c_int32

            main_addr = self.ee.get_function_address("main")

            if prog_args:
                argv_strings = ["pcc"] + list(prog_args)
                argc = len(argv_strings)
                ArgvType = c_char_p * (argc + 1)
                argv = ArgvType(*[s.encode() for s in argv_strings], None)
                fptr = CFUNCTYPE(return_type, c_int32, POINTER(c_char_p))(main_addr)
                result = fptr(argc, argv)
            else:
                fptr = CFUNCTYPE(return_type)(main_addr)
                if args is None:
                    args = []
                result = fptr(*args)

            return result
        finally:
            # MCJIT disposal is unstable for some large multi-TU programs on
            # this llvmlite/LLVM combination. Detach wrappers after execution
            # so Python GC does not call back into engine teardown.
            self._detach_execution_engine()

    def run_compiled_translation_units_with_system_cc(
        self,
        compiled_units,
        optimize=True,
        llvmdump=False,
        base_dir=None,
        prog_args=None,
        link_args=None,
        timeout=120,
        capture_output=True,
        text=True,
        freestanding_libc=False,
    ):
        link_args = list(link_args or [])
        self._validate_freestanding_link_args(
            link_args,
            enabled=freestanding_libc,
        )
        if self.backend == "self":
            return self._run_compiled_translation_units_self_backend(
                compiled_units,
                optimize=optimize,
                base_dir=base_dir,
                prog_args=prog_args,
                link_args=link_args,
                timeout=timeout,
                capture_output=capture_output,
                text=text,
                freestanding_libc=freestanding_libc,
            )

        _raise_if_duplicate_external_definitions(compiled_units)

        cc = self._system_cc()
        target_machine = self.target.create_target_machine()
        tmpdir = tempfile.mkdtemp(prefix="pcc_system_link_")
        try:
            obj_paths = []

            for unit_name, ir_text, _unit_return_type, _external_defs in compiled_units:
                llvmmod = self._prepare_llvm_module(
                    unit_name,
                    ir_text,
                    target_machine,
                    optimize=optimize,
                    llvmdump=llvmdump,
                )
                obj_path = os.path.join(
                    tmpdir, f"{re.sub(r'\\W+', '_', unit_name) or 'unit'}.o"
                )
                os.makedirs(os.path.dirname(obj_path), exist_ok=True)
                with open(obj_path, "wb") as f:
                    f.write(target_machine.emit_object(llvmmod))
                obj_paths.append(obj_path)

            startup_inputs, platform_link_flags, runtime_link_args = (
                self._freestanding_link_inputs(
                    tmpdir,
                    enabled=freestanding_libc,
                    timeout=timeout,
                )
            )
            bin_path = os.path.join(tmpdir, "a.out")
            link_cmd = (
                [cc]
                + obj_paths
                + startup_inputs
                + ["-o", bin_path]
                + platform_link_flags
                + link_args
                + runtime_link_args
            )
            link_run = subprocess.run(
                link_cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if link_run.returncode != 0:
                detail = (link_run.stderr or link_run.stdout or "unknown linker error")[
                    :400
                ]
                raise RuntimeError(f"system cc link failed: {detail}")

            run_cmd = [bin_path] + [str(arg) for arg in (prog_args or [])]
            # Finish running the child process before clearing the tmpdir —
            # `subprocess.run` waits, so unlinking after this point is safe.
            result = subprocess.run(
                run_cmd,
                capture_output=capture_output,
                text=text,
                timeout=timeout,
                cwd=base_dir or os.getcwd(),
            )
            return result
        finally:
            # The dir only held staging objects / the linked binary; nothing
            # in the returned `subprocess.CompletedProcess` references it.
            shutil.rmtree(tmpdir, ignore_errors=True)

    def emit_compiled_units(
        self,
        compiled_units,
        emit_obj=None,
        emit_asm=None,
        emit_llvm=None,
        optimize=True,
    ):
        """Emit compiled translation units to file(s) instead of running."""
        if self.backend == "self":
            self._emit_compiled_units_self_backend(
                compiled_units,
                emit_obj=emit_obj,
                emit_asm=emit_asm,
                emit_llvm=emit_llvm,
                optimize=optimize,
            )
            return

        target_machine = self.target.create_target_machine()

        combined = None
        for unit_name, ir_text, _unit_return_type, _external_defs in compiled_units:
            llvmmod = self._prepare_llvm_module(
                unit_name, ir_text, target_machine, optimize=optimize,
            )
            if combined is None:
                combined = llvmmod
            else:
                combined.link_in(llvmmod)

        if combined is None:
            raise ValueError("No translation units to emit")

        if emit_llvm:
            with open(emit_llvm, "w") as f:
                f.write(str(combined))

        if emit_asm:
            asm_text = target_machine.emit_assembly(combined)
            with open(emit_asm, "w") as f:
                f.write(asm_text)

        if emit_obj:
            obj_bytes = target_machine.emit_object(combined)
            with open(emit_obj, "wb") as f:
                f.write(obj_bytes)

    def _emit_compiled_units_self_backend(
        self,
        compiled_units,
        emit_obj=None,
        emit_asm=None,
        emit_llvm=None,
        optimize=True,
    ):
        prepared_units = (
            self._prepare_self_backend_units(compiled_units, optimize=optimize)
            if self._normalize_opt_level(optimize) > 0
            else compiled_units
        )
        ir_texts = [
            ir_text
            for _unit_name, ir_text, _unit_return_type, _external_defs
            in prepared_units
        ]
        object_emitter = None
        if emit_obj:
            if not ir_texts:
                raise ValueError("No translation units to emit")
            target_identities = [
                self_backend_target_identity(parse_self_backend_target_triple(ir_text))
                for ir_text in ir_texts
            ]
            target_identity = target_identities[0]
            if any(item != target_identity for item in target_identities[1:]):
                raise BackendUnavailable(
                    "self backend object emission cannot combine target identities: "
                    + ", ".join(sorted(set(target_identities)))
                )
            object_emitter = _select_self_object_emitter(
                os.environ.get(_SELF_OBJECT_EMITTER_ENV),
                target_identity,
            )
        asm_text = self._self_backend_asm_text(prepared_units)

        if emit_llvm:
            with open(emit_llvm, "w") as f:
                f.write("\n\n".join(ir_texts))

        if emit_asm:
            with open(emit_asm, "w") as f:
                f.write(asm_text)

        if emit_obj:
            # Owned targets default to pcc's assembler + object writer, so
            # --emit-obj does not serialize and reparse through as(1).
            # PCC_SELF_OBJ=system-as keeps the host assembler as an explicit
            # differential oracle.  Selection is fail-closed above: an
            # unknown value never becomes an accidental system-tool fallback.
            if object_emitter == _SELF_OBJECT_EMITTER_PCC:
                if target_identity == "self-aarch64-darwin-v0":
                    from ..backend.arm64_asm_driver import assemble_file
                    from ..backend.macho_link import link_relocatable_native
                    from ..backend.native_object import NativeObject

                    # Each translation unit owns one complete, versioned
                    # precise-stackmap table.  Concatenating their assembly
                    # first would concatenate table headers inside a single
                    # Mach-O section, which is not a valid stackmap payload.
                    # Assemble units independently, then use the semantic
                    # relocatable linker to merge/re-encode their tables and
                    # resolve cross-TU symbols deterministically.
                    native_objects = []
                    for (
                        _unit_name,
                        ir_text,
                        _unit_return_type,
                        _external_defs,
                    ) in prepared_units:
                        sections, undefined = assemble_file(emit_self_asm(ir_text))
                        native_objects.append(
                            NativeObject.from_sections(
                                sections,
                                undefined=undefined,
                            )
                        )
                    native_object = (
                        native_objects[0]
                        if len(native_objects) == 1
                        else link_relocatable_native(native_objects)
                    )
                    object_bytes = native_object.to_macho()
                elif target_identity == "self-x86_64-linux-v0":
                    from ..backend.elf_x86_64 import emit_relocatable
                    from ..backend.x86_64_asm_driver import assemble_file

                    object_bytes = emit_relocatable(assemble_file(asm_text))
                else:
                    raise BackendUnavailable(
                        "pcc self object emitter lost target validation for "
                        + repr(target_identity)
                    )
                with open(emit_obj, "wb") as f:
                    f.write(object_bytes)
                return
            cc = self._system_cc()
            with tempfile.TemporaryDirectory(prefix="pcc_self_obj_") as tmpdir:
                asm_path = os.path.join(tmpdir, "self_backend.s")
                with open(asm_path, "w") as f:
                    f.write(asm_text)
                result = subprocess.run(
                    [cc, "-c", asm_path, "-o", emit_obj],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode != 0:
                    detail = (result.stderr or result.stdout or "unknown assembler error")[:400]
                    raise RuntimeError(f"self backend object emission failed: {detail}")

        if not emit_asm and not emit_llvm:
            if not emit_obj:
                raise BackendUnavailable(
                    "backend 'self' currently supports --emit-asm, --emit-obj, and optional --emit-llvm"
                )

    def _prepare_self_backend_units(self, compiled_units, *, optimize=True):
        target_machine = self.target.create_target_machine()
        prepared_units = []
        for unit_name, ir_text, unit_return_type, external_defs in compiled_units:
            llvmmod = self._prepare_llvm_module(
                unit_name,
                ir_text,
                target_machine,
                optimize=optimize,
            )
            prepared_units.append((unit_name, str(llvmmod), unit_return_type, external_defs))
        return prepared_units

    def _self_backend_asm_text(self, compiled_units):
        asm_modules = []
        needs_subsections_via_symbols = False
        for _unit_name, ir_text, _unit_return_type, _external_defs in compiled_units:
            target_id = self_backend_target_identity(parse_self_backend_target_triple(ir_text))
            asm_lines = emit_self_asm(ir_text).splitlines()
            if asm_lines and asm_lines[-1] == ".subsections_via_symbols":
                asm_lines = asm_lines[:-1]
            if target_id == "self-aarch64-darwin-v0":
                needs_subsections_via_symbols = True
            asm_modules.append("\n".join(asm_lines).strip())
        asm_text = "\n\n".join(fragment for fragment in asm_modules if fragment)
        if needs_subsections_via_symbols:
            asm_text += "\n.subsections_via_symbols\n"
        return asm_text

    def _validate_freestanding_link_args(self, link_args, *, enabled):
        """Keep Linux's freestanding startup on its fixed ET_EXEC contract."""
        if not enabled or "linux" not in str(self.target_triple or "").lower():
            return

        rejected_arg = None
        for raw_arg in link_args:
            arg = str(raw_arg)
            candidates = [arg]
            if arg.startswith("-Wl,"):
                candidates.extend(arg[4:].split(","))
            elif arg.startswith("-Xlinker="):
                candidates.append(arg.split("=", 1)[1])
            if any(
                candidate in _LINUX_FREESTANDING_NON_EXEC_LINK_OPTIONS
                for candidate in candidates
            ):
                rejected_arg = arg
                break

        if rejected_arg is not None:
            raise RuntimeError(
                "--freestanding-libc on Linux requires a fixed ET_EXEC "
                "startup; PIE/static-PIE, shared, and relocatable link "
                "arguments are unsupported: "
                + rejected_arg
            )

    def _freestanding_link_inputs(self, tmpdir, *, enabled, timeout):
        """Return startup inputs and platform flags for one final C link.

        Darwin deliberately keeps clang's normal crt/libSystem machine
        boundary. Linux x86_64 replaces crt and libc with a process entry
        authored in strict pcc-Python and a static, no-interpreter link.
        """
        if not enabled:
            return [], self._platform_link_flags(), []

        triple = str(self.target_triple or "").lower()
        if "darwin" in triple or "apple" in triple:
            from pcc.py_frontend.pipeline import _ensure_runtime

            runtime_archive = _ensure_runtime(False, needs_libpython=False)
            if not runtime_archive:
                raise RuntimeError(
                    "--freestanding-libc could not build the shared "
                    "pcc-Python runtime archive"
                )
            if os.path.basename(runtime_archive) != "libpy_runtime_pcc_py.a":
                raise RuntimeError(
                    "--freestanding-libc requires libpy_runtime_pcc_py.a; "
                    "PCC_RUNTIME_ARCHIVE selected "
                    + os.path.basename(runtime_archive)
                )
            return [], self._platform_link_flags(), [runtime_archive]
        if "linux" not in triple:
            raise RuntimeError(
                "--freestanding-libc supports Darwin and Linux x86_64"
            )
        if not (triple.startswith("x86_64") or triple.startswith("amd64")):
            raise RuntimeError(
                "--freestanding-libc Linux startup currently requires x86_64"
            )
        if not sys.platform.startswith("linux") or platform.machine() not in (
            "x86_64",
            "AMD64",
        ):
            raise RuntimeError(
                "--freestanding-libc cannot cross-link the Linux startup with "
                "the host C linker; run it on Linux x86_64"
            )

        from pcc.py_frontend.pipeline import compile_python

        runtime_root = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "py_runtime"
        )
        source = os.path.join(runtime_root, "py", "freestanding_c_linux_start.py")
        ir_path = os.path.join(tmpdir, "freestanding_c_linux_start.ll")
        asm_path = os.path.join(tmpdir, "freestanding_c_linux_start.s")
        compile_python(
            source,
            ir_path,
            emit_llvm_only=True,
            libpython_mode="off",
            python_library=True,
            backend="self",
            target_triple=self.target_triple,
        )
        with open(ir_path, "r", encoding="utf-8") as f:
            startup_ir = f.read()
        with open(asm_path, "w", encoding="utf-8") as f:
            f.write(emit_self_asm(startup_ir, self.target_triple))

        libc_objects = []
        for module_name in FREESTANDING_C_LIBC_PY_MODULES:
            module_source = os.path.join(runtime_root, "py", module_name + ".py")
            module_ir_path = os.path.join(tmpdir, module_name + ".ll")
            module_asm_path = os.path.join(tmpdir, module_name + ".s")
            module_obj_path = os.path.join(tmpdir, module_name + ".o")
            compile_python(
                module_source,
                module_ir_path,
                emit_llvm_only=True,
                libpython_mode="off",
                python_library=True,
                backend="self",
                target_triple=self.target_triple,
            )
            with open(module_ir_path, "r", encoding="utf-8") as f:
                module_ir = f.read()
            with open(module_asm_path, "w", encoding="utf-8") as f:
                f.write(emit_self_asm(module_ir, self.target_triple))
            assemble_run = subprocess.run(
                [self._system_cc(), "-c", module_asm_path, "-o", module_obj_path],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if assemble_run.returncode != 0:
                detail = (
                    assemble_run.stderr
                    or assemble_run.stdout
                    or "unknown assembler error"
                )[:400]
                raise RuntimeError(
                    "freestanding pcc-Python libc object assembly failed: "
                    + detail
                )
            libc_objects.append(module_obj_path)

        ar = shutil.which("ar")
        if not ar:
            raise RuntimeError("ar is required for --freestanding-libc")
        archive_path = os.path.join(tmpdir, "libpcc_freestanding_c.a")
        archive_run = subprocess.run(
            [ar, "rcs", archive_path] + libc_objects,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if archive_run.returncode != 0:
            detail = (
                archive_run.stderr
                or archive_run.stdout
                or "unknown archive error"
            )[:400]
            raise RuntimeError(
                "freestanding pcc-Python libc archive failed: " + detail
            )
        return (
            [asm_path],
            ["-nostdlib", "-static", "-no-pie", "-Wl,-e,_start"],
            [archive_path],
        )

    def _run_compiled_translation_units_self_backend(
        self,
        compiled_units,
        *,
        optimize=True,
        base_dir=None,
        prog_args=None,
        link_args=None,
        timeout=120,
        capture_output=False,
        text=False,
        freestanding_libc=False,
    ):
        cc = self._system_cc()
        prepared_units = (
            self._prepare_self_backend_units(compiled_units, optimize=optimize)
            if self._normalize_opt_level(optimize) > 0
            else compiled_units
        )
        asm_text = self._self_backend_asm_text(prepared_units)
        tmpdir = tempfile.mkdtemp(prefix="pcc_self_run_")
        try:
            asm_path = os.path.join(tmpdir, "self_backend.s")
            with open(asm_path, "w") as f:
                f.write(asm_text)
            startup_inputs, platform_link_flags, runtime_link_args = (
                self._freestanding_link_inputs(
                    tmpdir,
                    enabled=freestanding_libc,
                    timeout=timeout,
                )
            )
            bin_path = os.path.join(tmpdir, "a.out")
            link_cmd = (
                [cc, asm_path]
                + startup_inputs
                + ["-o", bin_path]
                + platform_link_flags
                + list(link_args or [])
                + runtime_link_args
            )
            link_run = subprocess.run(
                link_cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if link_run.returncode != 0:
                detail = (link_run.stderr or link_run.stdout or "unknown linker error")[:400]
                raise RuntimeError(f"self backend link failed: {detail}")

            run_cmd = [bin_path] + [str(arg) for arg in (prog_args or [])]
            return subprocess.run(
                run_cmd,
                capture_output=capture_output,
                text=text,
                timeout=timeout,
                cwd=base_dir or os.getcwd(),
            )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def run_translation_units_with_system_cc(
        self,
        units,
        optimize=True,
        llvmdump=False,
        base_dir=None,
        use_system_cpp=None,
        prog_args=None,
        jobs=1,
        link_args=None,
        timeout=120,
        capture_output=True,
        text=True,
        include_dirs=None,
        cpp_args=None,
        use_compile_cache=True,
        cache_dir=None,
        freestanding_libc=False,
    ):
        link_args = list(link_args or [])
        self._validate_freestanding_link_args(
            link_args,
            enabled=freestanding_libc,
        )
        opt_level = self._normalize_opt_level(optimize)
        compiled_units = self.compile_translation_units(
            units,
            base_dir,
            use_system_cpp,
            jobs,
            include_dirs=include_dirs,
            cpp_args=cpp_args,
            use_compile_cache=use_compile_cache,
            cache_dir=cache_dir,
            frontend_opt_level=opt_level,
        )
        return self.run_compiled_translation_units_with_system_cc(
            compiled_units,
            optimize=optimize,
            llvmdump=llvmdump,
            base_dir=base_dir,
            prog_args=prog_args,
            link_args=link_args,
            timeout=timeout,
            capture_output=capture_output,
            text=text,
            freestanding_libc=freestanding_libc,
        )

    @staticmethod
    def _has_system_cpp():
        return shutil.which("cc") is not None or shutil.which("gcc") is not None

    @staticmethod
    def _system_cc():
        cc = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
        if not cc:
            raise RuntimeError("No system C compiler found for linking")
        return cc

    @staticmethod
    def _platform_link_flags():
        """Return extra link flags needed on the current platform.

        On Linux, pcc emits non-PIC object code (sspstrong references
        __stack_chk_guard with absolute relocations).  The default PIE
        linker mode rejects these, so we pass -no-pie.
        """
        import sys
        if sys.platform.startswith("linux"):
            return ["-no-pie"]
        return []

    @staticmethod
    def _system_cpp(source, base_dir=None, include_dirs=None, cpp_args=None):
        """Use system C preprocessor (cc -E) for fast preprocessing.

        Uses -nostdinc + fake libc headers so output is pycparser-compatible.
        """
        cc = CEvaluator._system_cc()
        cpp_args = list(cpp_args or [])

        # Find fake libc headers (shipped with pcc)
        # __file__ = pcc/evaluater/c_evaluator.py → project root is 2 levels up
        pcc_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        fake_libc = os.path.join(pcc_root, "utils", "fake_libc_include")
        base_dir = os.path.abspath(base_dir) if base_dir else os.getcwd()
        user_include_dirs = []
        seen = set()
        for include_dir in [base_dir] + list(include_dirs or []):
            if not include_dir:
                continue
            include_dir = os.path.abspath(include_dir)
            if include_dir in seen:
                continue
            seen.add(include_dir)
            user_include_dirs.append(include_dir)
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".c",
            delete=False,
            encoding="utf-8",
            errors="surrogateescape",
        ) as f:
            f.write(source)
            tmp_path = f.name
        try:
            platform_defs = []
            prefer_system_headers = any(
                marker in include_dir
                for include_dir in user_include_dirs
                for marker in ("zstd-", "openssl-", "postgresql-")
            )
            postgres_header_compat = any(
                "postgresql-" in include_dir for include_dir in user_include_dirs
            )
            system_header_compat_defs = []
            first_cpp_error = None
            def _postprocess_preprocessed_text(text):
                if postgres_header_compat:
                    text = re.sub(r"\b__restrict__\b", "", text)
                    text = re.sub(r"\b__restrict\b", "", text)
                    text = text.replace("({ do { ; } while(0); 1; })", "1")
                    # PostgreSQL frontend headers typedef int128 via a bare
                    # __int128 spelling that pycparser cannot parse. libpq's
                    # frontend sources do not rely on 128-bit semantics here,
                    # so narrow it to 64-bit during preprocessing.
                    text = re.sub(
                        r"\bunsigned\s+__int128\b", "unsigned long long", text
                    )
                    text = re.sub(r"\b__int128\b", "long long", text)
                return text
            if sys.platform == "darwin":
                # fake libc headers do not provide the stdio macro remaps that
                # macOS uses for FILE* globals.
                platform_defs.extend(
                    [
                        "-D__PCC_HOST_DARWIN__=1",
                        "-Dstdin=__stdinp",
                        "-Dstdout=__stdoutp",
                        "-Dstderr=__stderrp",
                        "-U__BLOCKS__",
                    ]
                )
                # pcc does not support ARM vector intrinsics; prefer scalar
                # fallbacks instead of pulling in headers like arm_neon.h.
                if platform.machine() in {"arm64", "aarch64"}:
                    platform_defs.extend(
                        [
                            "-U__ARM_NEON",
                            "-U__ARM_NEON__",
                            "-U__ARM_FEATURE_CRC32",
                        ]
                    )
            compat_defs = [
                # Standard limits (fake headers don't have these)
                "-DLLONG_MAX=9223372036854775807LL",
                "-DLLONG_MIN=(-9223372036854775807LL-1)",
                "-DULLONG_MAX=18446744073709551615ULL",
                "-DLONG_MAX=9223372036854775807L",
                "-DINT_MAX=2147483647",
                "-DINT_MIN=(-2147483647-1)",
                "-DLONG_MIN=(-9223372036854775807L-1)",
                "-DUINT_MAX=4294967295U",
                "-DCHAR_BIT=8",
                "-DSHRT_MAX=32767",
                "-DUSHRT_MAX=65535",
                "-DCHAR_MAX=127",
                "-DUCHAR_MAX=255",
                # stdint.h fixed-width limits (LP64 model, int64_t = long).
                "-DINT8_MIN=(-128)",
                "-DINT8_MAX=127",
                "-DUINT8_MAX=255",
                "-DINT16_MIN=(-32768)",
                "-DINT16_MAX=32767",
                "-DUINT16_MAX=65535",
                "-DINT32_MIN=(-2147483647-1)",
                "-DINT32_MAX=2147483647",
                "-DUINT32_MAX=4294967295U",
                "-DINT64_MIN=(-9223372036854775807L-1)",
                "-DINT64_MAX=9223372036854775807L",
                "-DUINT64_MAX=18446744073709551615UL",
                "-DINTPTR_MIN=(-9223372036854775807L-1)",
                "-DINTPTR_MAX=9223372036854775807L",
                "-DUINTPTR_MAX=18446744073709551615UL",
                "-DPTRDIFF_MIN=(-9223372036854775807L-1)",
                "-DPTRDIFF_MAX=9223372036854775807L",
                "-DSIZE_MAX=18446744073709551615UL",
                "-DINTMAX_MIN=(-9223372036854775807LL-1)",
                "-DINTMAX_MAX=9223372036854775807LL",
                "-DUINTMAX_MAX=18446744073709551615ULL",
                "-DWCHAR_MIN=(-2147483647-1)",
                "-DWCHAR_MAX=2147483647",
                "-DSIG_ATOMIC_MIN=(-2147483647-1)",
                "-DSIG_ATOMIC_MAX=2147483647",
                # inttypes.h format macros (int64_t = long in our LP64 model).
                '-DPRId8="d"',
                '-DPRIi8="i"',
                '-DPRIu8="u"',
                '-DPRIo8="o"',
                '-DPRIx8="x"',
                '-DPRIX8="X"',
                '-DPRId16="d"',
                '-DPRIi16="i"',
                '-DPRIu16="u"',
                '-DPRIo16="o"',
                '-DPRIx16="x"',
                '-DPRIX16="X"',
                '-DPRId32="d"',
                '-DPRIi32="i"',
                '-DPRIu32="u"',
                '-DPRIo32="o"',
                '-DPRIx32="x"',
                '-DPRIX32="X"',
                '-DPRId64="ld"',
                '-DPRIi64="li"',
                '-DPRIu64="lu"',
                '-DPRIo64="lo"',
                '-DPRIx64="lx"',
                '-DPRIX64="lX"',
                '-DPRIdPTR="ld"',
                '-DPRIiPTR="li"',
                '-DPRIuPTR="lu"',
                '-DPRIoPTR="lo"',
                '-DPRIxPTR="lx"',
                '-DPRIXPTR="lX"',
                '-DPRIdMAX="lld"',
                '-DPRIiMAX="lli"',
                '-DPRIuMAX="llu"',
                '-DPRIoMAX="llo"',
                '-DPRIxMAX="llx"',
                '-DPRIXMAX="llX"',
                "-DSIG_DFL=0",
                "-DSIG_IGN=1",
                "-DSIGINT=2",
                "-DCLOCKS_PER_SEC=1000000",
                "-DLC_ALL=0",
                "-DLC_COLLATE=1",
                "-DLC_CTYPE=2",
                "-DLC_MONETARY=3",
                "-DLC_NUMERIC=4",
                "-DLC_TIME=5",
                "-Doffsetof(t,m)=((long)&((t*)0)->m)",
                "-D__builtin_offsetof(t,m)=((long)&((t*)0)->m)",
                "-DDBL_MANT_DIG=53",
                "-DFLT_MANT_DIG=24",
                "-DDBL_MAX_EXP=1024",
                "-DFLT_MAX_EXP=128",
                "-DDBL_MAX=1.7976931348623158e+308",
                "-DHUGE_VAL=1e309",
                "-DHUGE_VALF=1e39f",
                "-DDBL_MAX_10_EXP=308",
                "-DFLT_MAX_10_EXP=38",
                "-DDBL_MIN_EXP=-1021",
                "-DDBL_EPSILON=2.2204460492503131e-16",
                "-DLDBL_MANT_DIG=53",
                "-DLDBL_MAX_EXP=1024",
                "-DLDBL_MAX_10_EXP=308",
                "-DLDBL_MIN_EXP=-1021",
                "-DLDBL_EPSILON=2.2204460492503131e-16L",
                "-D__ORDER_LITTLE_ENDIAN__=1234",
                "-D__ORDER_BIG_ENDIAN__=4321",
                "-D__BYTE_ORDER__=__ORDER_LITTLE_ENDIAN__",
                "-D__WCHAR_WIDTH__=32",
                "-D_IONBF=2",
                "-D_IOLBF=1",
                "-D_IOFBF=0",
                # GCC/Clang extensions that pycparser doesn't understand.
                "-D__attribute(x)=",
                "-D__attribute__(x)=",
                "-D__extension__=",
                "-D__FUNCTION__=__func__",
                "-D__inline=inline",
                "-D__inline__=inline",
                "-D__restrict=restrict",
                "-D__restrict__=restrict",
                "-D_Atomic(x)=x",
                "-Dasm(...)=",
                "-D__asm__(x)=",
                "-D__asm(x)=",
                "-D_Nonnull=",
                "-D_Nullable=",
                "-D_Null_unspecified=",
                "-D__nonnull=",
                "-D__nullable=",
                "-D__null_unspecified=",
                "-D__int128_t=long long",
                "-D__uint128_t=unsigned long long",
                "-D__builtin_memcpy=memcpy",
                "-D__builtin_memmove=memmove",
                "-D__builtin_memcmp=memcmp",
                "-D__builtin_memchr=memchr",
                "-D__builtin_memset=memset",
                "-D__builtin_malloc=malloc",
                "-D__builtin_free=free",
                "-D__builtin_abs=abs",
                "-D__builtin_bzero(p,n)=memset(p,0,n)",
                "-D__builtin___memcpy_chk(a,b,c,d)=memcpy(a,b,c)",
                "-D__builtin___memmove_chk(a,b,c,d)=memmove(a,b,c)",
                "-D__builtin___memset_chk(a,b,c,d)=memset(a,b,c)",
                "-D__builtin___strcpy_chk(a,b,c)=strcpy(a,b)",
                "-D__builtin___strcat_chk(a,b,c)=strcat(a,b)",
                "-D__builtin___strncpy_chk(a,b,c,d)=strncpy(a,b,c)",
                "-D__builtin___strncat_chk(a,b,c,d)=strncat(a,b,c)",
                "-D__builtin___strlcpy_chk(a,b,c,d)=strlcpy(a,b,c)",
                "-D__builtin___strlcat_chk(a,b,c,d)=strlcat(a,b,c)",
                "-D__builtin___printf_chk(flag,fmt,...)=printf(fmt,##__VA_ARGS__)",
                "-D__builtin___fprintf_chk(stream,flag,fmt,...)=fprintf(stream,fmt,##__VA_ARGS__)",
                "-D__builtin___sprintf_chk(buf,flag,obj,fmt,...)=sprintf(buf,fmt,##__VA_ARGS__)",
                "-D__builtin___snprintf_chk(buf,size,flag,obj,fmt,...)=snprintf(buf,size,fmt,##__VA_ARGS__)",
                "-D__builtin___vsprintf_chk(buf,flag,obj,fmt,ap)=vsprintf(buf,fmt,ap)",
                "-D__builtin___vsnprintf_chk(buf,size,flag,obj,fmt,ap)=vsnprintf(buf,size,fmt,ap)",
                "-D__builtin_printf=printf",
                "-D__builtin_fprintf=fprintf",
                "-D__builtin_abort()=abort()",
                "-D__builtin_return_address(level)=((void*)0)",
                "-D__builtin_choose_expr(cond,a,b)=((cond)?(a):(b))",
                "-D__builtin_strlen(x)=strlen(x)",
                "-D__builtin_strcmp(a,b)=strcmp(a,b)",
                "-D__sync_add_and_fetch(ptr,val)=(*(ptr)+=(val))",
                "-D__builtin_object_size(ptr,type)=((size_t)-1)",
                "-D__builtin_fabsf=fabsf",
                "-D__builtin_fabs=fabs",
                "-D__builtin_fabsl=fabsl",
                "-D__builtin_inff()=(1e39f)",
                "-D__builtin_inf()=(1e309)",
                "-D__builtin_infl()=((long double)1e309)",
                "-D__builtin_huge_val()=(1e309)",
                "-D__builtin_huge_valf()=(1e39f)",
                "-D__builtin_huge_vall()=((long double)1e309)",
                "-D_Alignas(x)=_Alignas(x)",
                "-Dalignas(x)=_Alignas(x)",
                # Strip _Static_assert in system-cpp mode because host headers
                # may embed __builtin_types_compatible_p or other unparseable
                # builtins inside static assertions. User code _Static_assert
                # still works via the built-in preprocessor path.
                "-D_Static_assert(x,...)=",
                "-Dstatic_assert(x,...)=",
            ]
            # Host headers on macOS emit __builtin_va_arg(ap, type), while
            # pcc already supports the fake-libc-expanded shape that casts a
            # pointer returned from __builtin_va_arg(&(ap), sizeof(type)).
            # Apply this only on the host-header preprocessing path below.
            system_header_compat_defs.append(
                "-D__builtin_va_arg(ap,t)=(*((t*)__builtin_va_arg(&(ap),sizeof(t))))"
            )
            if any("openssl-" in include_dir for include_dir in user_include_dirs):
                # OpenSSL's bn_local.h enables inline-asm/GNU statement-expression
                # helpers when the host compiler supports them. pcc does not,
                # so force the standard C fallback path during preprocessing.
                system_header_compat_defs.append("-DOPENSSL_NO_INLINE_ASM")
                # Prefer OpenSSL's non-C11 atomics fallbacks. This avoids
                # host stdatomic expansions that pcc can't parse yet and keeps
                # TSAN_QUALIFIER on its volatile/plain-C paths.
                system_header_compat_defs.extend(
                    [
                        "-DOPENSSL_DEV_NO_ATOMICS",
                        "-D__STDC_NO_ATOMICS__=1",
                        "-DATOMIC_POINTER_LOCK_FREE=0",
                        "-D__GCC_ATOMIC_POINTER_LOCK_FREE=0",
                    ]
                )
            if not prefer_system_headers:
                cmd = [
                    cc,
                    "-E",
                    "-P",
                    "-nostdinc",  # skip real system headers
                    "-isystem",
                    fake_libc,  # fake libc as system headers
                    *[
                        opt
                        for include_dir in user_include_dirs
                        for opt in ("-I", include_dir)
                    ],
                    *compat_defs,
                    *platform_defs,
                    *cpp_args,
                    tmp_path,
                ]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                if result.returncode == 0:
                    return _postprocess_preprocessed_text(result.stdout)
                first_cpp_error = (result.stderr or result.stdout or "").strip()
            # Fallback or preferred path: use the host headers.
            cmd = [
                cc,
                "-E",
                "-P",
                *[
                    opt
                    for include_dir in user_include_dirs
                    for opt in ("-I", include_dir)
                ],
                *compat_defs,
                *system_header_compat_defs,
                *platform_defs,
                *cpp_args,
                tmp_path,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                detail = (
                    (result.stderr or result.stdout or "").strip()
                    or first_cpp_error
                    or "unknown preprocessor error"
                )
                raise RuntimeError(f"system cpp failed: {detail}")
            return _postprocess_preprocessed_text(result.stdout)
        finally:
            os.unlink(tmp_path)
