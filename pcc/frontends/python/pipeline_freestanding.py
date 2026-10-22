"""Strict freestanding source and emitted-IR contract checks.

This module is deliberately independent of the compilation driver.  It owns
only literal source scanning, finite machine-boundary admission, and the
fail-closed emitted-IR verifier used by the thin pipeline facade.
"""
from __future__ import annotations

from typing import Optional

from pcc.frontends.python import pipeline_import_scan as _pipeline_import_scan
from pcc.frontends.python.pipeline_modes import PyPipelineError


_source_module_scope_lines = _pipeline_import_scan._source_module_scope_lines


def source_declares_freestanding_module(source: str) -> bool:
    """Return whether source opts into the strict freestanding contract."""
    return _source_declares_module_directive(source, "__pcc_freestanding__")


def source_declares_runtime_port_module(source: str) -> bool:
    """Return whether source is a runtime port (pointer-lane raw pointers).

    Runtime ports build Python objects out of raw memory, so their pointer
    intrinsics and ``c_ptr`` extern results stay in the pointer lane exactly
    as in freestanding mode.  Application modules (everything without this
    directive or the freestanding directive) type raw addresses as ``int``.
    """
    return _source_declares_module_directive(source, "__pcc_runtime_port__")


def _triple_at(chars: list[str], index: int, quote: str) -> bool:
    """``source[index:index + 3] == quote * 3`` over the character list."""
    return (
        index + 2 < len(chars)
        and chars[index] == quote
        and chars[index + 1] == quote
        and chars[index + 2] == quote
    )


def _source_without_literals_or_comments(source: str) -> str:
    """Mask strings/comments without changing indentation or line numbers.

    Directive names are also ordinary string data in the compiler itself.
    Looking for a substring before lexical masking rejects those modules.
    Keep this scan independent of the parser: it selects the parser's mode
    and must also execute in the native bootstrap compiler.

    Index a character list, not the str: pcc stores str as UTF-8, so
    ``source[i]`` on a non-ASCII source rescans from byte 0. This loop was
    quadratic under pcc1 and took 97% of a Stage2 export worker's samples.
    """
    chars = list(source)
    length = len(chars)
    out: list[str] = []
    quote = ""
    triple = False
    escaped = False
    comment = False
    index = 0
    while index < length:
        char = chars[index]
        if comment:
            if char == "\n":
                comment = False
                out.append(char)
            else:
                out.append(" ")
            index += 1
            continue
        if quote:
            if escaped:
                escaped = False
                out.append("\n" if char == "\n" else " ")
                index += 1
                continue
            if char == "\\":
                escaped = True
                out.append(" ")
                index += 1
                continue
            if char == quote:
                if not triple:
                    quote = ""
                elif _triple_at(chars, index, quote):
                    out.append("   ")
                    index += 3
                    quote = ""
                    triple = False
                    continue
            out.append("\n" if char == "\n" else " ")
            index += 1
            continue
        if char == "#":
            comment = True
            out.append(" ")
        elif char == "'" or char == '"':
            quote = char
            triple = _triple_at(chars, index, char)
            if triple:
                out.append("   ")
                index += 3
                continue
            out.append(" ")
        else:
            out.append(char)
        index += 1
    return "".join(out)


def _contains_directive_name(line: str, marker: str) -> bool:
    """Match a complete name, not a substring of another identifier."""
    offset = 0
    while True:
        start = line.find(marker, offset)
        if start < 0:
            return False
        end = start + len(marker)
        before = line[start - 1] if start else ""
        after = line[end] if end < len(line) else ""
        if not (before.isalnum() or before == "_") and not (
            after.isalnum() or after == "_"
        ):
            return True
        offset = end


def _source_declares_module_directive(source: str, marker: str) -> bool:
    # Masking only blanks literal and comment characters, so a line can name
    # the marker only if the source text contains it. Almost no module does;
    # skip the mask for them.
    if marker not in source:
        return False
    declaration = marker + " = True"
    found = False
    for raw_line, at_module_scope in _source_module_scope_lines(
        _source_without_literals_or_comments(source)
    ):
        if not _contains_directive_name(raw_line, marker):
            continue
        stripped = raw_line.split("#", 1)[0].strip()
        if not at_module_scope:
            continue
        compact = "".join(stripped.split())
        if (
            len(raw_line) != len(raw_line.lstrip())
            or compact != marker + "=True"
        ):
            raise PyPipelineError(
                marker + " directive must be the unconditional module-scope "
                "assignment `" + declaration + "`"
            )
        if found:
            raise PyPipelineError(marker + " directive may appear only once")
        found = True
    return found


def freestanding_allowed_external_symbols(source: str) -> set[str]:
    """Derive the narrow extern closure from imported unsafe intrinsics."""
    boundary_by_intrinsic = {
        "malloc": ("malloc",),
        "calloc": ("calloc",),
        "realloc": ("realloc",),
        "free": ("free",),
        "page_alloc": ("mmap",),
        "page_free": ("munmap",),
        "strlen": ("strlen",),
        "memset": ("memset",),
        "memcpy": ("memcpy",),
        "memmove": ("memmove",),
        "read": ("read",),
        "write": ("write",),
        "close": ("close",),
        "seek_file": ("lseek",),
        "pwrite_file": ("pwrite", "__error"),
        "truncate_file": ("ftruncate", "__error"),
        "open_readonly": ("open", "__error"),
        "darwin_current_rss_bytes": ("task_info", "mach_task_self_"),
        "darwin_peak_rss_bytes": ("getrusage",),
        "open_file": ("open", "__error"),
        "open_file_flags": ("openat", "__error"),
        "mkdir_at": ("mkdirat", "__error"),
        "rename_file": ("rename", "__error"),
        "chmod_file": ("chmod", "__error"),
        "utime_file": ("utime",),
        "sync_file": ("fsync", "__error"),
        "socket_open": ("socket", "__error"),
        "socket_connect": ("connect", "__error"),
        "socket_bind": ("bind", "__error"),
        "socket_listen": ("listen", "__error"),
        "socket_setsockopt": ("setsockopt", "__error"),
        "socket_getsockopt": ("getsockopt", "__error"),
        "fd_control": ("fcntl", "__error"),
        "file_flock": ("flock", "__error"),
        "socket_send": ("send", "__error"),
        "socket_recv": ("recv", "__error"),
        "socket_accept": ("accept", "__error"),
        "socket_shutdown": ("shutdown", "__error"),
        "socket_sockname": ("getsockname", "__error"),
        "socket_peername": ("getpeername", "__error"),
        "poll_fd": ("poll", "__error"),
        "poll_readable_pair": ("poll", "__error"),
        "getpid": ("getpid",),
        "getcwd": ("getcwd",),
        "readlink": ("readlink",),
        "mkdir": ("mkdir",),
        "unlinkat": ("unlinkat",),
        "access": ("access",),
        "stat_kind": ("stat",),
        "lstat_kind": ("lstat",),
        "stat_mtime": ("stat",),
        "stat_size": ("stat", "__error"),
        "is_symlink": ("lstat", "__error"),
        "uname": ("uname",),
        "cpu_query": ("sysctlbyname",),
        "clock_gettime": ("clock_gettime",),
        "nanosleep": ("nanosleep",),
        "getenv": ("getenv",),
        "setenv": ("setenv",),
        "unsetenv": ("unsetenv",),
        "waitpid": ("waitpid", "__error"),
        "kill": ("kill", "__error"),
        "process_exit": ("_exit",),
        "spawn_process": (
            "posix_spawn",
            "posix_spawn_file_actions_addopen",
            "posix_spawn_file_actions_destroy",
            "posix_spawn_file_actions_init",
            "posix_spawnattr_destroy",
            "posix_spawnattr_init",
            "posix_spawnattr_setflags",
            "posix_spawnattr_setpgroup",
        ),
        "spawn_process_pipe": (
            "pipe",
            "close",
            "posix_spawn",
            "posix_spawn_file_actions_addclose",
            "posix_spawn_file_actions_adddup2",
            "posix_spawn_file_actions_destroy",
            "posix_spawn_file_actions_init",
        ),
        "spawn_process_redirect": (
            "posix_spawn",
            "posix_spawn_file_actions_addchdir_np",
            "posix_spawn_file_actions_adddup2",
            "posix_spawn_file_actions_destroy",
            "posix_spawn_file_actions_init",
        ),
        "dynamic_library_open": ("dlopen",),
        "dynamic_library_open_global": ("dlopen",),
        "dynamic_library_symbol": ("dlsym",),
        "darwin_libsystem_symbol": ("dlopen", "dlsym", "dlclose"),
        "darwin_errno_location": ("__error",),
        "dynamic_library_close": ("dlclose",),
        "kqueue_create": ("kqueue", "__error"),
        "kevent_call": ("kevent", "__error"),
        "thread_safepoint": ("pcc_thread_safepoint",),
        "gc_backend_current": ("pcc_gc_backend",),
        "call_c_abi": ("__pcc_verified_indirect_call__",),
        "call_ptr1": ("__pcc_verified_indirect_call__",),
        "call_ptr0": ("__pcc_verified_indirect_call__",),
        "call_void_i32": ("__pcc_verified_indirect_call__",),
        "call_void_ptr0": ("__pcc_verified_indirect_call__",),
        "call_void_ptr1": ("__pcc_verified_indirect_call__",),
        "call_void_ptr_i64_ptr": ("__pcc_verified_indirect_call__",),
        "call_ptr2": ("__pcc_verified_indirect_call__",),
        "call_ptr4": ("__pcc_verified_indirect_call__",),
        "call_ptr3": ("__pcc_verified_indirect_call__",),
        "call_i64_i64_ptr": ("__pcc_verified_indirect_call__",),
        "call_i32_ptr1": ("__pcc_verified_indirect_call__",),
        "call_i32_ptr_i32": ("__pcc_verified_indirect_call__",),
        "call_i32_ptr_i32_i32": ("__pcc_verified_indirect_call__",),
        "call_i32_ptr_i32_i32_i32": ("__pcc_verified_indirect_call__",),
        "call_i32_ptr_i32_i32_i32_i32_i32_ptr_i32": (
            "__pcc_verified_indirect_call__",
        ),
        "call_i32_i32_ptr_i64": ("__pcc_verified_indirect_call__",),
        "call_i32_i64_i64_ptr": ("__pcc_verified_indirect_call__",),
        "call_i32_i64_i32_i64": ("__pcc_verified_indirect_call__",),
        "call_i64_ptr1": ("__pcc_verified_indirect_call__",),
        "call_i64_ptr2": ("__pcc_verified_indirect_call__",),
        "call_i64_ptr_i64_ptr": ("__pcc_verified_indirect_call__",),
        "call_i64_ptr_i64_i64": ("__pcc_verified_indirect_call__",),
        "call_i64_ptr_i64_ptr_i64": ("__pcc_verified_indirect_call__",),
        "call_i64_ptr_i64_i64_ptr": ("__pcc_verified_indirect_call__",),
        "call_i64_ptr_i64_ptr_ptr_ptr_ptr_bool": (
            "__pcc_verified_indirect_call__",
        ),
        "call_i64_ptr_ptr_ptr_ptr_ptr_bool": (
            "__pcc_verified_indirect_call__",
        ),
    }
    from pcc.frontends.python.codegen.windows_platform import _SIGNATURES as windows_intrinsics
    for intrinsic in windows_intrinsics:
        symbol = ("pcc_platform_" if intrinsic in ("getenv", "setenv", "unsetenv") else "pcc_win_") + intrinsic
        if intrinsic == "dynamic_library_open_global":
            symbol = "pcc_win_dynamic_library_open"
        boundary_by_intrinsic[intrinsic] = boundary_by_intrinsic.get(intrinsic, ()) + (symbol,)
    boundary_by_intrinsic.update({
        "linux_set_thread_pointer": ("pcc_linux_set_thread_pointer",),
        "epoll_wait": ("pcc_linux_aarch64_epoll_wait",),
        "directory_open": ("opendir", "pcc_linux_directory_open", "pcc_win_directory_open"),
        "directory_next": ("readdir", "__error", "pcc_linux_directory_next", "pcc_win_directory_next"),
        "directory_error": ("__error", "pcc_linux_directory_error", "pcc_win_directory_error"),
        "directory_close": ("closedir", "pcc_linux_directory_close", "pcc_win_directory_close"),
        "windows_full_path": ("pcc_win_full_path",),
        "windows_real_path": ("pcc_win_realpath",),
        "windows_tempdir_create": ("pcc_win_tempdir_create",),
        "windows_tempdir_remove_owned": ("pcc_win_tempdir_remove_owned",),
        "windows_tempdir_close_owned": ("pcc_win_tempdir_close_owned",),
    })
    imported = []
    collecting = False
    for raw_line in source.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not collecting:
            prefix = "from pcc.unsafe import "
            if not line.startswith(prefix):
                continue
            payload = line[len(prefix) :].strip()
            if payload.startswith("("):
                payload = payload[1:]
                collecting = True
        else:
            payload = line
        if collecting and ")" in payload:
            payload = payload.split(")", 1)[0]
            collecting = False
        for name in payload.split(","):
            clean = name.strip()
            if clean:
                imported.append(clean)
    allowed = set()
    for intrinsic in imported:
        for symbol in boundary_by_intrinsic.get(intrinsic, ()):
            allowed.add(symbol)
    for symbol in freestanding_readonly_gc_runtime_imports(source):
        allowed.add(symbol)
    for symbol in freestanding_gc_cross_object_runtime_imports(source):
        allowed.add(symbol)
    for symbol in freestanding_gc_runtime_global_imports(source):
        allowed.add(symbol)
    # Process entry is a deliberately tiny cross-object ABI: the kernel entry
    # may call the program's C ``main`` and initialize the pcc-owned environment
    # table.  Admit only these exact literal symbol/signature declarations;
    # arbitrary ``extern`` bindings remain rejected below.
    process_entry_abis = {
        "main": ("(c_int,c_ptr,c_ptr)", "c_int"),
        "pcc_platform_env_init": ("(c_ptr,)", "c_int"),
    }
    # Finite cross-object machine services shared by freestanding libc
    # modules.  Keep the exact source-level signature check: this must not
    # become a general escape hatch for arbitrary runtime externs.
    freestanding_machine_abis = {
        "pcc_errno_set": ("(c_int32,)", "c_void"),
        # Process-control signals are an explicitly named platform ABI.  They
        # are available to Darwin's libSystem-owned gateway control and to
        # libc-labeled Linux deployments; they do not satisfy Linux zero-libc.
        "sigaction": ("(c_int,c_ptr,c_ptr)", "c_int"),
        "sigemptyset": ("(c_ptr,)", "c_int"),
    }
    from pcc.frontends.python.codegen.platform_machine_abis import PLATFORM_MACHINE_ABIS
    freestanding_machine_abis.update(PLATFORM_MACHINE_ABIS)
    for symbol, compact_params, return_name in (
        freestanding_module_scope_extern_bindings(source)
    ):
        expected = process_entry_abis.get(symbol)
        if expected == (compact_params, return_name):
            allowed.add(symbol)
        expected = freestanding_machine_abis.get(symbol)
        if expected == (compact_params, return_name):
            allowed.add(symbol)
    return allowed


def source_call_arguments(source: str, call_start: int) -> tuple[list[str], int]:
    """Split one literal call without importing CPython's ``ast`` module.

    ``pipeline.py`` is part of the no-libpython pcc1 closure, so this scanner
    intentionally covers only the module-scope declaration shape needed by
    verified ``extern(...)`` bindings. It tracks strings and nested tuples and
    fails closed by returning no arguments for malformed input.
    """
    depth = 1
    quote = ""
    escaped = False
    i = call_start
    payload_start = call_start
    while i < len(source):
        ch = source[i]
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = ""
        elif ch == "'" or ch == '"':
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                payload = source[payload_start:i]
                args = []
                arg_start = 0
                nested = 0
                arg_quote = ""
                arg_escaped = False
                j = 0
                while j < len(payload):
                    item = payload[j]
                    if arg_quote:
                        if arg_escaped:
                            arg_escaped = False
                        elif item == "\\":
                            arg_escaped = True
                        elif item == arg_quote:
                            arg_quote = ""
                    elif item == "'" or item == '"':
                        arg_quote = item
                    elif item == "(" or item == "[" or item == "{":
                        nested += 1
                    elif item == ")" or item == "]" or item == "}":
                        nested -= 1
                    elif item == "," and nested == 0:
                        args.append(payload[arg_start:j].strip())
                        arg_start = j + 1
                    j += 1
                args.append(payload[arg_start:].strip())
                return args, i + 1
        i += 1
    return [], len(source)


def freestanding_module_scope_extern_bindings(
    source: str,
) -> list[tuple[str, str, str]]:
    """Return literal ``(symbol, compact-params, return-name)`` bindings."""
    module_source = "\n".join(
        raw_line if at_module_scope else ""
        for raw_line, at_module_scope in _source_module_scope_lines(source)
    )
    bindings = []
    cursor = 0
    marker = "extern("
    while cursor < len(module_source):
        call_at = module_source.find(marker, cursor)
        if call_at < 0:
            break
        line_start = module_source.rfind("\n", 0, call_at) + 1
        binding = module_source[line_start:call_at].strip()
        cursor = call_at + len(marker)
        if not binding.endswith("="):
            continue
        binding_name = binding[:-1].strip()
        if not binding_name.isidentifier():
            continue
        args, cursor = source_call_arguments(module_source, cursor)
        if len(args) != 3:
            continue
        symbol_literal = args[0].strip()
        if len(symbol_literal) < 2:
            continue
        quote = symbol_literal[0]
        if quote not in ("'", '"') or symbol_literal[-1] != quote:
            continue
        symbol = symbol_literal[1:-1]
        if "\\" in symbol:
            continue
        bindings.append(
            (symbol, "".join(args[1].split()), args[2].strip())
        )
    return bindings


def freestanding_readonly_gc_runtime_imports(source: str) -> set[str]:
    """Return exact verified ``pcc_gc_* () -> i64`` extern bindings.

    Cross-object GC telemetry queries are part of the raw collector closure,
    but an arbitrary ``pcc.extern`` must not become a freestanding escape.
    Admission requires explicit membership in the finite read-only query set,
    a canonical module-scope assignment, the exact no-argument ``c_int64``
    source shape, and an identical entry in the runtime ABI source of truth.
    """
    # Keep the LLVM-type registry out of pipeline module initialization.  The
    # compiled bootstrap imports pipeline before pcc.ir.ir has necessarily
    # finished initializing inherited singleton class attributes.
    from pcc.frontends.python.codegen.runtime_abi import is_freestanding_gc_readonly_runtime_import

    allowed = set()
    for symbol, parameters_source, return_source in (
        freestanding_module_scope_extern_bindings(source)
    ):
        if not is_freestanding_gc_readonly_runtime_import(symbol):
            continue
        if parameters_source != "()" or return_source != "c_int64":
            continue
        allowed.add(symbol)
    return allowed


def freestanding_gc_cross_object_runtime_imports(source: str) -> set[str]:
    """Return finite, signature-exact raw calls between GC objects."""
    from pcc.frontends.python.codegen.runtime_abi import is_freestanding_gc_cross_object_runtime_import

    allowed = set()
    for symbol, parameters_source, return_source in (
        freestanding_module_scope_extern_bindings(source)
    ):
        if not is_freestanding_gc_cross_object_runtime_import(
            symbol, parameters_source, return_source
        ):
            continue
        allowed.add(symbol)
    return allowed


def freestanding_gc_runtime_global_imports(source: str) -> set[str]:
    """Return literal references to typed GC globals in the runtime ABI."""
    from pcc.frontends.python.codegen.runtime_abi import is_freestanding_gc_runtime_global

    allowed = set()
    for intrinsic in ("global_addr", "global_load_ptr", "global_store_ptr"):
        marker = intrinsic + "("
        cursor = 0
        while cursor < len(source):
            call_at = source.find(marker, cursor)
            if call_at < 0:
                break
            cursor = call_at + len(marker)
            args, cursor = source_call_arguments(source, cursor)
            if not args:
                continue
            symbol_literal = args[0].strip()
            if len(symbol_literal) < 2:
                continue
            quote = symbol_literal[0]
            if quote not in ("'", '"') or symbol_literal[-1] != quote:
                continue
            symbol = symbol_literal[1:-1]
            if "\\" in symbol:
                continue
            if is_freestanding_gc_runtime_global(symbol):
                allowed.add(symbol)
    return allowed


def validate_freestanding_ir(
    ir_text: str, allowed_external_symbols: Optional[set[str]] = None
) -> None:
    """Fail closed when a defined body escapes the freestanding subset."""
    if allowed_external_symbols is None:
        allowed_external_symbols = set()
    defined_symbols = []
    for raw_line in ir_text.splitlines():
        stripped = raw_line.strip()
        if not stripped.startswith("define ") or "@" not in stripped:
            continue
        after_at = stripped.split("@", 1)[1]
        if "(" not in after_at:
            continue
        defined_symbols.append(after_at.split("(", 1)[0])

    in_definition = False
    current_definition = ""
    for raw_line in ir_text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("define "):
            in_definition = True
            current_definition = stripped
            continue
        if not in_definition:
            continue
        if stripped == "}":
            in_definition = False
            current_definition = ""
            continue
        if (
            "landingpad" in stripped
            or stripped.startswith("resume ")
            or "catchswitch" in stripped
            or "catchpad" in stripped
            or "cleanuppad" in stripped
        ):
            raise PyPipelineError(
                "freestanding module emitted exception machinery in "
                + current_definition
            )
        # A freestanding module may itself *implement* a managed-runtime ABI
        # symbol (for example ``pcc_gc_*``), and a verified slot walker may
        # take the address of a callback defined in that same object.  Remove
        # only exact references to definitions in this verified module before
        # checking for an escape.  Other arguments/globals on the line remain
        # visible to the fail-closed prefix check.
        managed_reference_text = stripped
        for symbol in defined_symbols:
            marker = "@" + symbol
            for delimiter in ("(", ",", " ", ")"):
                managed_reference_text = managed_reference_text.replace(
                    marker + delimiter,
                    "@freestanding.local" + delimiter,
                )
            if managed_reference_text.endswith(marker):
                managed_reference_text = (
                    managed_reference_text[: -len(marker)]
                    + "@freestanding.local"
                )
        # Explicit unsafe boundaries may themselves carry a managed-looking
        # ABI prefix (currently the initialized GC backend query).  Remove
        # only exact references derived from imported, fixed-target
        # intrinsics; arbitrary pcc.extern calls remain rejected.
        for symbol in allowed_external_symbols:
            marker = "@" + symbol
            for delimiter in ("(", ",", " ", ")"):
                managed_reference_text = managed_reference_text.replace(
                    marker + delimiter,
                    "@freestanding.boundary" + delimiter,
                )
            if managed_reference_text.endswith(marker):
                managed_reference_text = (
                    managed_reference_text[: -len(marker)]
                    + "@freestanding.boundary"
                )
        if (
            "@py_" in managed_reference_text
            or "@pcc_gc_" in managed_reference_text
            or "@py_None" in managed_reference_text
        ):
            raise PyPipelineError(
                "freestanding module emitted managed-runtime reference in "
                + current_definition
                + ": "
                + stripped
            )
        has_call = " call " in (" " + stripped + " ")
        has_invoke = " invoke " in (" " + stripped + " ")
        if has_call or has_invoke:
            if "@llvm." in stripped:
                continue
            if has_call and " asm " in (" " + stripped + " "):
                continue
            # Indirect calls are admitted only when the source imported a
            # fixed-signature call-pointer intrinsic.  A normal Python call or
            # arbitrary pcc.extern binding cannot set this marker.
            if (
                "__pcc_verified_indirect_call__" in allowed_external_symbols
                and "@" not in stripped
            ):
                continue
            if any(
                "@" + symbol + "(" in stripped
                for symbol in allowed_external_symbols
            ):
                continue
            calls_freestanding_definition = False
            for symbol in defined_symbols:
                if "@" + symbol + "(" in stripped:
                    calls_freestanding_definition = True
                    break
            if calls_freestanding_definition:
                continue
            raise PyPipelineError(
                "freestanding module emitted call outside its verified closure in "
                + current_definition
                + ": "
                + stripped
            )


__all__ = [
    "source_declares_freestanding_module",
    "freestanding_allowed_external_symbols",
    "source_call_arguments",
    "freestanding_module_scope_extern_bindings",
    "freestanding_readonly_gc_runtime_imports",
    "freestanding_gc_cross_object_runtime_imports",
    "freestanding_gc_runtime_global_imports",
    "validate_freestanding_ir",
]
