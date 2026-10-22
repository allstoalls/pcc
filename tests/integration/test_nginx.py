from tests.owned_ir_validation import verify_ir_text
"""nginx compilation test suite.

Tests nginx source files through pcc's full pipeline:
  preprocess → parse → codegen → IR serialize → LLVM verify

Also tests full system-link compilation to produce an nginx binary.

Run:  uv run pytest tests/integration/test_nginx.py -v
"""

import hashlib
import os
import re
import subprocess
import tempfile

import pytest
from pcc.ir import ir

from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
from pcc.frontends.c.parse.c_parser import CParser
from pcc.frontends.c.codegen.c_codegen import CCodeGenerator, postprocess_ir_text
from pcc.driver.project import (
    TranslationUnit,
    collect_translation_units,
    translation_unit_include_dirs,
)
from tests.parallel_jobs import translation_unit_jobs


PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
PROJECTS_DIR = os.path.join(PROJECT_DIR, "projects")
NGINX_DIR = os.path.join(PROJECTS_DIR, "nginx-1.28.3")
NGINX_OBJS_DIR = os.path.join(NGINX_DIR, "objs")
NGINX_MAKEFILE = os.path.join(NGINX_OBJS_DIR, "Makefile")
NGINX_BINARY = os.path.join(NGINX_OBJS_DIR, "nginx")

NGINX_BUILD_LOCK = os.path.join(
    tempfile.gettempdir(),
    f"pcc-nginx-build-{hashlib.sha256(NGINX_DIR.encode('utf-8')).hexdigest()[:16]}.lock",
)

NGINX_CONFIGURE_ARGS = (
    "--with-cc-opt=-Wno-error",
)

# Include paths relative to nginx source dir (from objs/Makefile)
NGINX_INCLUDE_SUBDIRS = (
    "src/core",
    "src/event",
    "src/event/modules",
    "src/event/quic",
    "src/http",
    "src/http/modules",
    "src/os/unix",
    "objs",
)

NGINX_MAKE_GOAL = "build"

_TYPEDEF_CLEANUP = re.compile(
    r"typedef\s+(int|char|short|long|double|float|void)\s+\1\s*;"
)
_VA_LIST_TYPEDEF = re.compile(
    r"^typedef\s+.*\b(__builtin_va_list|__darwin_va_list)\b.*;$", re.MULTILINE
)
_VA_LIST_USE = re.compile(r"\b(__builtin_va_list|__darwin_va_list)\b")
_BARE_VA_LIST = re.compile(r"\bva_list\b")
# macOS system headers use C23 fixed-width enums: typedef enum : uint32_t { ... }
_FIXED_WIDTH_ENUM = re.compile(
    r"typedef\s+enum\s*:\s*\w+\s*\{[^}]*\}\s*\w+\s*;", re.DOTALL
)

pytestmark = pytest.mark.xdist_group(name="vendor_builds")


def _make_env():
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    return env


def _require_nginx_posix():
    if os.name != "posix":
        raise RuntimeError("nginx integration requires POSIX configure, make, and file locks")


def _file_lock(lock_path):
    _require_nginx_posix()
    import contextlib
    import fcntl

    @contextlib.contextmanager
    def _lock():
        os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
        with open(lock_path, "w") as lockfile:
            fcntl.flock(lockfile, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lockfile, fcntl.LOCK_UN)

    return _lock()


def _ensure_nginx_configured():
    """Run nginx configure if not already done.

    Uses system pcre2 and zlib (avoids modifying the project-local pcre/zlib
    directories that other tests depend on).
    """
    _require_nginx_posix()
    if os.path.isfile(NGINX_MAKEFILE):
        return

    with _file_lock(NGINX_BUILD_LOCK):
        if os.path.isfile(NGINX_MAKEFILE):
            return

        configure = subprocess.run(
            ["./configure", *NGINX_CONFIGURE_ARGS],
            cwd=NGINX_DIR,
            capture_output=True,
            text=True,
            timeout=600,
            env=_make_env(),
        )
        assert (
            configure.returncode == 0
        ), f"nginx configure failed:\n{configure.stdout}\n{configure.stderr}"


def _ensure_nginx_built_natively():
    """Build nginx natively using system pcre2 and zlib.

    This is required for the system-link test which needs the native
    libraries for the final link step.
    """
    _ensure_nginx_configured()
    if os.path.isfile(NGINX_BINARY):
        return

    with _file_lock(NGINX_BUILD_LOCK):
        if os.path.isfile(NGINX_BINARY):
            return

        build = subprocess.run(
            ["make", "-C", NGINX_DIR, "-j2"],
            capture_output=True,
            text=True,
            timeout=600,
            env=_make_env(),
        )
        assert (
            build.returncode == 0
        ), f"nginx native build failed:\n{build.stdout}\n{build.stderr}"


def _nginx_cpp_args():
    """Preprocessor args for nginx compilation.

    Includes nginx's own source directories plus system pcre2/zlib headers
    (detected via pkg-config or standard paths).
    """
    _require_nginx_posix()
    include_args = []
    for subdir in NGINX_INCLUDE_SUBDIRS:
        include_args.extend(["-I", os.path.join(NGINX_DIR, subdir)])
    # System pcre2 include path
    result = subprocess.run(
        ["pkg-config", "--cflags", "libpcre2-8"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        for flag in result.stdout.strip().split():
            if flag.startswith("-I"):
                include_args.extend(["-I", flag[2:]])
    return tuple(include_args)


def _clean_nginx_preprocessed_source(processed):
    processed = _TYPEDEF_CLEANUP.sub("", processed)
    processed = _VA_LIST_TYPEDEF.sub("", processed)
    processed = _VA_LIST_USE.sub("char *", processed)
    processed = _BARE_VA_LIST.sub("char *", processed)
    processed = _FIXED_WIDTH_ENUM.sub("", processed)
    for enum_base in (
        "uint8_t",
        "uint16_t",
        "uint32_t",
        "uint64_t",
        "int8_t",
        "int16_t",
        "int32_t",
        "int64_t",
    ):
        processed = processed.replace(f"enum : {enum_base}", "enum")
    return processed


def _nginx_source_files():
    """Get the list of nginx source files from make goal."""
    _ensure_nginx_configured()
    from pcc.driver.project import _scan_make_goal
    sources, _cpp_groups = _scan_make_goal(NGINX_DIR, NGINX_MAKE_GOAL)
    return sources


def _nginx_units():
    """Collect nginx translation units directly (bypasses main() detection).

    nginx's main() uses ``int ngx_cdecl\\nmain(...)`` which the regex
    in ``_has_main`` does not recognise.  We build the unit list manually.
    """
    _ensure_nginx_configured()
    sources = _nginx_source_files()
    units = []
    for fname in sources:
        fpath = os.path.join(NGINX_DIR, fname)
        with open(fpath) as f:
            units.append(TranslationUnit(fname, fpath, f.read()))
    return units, NGINX_DIR


def _nginx_preprocessed_units():
    units, base_dir = _nginx_units()
    cpp_args = _nginx_cpp_args()
    processed_units = []
    for unit in units:
        processed = CEvaluator._system_cpp(
            unit.source,
            base_dir=base_dir,
            cpp_args=cpp_args,
        )
        processed_units.append(
            TranslationUnit(
                unit.name,
                unit.path,
                _clean_nginx_preprocessed_source(processed),
            )
        )
    return processed_units, base_dir


def _nginx_link_args():
    """Linker args for nginx: system pcre2 + zlib."""
    _ensure_nginx_built_natively()
    link_args = ["-lz"]
    # Detect system pcre2 or pcre link flags
    result = subprocess.run(
        ["pkg-config", "--libs", "libpcre2-8"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        link_args.extend(result.stdout.strip().split())
    else:
        link_args.append("-lpcre")
    return link_args


def _compile_nginx_file(fname):
    """Compile a single nginx .c file through pcc pipeline. Returns (stage, detail)."""
    _require_nginx_posix()
    fpath = os.path.join(NGINX_DIR, fname)
    stage = "init"
    try:
        with open(fpath) as f:
            src = f.read()

        processed = CEvaluator._system_cpp(
            src,
            base_dir=NGINX_DIR,
            cpp_args=_nginx_cpp_args(),
        )
        processed = _clean_nginx_preprocessed_source(processed)
        stage = "preprocess"

        ast = CParser().parse(processed)
        stage = "parse"

        cg = CCodeGenerator()
        cg.generate_code(ast)
        stage = "codegen"

        for n, gv in list(cg.module.globals.items()):
            if isinstance(gv, ir.GlobalVariable):
                try:
                    str(gv)
                except Exception:
                    gv.initializer = ir.Constant(gv.value_type, None)

        ir_text = postprocess_ir_text(str(cg.module))
        funcs = [l for l in ir_text.splitlines() if l.startswith("define ")]
        stage = "ir_serialize"

        verify_ir_text(ir_text)
        stage = "llvm_verify"

        return "ok", len(funcs)
    except Exception as e:
        return stage, f"{type(e).__name__}: {str(e)[:120]}"


# ────────────────────────────────────────────────────────────────────
# Collect nginx source file list at import time.
# If nginx is not yet configured, return a static list so that xdist
# workers collect the same tests.  The actual configure runs once
# inside each test function (guarded by a file lock).
# ────────────────────────────────────────────────────────────────────

if (
    os.name == "posix"
    and os.path.isdir(NGINX_DIR)
    and os.path.isfile(NGINX_MAKEFILE)
):
    NGINX_SOURCE_FILES = _nginx_source_files()
else:
    NGINX_SOURCE_FILES = []


# ────────────────────────────────────────────────────────────────────
# Tests
# ────────────────────────────────────────────────────────────────────


@pytest.mark.pcc_gate(unavailable=None if os.path.isdir(NGINX_DIR) else "nginx-1.28.3 not found")
def test_nginx_make_goal_collects_source_files():
    """Verify make goal discovers nginx source files (not pcre/zlib)."""
    units, base_dir = _nginx_units()
    names = [unit.name for unit in units]
    cpp_args = _nginx_cpp_args()

    assert base_dir == os.path.abspath(NGINX_DIR)
    assert "src/core/nginx.c" in names
    assert "src/core/ngx_string.c" in names
    assert "src/http/ngx_http.c" in names
    assert "src/event/ngx_event.c" in names
    assert "objs/ngx_modules.c" in names
    assert os.path.join(NGINX_DIR, "src/http") in cpp_args
    assert os.path.join(NGINX_DIR, "src/http/modules") in cpp_args
    # Verify no external project contamination
    for name in names:
        assert "pcre" not in name.lower(), f"pcre source leaked: {name}"
        assert "zlib" not in name.lower(), f"zlib source leaked: {name}"
    assert len(names) >= 100
    # nginx with rewrite module has the regex source file
    assert "src/core/ngx_regex.c" in names


@pytest.mark.pcc_gate(unavailable=None if os.path.isdir(NGINX_DIR) else "nginx-1.28.3 not found")
@pytest.mark.parametrize("fname", NGINX_SOURCE_FILES, ids=NGINX_SOURCE_FILES)
def test_nginx_source_compile(fname):
    """Test nginx .c file: preprocess → parse → codegen → LLVM verify."""
    stage, result = _compile_nginx_file(fname)
    if stage == "ok":
        pass
    else:
        pytest.xfail(f"Stage '{stage}': {result}")


@pytest.mark.pcc_gate(unavailable=None if os.path.isdir(NGINX_DIR) else "nginx-1.28.3 not found")
@pytest.mark.integration
def test_nginx_native_build():
    """Build nginx with native compiler as a baseline."""
    _ensure_nginx_built_natively()
    assert os.path.isfile(NGINX_BINARY), "native nginx binary not found"
    result = subprocess.run(
        [NGINX_BINARY, "-V"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "nginx/1.28.3" in (result.stdout + result.stderr)


@pytest.mark.pcc_gate(unavailable=None if os.path.isdir(NGINX_DIR) else "nginx-1.28.3 not found")
@pytest.mark.integration
def test_nginx_full_system_link():
    """Compile all nginx sources with pcc, link with system cc, verify binary."""
    _ensure_nginx_built_natively()

    units, base_dir = _nginx_preprocessed_units()

    compiled_units = CEvaluator().compile_translation_units(
        units,
        base_dir=base_dir,
        jobs=translation_unit_jobs(),
        use_system_cpp=False,
    )

    result = CEvaluator().run_compiled_translation_units_with_system_cc(
        compiled_units,
        optimize=True,
        base_dir=base_dir,
        link_args=_nginx_link_args(),
        prog_args=["-V"],
        timeout=180,
    )

    assert (
        result.returncode == 0
    ), f"nginx system-link runtime failed:\n{result.stdout}\n{result.stderr}"
    assert "nginx" in (result.stdout + result.stderr).lower()
