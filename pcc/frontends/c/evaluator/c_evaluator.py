import re
import json
import hashlib
import inspect
import time
import os
import multiprocessing
import subprocess
import shutil
import sys
import tempfile
import platform
from itertools import repeat




from pcc.frontends.python.pipeline_targets import host_target_triple
from pcc.backend import BackendUnavailable, backend_request_allows_unimplemented, backend_signature, resolve_backend
from pcc.backend.self_backend_dispatch import emit_self_asm, self_backend_target_identity
from pcc.backend.self_backend_parse import parse_self_backend_target_triple
from pcc.frontends.c.codegen.c_codegen import CCodeGenerator, postprocess_ir_text
from pcc.frontends.c.parse.c_parser import CParser
from pcc.frontends.c.preprocessor import preprocess
from pcc.driver.project import TranslationUnit
from pcc.driver.paths import resolve_pcc_dir_from_environment

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
_SELF_TYPEDEF = re.compile(
    r"^typedef\s+(\w+)\s+\1\s*;$", re.MULTILINE
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

_COMPILE_CACHE_VERSION = "v5-owned"
_PASS_DISABLE_ENV = "PCC_DISABLE_PASSES"
_SELF_OBJECT_EMITTER_ENV = "PCC_SELF_OBJ"
_SELF_OBJECT_EMITTER_PCC = "pcc"
_SELF_OBJECT_EMITTER_SYSTEM_AS = "system-as"
_SELF_OBJECT_EMITTER_DIRECT_TARGETS = frozenset(
    {
        "self-aarch64-darwin-v0",
        "self-aarch64-linux-v0",
        "self-x86_64-windows-v0",
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
def _optional_find_library(name):
    """``ctypes.util.find_library`` when the host has it, else ``None``.

    A compiled pcc1 has no ``ctypes.util``; the owned directory search above
    covers the same ground, so its absence must not be an import error.
    """
    try:
        from ctypes.util import find_library
    except ImportError:
        return None
    return find_library(name)


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










def _resolve_disabled_pass_names(raw_value=None):
    values = []
    if raw_value is not None:
        values.append(raw_value)
    else:
        values.extend(
            (
                os.environ.get(_PASS_DISABLE_ENV, ""),
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




def _apply_pass_selection_from_env(pass_ctx):
    from pcc.frontends.c.passes import expand_registered_pass_names

    for pass_name in expand_registered_pass_names(_resolve_disabled_pass_names()):
        pass_ctx.disable_pass(pass_name)


def _pass_selection_signature(raw_value=None):
    from pcc.frontends.c.passes import expand_registered_pass_names

    expanded = expand_registered_pass_names(_resolve_disabled_pass_names(raw_value))
    if not expanded:
        return ""
    return "\0".join(sorted(expanded))














def _compiler_cache_tracked_files():
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pcc_dir = resolve_pcc_dir_from_environment(__file__)
    tracked_files = [os.path.abspath(__file__)]
    roots = [base_dir] + [os.path.join(pcc_dir, dirname) for dirname in ("ir", "backend", "driver", "support")]
    for package_dir in roots:
        try:
            for root, dirs, files in os.walk(package_dir):
                dirs.sort()
                for filename in sorted(files):
                    if filename.endswith(".py"):
                        tracked_files.append(os.path.join(root, filename))
        except OSError:
            tracked_files.append(os.path.join(package_dir, "missing"))
    return tuple(dict.fromkeys(tracked_files))


def _host_compiler_cache_fingerprint():
    hasher = hashlib.sha256()
    hasher.update(b"pcc.c-compiler-content.v2\0")
    for tracked_path in _compiler_cache_tracked_files():
        hasher.update(tracked_path.encode("utf-8"))
        try:
            with open(tracked_path, "rb") as stream:
                hasher.update(stream.read())
        except OSError:
            hasher.update(b"missing")
        hasher.update(b"\0")
    hasher.update(sys.version.encode("utf-8"))
    hasher.update(sys.platform.encode("utf-8"))
    hasher.update(platform.machine().encode("utf-8"))
    return hasher.hexdigest()


def _compiler_cache_fingerprint():
    if sys.implementation.name == "pcc":
        from pcc.tools.compiler_identity import native_executable_identity

        return native_executable_identity(sys.executable)
    return _host_compiler_cache_fingerprint()


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
        # Malformed source bytes arrive as surrogate escapes; hash those bytes
        # so the encoding diagnostic comes from the frontend, not the cache.
        hasher.update(piece.encode("utf-8", "surrogateescape"))
        hasher.update(b"\0")
    return hasher.hexdigest()


def _native_cache_key(entry, opt_signature, pass_signature, backend_sig, source_text):
    return hashlib.sha256(
        (
            f"{_COMPILE_CACHE_VERSION}\0{_COMPILER_CACHE_FINGERPRINT}\0"
            f"{entry}\0{opt_signature}\0{pass_signature}\0"
            f"{backend_sig or backend_signature(None)}\0{source_text}"
        ).encode("utf-8", "surrogateescape")
    ).hexdigest()


def _compile_cache_path(cache_dir: str, cache_key: str) -> str:
    return os.path.join(cache_dir, cache_key[:2], cache_key[2:] + ".json")


def _native_cache_path(cache_dir: str, cache_key: str) -> str:
    ext = ".dylib" if sys.platform == "darwin" else ".so"
    return os.path.join(cache_dir, cache_key[:2], cache_key[2:] + ext)






def _load_compiled_artifact(cache_dir, cache_key):
    path = _compile_cache_path(cache_dir, cache_key)
    try:
        with open(path, encoding="utf-8") as f:
            artifact = json.load(f)
    except (OSError, ValueError):
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


def _store_compiled_artifact(cache_dir: str, cache_key: str, artifact):
    path = _compile_cache_path(cache_dir, cache_key)
    parent = os.path.dirname(path)
    temporary_dir = ""
    temporary_file = ""
    try:
        os.makedirs(parent, exist_ok=True)
        prefix = path + ".tmp." + str(os.getpid()) + "."
        attempt = 0
        while attempt < 128:
            candidate = prefix + str(attempt)
            try:
                os.makedirs(candidate, mode=0o700, exist_ok=False)
                temporary_dir = candidate
                break
            except OSError:
                if not os.path.isdir(candidate):
                    raise
                attempt += 1
        if not temporary_dir:
            return
        temporary_file = os.path.join(temporary_dir, "artifact.json")
        with open(temporary_file, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(artifact, sort_keys=True))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_file, 0o600)
        os.replace(temporary_file, path)
        temporary_file = ""
    except OSError:
        pass
    finally:
        if temporary_file:
            try:
                os.unlink(temporary_file)
            except OSError:
                pass
        if temporary_dir:
            try:
                os.rmdir(temporary_dir)
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


def _normalize_preprocessed_source(codestr, target_triple=None):
    from pcc.frontends.c.c_builtin_compat import normalize_builtin_type_compat

    codestr = normalize_builtin_type_compat(codestr, target_triple)
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


# GCC/Clang extensions pycparser does not understand, mapped to plain C.
# Both preprocessors apply them: the host ``cc -E`` path for the LLVM
# backend and the owned preprocessor for the self backend.  The owned path
# ran without them after the self backend stopped using ``cc -E``, so
# ``__attribute__``/``__builtin_*`` sources stopped parsing there.
_C_EXTENSION_COMPAT_DEFINES = (
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
    "-D__int128_t=__int128",
    "-D__uint128_t=unsigned __int128",
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
)


def _preprocess_translation_unit_source(
    source, base_dir, use_system_cpp, include_dirs=None, cpp_args=None, target_triple=None
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
        codestr = _SELF_TYPEDEF.sub("", codestr)
    else:
        codestr = preprocess(
            codestr,
            base_dir=base_dir,
            include_dirs=include_dirs,
            cpp_args=list(_C_EXTENSION_COMPAT_DEFINES) + list(cpp_args or []),
            target_triple=target_triple,
        )
    return _normalize_preprocessed_source(codestr, target_triple)


def _compile_preprocessed_translation_unit_artifact(
    unit_name,
    codestr,
    emit_debug=False,
    pass_pipeline=None,
    pass_ctx=None,
    frontend_opt_level=None,
    target_triple=None,
    fsanitize=None,
    evaluation_entry=None,
):
    from pcc.frontends.c.passes import PassContext, PassPipeline
    from pcc.frontends.c.c_abi_layout import c_target_triple

    from pcc.frontends.c.parse import make_c_parser
    ast = make_c_parser().parse(codestr)

    # --- Pass Framework Integration ---
    if pass_pipeline is None:
        pass_pipeline = PassPipeline.default()
    selected_target = c_target_triple(target_triple or (pass_ctx.target_triple if pass_ctx is not None else None))
    if pass_ctx is None:
        pass_ctx = PassContext(opt_level=frontend_opt_level, target_triple=selected_target)
    else:
        if pass_ctx.target_triple != selected_target:
            pass_ctx.clear_ssa_artifacts(reason="C target ABI changed")
        pass_ctx.target_triple = selected_target
        if frontend_opt_level is not None and pass_ctx.opt_level is None:
            pass_ctx.opt_level = int(frontend_opt_level)
    _apply_pass_selection_from_env(pass_ctx)
    if evaluation_entry:
        pass_ctx.entry_roots.add(evaluation_entry)

    # HighTier: AST analysis passes (populate PassContext)
    ast = pass_pipeline.run_high_tier(ast, pass_ctx)

    # MidTier: codegen reads PassContext for smarter IR generation
    codegen = CCodeGenerator(
        translation_unit_name=unit_name, emit_debug=emit_debug,
        pass_ctx=pass_ctx,
    )
    codegen.set_target_text(selected_target, "")
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
        "function_symbols": {
            name: state.symbol_name
            for name, state in codegen._file_scope_function_states.items()
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
    from pcc.frontends.c.passes import PassContext

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
    evaluation_entry=None,
):
    unit_base_dir = os.path.dirname(unit.path) if unit.path else base_dir
    codestr = _preprocess_translation_unit_source(
        unit.source,
        unit_base_dir,
        use_system_cpp,
        include_dirs=include_dirs,
        cpp_args=cpp_args,
        target_triple=target_triple,
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
            unit.name + ("|entry=" + evaluation_entry if evaluation_entry else ""),
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
            evaluation_entry=evaluation_entry,
        )
        _store_compiled_artifact(normalized_cache_dir, cache_key, artifact)
        return artifact

    return _invoke_compile_preprocessed_translation_unit_artifact(
        unit.name,
        codestr,
        frontend_opt_level=frontend_opt_level,
        target_triple=target_triple,
        fsanitize=fsanitize,
        evaluation_entry=evaluation_entry,
    )


def _invoke_compile_preprocessed_translation_unit_artifact(
    unit_name,
    codestr,
    frontend_opt_level=None,
    target_triple=None,
    fsanitize=None,
    evaluation_entry=None,
):
    """Call the owned frontend's fixed internal ABI."""
    return _compile_preprocessed_translation_unit_artifact(
        unit_name, codestr, frontend_opt_level=frontend_opt_level,
        target_triple=target_triple, fsanitize=fsanitize,
        evaluation_entry=evaluation_entry,
    )


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

        self.codegen = CCodeGenerator()
        from pcc.frontends.c.parse import make_c_parser
        self.parser = make_c_parser()
        host_triple = host_target_triple()
        self.target_triple = target_triple or host_triple
        self.codegen.set_target_text(self.target_triple, "")
        self.is_cross = target_triple is not None and target_triple != host_triple
        self._owned_host_images = []

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

        from pcc.frontends.c.evaluator.owned_execution import evaluate_units

        snippet_base_dir = os.path.abspath(base_dir) if base_dir else os.getcwd()
        artifact = _compile_translation_unit_artifact_job(
            TranslationUnit(name="__pcc_eval__.c", path=os.path.join(snippet_base_dir, "__pcc_eval__.c"), source=codestr),
            snippet_base_dir, bool(use_system_cpp), include_dirs, cpp_args,
            cache_dir, use_compile_cache, self._normalize_opt_level(optimize),
            self.backend_sig, fsanitize=fsanitize,
            evaluation_entry=entry,
        )
        if llvm_dump_dir:
            _write_llvm_dump(llvm_dump_dir, "temp.ir", artifact["ir_text"])
        return evaluate_units(
            self, [_artifact_to_compiled_unit(artifact)],
            entry=artifact.get("function_symbols", {}).get(entry, entry), args=args,
            prog_args=prog_args, optimize=optimize, base_dir=base_dir,
            link_args=link_args, dump_dir=llvm_dump_dir,
            return_descriptor=artifact.get("func_return_types", {}).get(entry),
        )

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

        # Imported here, not at module scope: a compiled pcc1 has no
        # ``concurrent.futures``, and a module-level import made this whole
        # module -- and with it the entire C driver -- unreachable there
        # ("ImportError: No module named 'concurrent.futures'").  The serial
        # branch above already covers the single-unit case pcc1 takes for one
        # translation unit, so the dependency belongs on the parallel edge.
        from concurrent.futures import ProcessPoolExecutor

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
            use_system_cpp = self.backend != "self" and self._has_system_cpp()

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
        from pcc.frontends.c.evaluator.owned_execution import evaluate_units

        dump_dir = _normalize_llvm_dump_dir(llvmdump)
        if dump_dir:
            _write_llvm_dump(dump_dir, "temp.ir", "\n".join(unit[1] for unit in compiled_units))
        return evaluate_units(self, compiled_units, optimize=optimize, args=args,
                              prog_args=prog_args, link_args=link_args, dump_dir=dump_dir)

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
        from pcc.frontends.c.evaluator.owned_execution import evaluate_units

        dump_dir = _normalize_llvm_dump_dir(llvmdump, cache_dir)
        if dump_dir:
            prepared = self._prepare_self_backend_units(compiled_units, optimize=optimize)
            for original, optimized in zip(compiled_units, prepared):
                stem = "temp." + re.sub(r"\W+", "_", original[0])
                _write_llvm_dump(dump_dir, stem + ".ir", original[1])
                _write_llvm_dump(dump_dir, stem + ".opt.ll", optimized[1])
            compiled_units = prepared
            optimize = False
        return evaluate_units(self, compiled_units, optimize=optimize, args=args,
                              prog_args=prog_args, base_dir=base_dir,
                              link_args=link_args)



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
            link_with_system_cc=True,
        )

    def emit_compiled_units(
        self,
        compiled_units,
        emit_obj=None,
        emit_asm=None,
        emit_llvm=None,
        optimize=True,
    ):
        """Emit compiled translation units to file(s) instead of running."""
        self._emit_compiled_units_self_backend(
            compiled_units,
            emit_obj=emit_obj,
            emit_asm=emit_asm,
            emit_llvm=emit_llvm,
            optimize=optimize,
        )
        return

    def validate_owned_freestanding_request(self, link_args=None):
        """Admit the real production runtime before a public C compilation."""
        self._validate_freestanding_link_args(link_args or (), enabled=True)
        triple = str(self.target_triple or "").lower()
        if not any(name in triple for name in ("linux", "darwin", "apple")):
            raise BackendUnavailable("owned --freestanding-libc supports Linux and Darwin")
        explicit = str(os.environ.get("PCC_RUNTIME_ARCHIVE", "") or "").strip()
        if explicit and os.path.basename(explicit) != "libpy_runtime_pcc_py.a":
            raise RuntimeError(
                "--freestanding-libc requires libpy_runtime_pcc_py.a; "
                "PCC_RUNTIME_ARCHIVE selected " + os.path.basename(explicit)
            )
        from pcc.frontends.python.pipeline import _ensure_runtime
        from pcc.frontends.python.pipeline_runtime_archive import runtime_build_mode

        if runtime_build_mode() != "owned":
            raise BackendUnavailable("owned --freestanding-libc requires the owned runtime builder")
        # This checks actual archive/member/source/compiler/target/configuration
        # identities and honors the no-provisioning guard. Never create a second
        # archive view or interpret absent member configuration as defaults.
        return _ensure_runtime(False, needs_libpython=False, target_triple=self.target_triple)

    def emit_executable(self, compiled_units, output: str, *, optimize=True,
                        link_args=None, freestanding_libc=False):
        """Publish a C executable with the explicitly selected backend owner."""
        runtime = (self.validate_owned_freestanding_request(link_args)
                   if freestanding_libc else None)
        external_objects = []
        external_archives = []
        map_path = ""
        for argument in link_args or ():
            path = os.fspath(argument)
            if path.startswith(("-Wl,-Map,", "-Wl,-Map=", "-Wl,-map,")):
                if not any(name in self.target_triple for name in ("linux", "darwin", "apple")):
                    raise BackendUnavailable("owned C link maps require Linux or Darwin")
                if map_path:
                    raise BackendUnavailable("owned C executable accepts one link map")
                map_path = path[len("-Wl,-Map,"):]
                if not map_path:
                    raise BackendUnavailable("owned C link map requires a path")
                continue
            if (freestanding_libc and "linux" in self.target_triple
                    and path in ("-nostdlib", "-static", "-no-pie", "-Wl,-e,_start")):
                continue
            if path.startswith("-") or not path.lower().endswith((".o", ".obj", ".a", ".lib")):
                raise BackendUnavailable("unsupported owned C executable link argument: " + path)
            if os.path.realpath(path) == os.path.realpath(output):
                raise ValueError("link output aliases an input: " + path)
            if path.lower().endswith((".a", ".lib")):
                external_archives.append(path)
            else:
                external_objects.append(path)
        if map_path:
            protected = [output, output + ".pcc-link.tmp"] + external_objects + external_archives
            if runtime is not None:
                protected.append(runtime)
            if any(os.path.realpath(candidate) == os.path.realpath(path)
                   for candidate in (map_path, map_path + ".pcc-link.tmp")
                   for path in protected):
                raise BackendUnavailable("link map output aliases an input or executable")
        prepared = self._prepare_self_backend_units(compiled_units, optimize=optimize) if self._normalize_opt_level(optimize) > 0 else compiled_units
        target_id = self._self_link_target_identity(prepared)
        if target_id in ("self-aarch64-linux-v0", "self-x86_64-linux-v0", "self-x86_64-windows-v0"):
            from pcc.frontends.python.pipeline import _ensure_runtime
            from pcc.frontends.python.owned_runtime_build import ensure_target_runtime
            from pcc.backend.owned_elf_link import link_inputs as elf_link
            from pcc.backend.owned_pe_link import link_inputs as pe_link
            with tempfile.TemporaryDirectory(prefix="pcc_c_owned_") as temporary:
                paths = []
                for index, unit in enumerate(prepared):
                    path = os.path.join(temporary, "unit_" + str(index) + ".s")
                    with open(path, "w", encoding="utf-8") as stream:
                        stream.write(self._self_backend_asm_text([unit]))
                    paths.append(path)
                if runtime is None:
                    if self.target_triple == host_target_triple():
                        runtime = _ensure_runtime(False, needs_libpython=False)
                    else:
                        runtime_root = os.path.join(resolve_pcc_dir_from_environment(__file__), "runtime")
                        runtime = ensure_target_runtime(runtime_root, self.target_triple)
                if "windows" in self.target_triple:
                    pe_link(output=output, assembly=paths, objects=external_objects,
                            archives=external_archives + [runtime])
                else:
                    elf_link(output=output, target=self.target_triple, assembly=paths,
                             objects=external_objects, archives=external_archives + [runtime],
                             map_path=map_path)
            return
        from pcc.backend.arm64_asm_driver import assemble_file
        from pcc.backend.native_object import NativeObject
        from pcc.backend.macho_exec import link_executable

        objects = []
        for unit in prepared:
            # Each translation unit owns one stack-map table. Keep object
            # boundaries until the relocatable linker merges those tables.
            sections, undefined = assemble_file(self._self_backend_asm_text([unit]))
            objects.append(NativeObject.from_sections(sections, undefined=undefined))
        for path in external_objects:
            with open(path, "rb") as stream:
                objects.append(stream.read())
        archive_paths = list(external_archives)
        if runtime is not None:
            archive_paths.append(runtime)
        archives = []
        for path in archive_paths:
            with open(path, "rb") as stream:
                archives.append(stream.read())
        receipt = {} if map_path else None
        image = link_executable(objects, archives=archives, entry="_main", link_receipt=receipt)
        map_text = ""
        if map_path:
            lines = ["# pcc owned Mach-O link map v1", "# target " + self.target_triple,
                     "# entry _main", "# image sha256=" + receipt["image_sha256"]]
            for selected in receipt["archive_members"]:
                lines.append(archive_paths[selected["archive_index"]] + "(" + selected["member"] + ")"
                             + " archive_sha256=" + selected["archive_sha256"]
                             + " member_sha256=" + selected["member_sha256"])
            for library in receipt["dynamic_libraries"]:
                lines.append("# declared_library " + json.dumps(library, sort_keys=True))
            for imported in receipt["imports"]:
                lines.append("# dynamic_import " + json.dumps(imported, sort_keys=True))
            lines.append("# Dynamic imports name declared load-command providers; physical reexport owners are not established.")
            map_text = "\n".join(lines) + "\n"
        temporary = output + ".pcc-link.tmp"
        with open(temporary, "wb") as stream:
            stream.write(image)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o755)
        os.replace(temporary, output)
        if map_path:
            map_temporary = map_path + ".pcc-link.tmp"
            try:
                with open(map_temporary, "w", encoding="utf-8") as stream:
                    stream.write(map_text)
                os.replace(map_temporary, map_path)
            finally:
                if os.path.exists(map_temporary):
                    os.unlink(map_temporary)

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
        asm_text = ""
        if emit_asm or (emit_obj and object_emitter != _SELF_OBJECT_EMITTER_PCC):
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
                    from pcc.backend.arm64_asm_driver import assemble_file
                    from pcc.backend.macho_link import link_relocatable_native
                    from pcc.backend.native_object import NativeObject

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
                elif target_identity in ("self-x86_64-linux-v0", "self-aarch64-linux-v0"):
                    from pcc.backend.elf_x86_64 import emit_relocatable
                    from pcc.backend.owned_elf_link import assemble
                    from pcc.backend.relocatable_merge import merge_elf_objects

                    objects = [assemble(emit_self_asm(unit[1]), parse_self_backend_target_triple(unit[1]))
                               for unit in prepared_units]
                    object_bytes = emit_relocatable(merge_elf_objects(objects))
                elif target_identity == "self-x86_64-windows-v0":
                    from pcc.backend.coff_x86_64 import assemble, emit_object
                    from pcc.backend.relocatable_merge import merge_coff_objects

                    objects = [assemble(emit_self_asm(unit[1])) for unit in prepared_units]
                    object_bytes = emit_object(merge_coff_objects(objects))
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
        """Optimize IR for the self backend with pcc's own passes.

        This used to build an LLVM target machine and run llvmlite's pass
        manager over every unit, which made *compiling C with the self
        backend* require llvmlite -- the one thing `CEvaluator.__init__`
        already says this path must not do.  `run_owned_passes` is the same
        llvmlite-free tier the Python frontend and pcc1 already run, so C and
        Python now optimize through one owned pipeline.

        Every `-O` above zero selects the same bounded owned tier; the caller
        gates on level > 0.  `pipeline_pass_config` owns that tuple's
        membership, so selecting it here cannot drift from the Python path.
        """
        from pcc.frontends.python.compiled_owned_passes import run_owned_passes
        from pcc.frontends.python.pipeline_pass_config import PYTHON_IR_PASS_DEFAULT_TIER

        pass_names = list(PYTHON_IR_PASS_DEFAULT_TIER)
        prepared_units = []
        for unit_name, ir_text, unit_return_type, external_defs in compiled_units:
            # C IR carries no `py_cpy_*` calls, so the strict-no-libpython
            # bailout cannot apply here; pass it off rather than leave the
            # decision to a value that means something else on this path.
            optimized = run_owned_passes(str(ir_text), pass_names, False)
            prepared_units.append(
                (unit_name, optimized, unit_return_type, external_defs)
            )
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
            from pcc.frontends.python.pipeline import _ensure_runtime

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

        from pcc.frontends.python.pipeline import compile_python

        runtime_root = os.path.join(
            resolve_pcc_dir_from_environment(__file__), "runtime"
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

    @staticmethod
    def _self_link_mode():
        """Which linker completes a self-backend C run.

        Defaults to pcc's own Mach-O linker.  `system-cc` keeps the host
        toolchain reachable as a differential oracle, the way `PCC_SELF_OBJ`
        already does for object emission.  An unknown value is refused rather
        than silently becoming a host-tool fallback.
        """
        raw = str(os.environ.get("PCC_SELF_LINK", "") or "").strip().lower()
        if not raw:
            return "pcc"
        if raw not in ("pcc", "system-cc"):
            raise BackendUnavailable(
                "unknown PCC_SELF_LINK value " + repr(raw)
                + "; expected 'pcc' or 'system-cc'"
            )
        return raw

    @staticmethod
    def _self_link_target_identity(prepared_units):
        for _unit_name, ir_text, _return_type, _external_defs in prepared_units:
            try:
                return self_backend_target_identity(
                    parse_self_backend_target_triple(ir_text)
                )
            except Exception:
                return None
        return None

    @staticmethod
    def _link_executable_owned(asm_text, bin_path):
        """Assemble and link a self-backend C program with pcc's own tools.

        Running a C program used to shell out to `cc` for assemble+link, so
        *running* C required a host C compiler even though `--emit-obj`
        already wrote the object with pcc's own assembler and object writer.
        Undefined libc symbols become ordinary dylib imports, which is what
        the Mach-O linker already does for every pcc-linked image.
        """
        from pcc.backend.arm64_asm_driver import assemble_file
        from pcc.backend.macho_exec import link_executable
        from pcc.backend.native_object import NativeObject

        sections, undefined = assemble_file(asm_text)
        native_object = NativeObject.from_sections(
            sections, undefined=undefined,
        )
        image = link_executable([native_object], entry="_main")
        with open(bin_path, "wb") as stream:
            stream.write(image)
        os.chmod(bin_path, 0o755)

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
        link_with_system_cc=False,
    ):
        prepared_units = (
            self._prepare_self_backend_units(compiled_units, optimize=optimize)
            if self._normalize_opt_level(optimize) > 0
            else compiled_units
        )
        tmpdir = tempfile.mkdtemp(prefix="pcc_self_run_")
        try:
            if not link_with_system_cc:
                if self.is_cross:
                    raise BackendUnavailable("cannot execute C for a foreign target")
                owned_bin = os.path.join(tmpdir, "program.exe" if "windows" in self.target_triple else "program")
                self.emit_executable(prepared_units, owned_bin, optimize=False,
                                     link_args=link_args, freestanding_libc=freestanding_libc)
                return subprocess.run([owned_bin] + [str(arg) for arg in (prog_args or [])],
                                      capture_output=capture_output, text=text,
                                      timeout=timeout, cwd=base_dir or os.getcwd())
            # The external toolchain is reachable only through an explicitly
            # requested system-cc oracle entrypoint.
            asm_text = self._self_backend_asm_text(prepared_units)
            cc = self._system_cc()
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

    def run_translation_units_owned(
        self, units, optimize=True, llvmdump=False, base_dir=None,
        prog_args=None, jobs=1, include_dirs=None, cpp_args=None,
        link_args=None, use_compile_cache=True, cache_dir=None,
        timeout=120, capture_output=True, text=True, freestanding_libc=False,
    ):
        """Run the public process ABI through the same owned emitter as -o."""
        if freestanding_libc:
            self.validate_owned_freestanding_request(link_args)
        compiled_units = self.compile_translation_units(
            units, base_dir=base_dir, jobs=jobs, include_dirs=include_dirs,
            cpp_args=cpp_args, use_compile_cache=use_compile_cache,
            cache_dir=cache_dir, frontend_opt_level=self._normalize_opt_level(optimize),
        )
        dump_dir = _normalize_llvm_dump_dir(llvmdump, cache_dir)
        if dump_dir:
            _write_llvm_dump(dump_dir, "temp.ir", "\n".join(unit[1] for unit in compiled_units))
        return self._run_compiled_translation_units_self_backend(
            compiled_units, optimize=optimize, base_dir=base_dir,
            prog_args=prog_args, link_args=link_args, timeout=timeout,
            capture_output=capture_output, text=text,
            freestanding_libc=freestanding_libc, link_with_system_cc=False,
        )

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
        pcc_root = os.path.dirname(resolve_pcc_dir_from_environment(__file__))
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
                *_C_EXTENSION_COMPAT_DEFINES,
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
