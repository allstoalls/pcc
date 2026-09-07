import os
import subprocess
import sys

from .cli_contract import (
    BACKEND_CHOICES,
    DEFAULT_PUBLIC_BACKEND,
    DEFAULT_EMIT_LL,
    DIAGNOSTIC_FORMAT_CHOICES,
    IR_SCAFFOLD_CHOICES,
    PYTHON_LIBPYTHON_CHOICES,
    cli_input_path,
)

from .package_schema import (
    PACKAGE_MANIFEST_SCHEMA,
    PACKAGE_MANIFEST_SCHEMA_VERSION,
    PCC_CAPI_HEADERS,
    campaign_profile,
    capability_profile,
    distribution_filename_fields,
    literal_project_metadata_fields,
    source_build_policy,
    pcc_native_extension_suffix,
    pcc_native_wheel_tag,
    wheel_tag_fields,
)
from .package_environment import (
    DEFAULT_PYTHON_SEMANTIC_TARGET,
    apply_locked_environment_resource_defaults,
    default_package_cache as _pcc_default_package_cache,
    default_package_site as _pcc_default_package_site,
    environment_info_json,
    environment_info_text,
    package_site_roots,
)
from .package_metadata_paths import package_metadata_paths

from .py_frontend.pipeline import compile_python as _compile_python
from .py_frontend.pipeline import (
    run_self_backend_emit_batch_worker as _run_self_backend_emit_batch_worker,
)
from .py_frontend.pipeline import (
    run_self_backend_emit_worker as _run_self_backend_emit_worker,
)
from .py_frontend.pipeline import (
    run_self_backend_indexed_emit_worker as _run_self_backend_indexed_emit_worker,
)
from .py_frontend.pipeline import (
    run_self_backend_split_worker as _run_self_backend_split_worker,
)
from .py_frontend.pipeline import (
    run_python_multi_codegen_worker as _run_python_multi_codegen_worker,
)
from .cli_bootstrap_array_core import _run_native_package_array_core_from_pcc1
from .cli_bootstrap_pytest import run_pcc1_pytest as _run_pcc1_pytest_harness

_DEFAULT_EMIT_LL = DEFAULT_EMIT_LL
_VALID_DIAGNOSTIC_FORMATS = DIAGNOSTIC_FORMAT_CHOICES
_DEFAULT_BOOTSTRAP_SUBPROCESS_TIMEOUT_SECONDS = 300
_PYTHON_ARGV0_MARKER = "--pcc-internal-python-argv0-v1"


def _bootstrap_subprocess_timeout_seconds() -> int:
    raw = os.environ.get("PCC_BOOTSTRAP_SUBPROCESS_TIMEOUT_SECONDS")
    if raw:
        try:
            seconds = int(raw)
        except Exception:
            seconds = 0
        if seconds > 0:
            return seconds
    return _DEFAULT_BOOTSTRAP_SUBPROCESS_TIMEOUT_SECONDS


def _bootstrap_subprocess_run(args, *, check=False):
    """Run one bootstrap child with a host- and pcc1-enforced deadline."""
    if not check:
        raise ValueError("bootstrap subprocess calls must use check=True")
    subprocess.run(
        args,
        check=True,
        timeout=_bootstrap_subprocess_timeout_seconds(),
    )
    return None


def _native_bootstrap_executable() -> str:
    argv0 = str(sys.argv[0] if len(sys.argv) > 0 else "" or "").strip()
    base = os.path.basename(argv0).lower()
    if not argv0 or not base.startswith("pcc") or argv0.endswith(".py"):
        return ""
    if os.path.isfile(argv0):
        return os.path.abspath(argv0)
    for root in _native_path_search_dirs():
        candidate = os.path.join(root, argv0)
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return ""


def _restart_with_locked_environment_defaults(raw_argv, applied_defaults):
    """Restart a native stage after publishing locked resource defaults.

    The compiled bootstrap imports the frontend before ``bootstrap_cli_main``.
    A fresh process is therefore required for locked-environment defaults to
    be initial state rather than a late environment mutation. The restarted
    child sees non-empty values, so the operation is bounded to one restart.
    """
    if len(applied_defaults) == 0:
        return None
    executable = _native_bootstrap_executable()
    if not executable:
        return None
    command = [executable]
    command.extend(raw_argv)
    try:
        _bootstrap_subprocess_run(command, check=True)
        return 0
    except subprocess.CalledProcessError as exc:
        return exc.returncode
    except Exception:
        _write_text(
            "Error: pcc1 locked-environment resource restart failed",
            err=True,
        )
        return 1


_HELP_TEXT = """Usage: pcc [OPTIONS] PATH [-- ARGS...]
       pcc -c COMMAND [ARGS...]
       pcc - [ARGS...]

Shared pcc command interface (CPython-hosted or native pcc1).

Python scripts and modules compile to native code before execution.
C/project requests enter the full C frontend in the current process.
The default backend is self. Native capability gaps fail explicitly.

Options:
  -h, --help                Show this help message and exit.
  env info [--json]         Inspect the selected pcc package environment.
  inspect PATH [--json]     Inspect a native artifact without executing it.
  bindgen HEADER [-o PATH]  Generate extern declarations from expanded C prototypes.
  -m MODULE [ARGS...]       Compile and run a Python module through pcc1.
  -c COMMAND [ARGS...]      Compile and run a Python command through pcc1.
  - [ARGS...]               Compile and run Python source read from stdin.
  --backend BACKEND         Native emission backend: self (default), llvm, llvm_capi.
  --python-libpython MODE   off (default), auto, or on for Python fallback linkage.
  --python-library          Emit a Python library module without @main.
  -g, --debug               Emit DWARF line information.
  --ir-scaffold MODE        on (default), off, or auto. Enables Path A
                            closed-world IR-builder lowering (Issue 1).
  --emit-llvm[=PATH]        Emit LLVM IR instead of linking a native binary.
  -o PATH                   Output path for the compiled Python input.
  --pass NAME               Accept optimizer pass selection for host CLI parity.
  --disable-pass NAME       Accept optimizer pass disabling for host CLI parity.
  --diagnostic-format FMT   text (default), json, or sarif for hard errors.
  --profile-json PATH       Write compiler profile JSON.
  --tooling-capabilities    Print the pcc1 tooling capability manifest.
  --explain-fallback        Include fallback routing details when known.
  --verbose                 Print Python pipeline timing.
  --pytest [ARGS...]        Run the pcc1-native pytest subset.

Interactive REPL sessions are not yet runtime-owned and fail closed with
PCC-CPY-UNSUPPORTED-L3-TOOLING-INTERACTIVE-REPL; pcc1 never invokes CPython.
"""


def _write_text(text: str, *, err: bool = False, nl: bool = True) -> None:
    # A None here used to reach `sys.stderr.write` unchecked on the nl=False
    # path, and pcc-compiled code writes it as the literal "None" rather than
    # raising.  That produced a bare `None` glued to the front of a real error
    # message, which reads as stray compiler noise and hides who emitted it.
    if text is None:
        text = "<_write_text(None) -- caller passed no text>"
    if nl:
        if text.endswith("\n"):
            if err:
                sys.stderr.write(text)
            else:
                sys.stdout.write(text)
        else:
            if err:
                sys.stderr.write(text + "\n")
            else:
                sys.stdout.write(text + "\n")
    else:
        if err:
            sys.stderr.write(text)
        else:
            sys.stdout.write(text)


def python_tooling_capabilities_json() -> str:
    """Return the mode-labelled, no-host tooling surface owned by pcc1."""
    return (
        '{"schema":"pcc.python-tooling.v1",'
        '"claim_mode":"pcc1/pcc-native/self/no-libpython",'
        '"supported":["script","module","command","stdin",'
        '"traceback","source-map","inspect","compiler-profile",'
        '"codegen-debug","warning-filters"],'
        '"unsupported":{'
        '"interactive-repl":"PCC-CPY-UNSUPPORTED-L3-TOOLING-INTERACTIVE-REPL",'
        '"line-debugger":"PCC-CPY-UNSUPPORTED-L3-TOOLING-DEBUGGER",'
        '"runtime-profiler":"PCC-CPY-UNSUPPORTED-L3-TOOLING-PROFILER",'
        '"coverage":"PCC-CPY-UNSUPPORTED-L3-TOOLING-COVERAGE"}}'
    )


def _unsupported_tooling_module(module_name: str):
    if module_name == "pdb":
        return (
            "PCC-CPY-UNSUPPORTED-L3-TOOLING-DEBUGGER",
            "the runtime-owned line debugger is not implemented",
        )
    if module_name in ("coverage", "coverage.__main__", "trace"):
        return (
            "PCC-CPY-UNSUPPORTED-L3-TOOLING-COVERAGE",
            "runtime line/branch coverage events are not implemented",
        )
    if module_name in ("cProfile", "profile"):
        return (
            "PCC-CPY-UNSUPPORTED-L3-TOOLING-PROFILER",
            "runtime call/return profiling events are not implemented",
        )
    return None


def _normalized_sys_argv():
    argv = []
    i = 1
    while i < len(sys.argv):
        argv.append((sys.argv[i] or "") + "")
        i += 1
    return argv


def _copy_seq(values):
    out = []
    if values is None:
        return out
    i = 0
    while i < len(values):
        out.append(values[i])
        i += 1
    return out


_PY_RUN_CACHE_VERSION = "pcc-py-run-cache-v47"


def _path_list_sep() -> str:
    return ";" if sys.platform.startswith("win") else ":"


def _split_path_list(raw):
    out = []
    parts = str(raw or "").split(_path_list_sep())
    i = 0
    while i < len(parts):
        item = str(parts[i] or "").strip()
        if item:
            out.append(os.path.abspath(item))
        i += 1
    return out


def _append_unique_path(paths, path) -> None:
    path = os.path.abspath(str(path or ""))
    if not path:
        return
    i = 0
    while i < len(paths):
        if paths[i] == path:
            return
        i += 1
    paths.append(path)


def _inferred_package_site_roots(src_path: str):
    roots = []
    src_dir = os.path.dirname(os.path.abspath(src_path))
    _append_unique_path(roots, src_dir)
    if os.path.basename(src_dir) == "test" or os.path.basename(src_dir) == "tests":
        _append_unique_path(roots, os.path.dirname(src_dir))
    env_roots = package_site_roots()
    i = 0
    while i < len(env_roots):
        _append_unique_path(roots, env_roots[i])
        i += 1
    return roots


def _seed_package_site_for_python_entry(src_path: str) -> None:
    roots = _inferred_package_site_roots(src_path)
    if len(roots) > 0:
        os.environ["PCC_PACKAGE_SITE"] = _path_list_sep().join(roots)


# Deliberately self-contained: this module compiles with ZERO CPython
# fallbacks as a single translation unit (tests/fallback_baseline.json),
# and importing the shared copy turns these calls into cross-module
# getattr bridges worth 47 fallbacks. The duplication is pinned instead:
# tests/python/test_cli_shared_helpers_contract.py fails if this copy and
# pcc/cli_shared_paths.py ever diverge (AUD-P2-CLI-SHARED-HELPER-DUPLICATION).
def _fnv1a_update_u64(value: int, text: str) -> int:
    h = value & 0xFFFFFFFFFFFFFFFF
    data = str(text or "")
    i = 0
    while i < len(data):
        h = h ^ (ord(data[i]) & 0xFF)
        h = (h * 1099511628211) & 0xFFFFFFFFFFFFFFFF
        i += 1
    return h


def _fnv1a_update_bytes_u64(value: int, data) -> int:
    h = value & 0xFFFFFFFFFFFFFFFF
    i = 0
    while i < len(data):
        h = h ^ (data[i] & 0xFF)
        h = (h * 1099511628211) & 0xFFFFFFFFFFFFFFFF
        i += 1
    return h


def _iter_py_sources_under(root: str):
    root = os.path.abspath(root)
    out = []
    if os.path.isfile(root):
        if root.endswith(".py"):
            out.append(root)
        return out
    if not os.path.isdir(root):
        return out
    stack = [root]
    while len(stack) > 0:
        cur = stack.pop()
        try:
            names = sorted(os.listdir(cur))
        except OSError:
            names = []
        i = 0
        while i < len(names):
            name = names[i]
            if name in (
                ".git",
                ".hg",
                ".mypy_cache",
                ".pytest_cache",
                ".ruff_cache",
                "__pycache__",
                "build",
                "dist",
                ".venv",
                "venv",
            ):
                i += 1
                continue
            full = os.path.join(cur, name)
            if os.path.isdir(full):
                stack.append(full)
            elif name.endswith(".py"):
                out.append(os.path.abspath(full))
            i += 1
    out.sort()
    return out


def _python_run_cache_key(
    src_path: str,
    *,
    python_libpython: str,
    python_library: bool,
    ir_scaffold: str,
    backend: str,
):
    roots = _inferred_package_site_roots(src_path)
    _append_unique_path(roots, src_path)
    h = 1469598103934665603
    parts = (
        _PY_RUN_CACHE_VERSION,
        os.path.abspath(src_path),
        str(python_libpython or ""),
        str(bool(python_library)),
        str(ir_scaffold or ""),
        str(backend or ""),
        sys.platform,
    )
    i = 0
    while i < len(parts):
        h = _fnv1a_update_u64(h, parts[i])
        h = _fnv1a_update_u64(h, "\0")
        i += 1
    # Identity, not content.  Hashing every byte of every ``.py`` file under
    # every package-site root cost 29.3 s on CPython for one 13302-file /
    # 236 MB root set, and pcc1's compiled byte loop turned ``pcc1 app.py``
    # (run mode) into an apparent hang -- 2463 of 2487 samples inside this
    # function.  A run cache only has to notice that a source changed, so it
    # hashes each file's relative path, size and modification time: an edit
    # always moves ``st_mtime_ns``.  ``seen`` is a dict because the previous
    # list membership test was quadratic in the file count.
    seen = {}
    prefixes = []
    r = 0
    while r < len(roots):
        prefix = os.path.abspath(roots[r])
        if not prefix.endswith(os.sep):
            prefix = prefix + os.sep
        prefixes.append(prefix)
        r += 1
    r = 0
    while r < len(roots):
        sources = _iter_py_sources_under(roots[r])
        s = 0
        while s < len(sources):
            path = sources[s]
            if path in seen:
                s += 1
                continue
            seen[path] = True
            rel = path
            r2 = 0
            while r2 < len(prefixes):
                if path.startswith(prefixes[r2]):
                    rel = path[len(prefixes[r2]) :]
                    break
                r2 += 1
            h = _fnv1a_update_u64(h, rel)
            try:
                # os.path.getsize/getmtime lower natively (codegen/native_os.py);
                # os.stat(...).st_size attribute access does not and cost 17
                # libpython fallbacks in this module.
                h = _fnv1a_update_u64(h, str(os.path.getsize(path)))
                h = _fnv1a_update_u64(h, str(os.path.getmtime(path)))
            except OSError:
                h = _fnv1a_update_u64(h, "missing")
            h = _fnv1a_update_u64(h, "\0")
            s += 1
        r += 1
    return format(h, "016x")


def _python_run_cache_path(
    src_path: str,
    *,
    python_libpython: str,
    python_library: bool,
    ir_scaffold: str,
    backend: str,
):
    disabled = str(os.environ.get("PCC_DISABLE_PY_RUN_CACHE", "") or "").strip()
    if disabled in ("1", "true", "yes", "on"):
        return None
    root = os.environ.get("PCC_PY_RUN_CACHE_DIR", "")
    if not root:
        home = os.environ.get("HOME", "")
        if home:
            root = os.path.join(home, ".cache", "pcc", "py-run-cache")
    if not root:
        return None
    key = _python_run_cache_key(
        src_path,
        python_libpython=python_libpython,
        python_library=python_library,
        ir_scaffold=ir_scaffold,
        backend=backend,
    )
    tag = os.path.basename(src_path)
    if tag.endswith(".py"):
        tag = tag[:-3]
    safe = ""
    i = 0
    while i < len(tag):
        ch = tag[i]
        if ch.isalnum() or ch == "_" or ch == "-":
            safe += ch
        else:
            safe += "_"
        i += 1
    if not safe:
        safe = "pcc1_run"
    cache_dir = os.path.join(root, key)
    return os.path.join(cache_dir, safe)


def _is_pytest_request(argv) -> bool:
    if len(argv) == 0:
        return False
    first = argv[0]
    return first == "--pytest" or first == "pytest"


def _run_pytest_from_pcc1(argv) -> int:
    """Thin facade retaining the historical bootstrap CLI dispatch point."""
    return _run_pcc1_pytest_harness(
        argv,
        sys.executable,
        _bootstrap_subprocess_timeout_seconds(),
    )


def _is_module_request(argv) -> bool:
    return len(argv) > 0 and argv[0] == "-m"


def _is_libpython_mode(value) -> bool:
    return value in PYTHON_LIBPYTHON_CHOICES


def _module_request_libpython_mode(argv):
    """Detect a module run, optionally preceded by --python-libpython=<mode>.

    Returns (is_module_request, mode, module_argv) where module_argv is the
    argv slice starting at "-m". The plain `-m ...` form (no flag) keeps the
    strict research default mode "off", so existing behavior is unchanged.
    Only `off`, `auto`, and `on` are accepted as leading modes.
    """
    if len(argv) == 0:
        return (False, "off", [])
    first = argv[0]
    if first == "-m":
        return (True, "off", _copy_seq(argv))
    if first.startswith("--python-libpython="):
        mode = _option_value(first)
        if _is_libpython_mode(mode) and len(argv) > 1 and argv[1] == "-m":
            return (True, mode, _copy_seq(argv[1:]))
        return (False, "off", [])
    if first == "--python-libpython":
        if len(argv) > 2 and _is_libpython_mode(argv[1]) and argv[2] == "-m":
            return (True, argv[1], _copy_seq(argv[2:]))
        return (False, "off", [])
    return (False, "off", [])


def _json_escape(text: str) -> str:
    out = ""
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            out += "\\\\"
        elif ch == '"':
            out += '\\"'
        elif ch == "\n":
            out += "\\n"
        elif ch == "\r":
            out += "\\r"
        elif ch == "\t":
            out += "\\t"
        else:
            out += ch
        i += 1
    return out


def _json_str(text: str) -> str:
    return '"' + _json_escape(text) + '"'


def _make_bootstrap_run_tempdir(prefix: str) -> str:
    base = os.environ.get("TMPDIR") or "/tmp"
    pid = os.getpid()
    path = os.path.join(base, prefix + str(pid))
    _bootstrap_subprocess_run(["rm", "-rf", path], check=True)
    _bootstrap_subprocess_run(["mkdir", "-p", path], check=True)
    return path


def _remove_bootstrap_run_tempdir(path: str) -> None:
    _bootstrap_subprocess_run(["rm", "-rf", path], check=True)


def _python_program_command(
    executable: str, execution_mode: str, logical_argv0: str, program_args
):
    """Build the private argv envelope consumed by ``py_set_program_args``.

    The OS necessarily starts the compiled artifact with its executable path
    in argv[0].  Script, ``-m``, ``-c`` and stdin execution instead expose the
    same logical argv[0] values as python3.  The runtime strips this private
    three-field envelope before publishing ``sys.argv``; direct execution of
    a compiled artifact remains unchanged.
    """
    command = [executable, _PYTHON_ARGV0_MARKER, execution_mode, logical_argv0]
    i = 0
    while i < len(program_args):
        command.append(program_args[i])
        i += 1
    return command


def _read_python_stdin_source() -> str:
    chunks = []
    while True:
        line = sys.stdin.readline()
        if line == "":
            break
        chunks.append(line)
    return "".join(chunks)


def _run_inline_python_request(kind: str, source: str, program_args) -> int:
    """Materialize command/stdin source and reuse the strict script pipeline."""
    if kind not in ("command", "stdin"):
        raise ValueError("unsupported inline Python execution mode: " + str(kind))
    td = _make_bootstrap_run_tempdir("pcc1-python-input-")
    old_package_site = str(os.environ.get("PCC_PACKAGE_SITE") or "")
    old_disable_cache = str(os.environ.get("PCC_DISABLE_PY_RUN_CACHE") or "")
    if kind == "command":
        filename = "__pcc_command__.py"
        logical_argv0 = "-c"
    else:
        filename = "__pcc_stdin__.py"
        logical_argv0 = "-"
    path = os.path.join(td, filename)
    status = 1
    try:
        roots = [os.getcwd()]
        if old_package_site:
            roots.extend(_split_path_list(old_package_site))
        os.environ["PCC_PACKAGE_SITE"] = _path_list_sep().join(roots)
        # Inline source has a process-scoped temporary path.  Caching it would
        # either retain dead paths or hash the entire current working tree.
        os.environ["PCC_DISABLE_PY_RUN_CACHE"] = "1"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(source)
            if source and not source.endswith("\n"):
                fh.write("\n")
        forwarded = [path]
        i = 0
        while i < len(program_args):
            forwarded.append(program_args[i])
            i += 1
        status = _bootstrap_cli_main_impl(
            forwarded,
            _logical_argv0=logical_argv0,
            _execution_mode=kind,
        )
    except Exception:
        _write_text("Error: pcc1 inline Python compile/run failed", err=True)
        status = 1
    finally:
        # Assignment is intentional: the standalone pcc1 compiler owns native
        # environment assignment, while mapping ``pop`` would introduce a
        # libpython bridge into its closed-world bootstrap.  Every pcc
        # consumer treats an empty value as absent, matching the pre-existing
        # bootstrap environment contract.
        os.environ["PCC_PACKAGE_SITE"] = old_package_site
        os.environ["PCC_DISABLE_PY_RUN_CACHE"] = old_disable_cache
    try:
        _remove_bootstrap_run_tempdir(td)
    except Exception:
        if status == 0:
            _write_text("Error: pcc1 inline Python scratch cleanup failed", err=True)
            status = 1
    return status


def _json_str_or_null(text) -> str:
    if text is None:
        return "null"
    return _json_str(str(text))


def _json_str_list(items) -> str:
    out = "["
    i = 0
    while i < len(items):
        if i > 0:
            out += ", "
        out += _json_str(str(items[i]))
        i += 1
    out += "]"
    return out


def _json_int_list(items) -> str:
    out = "["
    i = 0
    while i < len(items):
        if i > 0:
            out += ", "
        out += str(int(items[i]))
        i += 1
    out += "]"
    return out


def compat_runner_manifest(mode) -> dict:
    """Describe the compatibility-runner contract for a --python-libpython mode.

    `auto`/`on` run the requested module in the explicit CPython compatibility
    subprocess. The pcc1 binary itself remains no-libpython, so links_libpython
    stays false. `native_package_claim` is always False: this route is never a
    pcc-native package support proof.
    """
    if mode == "auto" or mode == "on":
        return {
            "requested_execution_mode": "cpython-compat",
            "execution_mode": "cpython-compat",
            "python_libpython_mode": mode,
            "allows_libpython_fallback": True,
            "links_libpython": False,
            "native_package_claim": False,
        }
    return {
        "requested_execution_mode": "pcc-native",
        "execution_mode": "pcc-native",
        "python_libpython_mode": "off",
        "allows_libpython_fallback": False,
        "links_libpython": False,
        "native_package_claim": False,
    }


def compat_runner_manifest_json(mode) -> str:
    """Serialize compat_runner_manifest(mode) via the hand-rolled JSON helpers.

    cli_bootstrap.py is compiled no-libpython into pcc1, so this must not use
    the json module; it uses _json_str plus literal boolean tokens.
    """
    if mode == "auto" or mode == "on":
        requested_execution_mode = "cpython-compat"
        python_libpython_mode = mode
    else:
        requested_execution_mode = "pcc-native"
        python_libpython_mode = "off"
    if mode == "auto" or mode == "on":
        execution_mode = "cpython-compat"
        allows_libpython_fallback = "true"
    else:
        execution_mode = "pcc-native"
        allows_libpython_fallback = "false"
    return (
        '{"requested_execution_mode": '
        + _json_str(requested_execution_mode)
        + ', "execution_mode": '
        + _json_str(execution_mode)
        + ', "python_libpython_mode": '
        + _json_str(python_libpython_mode)
        + ', "allows_libpython_fallback": '
        + allows_libpython_fallback
        + ', "links_libpython": false'
        + ', "native_package_claim": false}'
    )


_PACKAGE_COMPAT_TARGETS = (
    ("pytest", "compat_python", "test runner compatibility target"),
    ("packaging", "compat_python", "pure-Python packaging metadata"),
    ("requests", "nolibpython_python", "pure-Python network stack smoke"),
    (
        "numpy",
        "c_extension_abi",
        "unchanged import via CPython C-API/extension ABI first",
    ),
    (
        "mlx",
        "c_extension_abi",
        "Apple MLX C++/Metal extension ABI and array-runtime target; cpython-compat import first",
    ),
    (
        "vllm",
        "c_extension_abi",
        "vLLM PyTorch/CUDA extension stack target; cpython-compat import first",
    ),
    (
        "tilelang",
        "c_extension_abi",
        "TileLang TVM/GPU kernel DSL compiler stack target; cpython-compat import first",
    ),
    (
        "vllm-metal",
        "c_extension_abi",
        "vLLM-Metal Apple Silicon extension stack on MLX/Metal; cpython-compat import first",
    ),
    ("cffi", "c_extension_abi", "C FFI package target"),
    ("pybind11", "c_extension_abi", "C++ extension ABI target"),
    ("pandas", "c_extension_abi", "depends on NumPy ABI progress"),
)


def _package_compat_row_for_name(name: str):
    i = 0
    while i < len(_PACKAGE_COMPAT_TARGETS):
        row = _PACKAGE_COMPAT_TARGETS[i]
        if name == row[0]:
            return row
        i += 1
    return None


def _package_level_for_name(name: str):
    row = _package_compat_row_for_name(name)
    if row is None:
        return None
    return row[1]


def _package_summary_for_name(name: str):
    row = _package_compat_row_for_name(name)
    if row is None:
        return None
    return row[2]


def _sanitize_tag(text: str) -> str:
    out = ""
    i = 0
    while i < len(text):
        ch = text[i]
        ok = ("a" <= ch <= "z") or ("A" <= ch <= "Z") or ("0" <= ch <= "9") or ch == "_"
        out += ch if ok else "_"
        i += 1
    return out or "unknown"


def _native_current_platform_tag() -> str:
    # pcc1 cannot rely on host sysconfig; use an explicit tag when provided
    # and otherwise stay deterministic.
    env_tag = os.environ.get("PCC_PLATFORM_TAG")
    if env_tag:
        return env_tag
    if sys.platform == "darwin":
        return "macosx_arm64"
    if sys.platform.startswith("linux"):
        return "linux_x86_64"
    if sys.platform.startswith("win"):
        return "win_amd64"
    return "unknown"


def _native_pcc_wheel_tag() -> str:
    return pcc_native_wheel_tag(_native_current_platform_tag())


def _native_pcc_extension_suffix() -> str:
    return pcc_native_extension_suffix(_native_current_platform_tag())


def _native_find_from(text: str, needle: str, start: int) -> int:
    if needle == "":
        return start
    i = start
    limit = len(text) - len(needle)
    while i <= limit:
        j = 0
        matched = True
        while j < len(needle):
            if text[i + j] != needle[j]:
                matched = False
                break
            j += 1
        if matched:
            return i
        i += 1
    return -1


def _native_range_endswith(text: str, start: int, end: int, suffix: str) -> bool:
    length = end - start
    if length < len(suffix):
        return False
    offset = end - len(suffix)
    i = 0
    while i < len(suffix):
        if text[offset + i] != suffix[i]:
            return False
        i += 1
    return True


def _native_source_kind(path) -> str:
    if path is None:
        return "unresolved"
    lower = path.lower()
    if os.path.isdir(path):
        return "local_source"
    if lower.endswith(".whl"):
        return "wheel"
    if (
        lower.endswith(".tar.gz")
        or lower.endswith(".tar.bz2")
        or lower.endswith(".tar.xz")
        or lower.endswith(".tgz")
        or lower.endswith(".zip")
    ):
        return "sdist"
    return "artifact"


def _native_compile_commands_json(root) -> str:
    path = None
    entries = 0
    c_entries = 0
    cxx_entries = 0
    fortran_entries = 0
    if root is not None:
        candidate = os.path.join(root, "compile_commands.json")
        if os.path.exists(candidate):
            path = candidate
            try:
                with open(candidate, "r") as fh:
                    text = fh.read()
            except Exception:
                text = ""
            pos = 0
            while True:
                key = _native_find_from(text, '"file"', pos)
                if key < 0:
                    break
                colon = _native_find_from(text, ":", key)
                first = _native_find_from(text, '"', colon + 1)
                second = _native_find_from(text, '"', first + 1)
                if colon >= 0 and first >= 0 and second >= 0:
                    entries += 1
                    if _native_range_endswith(text, first + 1, second, ".c"):
                        c_entries += 1
                    elif (
                        _native_range_endswith(text, first + 1, second, ".cc")
                        or _native_range_endswith(text, first + 1, second, ".cpp")
                        or _native_range_endswith(text, first + 1, second, ".cxx")
                    ):
                        cxx_entries += 1
                    elif (
                        _native_range_endswith(text, first + 1, second, ".f")
                        or _native_range_endswith(text, first + 1, second, ".for")
                        or _native_range_endswith(text, first + 1, second, ".f77")
                        or _native_range_endswith(text, first + 1, second, ".f90")
                        or _native_range_endswith(text, first + 1, second, ".f95")
                        or _native_range_endswith(text, first + 1, second, ".f03")
                        or _native_range_endswith(text, first + 1, second, ".f08")
                    ):
                        fortran_entries += 1
                    pos = second + 1
                else:
                    pos = key + 6
    out = "{"
    out += '"c_entries": ' + str(c_entries)
    out += ', "cxx_entries": ' + str(cxx_entries)
    out += ', "entries": ' + str(entries)
    out += ', "fortran_entries": ' + str(fortran_entries)
    out += ', "path": ' + _json_str_or_null(path)
    out += "}"
    return out


def _native_meson_allows_noblas(root) -> bool:
    if root is None:
        return False
    candidates = [
        os.path.join(root, "meson.options"),
        os.path.join(root, "meson_options.txt"),
    ]
    i = 0
    while i < len(candidates):
        path = candidates[i]
        if os.path.exists(path):
            try:
                with open(path, "r") as fh:
                    text = fh.read().lower()
            except Exception:
                text = ""
            pos = 0
            while True:
                pos = _native_find_from(text, "allow-noblas", pos)
                if pos < 0:
                    break
                end = _native_find_from(text, ")", pos)
                if end < 0:
                    end = pos + 240
                body = text[pos:end]
                if (
                    _native_find_from(body, "type", 0) >= 0
                    and _native_find_from(body, "boolean", 0) >= 0
                ):
                    value_pos = _native_find_from(body, "value", 0)
                    colon = _native_find_from(body, ":", value_pos)
                    value_text = body[colon : colon + 24] if colon >= 0 else ""
                    if (
                        value_pos >= 0
                        and colon >= 0
                        and _native_find_from(value_text, "true", 0) >= 0
                    ):
                        return True
                pos += 1
        i += 1
    return False


def _native_package_metadata_json(pkg_name: str, root) -> str:
    source_kind = _native_source_kind(root)
    pyproject_backend = None
    meson_build = False
    if root is not None and os.path.isdir(root):
        if os.path.exists(os.path.join(root, "meson.build")):
            meson_build = True
        if os.path.exists(os.path.join(root, "pyproject.toml")):
            pyproject_backend = "unknown"
    out = "{"
    out += '"abi_tag": null'
    out += ', "blas_indicators": []'
    out += ', "compile_commands": ' + _native_compile_commands_json(root)
    out += ', "current_platform_tag": ' + _json_str(_native_current_platform_tag())
    out += ', "cython_sources": []'
    out += ', "diagnostics": []'
    out += ', "fortran_sources": []'
    out += ', "generated_c_artifacts": []'
    out += ', "generated_c_policy": "none"'
    out += ', "lapack_indicators": []'
    out += ', "meson_build": ' + ("true" if meson_build else "false")
    out += ', "name": ' + _json_str(pkg_name)
    out += ', "native_extensions": []'
    if _native_meson_allows_noblas(root):
        out += ', "native_library_fallbacks": ["blas", "lapack"]'
    else:
        out += ', "native_library_fallbacks": []'
    out += ', "path": ' + _json_str_or_null(root)
    out += ', "pcc_native_wheel_tag": ' + _json_str(_native_pcc_wheel_tag())
    out += ', "platform_tag": null'
    out += ', "pyproject_build_backend": ' + _json_str_or_null(pyproject_backend)
    out += ', "pyproject_requires": []'
    out += ', "python_tag": null'
    out += ', "requires_cython_regeneration": false'
    out += ', "source_kind": ' + _json_str(source_kind)
    out += "}"
    return out


def _native_package_path(name: str, explicit_path):
    if explicit_path:
        return os.path.abspath(explicit_path)
    if os.path.exists(name):
        return os.path.abspath(name)
    projects = os.path.join(os.getcwd(), "projects")
    if not os.path.isdir(projects):
        return None
    prefix = name + "-"
    best = None
    try:
        names = os.listdir(projects)
    except Exception:
        return None
    i = 0
    while i < len(names):
        candidate = names[i]
        if candidate.startswith(prefix):
            full = os.path.join(projects, candidate)
            if os.path.isdir(full):
                if best is None or candidate > best:
                    best = candidate
        i += 1
    if best is None:
        return None
    return os.path.abspath(os.path.join(projects, best))


def _should_skip_package_dir(name: str) -> bool:
    return (
        name == ".git"
        or name == ".mypy_cache"
        or name == ".pytest_cache"
        or name == "__pycache__"
    )


def _should_skip_package_build_dir(name: str) -> bool:
    if _should_skip_package_dir(name):
        return True
    lowered = name.lower()
    while lowered.startswith("_"):
        lowered = lowered[1:]
    while lowered.endswith("_"):
        lowered = lowered[:-1]
    return (
        lowered == ".github"
        or lowered == ".spin"
        or lowered == "benchmark"
        or lowered == "benchmarks"
        or lowered == "doc"
        or lowered == "docs"
        or lowered == "example"
        or lowered == "examples"
        or lowered == "test"
        or lowered == "tests"
        or lowered == "tools"
        or lowered == "vendored-meson"
        or lowered.startswith(".")
    )


def _count_package_files(root: str, build_relevant: bool = False):
    counts = [0, 0, 0, 0, 0, 0, 0, 0]
    if root is None or not os.path.exists(root):
        return counts
    stack = [root]
    while len(stack) > 0:
        current = stack.pop()
        try:
            entries = os.listdir(current)
        except Exception:
            entries = []
        i = 0
        while i < len(entries):
            name = entries[i]
            full = os.path.join(current, name)
            if os.path.isdir(full):
                skip = (
                    _should_skip_package_build_dir(name)
                    if build_relevant
                    else _should_skip_package_dir(name)
                )
                if not skip:
                    stack.append(full)
            else:
                counts[0] += 1
                lowered = name.lower()
                if lowered.endswith(".py"):
                    counts[1] += 1
                elif lowered.endswith(".c"):
                    counts[2] += 1
                elif (
                    lowered.endswith(".cc")
                    or lowered.endswith(".cpp")
                    or lowered.endswith(".cxx")
                ):
                    counts[3] += 1
                elif lowered.endswith(".pyx"):
                    counts[4] += 1
                elif (
                    lowered.endswith(".f")
                    or lowered.endswith(".for")
                    or lowered.endswith(".f77")
                    or lowered.endswith(".f90")
                    or lowered.endswith(".f95")
                    or lowered.endswith(".f03")
                    or lowered.endswith(".f08")
                ):
                    counts[7] += 1
                elif (
                    lowered.endswith(".so")
                    or lowered.endswith(".pyd")
                    or lowered.endswith(".dylib")
                ):
                    counts[5] += 1
                elif (
                    lowered.endswith(".h")
                    or lowered.endswith(".hpp")
                    or lowered.endswith(".hh")
                ):
                    counts[6] += 1
            i += 1
    return counts


def _package_inspection_json(name: str, explicit_path=None) -> str:
    raw_name = (name or "").strip() or "package"
    root = _native_package_path(raw_name, explicit_path)
    pkg_name = raw_name
    if explicit_path is None and root is not None and os.path.exists(raw_name):
        pkg_name = _native_package_basename(root)
    counts = _count_package_files(root)
    level = _package_level_for_name(pkg_name)
    summary = _package_summary_for_name(pkg_name)
    out = "{"
    out += '"c_files": ' + str(counts[2])
    out += ', "cxx_files": ' + str(counts[3])
    out += ', "files": ' + str(counts[0])
    out += ', "header_files": ' + str(counts[6])
    out += ', "name": ' + _json_str(pkg_name)
    out += ', "package_level": ' + _json_str_or_null(level)
    out += ', "package_summary": ' + _json_str_or_null(summary)
    out += ', "path": ' + _json_str_or_null(root)
    out += ', "python_files": ' + str(counts[1])
    out += ', "pyx_files": ' + str(counts[4])
    out += ', "shared_objects": ' + str(counts[5])
    out += ', "smoke_tests": []'
    out += ', "artifact_metadata": ' + _native_package_metadata_json(pkg_name, root)
    out += "}"
    return out


def _native_build_plan_json(name: str, explicit_path=None) -> str:
    pkg_name = (name or "").strip() or "package"
    root = _native_package_path(pkg_name, explicit_path)
    counts = _count_package_files(root, True)
    compile_commands = _native_compile_commands_json(root)
    actions = []
    diagnostics = []
    if root is not None and os.path.exists(os.path.join(root, "compile_commands.json")):
        actions.append("consume_compile_commands")
    elif root is not None:
        diagnostics.append("compile_commands_missing")
    if counts[4] > 0 and counts[2] == 0:
        diagnostics.append("cython_regeneration_required")
    if counts[4] > 0 and counts[2] > 0:
        actions.append("consume_generated_c")
    if counts[7] > 0:
        actions.append("delegate_fortran_toolchain")
        diagnostics.append("fortran_toolchain_required")
    if root is not None and os.path.exists(os.path.join(root, "meson.build")):
        actions.append("consume_meson_plan")
    if counts[5] > 0:
        diagnostics.append("prebuilt_native_extension_requires_abi_check")

    # pcc1 keeps this native path intentionally conservative: it reports the
    # same schema as the host build-plan command without importing json/shlex.
    out = "{"
    out += '"actions": ' + _json_str_list(actions)
    out += ', "commands": []'
    out += ', "diagnostics": ' + _json_str_list(diagnostics)
    out += ', "metadata": ' + _native_package_metadata_json(pkg_name, root)
    out += ', "name": ' + _json_str(pkg_name)
    out += ', "ok": true'
    out += ', "path": ' + _json_str_or_null(root)
    out += ', "source_kind": ' + _json_str(_native_source_kind(root))
    out += ', "source_summary": {'
    out += '"c": ' + str(counts[2])
    out += ', "cxx": ' + str(counts[3])
    out += ', "fortran": ' + str(counts[7])
    out += ', "unknown": 0'
    out += "}"
    out += ', "compile_commands": ' + compile_commands
    out += "}"
    return out


_NATIVE_CAPI_HEADER_BY_SYMBOL = (
    ("Py_Initialize", "Python.h"),
    ("Py_UNUSED", "Python.h"),
    ("PyOS_snprintf", "Python.h"),
    ("PyOS_vsnprintf", "Python.h"),
    ("Py_REFCNT", "object.h"),
    ("Py_SET_REFCNT", "object.h"),
    ("PyMapping_Size", "abstract.h"),
    ("PyMapping_Length", "abstract.h"),
    ("PyMapping_Keys", "abstract.h"),
    ("PyMapping_Values", "abstract.h"),
    ("PyMapping_Items", "abstract.h"),
    ("PyObject_LengthHint", "abstract.h"),
    ("PySequence_SetItem", "abstract.h"),
    ("PySequence_Concat", "abstract.h"),
    ("PySequence_Repeat", "abstract.h"),
    ("PySequence_InPlaceConcat", "abstract.h"),
    ("PySequence_InPlaceRepeat", "abstract.h"),
    ("PyMem_Malloc", "pymem.h"),
    ("PyMem_Calloc", "pymem.h"),
    ("PyMem_Realloc", "pymem.h"),
    ("PyMem_Free", "pymem.h"),
    ("PyMem_RawMalloc", "pymem.h"),
    ("PyMem_RawCalloc", "pymem.h"),
    ("PyMem_RawRealloc", "pymem.h"),
    ("PyMem_RawFree", "pymem.h"),
    ("PyMem_FREE", "pymem.h"),
    ("PyModule_Create", "moduleobject.h"),
    ("PyModule_Create2", "moduleobject.h"),
    ("PyModule_AddObject", "moduleobject.h"),
    ("PyModule_AddObjectRef", "moduleobject.h"),
    ("PyModule_Add", "moduleobject.h"),
    ("PyModule_AddIntConstant", "moduleobject.h"),
    ("PyModule_AddStringConstant", "moduleobject.h"),
    ("PyModule_GetDict", "moduleobject.h"),
    ("PyArg_ParseTuple", "modsupport.h"),
    ("PyArg_ParseTupleAndKeywords", "modsupport.h"),
    ("Py_BuildValue", "modsupport.h"),
    ("PyLong_FromDouble", "longobject.h"),
    ("PyLong_AsDouble", "longobject.h"),
    ("PyFloat_AS_DOUBLE", "floatobject.h"),
    ("PyNumber_Check", "abstract.h"),
    ("PyNumber_Long", "abstract.h"),
    ("PyNumber_Float", "abstract.h"),
    ("PyNumber_And", "abstract.h"),
    ("PyNumber_Or", "abstract.h"),
    ("PyNumber_Xor", "abstract.h"),
    ("PyNumber_Invert", "abstract.h"),
    ("PyNumber_Lshift", "abstract.h"),
    ("PyNumber_Rshift", "abstract.h"),
    ("PySet_New", "setobject.h"),
    ("PySet_Add", "setobject.h"),
    ("PySet_Contains", "setobject.h"),
    ("PySet_Discard", "setobject.h"),
    ("PySet_Size", "setobject.h"),
    ("PySet_GET_SIZE", "setobject.h"),
    ("PySet_Check", "setobject.h"),
    ("PySet_CheckExact", "setobject.h"),
    ("PyAnySet_Check", "setobject.h"),
    ("PyAnySet_CheckExact", "setobject.h"),
    ("PyList_AsTuple", "listobject.h"),
    ("PyDict_Keys", "dictobject.h"),
    ("PyDict_Values", "dictobject.h"),
    ("PyDict_Items", "dictobject.h"),
    ("PyLong_FromLong", "longobject.h"),
    ("PyLong_FromUnsignedLong", "longobject.h"),
    ("PyLong_AsLong", "longobject.h"),
    ("PyLong_FromLongLong", "longobject.h"),
    ("PyLong_FromUnsignedLongLong", "longobject.h"),
    ("PyLong_FromInt32", "longobject.h"),
    ("PyLong_FromInt64", "longobject.h"),
    ("PyLong_FromUInt32", "longobject.h"),
    ("PyLong_FromUInt64", "longobject.h"),
    ("PyLong_FromVoidPtr", "longobject.h"),
    ("PyLong_FromSsize_t", "longobject.h"),
    ("PyLong_FromSize_t", "longobject.h"),
    ("PyLong_AsLongLong", "longobject.h"),
    ("PyLong_AsInt", "longobject.h"),
    ("PyLong_AsInt32", "longobject.h"),
    ("PyLong_AsInt64", "longobject.h"),
    ("PyLong_AsUInt32", "longobject.h"),
    ("PyLong_AsUInt64", "longobject.h"),
    ("PyLong_AsVoidPtr", "longobject.h"),
    ("PyLong_AsLongAndOverflow", "longobject.h"),
    ("PyLong_AsUnsignedLong", "longobject.h"),
    ("PyLong_AsUnsignedLongLong", "longobject.h"),
    ("PyLong_AsUnsignedLongLongMask", "longobject.h"),
    ("PyLong_AsSsize_t", "longobject.h"),
    ("PyLong_AsSize_t", "longobject.h"),
    ("PyLong_Check", "longobject.h"),
    ("PyLong_CheckExact", "longobject.h"),
    ("PyBool_FromLong", "boolobject.h"),
    ("PyBool_Check", "boolobject.h"),
    ("PyFloat_FromDouble", "floatobject.h"),
    ("PyFloat_AsDouble", "floatobject.h"),
    ("PyFloat_Check", "floatobject.h"),
    ("PyFloat_CheckExact", "floatobject.h"),
    ("Py_complex", "complexobject.h"),
    ("PyComplex_FromDoubles", "complexobject.h"),
    ("PyComplex_FromCComplex", "complexobject.h"),
    ("PyComplex_AsCComplex", "complexobject.h"),
    ("PyComplex_RealAsDouble", "complexobject.h"),
    ("PyComplex_ImagAsDouble", "complexobject.h"),
    ("PyComplex_Check", "complexobject.h"),
    ("PyComplex_CheckExact", "complexobject.h"),
    ("PyUnicode_AsUTF8String", "unicodeobject.h"),
    ("PyUnicode_AsASCIIString", "unicodeobject.h"),
    ("PyUnicode_AsEncodedString", "unicodeobject.h"),
    ("PyUnicode_FromKindAndData", "unicodeobject.h"),
    ("PyUnicode_FromOrdinal", "unicodeobject.h"),
    ("PyUnicode_AsUCS4", "unicodeobject.h"),
    ("PyUnicode_AsUCS4Copy", "unicodeobject.h"),
    ("PyUnicode_Tailmatch", "unicodeobject.h"),
    ("PyUnicode_Find", "unicodeobject.h"),
    ("PyUnicode_ReadChar", "unicodeobject.h"),
    ("PyUnicode_FindChar", "unicodeobject.h"),
    ("PyUnicode_Count", "unicodeobject.h"),
    ("PyUnicode_Replace", "unicodeobject.h"),
    ("PyUnicode_Substring", "unicodeobject.h"),
    ("PyUnicode_Contains", "unicodeobject.h"),
    ("PyUnicode_Concat", "unicodeobject.h"),
    ("Py_UCS1", "unicodeobject.h"),
    ("Py_UCS2", "unicodeobject.h"),
    ("PyUnicode_1BYTE_KIND", "unicodeobject.h"),
    ("PyUnicode_2BYTE_KIND", "unicodeobject.h"),
    ("PyUnicode_4BYTE_KIND", "unicodeobject.h"),
    ("PyObject_SelfIter", "object.h"),
    ("PyIter_NextItem", "abstract.h"),
    ("PyErr_Print", "pyerrors.h"),
    ("PyErr_CheckSignals", "pyerrors.h"),
    ("Py_UCS4", "unicodeobject.h"),
    ("PyUnicode_FromString", "unicodeobject.h"),
    ("PyUnicode_FromStringAndSize", "unicodeobject.h"),
    ("PyUnicode_FromFormat", "unicodeobject.h"),
    ("PyUnicode_FromFormatV", "unicodeobject.h"),
    ("PyUnicode_InternFromString", "unicodeobject.h"),
    ("PyUnicode_FromEncodedObject", "unicodeobject.h"),
    ("PyUnicode_AsUTF8", "unicodeobject.h"),
    ("PyUnicode_AsUTF8AndSize", "unicodeobject.h"),
    ("PyUnicode_Check", "unicodeobject.h"),
    ("PyUnicode_CheckExact", "unicodeobject.h"),
    ("PyUnicode_GetLength", "unicodeobject.h"),
    ("PyUnicode_GET_LENGTH", "unicodeobject.h"),
    ("PyUnicode_Compare", "unicodeobject.h"),
    ("PyUnicode_CompareWithASCIIString", "unicodeobject.h"),
    ("PyUnicode_EqualToUTF8", "unicodeobject.h"),
    ("PyUnicode_EqualToUTF8AndSize", "unicodeobject.h"),
    ("Py_UNICODE_ISSPACE", "unicodeobject.h"),
    ("Py_UNICODE_ISDIGIT", "unicodeobject.h"),
    ("Py_UNICODE_ISDECIMAL", "unicodeobject.h"),
    ("Py_UNICODE_ISNUMERIC", "unicodeobject.h"),
    ("Py_UNICODE_ISLOWER", "unicodeobject.h"),
    ("Py_UNICODE_ISUPPER", "unicodeobject.h"),
    ("Py_UNICODE_ISTITLE", "unicodeobject.h"),
    ("Py_UNICODE_ISALPHA", "unicodeobject.h"),
    ("Py_UNICODE_ISALNUM", "unicodeobject.h"),
    ("PyErr_SetString", "pyerrors.h"),
    ("PyErr_SetNone", "pyerrors.h"),
    ("PyErr_SetObject", "pyerrors.h"),
    ("PyErr_Format", "pyerrors.h"),
    ("PyErr_FormatV", "pyerrors.h"),
    ("PyErr_NoMemory", "pyerrors.h"),
    ("PyErr_SetFromErrno", "pyerrors.h"),
    ("PyErr_SetFromErrnoWithFilenameObject", "pyerrors.h"),
    ("PyErr_NewException", "pyerrors.h"),
    ("PyErr_BadInternalCall", "pyerrors.h"),
    ("PyErr_WarnEx", "pyerrors.h"),
    ("PyErr_WarnFormat", "pyerrors.h"),
    ("PyErr_WriteUnraisable", "pyerrors.h"),
    ("PyErr_Occurred", "pyerrors.h"),
    ("PyErr_Clear", "pyerrors.h"),
    ("PyErr_GivenExceptionMatches", "pyerrors.h"),
    ("PyErr_ExceptionMatches", "pyerrors.h"),
    ("PyErr_Fetch", "pyerrors.h"),
    ("PyErr_Restore", "pyerrors.h"),
    ("PyExc_BaseException", "pyerrors.h"),
    ("PyExc_Exception", "pyerrors.h"),
    ("PyExc_ValueError", "pyerrors.h"),
    ("PyExc_TypeError", "pyerrors.h"),
    ("PyExc_RuntimeError", "pyerrors.h"),
    ("PyExc_KeyError", "pyerrors.h"),
    ("PyExc_IndexError", "pyerrors.h"),
    ("PyExc_AttributeError", "pyerrors.h"),
    ("PyExc_MemoryError", "pyerrors.h"),
    ("PyExc_OverflowError", "pyerrors.h"),
    ("PyExc_SystemError", "pyerrors.h"),
    ("PyExc_NameError", "pyerrors.h"),
    ("PyExc_NotImplementedError", "pyerrors.h"),
    ("PyExc_ArithmeticError", "pyerrors.h"),
    ("PyExc_LookupError", "pyerrors.h"),
    ("PyExc_OSError", "pyerrors.h"),
    ("PyExc_IOError", "pyerrors.h"),
    ("PyExc_AssertionError", "pyerrors.h"),
    ("PyExc_StopIteration", "pyerrors.h"),
    ("PyExc_StopAsyncIteration", "pyerrors.h"),
    ("PyExc_ZeroDivisionError", "pyerrors.h"),
    ("PyExc_ReferenceError", "pyerrors.h"),
    ("PyExc_BufferError", "pyerrors.h"),
    ("PyExc_ImportError", "pyerrors.h"),
    ("PyExc_ModuleNotFoundError", "pyerrors.h"),
    ("PyExc_ImportWarning", "pyerrors.h"),
    ("PyExc_FloatingPointError", "pyerrors.h"),
    ("PyExc_RecursionError", "pyerrors.h"),
    ("PyExc_UnicodeDecodeError", "pyerrors.h"),
    ("PyExc_Warning", "pyerrors.h"),
    ("PyExc_UserWarning", "pyerrors.h"),
    ("PyExc_RuntimeWarning", "pyerrors.h"),
    ("PyExc_DeprecationWarning", "pyerrors.h"),
    ("PyExc_FutureWarning", "pyerrors.h"),
    ("PyObject_Call", "abstract.h"),
    ("PyObject_CallObject", "abstract.h"),
    ("PyObject_CallNoArgs", "abstract.h"),
    ("PyObject_CallOneArg", "abstract.h"),
    ("PyObject_Vectorcall", "abstract.h"),
    ("PyObject_VectorcallMethod", "abstract.h"),
    ("PyObject_CallFunction", "abstract.h"),
    ("PyObject_CallMethod", "abstract.h"),
    ("PyObject_CallMethodNoArgs", "abstract.h"),
    ("PyObject_CallMethodOneArg", "abstract.h"),
    ("PyObject_CallFunctionObjArgs", "abstract.h"),
    ("PyObject_GetIter", "abstract.h"),
    ("PyIter_Next", "abstract.h"),
    ("PyIter_Check", "abstract.h"),
    ("PyNumber_Add", "abstract.h"),
    ("PyNumber_Subtract", "abstract.h"),
    ("PyNumber_Multiply", "abstract.h"),
    ("PyNumber_TrueDivide", "abstract.h"),
    ("PyNumber_FloorDivide", "abstract.h"),
    ("PyNumber_Remainder", "abstract.h"),
    ("PyNumber_Power", "abstract.h"),
    ("PyNumber_Negative", "abstract.h"),
    ("PyNumber_Positive", "abstract.h"),
    ("PyNumber_Absolute", "abstract.h"),
    ("PyNumber_Index", "abstract.h"),
    ("PyNumber_AsSsize_t", "abstract.h"),
    ("PyIndex_Check", "abstract.h"),
    ("PyObject_GetBuffer", "abstract.h"),
    ("PyObject_CheckBuffer", "abstract.h"),
    ("PyBuffer_Release", "abstract.h"),
    ("PySequence_Check", "abstract.h"),
    ("PyMapping_Check", "abstract.h"),
    ("PyMapping_GetItemString", "abstract.h"),
    ("PyMapping_SetItemString", "abstract.h"),
    ("PyMapping_HasKey", "abstract.h"),
    ("PyMapping_HasKeyString", "abstract.h"),
    ("PyMapping_GetOptionalItem", "abstract.h"),
    ("PyMapping_GetOptionalItemString", "abstract.h"),
    ("PyMapping_HasKeyWithError", "abstract.h"),
    ("PyMapping_HasKeyStringWithError", "abstract.h"),
    ("PySequence_Size", "abstract.h"),
    ("PySequence_Length", "abstract.h"),
    ("PySequence_GetItem", "abstract.h"),
    ("PySequence_Contains", "abstract.h"),
    ("PySequence_Fast", "abstract.h"),
    ("PySequence_Fast_GET_SIZE", "abstract.h"),
    ("PySequence_Fast_ITEMS", "abstract.h"),
    ("PySequence_Fast_GET_ITEM", "abstract.h"),
    ("PySequence_List", "abstract.h"),
    ("PySequence_Tuple", "abstract.h"),
    ("Py_Is", "object.h"),
    ("Py_IsNone", "object.h"),
    ("Py_IsTrue", "object.h"),
    ("Py_IsFalse", "object.h"),
    ("Py_PRINT_RAW", "object.h"),
    ("PyObject_Print", "object.h"),
    ("Py_INCREF", "object.h"),
    ("Py_DECREF", "object.h"),
    ("Py_XINCREF", "object.h"),
    ("Py_XDECREF", "object.h"),
    ("Py_NewRef", "object.h"),
    ("Py_XNewRef", "object.h"),
    ("Py_CLEAR", "object.h"),
    ("Py_SETREF", "object.h"),
    ("Py_XSETREF", "object.h"),
    ("Py_None", "object.h"),
    ("Py_True", "object.h"),
    ("Py_False", "object.h"),
    ("Py_NotImplemented", "object.h"),
    ("Py_RETURN_NONE", "object.h"),
    ("Py_RETURN_TRUE", "object.h"),
    ("Py_RETURN_FALSE", "object.h"),
    ("Py_RETURN_NOTIMPLEMENTED", "object.h"),
    ("Py_UNUSED", "object.h"),
    ("PyOS_snprintf", "object.h"),
    ("PyOS_vsnprintf", "object.h"),
    ("PyObject_GetAttrString", "object.h"),
    ("PyObject_GetAttr", "object.h"),
    ("PyObject_GetOptionalAttr", "object.h"),
    ("PyObject_GetOptionalAttrString", "object.h"),
    ("PyObject_SetAttrString", "object.h"),
    ("PyObject_SetAttr", "object.h"),
    ("PyObject_HasAttr", "object.h"),
    ("PyObject_HasAttrString", "object.h"),
    ("PyObject_HasAttrWithError", "object.h"),
    ("PyObject_HasAttrStringWithError", "object.h"),
    ("PyObject_IsTrue", "object.h"),
    ("PyObject_Not", "object.h"),
    ("PyObject_Hash", "object.h"),
    ("PyCallable_Check", "object.h"),
    ("PyObject_Str", "object.h"),
    ("PyObject_Repr", "object.h"),
    ("PyObject_Bytes", "object.h"),
    ("PyObject_Format", "object.h"),
    ("PyObject_Type", "object.h"),
    ("PyObject_IsInstance", "object.h"),
    ("PyObject_RichCompare", "object.h"),
    ("PyObject_RichCompareBool", "object.h"),
    ("PyObject_GetItem", "object.h"),
    ("PyObject_SetItem", "object.h"),
    ("PyObject_DelItem", "object.h"),
    ("PyObject_Size", "object.h"),
    ("PyObject_Length", "object.h"),
    ("PyObject_Malloc", "object.h"),
    ("PyObject_Calloc", "object.h"),
    ("PyObject_Realloc", "object.h"),
    ("PyObject_Free", "object.h"),
    ("PyObject_MALLOC", "object.h"),
    ("PyObject_REALLOC", "object.h"),
    ("PyObject_FREE", "object.h"),
    ("PyObject_Del", "object.h"),
    ("PyObject_DEL", "object.h"),
    ("PyTuple_New", "tupleobject.h"),
    ("PyTuple_SetItem", "tupleobject.h"),
    ("PyTuple_GetItem", "tupleobject.h"),
    ("PyTuple_Size", "tupleobject.h"),
    ("PyTuple_GET_ITEM", "tupleobject.h"),
    ("PyTuple_GET_SIZE", "tupleobject.h"),
    ("PyTuple_SET_ITEM", "tupleobject.h"),
    ("PyTuple_Pack", "tupleobject.h"),
    ("PyTuple_Check", "tupleobject.h"),
    ("PyTuple_CheckExact", "tupleobject.h"),
    ("PyList_New", "listobject.h"),
    ("PyList_SetItem", "listobject.h"),
    ("PyList_GetItem", "listobject.h"),
    ("PyList_GetItemRef", "listobject.h"),
    ("PyList_Size", "listobject.h"),
    ("PyList_GET_ITEM", "listobject.h"),
    ("PyList_GET_SIZE", "listobject.h"),
    ("PyList_SET_ITEM", "listobject.h"),
    ("PyList_Append", "listobject.h"),
    ("PyList_Check", "listobject.h"),
    ("PyList_CheckExact", "listobject.h"),
    ("PyDict_New", "dictobject.h"),
    ("PyDict_SetItem", "dictobject.h"),
    ("PyDict_SetItemString", "dictobject.h"),
    ("PyDict_GetItem", "dictobject.h"),
    ("PyDict_GetItemString", "dictobject.h"),
    ("PyDict_GetItemWithError", "dictobject.h"),
    ("PyDict_GetItemRef", "dictobject.h"),
    ("PyDict_GetItemStringRef", "dictobject.h"),
    ("PyDict_SetDefaultRef", "dictobject.h"),
    ("PyDict_Pop", "dictobject.h"),
    ("PyDict_PopString", "dictobject.h"),
    ("PyDict_DelItem", "dictobject.h"),
    ("PyDict_DelItemString", "dictobject.h"),
    ("PyDict_Size", "dictobject.h"),
    ("PyDict_Contains", "dictobject.h"),
    ("PyDict_ContainsString", "dictobject.h"),
    ("PyDict_Next", "dictobject.h"),
    ("PyDict_Check", "dictobject.h"),
    ("PyDict_CheckExact", "dictobject.h"),
    ("PyBytes_FromString", "bytesobject.h"),
    ("PyBytes_FromStringAndSize", "bytesobject.h"),
    ("PyBytes_AsString", "bytesobject.h"),
    ("PyBytes_AsStringAndSize", "bytesobject.h"),
    ("PyBytes_AS_STRING", "bytesobject.h"),
    ("PyBytes_Size", "bytesobject.h"),
    ("PyBytes_GET_SIZE", "bytesobject.h"),
    ("PyBytes_Check", "bytesobject.h"),
    ("PyBytes_CheckExact", "bytesobject.h"),
    ("PyImport_ImportModule", "import.h"),
    ("PyCapsule_New", "pycapsule.h"),
    ("PyCapsule_GetPointer", "pycapsule.h"),
    ("PyCapsule_GetName", "pycapsule.h"),
    ("PyCapsule_GetContext", "pycapsule.h"),
    ("PyCapsule_IsValid", "pycapsule.h"),
    ("PyCapsule_CheckExact", "pycapsule.h"),
    ("PyCapsule_SetContext", "pycapsule.h"),
    ("PyCapsule_SetName", "pycapsule.h"),
    ("PyCapsule_SetPointer", "pycapsule.h"),
    ("PyCapsule_GetDestructor", "pycapsule.h"),
    ("PyCapsule_SetDestructor", "pycapsule.h"),
    ("PyCapsule_Import", "pycapsule.h"),
    ("PyMemoryView_FromObject", "memoryobject.h"),
    ("PyMemoryView_FromMemory", "memoryobject.h"),
    ("PyMemoryView_Check", "memoryobject.h"),
    ("PyMemoryView_GET_BUFFER", "memoryobject.h"),
    ("PyMemoryView_GET_BASE", "memoryobject.h"),
    ("Py_IsInitialized", "pylifecycle.h"),
    ("PyGILState_Ensure", "pystate.h"),
    ("PyGILState_Release", "pystate.h"),
    ("PyGILState_Check", "pystate.h"),
    ("PyArray_API", "numpy/arrayobject.h"),
    ("PyDimMem_NEW", "numpy/arrayobject.h"),
    ("PyDimMem_FREE", "numpy/arrayobject.h"),
    ("PyDimMem_RENEW", "numpy/arrayobject.h"),
    ("PyArray_Type", "numpy/arrayobject.h"),
    ("PyArrayDescr_Type", "numpy/arrayobject.h"),
)

_NATIVE_CAPI_HEADER_PREFIXES = (
    ("PyArray_", "numpy/arrayobject.h"),
    ("PyDimMem_", "numpy/arrayobject.h"),
    ("PyDataMem_", "numpy/arrayobject.h"),
    ("PyDataType_", "numpy/arrayobject.h"),
    ("PyTypeNum_", "numpy/arrayobject.h"),
    ("PyUFunc_", "numpy/ufuncobject.h"),
)

_NATIVE_CAPI_IMPLEMENTED_SYMBOLS = (
    "Py_Is",
    "Py_IsNone",
    "Py_IsTrue",
    "Py_IsFalse",
    "Py_PRINT_RAW",
    "PyObject_Print",
    "PyLong_FromDouble",
    "PyErr_Print",
    "PyErr_CheckSignals",
    "PyUnicode_AsUTF8String",
    "PyUnicode_AsASCIIString",
    "PyUnicode_AsEncodedString",
    "PyUnicode_FromKindAndData",
    "PyUnicode_FromOrdinal",
    "PyUnicode_AsUCS4",
    "PyUnicode_AsUCS4Copy",
    "PyUnicode_Tailmatch",
    "PyUnicode_Find",
    "PyUnicode_ReadChar",
    "PyUnicode_FindChar",
    "PyUnicode_Count",
    "PyUnicode_Replace",
    "PyUnicode_Substring",
    "PyUnicode_Contains",
    "PyUnicode_Concat",
    "Py_UCS1",
    "Py_UCS2",
    "PyUnicode_1BYTE_KIND",
    "PyUnicode_2BYTE_KIND",
    "PyUnicode_4BYTE_KIND",
    "Py_REFCNT",
    "Py_SET_REFCNT",
    "PyMapping_Size",
    "PyMapping_Length",
    "PyMapping_Keys",
    "PyMapping_Values",
    "PyMapping_Items",
    "PyObject_LengthHint",
    "PyObject_SelfIter",
    "PyIter_NextItem",
    "PySequence_SetItem",
    "PySequence_Concat",
    "PySequence_Repeat",
    "PySequence_InPlaceConcat",
    "PySequence_InPlaceRepeat",
    "PyLong_AsDouble",
    "PyFloat_AS_DOUBLE",
    "PyNumber_Check",
    "PyNumber_Long",
    "PyNumber_Float",
    "PyNumber_And",
    "PyNumber_Or",
    "PyNumber_Xor",
    "PyNumber_Invert",
    "PyNumber_Lshift",
    "PyNumber_Rshift",
    "PySet_New",
    "PySet_Add",
    "PySet_Contains",
    "PySet_Discard",
    "PySet_Size",
    "PySet_GET_SIZE",
    "PySet_Check",
    "PySet_CheckExact",
    "PyAnySet_Check",
    "PyAnySet_CheckExact",
    "PyList_AsTuple",
    "PyDict_Keys",
    "PyDict_Values",
    "PyDict_Items",
    "Py_Initialize",
    "Py_INCREF",
    "Py_DECREF",
    "Py_XINCREF",
    "Py_XDECREF",
    "Py_NewRef",
    "Py_XNewRef",
    "Py_CLEAR",
    "Py_SETREF",
    "Py_XSETREF",
    "Py_None",
    "Py_True",
    "Py_False",
    "Py_NotImplemented",
    "Py_RETURN_NONE",
    "Py_RETURN_TRUE",
    "Py_RETURN_FALSE",
    "Py_RETURN_NOTIMPLEMENTED",
    "Py_UNUSED",
    "PyOS_snprintf",
    "PyOS_vsnprintf",
    "PyMem_Malloc",
    "PyMem_Calloc",
    "PyMem_Realloc",
    "PyMem_Free",
    "PyMem_RawMalloc",
    "PyMem_RawCalloc",
    "PyMem_RawRealloc",
    "PyMem_RawFree",
    "PyMem_FREE",
    "PyObject_Malloc",
    "PyObject_Calloc",
    "PyObject_Realloc",
    "PyObject_Free",
    "PyObject_MALLOC",
    "PyObject_REALLOC",
    "PyObject_FREE",
    "PyObject_Del",
    "PyObject_DEL",
    "PyModule_Create",
    "PyModule_Create2",
    "PyModule_AddObject",
    "PyModule_AddObjectRef",
    "PyModule_Add",
    "PyModule_AddIntConstant",
    "PyModule_AddStringConstant",
    "PyModule_GetDict",
    "PyArg_ParseTuple",
    "PyArg_ParseTupleAndKeywords",
    "Py_BuildValue",
    "PyLong_FromLong",
    "PyLong_FromUnsignedLong",
    "PyLong_AsLong",
    "PyLong_FromLongLong",
    "PyLong_FromUnsignedLongLong",
    "PyLong_FromInt32",
    "PyLong_FromInt64",
    "PyLong_FromUInt32",
    "PyLong_FromUInt64",
    "PyLong_FromVoidPtr",
    "PyLong_FromSsize_t",
    "PyLong_FromSize_t",
    "PyLong_AsLongLong",
    "PyLong_AsInt",
    "PyLong_AsInt32",
    "PyLong_AsInt64",
    "PyLong_AsUInt32",
    "PyLong_AsUInt64",
    "PyLong_AsVoidPtr",
    "PyLong_AsLongAndOverflow",
    "PyLong_AsUnsignedLong",
    "PyLong_AsUnsignedLongLong",
    "PyLong_AsUnsignedLongLongMask",
    "PyLong_AsSsize_t",
    "PyLong_AsSize_t",
    "PyLong_Check",
    "PyLong_CheckExact",
    "PyBool_FromLong",
    "PyBool_Check",
    "PyFloat_FromDouble",
    "PyFloat_AsDouble",
    "PyFloat_Check",
    "PyFloat_CheckExact",
    "Py_complex",
    "PyComplex_FromDoubles",
    "PyComplex_FromCComplex",
    "PyComplex_AsCComplex",
    "PyComplex_RealAsDouble",
    "PyComplex_ImagAsDouble",
    "PyComplex_Check",
    "PyComplex_CheckExact",
    "Py_UCS4",
    "PyUnicode_FromString",
    "PyUnicode_FromStringAndSize",
    "PyUnicode_FromFormat",
    "PyUnicode_FromFormatV",
    "PyUnicode_InternFromString",
    "PyUnicode_FromEncodedObject",
    "PyUnicode_AsUTF8",
    "PyUnicode_AsUTF8AndSize",
    "PyUnicode_Check",
    "PyUnicode_CheckExact",
    "PyUnicode_GetLength",
    "PyUnicode_GET_LENGTH",
    "PyUnicode_Compare",
    "PyUnicode_CompareWithASCIIString",
    "PyUnicode_EqualToUTF8",
    "PyUnicode_EqualToUTF8AndSize",
    "Py_UNICODE_ISSPACE",
    "Py_UNICODE_ISDIGIT",
    "Py_UNICODE_ISDECIMAL",
    "Py_UNICODE_ISNUMERIC",
    "Py_UNICODE_ISLOWER",
    "Py_UNICODE_ISUPPER",
    "Py_UNICODE_ISTITLE",
    "Py_UNICODE_ISALPHA",
    "Py_UNICODE_ISALNUM",
    "PyErr_SetString",
    "PyErr_SetNone",
    "PyErr_SetObject",
    "PyErr_Format",
    "PyErr_FormatV",
    "PyErr_NoMemory",
    "PyErr_SetFromErrno",
    "PyErr_SetFromErrnoWithFilenameObject",
    "PyErr_NewException",
    "PyErr_BadInternalCall",
    "PyErr_WarnEx",
    "PyErr_WarnFormat",
    "PyErr_WriteUnraisable",
    "PyErr_Occurred",
    "PyErr_Clear",
    "PyErr_GivenExceptionMatches",
    "PyErr_ExceptionMatches",
    "PyErr_Fetch",
    "PyErr_Restore",
    "PyExc_BaseException",
    "PyExc_Exception",
    "PyExc_ValueError",
    "PyExc_TypeError",
    "PyExc_RuntimeError",
    "PyExc_KeyError",
    "PyExc_IndexError",
    "PyExc_AttributeError",
    "PyExc_MemoryError",
    "PyExc_OverflowError",
    "PyExc_SystemError",
    "PyExc_NameError",
    "PyExc_NotImplementedError",
    "PyExc_ArithmeticError",
    "PyExc_LookupError",
    "PyExc_OSError",
    "PyExc_IOError",
    "PyExc_AssertionError",
    "PyExc_StopIteration",
    "PyExc_StopAsyncIteration",
    "PyExc_ZeroDivisionError",
    "PyExc_ReferenceError",
    "PyExc_BufferError",
    "PyExc_ImportError",
    "PyExc_ModuleNotFoundError",
    "PyExc_ImportWarning",
    "PyExc_FloatingPointError",
    "PyExc_RecursionError",
    "PyExc_UnicodeDecodeError",
    "PyExc_Warning",
    "PyExc_UserWarning",
    "PyExc_RuntimeWarning",
    "PyExc_DeprecationWarning",
    "PyExc_FutureWarning",
    "PyObject_Call",
    "PyObject_CallObject",
    "PyObject_CallNoArgs",
    "PyObject_CallOneArg",
    "PyObject_Vectorcall",
    "PyObject_VectorcallMethod",
    "PyObject_CallFunction",
    "PyObject_CallMethod",
    "PyObject_CallMethodNoArgs",
    "PyObject_CallMethodOneArg",
    "PyObject_CallFunctionObjArgs",
    "PyObject_GetIter",
    "PyIter_Next",
    "PyIter_Check",
    "PyNumber_Add",
    "PyNumber_Subtract",
    "PyNumber_Multiply",
    "PyNumber_TrueDivide",
    "PyNumber_FloorDivide",
    "PyNumber_Remainder",
    "PyNumber_Power",
    "PyNumber_Negative",
    "PyNumber_Positive",
    "PyNumber_Absolute",
    "PyNumber_Index",
    "PyNumber_AsSsize_t",
    "PyIndex_Check",
    "PyObject_GetAttrString",
    "PyObject_GetAttr",
    "PyObject_GetOptionalAttr",
    "PyObject_GetOptionalAttrString",
    "PyObject_SetAttrString",
    "PyObject_SetAttr",
    "PyObject_HasAttr",
    "PyObject_HasAttrString",
    "PyObject_HasAttrWithError",
    "PyObject_HasAttrStringWithError",
    "PyObject_IsTrue",
    "PyObject_Not",
    "PyObject_Hash",
    "PyCallable_Check",
    "PyObject_Str",
    "PyObject_Repr",
    "PyObject_Bytes",
    "PyObject_Format",
    "PyObject_Type",
    "PyObject_IsInstance",
    "PyObject_RichCompare",
    "PyObject_RichCompareBool",
    "PyObject_GetItem",
    "PyObject_SetItem",
    "PyObject_DelItem",
    "PyObject_Size",
    "PyObject_Length",
    "PyTuple_New",
    "PyTuple_SetItem",
    "PyTuple_GetItem",
    "PyTuple_Size",
    "PyTuple_GET_ITEM",
    "PyTuple_GET_SIZE",
    "PyTuple_SET_ITEM",
    "PyTuple_Pack",
    "PyTuple_Check",
    "PyTuple_CheckExact",
    "PyList_New",
    "PyList_SetItem",
    "PyList_GetItem",
    "PyList_GetItemRef",
    "PyList_Size",
    "PyList_GET_ITEM",
    "PyList_GET_SIZE",
    "PyList_SET_ITEM",
    "PyList_Append",
    "PyList_Check",
    "PyList_CheckExact",
    "PyDict_New",
    "PyDict_SetItem",
    "PyDict_SetItemString",
    "PyDict_GetItem",
    "PyDict_GetItemString",
    "PyDict_GetItemWithError",
    "PyDict_GetItemRef",
    "PyDict_GetItemStringRef",
    "PyDict_SetDefaultRef",
    "PyDict_Pop",
    "PyDict_PopString",
    "PyDict_DelItem",
    "PyDict_DelItemString",
    "PyDict_Size",
    "PyDict_Contains",
    "PyDict_ContainsString",
    "PyDict_Next",
    "PyDict_Check",
    "PyDict_CheckExact",
    "PyBytes_FromString",
    "PyBytes_FromStringAndSize",
    "PyBytes_AsString",
    "PyBytes_AsStringAndSize",
    "PyBytes_AS_STRING",
    "PyBytes_Size",
    "PyBytes_GET_SIZE",
    "PyBytes_Check",
    "PyBytes_CheckExact",
    "PyCapsule_New",
    "PyCapsule_GetPointer",
    "PyCapsule_GetName",
    "PyCapsule_GetContext",
    "PyCapsule_IsValid",
    "PyCapsule_CheckExact",
    "PyCapsule_SetContext",
    "PyCapsule_SetName",
    "PyCapsule_SetPointer",
    "PyCapsule_GetDestructor",
    "PyCapsule_SetDestructor",
    "PyCapsule_Import",
    "PyObject_GetBuffer",
    "PyObject_CheckBuffer",
    "PyBuffer_Release",
    "PyMemoryView_FromObject",
    "PyMemoryView_FromMemory",
    "PyMemoryView_Check",
    "PyMemoryView_GET_BUFFER",
    "PyMemoryView_GET_BASE",
    "Py_IsInitialized",
    "PyGILState_Ensure",
    "PyGILState_Release",
    "PyGILState_Check",
    "PyImport_ImportModule",
    "PySequence_Check",
    "PyMapping_Check",
    "PyMapping_GetItemString",
    "PyMapping_SetItemString",
    "PyMapping_HasKey",
    "PyMapping_HasKeyString",
    "PyMapping_GetOptionalItem",
    "PyMapping_GetOptionalItemString",
    "PyMapping_HasKeyWithError",
    "PyMapping_HasKeyStringWithError",
    "PySequence_Size",
    "PySequence_Length",
    "PySequence_GetItem",
    "PySequence_Contains",
    "PySequence_Fast",
    "PySequence_Fast_GET_SIZE",
    "PySequence_Fast_ITEMS",
    "PySequence_Fast_GET_ITEM",
    "PySequence_List",
    "PySequence_Tuple",
    "PyArray_API",
    "PyArray_malloc",
    "PyArray_free",
    "PyArray_realloc",
    "PyDimMem_NEW",
    "PyDimMem_FREE",
    "PyDimMem_RENEW",
    "PyArray_Type",
    "PyArrayDescr_Type",
    "PyArray_DescrCheck",
    "PyArray_DescrFromType",
    "PyArray_TypeObjectFromType",
    "PyArray_DescrNewFromType",
    "PyArray_DescrNew",
    "PyArray_DescrNewByteorder",
    "PyArray_CanCastSafely",
    "PyArray_CanCastTo",
    "PyArray_CanCastTypeTo",
    "PyArray_CanCastArrayTo",
    "PyArray_CastingConverter",
    "PyArray_Zero",
    "PyArray_One",
    "PyArray_ObjectType",
    "PyArray_DescrFromObject",
    "PyArray_Size",
    "PyArray_DescrFromScalar",
    "PyArray_DescrFromTypeObject",
    "PyArray_Scalar",
    "PyArray_ScalarAsCtype",
    "PyArray_FromScalar",
    "PyArray_CastScalarToCtype",
    "PyArray_CastScalarDirect",
    "PyArray_Pack",
    "PyArray_CastToType",
    "PyArray_Cast",
    "PyArray_FillWithScalar",
    "PyArray_ToList",
    "PyArray_ToString",
    "PyArray_Byteswap",
    "PyArray_FromString",
    "PyArray_FromBuffer",
    "PyArray_FromIter",
    "PyArray_Converter",
    "PyArray_IterNew",
    "PyArray_BroadcastToShape",
    "PyArray_Broadcast",
    "PyArray_Concatenate",
    "PyArray_Arange",
    "PyArray_ArangeObj",
    "PyArray_LexSort",
    "PyArray_InnerProduct",
    "PyArray_MatrixProduct",
    "PyArray_MatrixProduct2",
    "PyArray_CountNonzero",
    "PyArray_MinScalarType",
    "PyArray_CreateSortedStridePerm",
    "PyArray_RemoveAxesInPlace",
    "PyArray_DebugPrint",
    "PyArray_EinsteinSum",
    "PyArray_Partition",
    "PyArray_ArgPartition",
    "PyArray_CheckAnyScalarExact",
    "PyArray_Correlate",
    "PyArray_Correlate2",
    "PyArray_RemoveSmallest",
    "PyArray_IterAllButAxis",
    "PyArray_PyIntAsInt",
    "PyArray_PyIntAsIntp",
    "PyArray_PythonPyIntFromInt",
    "PyArray_IntpFromSequence",
    "PyArray_IntpConverter",
    "PyArray_BufferConverter",
    "PyArray_OptionalIntpConverter",
    "PyArray_Free",
    "PyArray_AsCArray",
    "PyArray_FailUnlessWriteable",
    "PyArray_CheckStrides",
    "PyArray_GetPriority",
    "PyArray_ITER_RESET",
    "PyArray_ITER_NEXT",
    "PyArray_ITER_DATA",
    "PyArray_ITER_NOTDONE",
    "PyArray_CopyObject",
    "PyArray_Resize",
    "PyArray_NewLikeArray",
    "PyArray_View",
    "PyArray_Squeeze",
    "PyArray_Transpose",
    "PyArray_Ravel",
    "PyArray_Flatten",
    "PyArray_TakeFrom",
    "PyArray_PutTo",
    "PyArray_PutMask",
    "PyArray_Repeat",
    "PyArray_Choose",
    "PyArray_Sort",
    "PyArray_ArgSort",
    "PyArray_SearchSorted",
    "PyArray_Nonzero",
    "PyArray_Where",
    "PyArray_Compress",
    "PyArray_Diagonal",
    "PyArray_Trace",
    "PyArray_Clip",
    "PyArray_Conjugate",
    "PyArray_Std",
    "PyArray_Round",
    "PyArray_EquivTypenums",
    "PyArray_ScalarKind",
    "PyArray_CanCoerceScalar",
    "PyArray_CanCastScalar",
    "PyArray_PromoteTypes",
    "PyArray_ResultType",
    "PyArray_ConvertToCommonType",
    "PyArray_IntTupleFromIntp",
    "PyArray_ClipmodeConverter",
    "PyArray_ConvertClipmodeSequence",
    "PyArray_OutputConverter",
    "PyArray_SearchsideConverter",
    "PyArray_OrderConverter",
    "PyArray_BoolConverter",
    "PyArray_OptionalBoolConverter",
    "PyArray_AxisConverter",
    "PyArray_GetNDArrayCVersion",
    "PyArray_ByteorderConverter",
    "PyArray_SortkindConverter",
    "PyArray_SelectkindConverter",
    "PyArray_OverflowMultiplyList",
    "PyArray_GetEndianness",
    "PyArray_GetNDArrayCFeatureVersion",
    "PyArray_CheckAxis",
    "PyArray_DescrAlignConverter",
    "PyArray_DescrAlignConverter2",
    "PyArray_DescrConverter",
    "PyArray_DescrConverter2",
    "PyArray_Sum",
    "PyArray_CumSum",
    "PyArray_Prod",
    "PyArray_CumProd",
    "PyArray_Max",
    "PyArray_Min",
    "PyArray_Ptp",
    "PyArray_Mean",
    "PyArray_Any",
    "PyArray_All",
    "PyArray_ArgMax",
    "PyArray_ArgMin",
    "PyArray_Reshape",
    "PyArray_Newshape",
    "PyArray_SwapAxes",
    "PyArray_CheckFromAny",
    "PyArray_FromArray",
    "PyArray_MultiplyList",
    "PyArray_MultiplyIntList",
    "PyArray_GetPtr",
    "PyArray_ElementStrides",
    "PyArray_ValidType",
    "PyArray_Item_INCREF",
    "PyArray_Item_XDECREF",
    "PyArray_NewCopy",
    "PyArray_INCREF",
    "PyArray_XDECREF",
    "PyArray_FromAny",
    "PyArray_SimpleNew",
    "PyArray_SimpleNewFromData",
    "PyArray_NDIM",
    "PyArray_DIMS",
    "PyArray_STRIDES",
    "PyArray_DATA",
    "PyArray_DESCR",
    "PyArray_DTYPE",
    "PyArray_TYPE",
    "PyDataType_TYPE",
    "PyDataType_KIND",
    "PyDataType_ELSIZE",
    "PyDataType_ALIGNMENT",
    "PyTypeNum_ISBOOL",
    "PyTypeNum_ISUNSIGNED",
    "PyTypeNum_ISSIGNED",
    "PyTypeNum_ISINTEGER",
    "PyTypeNum_ISFLOAT",
    "PyTypeNum_ISNUMBER",
    "PyTypeNum_ISSTRING",
    "PyTypeNum_ISCOMPLEX",
    "PyTypeNum_ISFLEXIBLE",
    "PyTypeNum_ISOBJECT",
    "PyDataType_ISBOOL",
    "PyDataType_ISUNSIGNED",
    "PyDataType_ISSIGNED",
    "PyDataType_ISINTEGER",
    "PyDataType_ISFLOAT",
    "PyDataType_ISNUMBER",
    "PyDataType_ISSTRING",
    "PyDataType_ISCOMPLEX",
    "PyDataType_ISFLEXIBLE",
    "PyDataType_ISOBJECT",
    "PyArray_GETITEM",
    "PyArray_SETITEM",
    "PyArray_NBYTES",
    "PyArray_FILLWBYTE",
    "PyArray_EquivByteorders",
    "PyArray_SHAPE",
    "PyArray_FLAGS",
    "PyArray_CompareLists",
    "PyArray_Empty",
    "PyArray_Zeros",
    "PyArray_EMPTY",
    "PyArray_ZEROS",
    "PyArray_EquivTypes",
    "PyArray_EquivArrTypes",
    "PyArray_NewFromDescr",
    "PyArray_New",
    "PyArray_MultiIterNew",
    "PyArray_MultiIterFromObjects",
    "PyArray_SimpleNewFromDescr",
    "PyArray_BASE",
    "PyArray_SetBaseObject",
    "PyArray_SetUpdateIfCopyBase",
    "PyArray_SetWritebackIfCopyBase",
    "PyArray_ResolveWritebackIfCopy",
    "PyArray_DiscardWritebackIfCopy",
    "PyDataMem_NEW",
    "PyDataMem_FREE",
    "PyDataMem_RENEW",
    "PyDataMem_NEW_ZEROED",
    "PyDataMem_GetHandler",
    "PyDataMem_UserNEW",
    "PyDataMem_UserFREE",
    "PyDataMem_UserRENEW",
    "PyDataMem_UserNEW_ZEROED",
    "PyArray_Return",
    "PyArray_ENABLEFLAGS",
    "PyArray_CLEARFLAGS",
    "PyArray_UpdateFlags",
    "PyArray_CopyInto",
    "PyArray_CopyAnyInto",
    "PyArray_ToScalar",
    "PyArray_Copy",
    "PyArray_EnsureArray",
    "PyArray_EnsureAnyArray",
    "PyArray_SAMESHAPE",
    "PyArray_CHKFLAGS",
    "PyArray_FROM_O",
    "PyArray_FROM_OF",
    "PyArray_FROM_OT",
    "PyArray_FROM_OTF",
    "PyArray_FROMANY",
    "PyArray_ContiguousFromAny",
    "PyArray_FromObject",
    "PyArray_ContiguousFromObject",
    "PyArray_CopyFromObject",
    "PyArray_ISCONTIGUOUS",
    "PyArray_IS_C_CONTIGUOUS",
    "PyArray_ISALIGNED",
    "PyArray_ISWRITEABLE",
    "PyArray_ISCARRAY",
    "PyArray_IS_F_CONTIGUOUS",
    "PyArray_ISONESEGMENT",
    "PyArray_ISFORTRAN",
    "PyArray_FORTRAN_IF",
    "PyArray_ISNBO",
    "PyArray_IsNativeByteOrder",
    "PyArray_ISNOTSWAPPED",
    "PyArray_ISBYTESWAPPED",
    "PyArray_FLAGSWAP",
    "PyArray_ISCARRAY_RO",
    "PyArray_ISFARRAY",
    "PyArray_ISFARRAY_RO",
    "PyArray_ISBEHAVED",
    "PyArray_ISBEHAVED_RO",
    "PyDataType_ISNOTSWAPPED",
    "PyDataType_ISBYTESWAPPED",
    "PyArray_ISVARIABLE",
    "PyArray_SAFEALIGNEDCOPY",
    "PyArray_ISBOOL",
    "PyArray_ISUNSIGNED",
    "PyArray_ISSIGNED",
    "PyArray_ISINTEGER",
    "PyArray_ISFLOAT",
    "PyArray_ISNUMBER",
    "PyArray_ISSTRING",
    "PyArray_ISCOMPLEX",
    "PyArray_ISFLEXIBLE",
    "PyArray_ISOBJECT",
    "PyArray_DIM",
    "PyArray_BYTES",
    "PyArray_SIZE",
    "PyArray_ITEMSIZE",
    "PyArray_Check",
    "PyArray_CheckExact",
    "PyArray_STRIDE",
    "PyArray_GETPTR1",
    "PyArray_GETPTR2",
    "PyArray_GETPTR3",
    "PyArray_GETPTR4",
    "PyUFunc_API",
    "PyUFunc_FromFuncAndData",
)


def _native_known_capi_header(symbol: str):
    i = 0
    while i < len(_NATIVE_CAPI_HEADER_BY_SYMBOL):
        row = _NATIVE_CAPI_HEADER_BY_SYMBOL[i]
        if symbol == row[0]:
            return row[1]
        i += 1
    i = 0
    while i < len(_NATIVE_CAPI_HEADER_PREFIXES):
        row = _NATIVE_CAPI_HEADER_PREFIXES[i]
        if symbol.startswith(row[0]):
            return row[1]
        i += 1
    return None


def _native_capi_implemented(symbol: str) -> bool:
    return _native_list_contains(_NATIVE_CAPI_IMPLEMENTED_SYMBOLS, symbol)


def _native_list_contains(items, value) -> bool:
    i = 0
    while i < len(items):
        if items[i] == value:
            return True
        i += 1
    return False


def _native_numpy_capi_family(symbol: str):
    if symbol == "PyUFunc_API" or symbol.startswith("PyUFunc_"):
        return "ufunc_api"
    if (
        symbol == "PyArray_API"
        or symbol == "PyArray_malloc"
        or symbol == "PyArray_free"
        or symbol == "PyArray_realloc"
        or symbol == "PyArray_Type"
        or symbol == "PyArrayDescr_Type"
        or symbol.startswith("PyArray_")
        or symbol.startswith("PyDimMem_")
        or symbol.startswith("PyDataMem_")
        or symbol.startswith("PyDataType_")
        or symbol.startswith("PyTypeNum_")
    ):
        return "array_api"
    return None


def _native_numpy_capi_slot(symbol: str):
    if symbol == "PyArray_Type":
        return 0
    if symbol == "PyArrayDescr_Type":
        return 1
    if symbol == "PyArray_DescrFromType":
        return 2
    if symbol == "PyArray_FromAny":
        return 3
    if symbol == "PyArray_SimpleNew":
        return 4
    if symbol == "PyArray_SimpleNewFromData":
        return 5
    if symbol == "PyArray_NDIM":
        return 6
    if symbol == "PyArray_DIMS":
        return 7
    if symbol == "PyArray_STRIDES":
        return 8
    if symbol == "PyArray_DATA":
        return 9
    if symbol == "PyArray_DESCR":
        return 10
    if symbol == "PyArray_GETITEM":
        return 11
    if symbol == "PyArray_SETITEM":
        return 12
    if symbol == "PyArray_SIZE":
        return 13
    if symbol == "PyArray_ITEMSIZE":
        return 14
    if symbol == "PyArray_Check":
        return 15
    if symbol == "PyArray_CheckExact":
        return 16
    if symbol == "PyArray_FLAGS":
        return 17
    if symbol == "PyArray_CompareLists":
        return 18
    if symbol == "PyArray_Empty":
        return 19
    if symbol == "PyArray_Zeros":
        return 20
    if symbol == "PyArray_EquivTypes":
        return 21
    if symbol == "PyArray_NewFromDescr":
        return 22
    if symbol == "PyArray_New":
        return 172
    if symbol == "PyArray_MultiIterNew":
        return 173
    if symbol == "PyArray_MultiIterFromObjects":
        return 177
    if symbol == "PyArray_BASE":
        return 23
    if symbol == "PyArray_SetBaseObject":
        return 24
    if symbol == "PyArray_SetUpdateIfCopyBase":
        return 152
    if symbol == "PyArray_SetWritebackIfCopyBase":
        return 153
    if symbol == "PyArray_ResolveWritebackIfCopy":
        return 154
    if symbol == "PyArray_DiscardWritebackIfCopy":
        return 155
    if symbol == "PyDataMem_NEW":
        return 156
    if symbol == "PyDataMem_FREE":
        return 157
    if symbol == "PyDataMem_RENEW":
        return 158
    if symbol == "PyDataMem_NEW_ZEROED":
        return 159
    if symbol == "PyDataMem_GetHandler":
        return 160
    if symbol == "PyDataMem_UserNEW":
        return 161
    if symbol == "PyDataMem_UserFREE":
        return 162
    if symbol == "PyDataMem_UserRENEW":
        return 163
    if symbol == "PyDataMem_UserNEW_ZEROED":
        return 164
    if symbol == "PyArray_Return":
        return 25
    if symbol == "PyArray_ENABLEFLAGS":
        return 26
    if symbol == "PyArray_CLEARFLAGS":
        return 27
    if symbol == "PyArray_UpdateFlags":
        return 28
    if symbol == "PyArray_CopyInto":
        return 29
    if symbol == "PyArray_CopyAnyInto":
        return 30
    if symbol == "PyArray_ToScalar":
        return 31
    if symbol == "PyArray_Copy":
        return 32
    if symbol == "PyArray_EnsureArray":
        return 33
    if symbol == "PyArray_EnsureAnyArray":
        return 34
    if symbol == "PyArray_DescrNewFromType":
        return 35
    if symbol == "PyArray_DescrNew":
        return 36
    if symbol == "PyArray_DescrNewByteorder":
        return 37
    if symbol == "PyArray_CanCastSafely":
        return 38
    if symbol == "PyArray_ObjectType":
        return 39
    if symbol == "PyArray_CheckFromAny":
        return 40
    if symbol == "PyArray_FromArray":
        return 41
    if symbol == "PyArray_MultiplyList":
        return 42
    if symbol == "PyArray_MultiplyIntList":
        return 43
    if symbol == "PyArray_GetPtr":
        return 44
    if symbol == "PyArray_ElementStrides":
        return 45
    if symbol == "PyArray_ValidType":
        return 46
    if symbol == "PyArray_Item_INCREF":
        return 47
    if symbol == "PyArray_Item_XDECREF":
        return 48
    if symbol == "PyArray_NewCopy":
        return 49
    if symbol == "PyArray_INCREF":
        return 50
    if symbol == "PyArray_XDECREF":
        return 51
    if symbol == "PyArray_CanCastTo":
        return 52
    if symbol == "PyArray_CanCastTypeTo":
        return 165
    if symbol == "PyArray_CanCastArrayTo":
        return 166
    if symbol == "PyArray_CastingConverter":
        return 168
    if symbol == "PyArray_Zero":
        return 53
    if symbol == "PyArray_One":
        return 54
    if symbol == "PyArray_TypeObjectFromType":
        return 55
    if symbol == "PyArray_DescrFromObject":
        return 56
    if symbol == "PyArray_Size":
        return 57
    if symbol == "PyArray_DescrFromScalar":
        return 58
    if symbol == "PyArray_DescrFromTypeObject":
        return 59
    if symbol == "PyArray_Scalar":
        return 169
    if symbol == "PyArray_ScalarAsCtype":
        return 60
    if symbol == "PyArray_FromScalar":
        return 61
    if symbol == "PyArray_CastScalarToCtype":
        return 62
    if symbol == "PyArray_Pack":
        return 63
    if symbol == "PyArray_CastScalarDirect":
        return 64
    if symbol == "PyArray_CastToType":
        return 65
    if symbol == "PyArray_FillWithScalar":
        return 66
    if symbol == "PyArray_ToList":
        return 67
    if symbol == "PyArray_ToString":
        return 68
    if symbol == "PyArray_Byteswap":
        return 69
    if symbol == "PyArray_FromString":
        return 70
    if symbol == "PyArray_FromBuffer":
        return 71
    if symbol == "PyArray_FromIter":
        return 72
    if symbol == "PyArray_Converter":
        return 144
    if symbol == "PyArray_IterNew":
        return 127
    if symbol == "PyArray_BroadcastToShape":
        return 128
    if symbol == "PyArray_Broadcast":
        return 176
    if symbol == "PyArray_Concatenate":
        return 180
    if symbol == "PyArray_Arange":
        return 181
    if symbol == "PyArray_ArangeObj":
        return 182
    if symbol == "PyArray_LexSort":
        return 183
    if symbol == "PyArray_InnerProduct":
        return 184
    if symbol == "PyArray_MatrixProduct":
        return 185
    if symbol == "PyArray_MatrixProduct2":
        return 188
    if symbol == "PyArray_CountNonzero":
        return 189
    if symbol == "PyArray_MinScalarType":
        return 190
    if symbol == "PyArray_CreateSortedStridePerm":
        return 191
    if symbol == "PyArray_RemoveAxesInPlace":
        return 192
    if symbol == "PyArray_DebugPrint":
        return 193
    if symbol == "PyArray_EinsteinSum":
        return 194
    if symbol == "PyArray_Partition":
        return 195
    if symbol == "PyArray_ArgPartition":
        return 196
    if symbol == "PyArray_CheckAnyScalarExact":
        return 197
    if symbol == "PyArray_Correlate":
        return 186
    if symbol == "PyArray_Correlate2":
        return 187
    if symbol == "PyArray_RemoveSmallest":
        return 178
    if symbol == "PyArray_IterAllButAxis":
        return 129
    if symbol == "PyArray_PyIntAsInt":
        return 130
    if symbol == "PyArray_PyIntAsIntp":
        return 131
    if symbol == "PyArray_PythonPyIntFromInt":
        return 167
    if symbol == "PyArray_IntpFromSequence":
        return 143
    if symbol == "PyArray_IntpConverter":
        return 145
    if symbol == "PyArray_BufferConverter":
        return 179
    if symbol == "PyArray_OptionalIntpConverter":
        return 146
    if symbol == "PyArray_Free":
        return 147
    if symbol == "PyArray_AsCArray":
        return 148
    if symbol == "PyArray_FailUnlessWriteable":
        return 149
    if symbol == "PyArray_CheckStrides":
        return 132
    if symbol == "PyArray_GetPriority":
        return 133
    if symbol == "PyArray_CopyObject":
        return 73
    if symbol == "PyArray_Resize":
        return 74
    if symbol == "PyArray_NewLikeArray":
        return 75
    if symbol == "PyArray_View":
        return 76
    if symbol == "PyArray_Squeeze":
        return 77
    if symbol == "PyArray_Transpose":
        return 78
    if symbol == "PyArray_Ravel":
        return 79
    if symbol == "PyArray_Flatten":
        return 80
    if symbol == "PyArray_TakeFrom":
        return 81
    if symbol == "PyArray_PutTo":
        return 82
    if symbol == "PyArray_PutMask":
        return 83
    if symbol == "PyArray_Repeat":
        return 84
    if symbol == "PyArray_Choose":
        return 85
    if symbol == "PyArray_Sort":
        return 86
    if symbol == "PyArray_ArgSort":
        return 87
    if symbol == "PyArray_SearchSorted":
        return 88
    if symbol == "PyArray_Nonzero":
        return 89
    if symbol == "PyArray_Where":
        return 90
    if symbol == "PyArray_Compress":
        return 91
    if symbol == "PyArray_Diagonal":
        return 92
    if symbol == "PyArray_Trace":
        return 93
    if symbol == "PyArray_Clip":
        return 94
    if symbol == "PyArray_Conjugate":
        return 95
    if symbol == "PyArray_Sum":
        return 96
    if symbol == "PyArray_CumSum":
        return 109
    if symbol == "PyArray_Prod":
        return 97
    if symbol == "PyArray_CumProd":
        return 110
    if symbol == "PyArray_Std":
        return 111
    if symbol == "PyArray_Round":
        return 112
    if symbol == "PyArray_EquivTypenums":
        return 113
    if symbol == "PyArray_ScalarKind":
        return 170
    if symbol == "PyArray_CanCoerceScalar":
        return 114
    if symbol == "PyArray_CanCastScalar":
        return 116
    if symbol == "PyArray_PromoteTypes":
        return 174
    if symbol == "PyArray_ResultType":
        return 175
    if symbol == "PyArray_ConvertToCommonType":
        return 171
    if symbol == "PyArray_IntTupleFromIntp":
        return 117
    if symbol == "PyArray_ClipmodeConverter":
        return 118
    if symbol == "PyArray_ConvertClipmodeSequence":
        return 141
    if symbol == "PyArray_OutputConverter":
        return 119
    if symbol == "PyArray_SearchsideConverter":
        return 120
    if symbol == "PyArray_OrderConverter":
        return 134
    if symbol == "PyArray_BoolConverter":
        return 135
    if symbol == "PyArray_OptionalBoolConverter":
        return 142
    if symbol == "PyArray_AxisConverter":
        return 136
    if symbol == "PyArray_GetNDArrayCVersion":
        return 137
    if symbol == "PyArray_ByteorderConverter":
        return 138
    if symbol == "PyArray_SortkindConverter":
        return 139
    if symbol == "PyArray_SelectkindConverter":
        return 140
    if symbol == "PyArray_OverflowMultiplyList":
        return 121
    if symbol == "PyArray_GetEndianness":
        return 122
    if symbol == "PyArray_GetNDArrayCFeatureVersion":
        return 123
    if symbol == "PyArray_CheckAxis":
        return 124
    if symbol == "PyArray_DescrAlignConverter":
        return 125
    if symbol == "PyArray_DescrAlignConverter2":
        return 126
    if symbol == "PyArray_DescrConverter":
        return 150
    if symbol == "PyArray_DescrConverter2":
        return 151
    if symbol == "PyArray_Max":
        return 98
    if symbol == "PyArray_Min":
        return 99
    if symbol == "PyArray_Ptp":
        return 105
    if symbol == "PyArray_Mean":
        return 106
    if symbol == "PyArray_Any":
        return 107
    if symbol == "PyArray_All":
        return 108
    if symbol == "PyArray_ArgMax":
        return 100
    if symbol == "PyArray_ArgMin":
        return 101
    if symbol == "PyArray_Reshape":
        return 102
    if symbol == "PyArray_Newshape":
        return 103
    if symbol == "PyArray_SwapAxes":
        return 104
    if symbol == "PyArray_DIM":
        return 7
    if symbol == "PyArray_BYTES":
        return 9
    if symbol == "PyUFunc_FromFuncAndData":
        return 0
    return None


def _native_numpy_capi_failure_mode(symbol: str) -> str:
    if symbol == "PyArray_API" or symbol == "PyUFunc_API":
        return "implemented_provider_table"
    if symbol == "PyArray_Type" or symbol == "PyArrayDescr_Type":
        return "implemented_provider_type_object"
    if (
        symbol == "PyArray_TYPE"
        or symbol == "PyArray_malloc"
        or symbol == "PyArray_free"
        or symbol == "PyArray_realloc"
        or symbol == "PyDimMem_NEW"
        or symbol == "PyDimMem_FREE"
        or symbol == "PyDimMem_RENEW"
        or symbol == "PyArray_DTYPE"
        or symbol == "PyDataType_TYPE"
        or symbol == "PyDataType_KIND"
        or symbol == "PyDataType_ELSIZE"
        or symbol == "PyDataType_ALIGNMENT"
        or symbol.startswith("PyTypeNum_")
        or symbol.startswith("PyDataType_IS")
        or symbol == "PyArray_ISBOOL"
        or symbol == "PyArray_ISUNSIGNED"
        or symbol == "PyArray_ISSIGNED"
        or symbol == "PyArray_ISINTEGER"
        or symbol == "PyArray_ISFLOAT"
        or symbol == "PyArray_ISNUMBER"
        or symbol == "PyArray_ISSTRING"
        or symbol == "PyArray_ISCOMPLEX"
        or symbol == "PyArray_ISFLEXIBLE"
        or symbol == "PyArray_ISOBJECT"
        or symbol == "PyArray_NBYTES"
        or symbol == "PyArray_FILLWBYTE"
        or symbol == "PyArray_EquivByteorders"
        or symbol == "PyArray_SHAPE"
        or symbol == "PyArray_EMPTY"
        or symbol == "PyArray_ZEROS"
        or symbol == "PyArray_EquivArrTypes"
        or symbol == "PyArray_SimpleNewFromDescr"
        or symbol == "PyArray_BASE"
        or symbol == "PyArray_DescrCheck"
        or symbol == "PyArray_SAMESHAPE"
        or symbol == "PyArray_DIM"
        or symbol == "PyArray_BYTES"
        or symbol == "PyArray_CHKFLAGS"
        or symbol == "PyArray_FROM_O"
        or symbol == "PyArray_FROM_OF"
        or symbol == "PyArray_FROM_OT"
        or symbol == "PyArray_FROM_OTF"
        or symbol == "PyArray_FROMANY"
        or symbol == "PyArray_ContiguousFromAny"
        or symbol == "PyArray_FromObject"
        or symbol == "PyArray_ContiguousFromObject"
        or symbol == "PyArray_CopyFromObject"
        or symbol == "PyArray_ISCONTIGUOUS"
        or symbol == "PyArray_IS_C_CONTIGUOUS"
        or symbol == "PyArray_ISALIGNED"
        or symbol == "PyArray_ISWRITEABLE"
        or symbol == "PyArray_ISCARRAY"
        or symbol == "PyArray_IS_F_CONTIGUOUS"
        or symbol == "PyArray_ISONESEGMENT"
        or symbol == "PyArray_ISFORTRAN"
        or symbol == "PyArray_FORTRAN_IF"
        or symbol == "PyArray_ISNBO"
        or symbol == "PyArray_IsNativeByteOrder"
        or symbol == "PyArray_ISNOTSWAPPED"
        or symbol == "PyArray_ISBYTESWAPPED"
        or symbol == "PyArray_FLAGSWAP"
        or symbol == "PyArray_ISCARRAY_RO"
        or symbol == "PyArray_ISFARRAY"
        or symbol == "PyArray_ISFARRAY_RO"
        or symbol == "PyArray_ISBEHAVED"
        or symbol == "PyArray_ISBEHAVED_RO"
        or symbol == "PyDataType_ISNOTSWAPPED"
        or symbol == "PyDataType_ISBYTESWAPPED"
        or symbol == "PyArray_ISVARIABLE"
        or symbol == "PyArray_SAFEALIGNEDCOPY"
        or symbol == "PyArray_STRIDE"
        or symbol == "PyArray_GETPTR1"
        or symbol == "PyArray_GETPTR2"
        or symbol == "PyArray_GETPTR3"
        or symbol == "PyArray_GETPTR4"
        or symbol == "PyArray_Cast"
        or symbol == "PyArray_ITER_RESET"
        or symbol == "PyArray_ITER_NEXT"
        or symbol == "PyArray_ITER_DATA"
        or symbol == "PyArray_ITER_NOTDONE"
    ):
        return "implemented_header_macro"
    if (
        symbol == "PyArray_DescrFromType"
        or symbol == "PyArray_FromAny"
        or symbol == "PyArray_SimpleNew"
        or symbol == "PyArray_SimpleNewFromData"
        or symbol == "PyArray_GETITEM"
        or symbol == "PyArray_SETITEM"
        or symbol == "PyArray_NDIM"
        or symbol == "PyArray_DIMS"
        or symbol == "PyArray_STRIDES"
        or symbol == "PyArray_DATA"
        or symbol == "PyArray_DESCR"
        or symbol == "PyArray_FLAGS"
        or symbol == "PyArray_CompareLists"
        or symbol == "PyArray_Empty"
        or symbol == "PyArray_Zeros"
        or symbol == "PyArray_EquivTypes"
        or symbol == "PyArray_NewFromDescr"
        or symbol == "PyArray_New"
        or symbol == "PyArray_MultiIterNew"
        or symbol == "PyArray_MultiIterFromObjects"
        or symbol == "PyArray_SetBaseObject"
        or symbol == "PyArray_SetUpdateIfCopyBase"
        or symbol == "PyArray_SetWritebackIfCopyBase"
        or symbol == "PyArray_ResolveWritebackIfCopy"
        or symbol == "PyArray_DiscardWritebackIfCopy"
        or symbol == "PyDataMem_NEW"
        or symbol == "PyDataMem_FREE"
        or symbol == "PyDataMem_RENEW"
        or symbol == "PyDataMem_NEW_ZEROED"
        or symbol == "PyDataMem_GetHandler"
        or symbol == "PyDataMem_UserNEW"
        or symbol == "PyDataMem_UserFREE"
        or symbol == "PyDataMem_UserRENEW"
        or symbol == "PyDataMem_UserNEW_ZEROED"
        or symbol == "PyArray_Return"
        or symbol == "PyArray_ENABLEFLAGS"
        or symbol == "PyArray_CLEARFLAGS"
        or symbol == "PyArray_UpdateFlags"
        or symbol == "PyArray_CopyInto"
        or symbol == "PyArray_CopyAnyInto"
        or symbol == "PyArray_ToScalar"
        or symbol == "PyArray_Copy"
        or symbol == "PyArray_EnsureArray"
        or symbol == "PyArray_EnsureAnyArray"
        or symbol == "PyArray_DescrNewFromType"
        or symbol == "PyArray_DescrNew"
        or symbol == "PyArray_DescrNewByteorder"
        or symbol == "PyArray_CanCastSafely"
        or symbol == "PyArray_ObjectType"
        or symbol == "PyArray_CheckFromAny"
        or symbol == "PyArray_FromArray"
        or symbol == "PyArray_MultiplyList"
        or symbol == "PyArray_MultiplyIntList"
        or symbol == "PyArray_GetPtr"
        or symbol == "PyArray_ElementStrides"
        or symbol == "PyArray_ValidType"
        or symbol == "PyArray_Item_INCREF"
        or symbol == "PyArray_Item_XDECREF"
        or symbol == "PyArray_NewCopy"
        or symbol == "PyArray_INCREF"
        or symbol == "PyArray_XDECREF"
        or symbol == "PyArray_CanCastTo"
        or symbol == "PyArray_CanCastTypeTo"
        or symbol == "PyArray_CanCastArrayTo"
        or symbol == "PyArray_CastingConverter"
        or symbol == "PyArray_Zero"
        or symbol == "PyArray_One"
        or symbol == "PyArray_TypeObjectFromType"
        or symbol == "PyArray_DescrFromObject"
        or symbol == "PyArray_Size"
        or symbol == "PyArray_DescrFromScalar"
        or symbol == "PyArray_DescrFromTypeObject"
        or symbol == "PyArray_Scalar"
        or symbol == "PyArray_ScalarAsCtype"
        or symbol == "PyArray_FromScalar"
        or symbol == "PyArray_CastScalarToCtype"
        or symbol == "PyArray_CastScalarDirect"
        or symbol == "PyArray_Pack"
        or symbol == "PyArray_CastToType"
        or symbol == "PyArray_FillWithScalar"
        or symbol == "PyArray_ToList"
        or symbol == "PyArray_ToString"
        or symbol == "PyArray_Byteswap"
        or symbol == "PyArray_FromString"
        or symbol == "PyArray_FromBuffer"
        or symbol == "PyArray_FromIter"
        or symbol == "PyArray_Converter"
        or symbol == "PyArray_IterNew"
        or symbol == "PyArray_BroadcastToShape"
        or symbol == "PyArray_Broadcast"
        or symbol == "PyArray_Concatenate"
        or symbol == "PyArray_Arange"
        or symbol == "PyArray_ArangeObj"
        or symbol == "PyArray_LexSort"
        or symbol == "PyArray_InnerProduct"
        or symbol == "PyArray_MatrixProduct"
        or symbol == "PyArray_MatrixProduct2"
        or symbol == "PyArray_CountNonzero"
        or symbol == "PyArray_MinScalarType"
        or symbol == "PyArray_CreateSortedStridePerm"
        or symbol == "PyArray_RemoveAxesInPlace"
        or symbol == "PyArray_DebugPrint"
        or symbol == "PyArray_EinsteinSum"
        or symbol == "PyArray_Partition"
        or symbol == "PyArray_ArgPartition"
        or symbol == "PyArray_CheckAnyScalarExact"
        or symbol == "PyArray_Correlate"
        or symbol == "PyArray_Correlate2"
        or symbol == "PyArray_RemoveSmallest"
        or symbol == "PyArray_IterAllButAxis"
        or symbol == "PyArray_PyIntAsInt"
        or symbol == "PyArray_PyIntAsIntp"
        or symbol == "PyArray_PythonPyIntFromInt"
        or symbol == "PyArray_IntpFromSequence"
        or symbol == "PyArray_IntpConverter"
        or symbol == "PyArray_BufferConverter"
        or symbol == "PyArray_OptionalIntpConverter"
        or symbol == "PyArray_Free"
        or symbol == "PyArray_AsCArray"
        or symbol == "PyArray_FailUnlessWriteable"
        or symbol == "PyArray_CheckStrides"
        or symbol == "PyArray_GetPriority"
        or symbol == "PyArray_CopyObject"
        or symbol == "PyArray_Resize"
        or symbol == "PyArray_NewLikeArray"
        or symbol == "PyArray_View"
        or symbol == "PyArray_Squeeze"
        or symbol == "PyArray_Transpose"
        or symbol == "PyArray_Ravel"
        or symbol == "PyArray_Flatten"
        or symbol == "PyArray_TakeFrom"
        or symbol == "PyArray_PutTo"
        or symbol == "PyArray_PutMask"
        or symbol == "PyArray_Repeat"
        or symbol == "PyArray_Choose"
        or symbol == "PyArray_Sort"
        or symbol == "PyArray_ArgSort"
        or symbol == "PyArray_SearchSorted"
        or symbol == "PyArray_Nonzero"
        or symbol == "PyArray_Where"
        or symbol == "PyArray_Compress"
        or symbol == "PyArray_Diagonal"
        or symbol == "PyArray_Trace"
        or symbol == "PyArray_Clip"
        or symbol == "PyArray_Conjugate"
        or symbol == "PyArray_Std"
        or symbol == "PyArray_Round"
        or symbol == "PyArray_EquivTypenums"
        or symbol == "PyArray_ScalarKind"
        or symbol == "PyArray_CanCoerceScalar"
        or symbol == "PyArray_CanCastScalar"
        or symbol == "PyArray_PromoteTypes"
        or symbol == "PyArray_ResultType"
        or symbol == "PyArray_ConvertToCommonType"
        or symbol == "PyArray_IntTupleFromIntp"
        or symbol == "PyArray_ClipmodeConverter"
        or symbol == "PyArray_ConvertClipmodeSequence"
        or symbol == "PyArray_OutputConverter"
        or symbol == "PyArray_SearchsideConverter"
        or symbol == "PyArray_OrderConverter"
        or symbol == "PyArray_BoolConverter"
        or symbol == "PyArray_OptionalBoolConverter"
        or symbol == "PyArray_AxisConverter"
        or symbol == "PyArray_GetNDArrayCVersion"
        or symbol == "PyArray_ByteorderConverter"
        or symbol == "PyArray_SortkindConverter"
        or symbol == "PyArray_SelectkindConverter"
        or symbol == "PyArray_OverflowMultiplyList"
        or symbol == "PyArray_GetEndianness"
        or symbol == "PyArray_GetNDArrayCFeatureVersion"
        or symbol == "PyArray_CheckAxis"
        or symbol == "PyArray_DescrAlignConverter"
        or symbol == "PyArray_DescrAlignConverter2"
        or symbol == "PyArray_DescrConverter"
        or symbol == "PyArray_DescrConverter2"
        or symbol == "PyArray_Sum"
        or symbol == "PyArray_CumSum"
        or symbol == "PyArray_Prod"
        or symbol == "PyArray_CumProd"
        or symbol == "PyArray_Max"
        or symbol == "PyArray_Min"
        or symbol == "PyArray_Ptp"
        or symbol == "PyArray_Mean"
        or symbol == "PyArray_Any"
        or symbol == "PyArray_All"
        or symbol == "PyArray_ArgMax"
        or symbol == "PyArray_ArgMin"
        or symbol == "PyArray_Reshape"
        or symbol == "PyArray_Newshape"
        or symbol == "PyArray_SwapAxes"
        or symbol == "PyArray_SIZE"
        or symbol == "PyArray_ITEMSIZE"
        or symbol == "PyArray_Check"
        or symbol == "PyArray_CheckExact"
        or symbol == "PyUFunc_FromFuncAndData"
    ):
        return "implemented_provider_slot"
    return "unsupported_stub"


def _native_numpy_capi_status_json(symbol: str) -> str:
    family = _native_numpy_capi_family(symbol)
    if family is None:
        return "null"
    table = "_UFUNC_API" if family == "ufunc_api" else "_ARRAY_API"
    slot = _native_numpy_capi_slot(symbol)
    out = "{"
    out += '"capability": "numpy_capi"'
    out += ', "failure_mode": ' + _json_str(_native_numpy_capi_failure_mode(symbol))
    out += ', "implemented": ' + (
        "true" if _native_capi_implemented(symbol) else "false"
    )
    out += ', "provider_shape": ' + _json_str(family)
    out += ', "slot": ' + ("null" if slot is None else str(slot))
    out += ', "symbol": ' + _json_str(symbol)
    out += ', "table": ' + _json_str(table)
    out += "}"
    return out


def _native_extension_abi_json(
    symbols,
    provider: str,
    expected_abi,
    actual_abi,
    abi_mode: str,
    include_dir,
    require_capsule: bool,
    require_buffer: bool,
    require_memoryview: bool,
    require_numpy_capi: bool,
) -> str:
    requested = []

    def add_symbol(name: str) -> None:
        if not _native_list_contains(requested, name):
            requested.append(name)

    i = 0
    while i < len(symbols):
        add_symbol(symbols[i])
        i += 1
    if require_capsule:
        add_symbol("PyCapsule_New")
        add_symbol("PyCapsule_GetPointer")
        add_symbol("PyCapsule_GetName")
        add_symbol("PyCapsule_GetContext")
        add_symbol("PyCapsule_IsValid")
        add_symbol("PyCapsule_CheckExact")
        add_symbol("PyCapsule_SetContext")
        add_symbol("PyCapsule_SetName")
        add_symbol("PyCapsule_SetPointer")
        add_symbol("PyCapsule_GetDestructor")
        add_symbol("PyCapsule_SetDestructor")
        add_symbol("PyCapsule_Import")
    if require_buffer:
        add_symbol("PyObject_GetBuffer")
        add_symbol("PyObject_CheckBuffer")
        add_symbol("PyBuffer_Release")
    if require_memoryview:
        add_symbol("PyMemoryView_FromObject")
        add_symbol("PyMemoryView_FromMemory")
        add_symbol("PyMemoryView_Check")
        add_symbol("PyMemoryView_GET_BUFFER")
        add_symbol("PyMemoryView_GET_BASE")
    if require_numpy_capi:
        add_symbol("PyArray_API")
        add_symbol("PyArray_malloc")
        add_symbol("PyArray_free")
        add_symbol("PyArray_realloc")
        add_symbol("PyDimMem_NEW")
        add_symbol("PyDimMem_FREE")
        add_symbol("PyDimMem_RENEW")
        add_symbol("PyArray_Type")
        add_symbol("PyArrayDescr_Type")
        add_symbol("PyArray_DescrCheck")
        add_symbol("PyArray_DescrFromType")
        add_symbol("PyArray_TypeObjectFromType")
        add_symbol("PyArray_DescrNewFromType")
        add_symbol("PyArray_DescrNew")
        add_symbol("PyArray_DescrNewByteorder")
        add_symbol("PyArray_CanCastSafely")
        add_symbol("PyArray_CanCastTo")
        add_symbol("PyArray_CanCastTypeTo")
        add_symbol("PyArray_CanCastArrayTo")
        add_symbol("PyArray_CastingConverter")
        add_symbol("PyArray_Zero")
        add_symbol("PyArray_One")
        add_symbol("PyArray_ObjectType")
        add_symbol("PyArray_DescrFromObject")
        add_symbol("PyArray_Size")
        add_symbol("PyArray_DescrFromScalar")
        add_symbol("PyArray_DescrFromTypeObject")
        add_symbol("PyArray_Scalar")
        add_symbol("PyArray_ScalarAsCtype")
        add_symbol("PyArray_FromScalar")
        add_symbol("PyArray_CastScalarToCtype")
        add_symbol("PyArray_CastScalarDirect")
        add_symbol("PyArray_Pack")
        add_symbol("PyArray_CastToType")
        add_symbol("PyArray_Cast")
        add_symbol("PyArray_FillWithScalar")
        add_symbol("PyArray_ToList")
        add_symbol("PyArray_ToString")
        add_symbol("PyArray_Byteswap")
        add_symbol("PyArray_FromString")
        add_symbol("PyArray_FromBuffer")
        add_symbol("PyArray_FromIter")
        add_symbol("PyArray_Converter")
        add_symbol("PyArray_IterNew")
        add_symbol("PyArray_BroadcastToShape")
        add_symbol("PyArray_Broadcast")
        add_symbol("PyArray_Concatenate")
        add_symbol("PyArray_Arange")
        add_symbol("PyArray_ArangeObj")
        add_symbol("PyArray_LexSort")
        add_symbol("PyArray_InnerProduct")
        add_symbol("PyArray_MatrixProduct")
        add_symbol("PyArray_MatrixProduct2")
        add_symbol("PyArray_CountNonzero")
        add_symbol("PyArray_MinScalarType")
        add_symbol("PyArray_CreateSortedStridePerm")
        add_symbol("PyArray_RemoveAxesInPlace")
        add_symbol("PyArray_DebugPrint")
        add_symbol("PyArray_EinsteinSum")
        add_symbol("PyArray_Partition")
        add_symbol("PyArray_ArgPartition")
        add_symbol("PyArray_CheckAnyScalarExact")
        add_symbol("PyArray_Correlate")
        add_symbol("PyArray_Correlate2")
        add_symbol("PyArray_RemoveSmallest")
        add_symbol("PyArray_IterAllButAxis")
        add_symbol("PyArray_PyIntAsInt")
        add_symbol("PyArray_PyIntAsIntp")
        add_symbol("PyArray_PythonPyIntFromInt")
        add_symbol("PyArray_IntpFromSequence")
        add_symbol("PyArray_IntpConverter")
        add_symbol("PyArray_BufferConverter")
        add_symbol("PyArray_OptionalIntpConverter")
        add_symbol("PyArray_Free")
        add_symbol("PyArray_AsCArray")
        add_symbol("PyArray_FailUnlessWriteable")
        add_symbol("PyArray_CheckStrides")
        add_symbol("PyArray_GetPriority")
        add_symbol("PyArray_ITER_RESET")
        add_symbol("PyArray_ITER_NEXT")
        add_symbol("PyArray_ITER_DATA")
        add_symbol("PyArray_ITER_NOTDONE")
        add_symbol("PyArray_CopyObject")
        add_symbol("PyArray_Resize")
        add_symbol("PyArray_NewLikeArray")
        add_symbol("PyArray_View")
        add_symbol("PyArray_Squeeze")
        add_symbol("PyArray_Transpose")
        add_symbol("PyArray_Ravel")
        add_symbol("PyArray_Flatten")
        add_symbol("PyArray_TakeFrom")
        add_symbol("PyArray_PutTo")
        add_symbol("PyArray_PutMask")
        add_symbol("PyArray_Repeat")
        add_symbol("PyArray_Choose")
        add_symbol("PyArray_Sort")
        add_symbol("PyArray_ArgSort")
        add_symbol("PyArray_SearchSorted")
        add_symbol("PyArray_Nonzero")
        add_symbol("PyArray_Where")
        add_symbol("PyArray_Compress")
        add_symbol("PyArray_Diagonal")
        add_symbol("PyArray_Trace")
        add_symbol("PyArray_Clip")
        add_symbol("PyArray_Conjugate")
        add_symbol("PyArray_Std")
        add_symbol("PyArray_Round")
        add_symbol("PyArray_EquivTypenums")
        add_symbol("PyArray_ScalarKind")
        add_symbol("PyArray_CanCoerceScalar")
        add_symbol("PyArray_CanCastScalar")
        add_symbol("PyArray_PromoteTypes")
        add_symbol("PyArray_ResultType")
        add_symbol("PyArray_ConvertToCommonType")
        add_symbol("PyArray_IntTupleFromIntp")
        add_symbol("PyArray_ClipmodeConverter")
        add_symbol("PyArray_ConvertClipmodeSequence")
        add_symbol("PyArray_OutputConverter")
        add_symbol("PyArray_SearchsideConverter")
        add_symbol("PyArray_OrderConverter")
        add_symbol("PyArray_BoolConverter")
        add_symbol("PyArray_OptionalBoolConverter")
        add_symbol("PyArray_AxisConverter")
        add_symbol("PyArray_GetNDArrayCVersion")
        add_symbol("PyArray_ByteorderConverter")
        add_symbol("PyArray_SortkindConverter")
        add_symbol("PyArray_SelectkindConverter")
        add_symbol("PyArray_OverflowMultiplyList")
        add_symbol("PyArray_GetEndianness")
        add_symbol("PyArray_GetNDArrayCFeatureVersion")
        add_symbol("PyArray_CheckAxis")
        add_symbol("PyArray_DescrAlignConverter")
        add_symbol("PyArray_DescrAlignConverter2")
        add_symbol("PyArray_DescrConverter")
        add_symbol("PyArray_DescrConverter2")
        add_symbol("PyArray_Sum")
        add_symbol("PyArray_CumSum")
        add_symbol("PyArray_Prod")
        add_symbol("PyArray_CumProd")
        add_symbol("PyArray_Max")
        add_symbol("PyArray_Min")
        add_symbol("PyArray_Ptp")
        add_symbol("PyArray_Mean")
        add_symbol("PyArray_Any")
        add_symbol("PyArray_All")
        add_symbol("PyArray_ArgMax")
        add_symbol("PyArray_ArgMin")
        add_symbol("PyArray_Reshape")
        add_symbol("PyArray_Newshape")
        add_symbol("PyArray_SwapAxes")
        add_symbol("PyArray_CheckFromAny")
        add_symbol("PyArray_FromArray")
        add_symbol("PyArray_MultiplyList")
        add_symbol("PyArray_MultiplyIntList")
        add_symbol("PyArray_GetPtr")
        add_symbol("PyArray_ElementStrides")
        add_symbol("PyArray_ValidType")
        add_symbol("PyArray_Item_INCREF")
        add_symbol("PyArray_Item_XDECREF")
        add_symbol("PyArray_NewCopy")
        add_symbol("PyArray_INCREF")
        add_symbol("PyArray_XDECREF")
        add_symbol("PyArray_FromAny")
        add_symbol("PyArray_SimpleNew")
        add_symbol("PyArray_SimpleNewFromData")
        add_symbol("PyArray_NDIM")
        add_symbol("PyArray_DIMS")
        add_symbol("PyArray_STRIDES")
        add_symbol("PyArray_DATA")
        add_symbol("PyArray_DESCR")
        add_symbol("PyArray_DTYPE")
        add_symbol("PyArray_TYPE")
        add_symbol("PyDataType_TYPE")
        add_symbol("PyDataType_KIND")
        add_symbol("PyDataType_ELSIZE")
        add_symbol("PyDataType_ALIGNMENT")
        add_symbol("PyTypeNum_ISBOOL")
        add_symbol("PyTypeNum_ISUNSIGNED")
        add_symbol("PyTypeNum_ISSIGNED")
        add_symbol("PyTypeNum_ISINTEGER")
        add_symbol("PyTypeNum_ISFLOAT")
        add_symbol("PyTypeNum_ISNUMBER")
        add_symbol("PyTypeNum_ISSTRING")
        add_symbol("PyTypeNum_ISCOMPLEX")
        add_symbol("PyTypeNum_ISFLEXIBLE")
        add_symbol("PyTypeNum_ISOBJECT")
        add_symbol("PyDataType_ISBOOL")
        add_symbol("PyDataType_ISUNSIGNED")
        add_symbol("PyDataType_ISSIGNED")
        add_symbol("PyDataType_ISINTEGER")
        add_symbol("PyDataType_ISFLOAT")
        add_symbol("PyDataType_ISNUMBER")
        add_symbol("PyDataType_ISSTRING")
        add_symbol("PyDataType_ISCOMPLEX")
        add_symbol("PyDataType_ISFLEXIBLE")
        add_symbol("PyDataType_ISOBJECT")
        add_symbol("PyArray_GETITEM")
        add_symbol("PyArray_SETITEM")
        add_symbol("PyArray_SIZE")
        add_symbol("PyArray_ITEMSIZE")
        add_symbol("PyArray_NBYTES")
        add_symbol("PyArray_FILLWBYTE")
        add_symbol("PyArray_EquivByteorders")
        add_symbol("PyArray_SHAPE")
        add_symbol("PyArray_FLAGS")
        add_symbol("PyArray_CompareLists")
        add_symbol("PyArray_Empty")
        add_symbol("PyArray_Zeros")
        add_symbol("PyArray_EMPTY")
        add_symbol("PyArray_ZEROS")
        add_symbol("PyArray_EquivTypes")
        add_symbol("PyArray_EquivArrTypes")
        add_symbol("PyArray_NewFromDescr")
        add_symbol("PyArray_New")
        add_symbol("PyArray_MultiIterNew")
        add_symbol("PyArray_MultiIterFromObjects")
        add_symbol("PyArray_SimpleNewFromDescr")
        add_symbol("PyArray_BASE")
        add_symbol("PyArray_SetBaseObject")
        add_symbol("PyArray_SetUpdateIfCopyBase")
        add_symbol("PyArray_SetWritebackIfCopyBase")
        add_symbol("PyArray_ResolveWritebackIfCopy")
        add_symbol("PyArray_DiscardWritebackIfCopy")
        add_symbol("PyDataMem_NEW")
        add_symbol("PyDataMem_FREE")
        add_symbol("PyDataMem_RENEW")
        add_symbol("PyDataMem_NEW_ZEROED")
        add_symbol("PyDataMem_GetHandler")
        add_symbol("PyDataMem_UserNEW")
        add_symbol("PyDataMem_UserFREE")
        add_symbol("PyDataMem_UserRENEW")
        add_symbol("PyDataMem_UserNEW_ZEROED")
        add_symbol("PyArray_Return")
        add_symbol("PyArray_ENABLEFLAGS")
        add_symbol("PyArray_CLEARFLAGS")
        add_symbol("PyArray_UpdateFlags")
        add_symbol("PyArray_CopyInto")
        add_symbol("PyArray_CopyAnyInto")
        add_symbol("PyArray_ToScalar")
        add_symbol("PyArray_Copy")
        add_symbol("PyArray_EnsureArray")
        add_symbol("PyArray_EnsureAnyArray")
        add_symbol("PyArray_SAMESHAPE")
        add_symbol("PyArray_CHKFLAGS")
        add_symbol("PyArray_FROM_O")
        add_symbol("PyArray_FROM_OF")
        add_symbol("PyArray_FROM_OT")
        add_symbol("PyArray_FROM_OTF")
        add_symbol("PyArray_FROMANY")
        add_symbol("PyArray_ContiguousFromAny")
        add_symbol("PyArray_FromObject")
        add_symbol("PyArray_ContiguousFromObject")
        add_symbol("PyArray_CopyFromObject")
        add_symbol("PyArray_ISCONTIGUOUS")
        add_symbol("PyArray_IS_C_CONTIGUOUS")
        add_symbol("PyArray_ISALIGNED")
        add_symbol("PyArray_ISWRITEABLE")
        add_symbol("PyArray_ISCARRAY")
        add_symbol("PyArray_IS_F_CONTIGUOUS")
        add_symbol("PyArray_ISONESEGMENT")
        add_symbol("PyArray_ISFORTRAN")
        add_symbol("PyArray_FORTRAN_IF")
        add_symbol("PyArray_ISNBO")
        add_symbol("PyArray_IsNativeByteOrder")
        add_symbol("PyArray_ISNOTSWAPPED")
        add_symbol("PyArray_ISBYTESWAPPED")
        add_symbol("PyArray_FLAGSWAP")
        add_symbol("PyArray_ISCARRAY_RO")
        add_symbol("PyArray_ISFARRAY")
        add_symbol("PyArray_ISFARRAY_RO")
        add_symbol("PyArray_ISBEHAVED")
        add_symbol("PyArray_ISBEHAVED_RO")
        add_symbol("PyDataType_ISNOTSWAPPED")
        add_symbol("PyDataType_ISBYTESWAPPED")
        add_symbol("PyArray_ISVARIABLE")
        add_symbol("PyArray_SAFEALIGNEDCOPY")
        add_symbol("PyArray_ISBOOL")
        add_symbol("PyArray_ISUNSIGNED")
        add_symbol("PyArray_ISSIGNED")
        add_symbol("PyArray_ISINTEGER")
        add_symbol("PyArray_ISFLOAT")
        add_symbol("PyArray_ISNUMBER")
        add_symbol("PyArray_ISSTRING")
        add_symbol("PyArray_ISCOMPLEX")
        add_symbol("PyArray_ISFLEXIBLE")
        add_symbol("PyArray_ISOBJECT")
        add_symbol("PyArray_Check")
        add_symbol("PyArray_CheckExact")
        add_symbol("PyArray_DIM")
        add_symbol("PyArray_BYTES")
        add_symbol("PyArray_STRIDE")
        add_symbol("PyArray_GETPTR1")
        add_symbol("PyArray_GETPTR2")
        add_symbol("PyArray_GETPTR3")
        add_symbol("PyArray_GETPTR4")
        add_symbol("PyUFunc_API")
        add_symbol("PyUFunc_FromFuncAndData")

    headers = []
    provided_headers = []
    missing_headers = []
    missing = []
    unknown = []
    symbol_rows = "["
    i = 0
    while i < len(requested):
        header = _native_known_capi_header(requested[i])
        if header is None:
            unknown.append(requested[i])
        else:
            if not _native_list_contains(headers, header):
                headers.append(header)
                if include_dir is not None:
                    header_path_check = include_dir + "/" + header
                    if os.path.isfile(header_path_check):
                        provided_headers.append(header)
                    else:
                        missing_headers.append(header)
            if not _native_capi_implemented(requested[i]):
                missing.append(requested[i])
            if symbol_rows != "[":
                symbol_rows += ", "
            header_path = None
            provided_by_package = False
            if include_dir is not None:
                header_path = include_dir + "/" + header
                provided_by_package = os.path.isfile(header_path)
            symbol_rows += "{"
            symbol_rows += '"header": ' + _json_str(header)
            symbol_rows += ', "header_path": ' + _json_str_or_null(header_path)
            symbol_rows += ', "implemented": ' + (
                "true" if _native_capi_implemented(requested[i]) else "false"
            )
            symbol_rows += ', "name": ' + _json_str(requested[i])
            symbol_rows += ', "provided_by_package": ' + (
                "true" if provided_by_package else "false"
            )
            symbol_rows += "}"
        i += 1
    symbol_rows += "]"

    diagnostics = "["
    i = 0
    while i < len(missing):
        if diagnostics != "[":
            diagnostics += ", "
        diagnostics += "{"
        numpy_family = _native_numpy_capi_family(missing[i])
        if numpy_family is not None:
            diagnostics += '"capability": "numpy_capi"'
            diagnostics += ', "code": "PCC-EXT-MISSING-NUMPY-CAPI-SYMBOL"'
            diagnostics += ', "message": ' + _json_str(
                missing[i]
                + " requires a pcc-native NumPy "
                + numpy_family
                + " provider; it is not implemented for "
                + abi_mode
            )
            diagnostics += ', "provider_shape": ' + _json_str(numpy_family)
            diagnostics += ', "table": ' + _json_str(
                "_UFUNC_API" if numpy_family == "ufunc_api" else "_ARRAY_API"
            )
            slot = _native_numpy_capi_slot(missing[i])
            diagnostics += ', "slot": ' + ("null" if slot is None else str(slot))
            diagnostics += ', "failure_mode": ' + _json_str(
                _native_numpy_capi_failure_mode(missing[i])
            )
        else:
            diagnostics += '"code": "PCC-EXT-MISSING-CAPI-SYMBOL"'
            diagnostics += ', "message": ' + _json_str(
                missing[i] + " is not implemented for " + abi_mode
            )
        diagnostics += ', "symbol": ' + _json_str(missing[i])
        diagnostics += "}"
        i += 1
    i = 0
    while i < len(unknown):
        if diagnostics != "[":
            diagnostics += ", "
        diagnostics += "{"
        diagnostics += '"code": "PCC-EXT-UNKNOWN-CAPI-SYMBOL"'
        diagnostics += ', "message": ' + _json_str(
            unknown[i] + " is not in the pcc C-API catalogue"
        )
        diagnostics += ', "symbol": ' + _json_str(unknown[i])
        diagnostics += "}"
        i += 1
    if include_dir is not None:
        i = 0
        while i < len(missing_headers):
            if diagnostics != "[":
                diagnostics += ", "
            diagnostics += "{"
            diagnostics += '"code": "PCC-EXT-MISSING-CAPI-HEADER"'
            diagnostics += ', "header": ' + _json_str(missing_headers[i])
            diagnostics += ', "include_dir": ' + _json_str(include_dir)
            diagnostics += ', "message": ' + _json_str(
                missing_headers[i] + " was not provided by " + include_dir
            )
            diagnostics += "}"
            i += 1
    abi_version = "null"
    if expected_abi is not None and actual_abi is not None:
        abi_ok = expected_abi == actual_abi
        abi_version = "{"
        abi_version += '"abi_mode": ' + _json_str(abi_mode)
        abi_version += ', "actual": ' + str(actual_abi)
        abi_version += ', "code": ' + (
            "null" if abi_ok else '"PCC-EXT-ABI-VERSION-MISMATCH"'
        )
        abi_version += ', "expected": ' + str(expected_abi)
        abi_version += ', "message": '
        if abi_ok:
            abi_version += _json_str("ABI versions match")
        else:
            abi_version += _json_str(
                provider
                + " ABI version mismatch: expected "
                + str(expected_abi)
                + ", got "
                + str(actual_abi)
                + " under "
                + abi_mode
            )
        abi_version += ', "ok": ' + ("true" if abi_ok else "false")
        abi_version += ', "provider": ' + _json_str(provider)
        abi_version += "}"
        if not abi_ok:
            if diagnostics != "[":
                diagnostics += ", "
            diagnostics += abi_version
    diagnostics += "]"

    numpy_capi_status = "["
    i = 0
    while i < len(requested):
        if _native_numpy_capi_family(requested[i]) is not None:
            if numpy_capi_status != "[":
                numpy_capi_status += ", "
            numpy_capi_status += _native_numpy_capi_status_json(requested[i])
        i += 1
    numpy_capi_status += "]"

    ok = len(missing) == 0 and len(unknown) == 0 and len(missing_headers) == 0
    if (
        expected_abi is not None
        and actual_abi is not None
        and expected_abi != actual_abi
    ):
        ok = False

    manifest = "{"
    manifest += '"headers": ' + _json_str_list(headers)
    manifest += ', "include_dir": ' + _json_str_or_null(include_dir)
    manifest += ', "missing_headers": ' + _json_str_list(missing_headers)
    manifest += ', "provided_headers": ' + _json_str_list(provided_headers)
    manifest += ', "symbols": ' + symbol_rows
    manifest += ', "unknown_symbols": ' + _json_str_list(unknown)
    manifest += "}"

    out = "{"
    out += '"abi_mode": ' + _json_str(abi_mode)
    out += ', "abi_version": ' + abi_version
    out += ', "diagnostics": ' + diagnostics
    out += ', "header_manifest": ' + manifest
    out += ', "missing_symbols": ' + _json_str_list(missing)
    out += ', "numpy_capi_status": ' + numpy_capi_status
    out += ', "ok": ' + ("true" if ok else "false")
    out += ', "provider": ' + _json_str(provider)
    out += ', "required_symbols": ' + _json_str_list(requested)
    out += ', "unknown_symbols": ' + _json_str_list(unknown)
    out += "}"
    return out


def _native_extension_abi_ok(
    symbols,
    expected_abi,
    actual_abi,
    require_capsule: bool,
    require_buffer: bool,
    require_memoryview: bool,
    require_numpy_capi: bool,
    include_dir,
) -> bool:
    requested = []

    def add_symbol(name: str) -> None:
        if not _native_list_contains(requested, name):
            requested.append(name)

    i = 0
    while i < len(symbols):
        add_symbol(symbols[i])
        i += 1
    if require_capsule:
        add_symbol("PyCapsule_New")
        add_symbol("PyCapsule_GetPointer")
        add_symbol("PyCapsule_GetName")
        add_symbol("PyCapsule_GetContext")
        add_symbol("PyCapsule_IsValid")
        add_symbol("PyCapsule_CheckExact")
        add_symbol("PyCapsule_SetContext")
        add_symbol("PyCapsule_SetName")
        add_symbol("PyCapsule_SetPointer")
        add_symbol("PyCapsule_GetDestructor")
        add_symbol("PyCapsule_SetDestructor")
        add_symbol("PyCapsule_Import")
    if require_buffer:
        add_symbol("PyObject_GetBuffer")
        add_symbol("PyObject_CheckBuffer")
        add_symbol("PyBuffer_Release")
    if require_memoryview:
        add_symbol("PyMemoryView_FromObject")
        add_symbol("PyMemoryView_FromMemory")
        add_symbol("PyMemoryView_Check")
        add_symbol("PyMemoryView_GET_BUFFER")
        add_symbol("PyMemoryView_GET_BASE")
    if require_numpy_capi:
        add_symbol("PyArray_API")
        add_symbol("PyArray_malloc")
        add_symbol("PyArray_free")
        add_symbol("PyArray_realloc")
        add_symbol("PyDimMem_NEW")
        add_symbol("PyDimMem_FREE")
        add_symbol("PyDimMem_RENEW")
        add_symbol("PyArray_Type")
        add_symbol("PyArrayDescr_Type")
        add_symbol("PyArray_DescrCheck")
        add_symbol("PyArray_DescrFromType")
        add_symbol("PyArray_TypeObjectFromType")
        add_symbol("PyArray_DescrNewFromType")
        add_symbol("PyArray_DescrNew")
        add_symbol("PyArray_DescrNewByteorder")
        add_symbol("PyArray_CanCastSafely")
        add_symbol("PyArray_CanCastTo")
        add_symbol("PyArray_CanCastTypeTo")
        add_symbol("PyArray_CanCastArrayTo")
        add_symbol("PyArray_CastingConverter")
        add_symbol("PyArray_Zero")
        add_symbol("PyArray_One")
        add_symbol("PyArray_ObjectType")
        add_symbol("PyArray_DescrFromObject")
        add_symbol("PyArray_Size")
        add_symbol("PyArray_DescrFromScalar")
        add_symbol("PyArray_DescrFromTypeObject")
        add_symbol("PyArray_Scalar")
        add_symbol("PyArray_ScalarAsCtype")
        add_symbol("PyArray_FromScalar")
        add_symbol("PyArray_CastScalarToCtype")
        add_symbol("PyArray_CastScalarDirect")
        add_symbol("PyArray_Pack")
        add_symbol("PyArray_CastToType")
        add_symbol("PyArray_Cast")
        add_symbol("PyArray_FillWithScalar")
        add_symbol("PyArray_ToList")
        add_symbol("PyArray_ToString")
        add_symbol("PyArray_Byteswap")
        add_symbol("PyArray_FromString")
        add_symbol("PyArray_FromBuffer")
        add_symbol("PyArray_FromIter")
        add_symbol("PyArray_Converter")
        add_symbol("PyArray_IterNew")
        add_symbol("PyArray_BroadcastToShape")
        add_symbol("PyArray_Broadcast")
        add_symbol("PyArray_Concatenate")
        add_symbol("PyArray_Arange")
        add_symbol("PyArray_ArangeObj")
        add_symbol("PyArray_LexSort")
        add_symbol("PyArray_InnerProduct")
        add_symbol("PyArray_MatrixProduct")
        add_symbol("PyArray_MatrixProduct2")
        add_symbol("PyArray_CountNonzero")
        add_symbol("PyArray_MinScalarType")
        add_symbol("PyArray_CreateSortedStridePerm")
        add_symbol("PyArray_RemoveAxesInPlace")
        add_symbol("PyArray_DebugPrint")
        add_symbol("PyArray_EinsteinSum")
        add_symbol("PyArray_Partition")
        add_symbol("PyArray_ArgPartition")
        add_symbol("PyArray_CheckAnyScalarExact")
        add_symbol("PyArray_Correlate")
        add_symbol("PyArray_Correlate2")
        add_symbol("PyArray_RemoveSmallest")
        add_symbol("PyArray_IterAllButAxis")
        add_symbol("PyArray_PyIntAsInt")
        add_symbol("PyArray_PyIntAsIntp")
        add_symbol("PyArray_PythonPyIntFromInt")
        add_symbol("PyArray_IntpFromSequence")
        add_symbol("PyArray_IntpConverter")
        add_symbol("PyArray_BufferConverter")
        add_symbol("PyArray_OptionalIntpConverter")
        add_symbol("PyArray_Free")
        add_symbol("PyArray_AsCArray")
        add_symbol("PyArray_FailUnlessWriteable")
        add_symbol("PyArray_CheckStrides")
        add_symbol("PyArray_GetPriority")
        add_symbol("PyArray_ITER_RESET")
        add_symbol("PyArray_ITER_NEXT")
        add_symbol("PyArray_ITER_DATA")
        add_symbol("PyArray_ITER_NOTDONE")
        add_symbol("PyArray_CopyObject")
        add_symbol("PyArray_Resize")
        add_symbol("PyArray_NewLikeArray")
        add_symbol("PyArray_View")
        add_symbol("PyArray_Squeeze")
        add_symbol("PyArray_Transpose")
        add_symbol("PyArray_Ravel")
        add_symbol("PyArray_Flatten")
        add_symbol("PyArray_TakeFrom")
        add_symbol("PyArray_PutTo")
        add_symbol("PyArray_PutMask")
        add_symbol("PyArray_Repeat")
        add_symbol("PyArray_Choose")
        add_symbol("PyArray_Sort")
        add_symbol("PyArray_ArgSort")
        add_symbol("PyArray_SearchSorted")
        add_symbol("PyArray_Nonzero")
        add_symbol("PyArray_Where")
        add_symbol("PyArray_Compress")
        add_symbol("PyArray_Diagonal")
        add_symbol("PyArray_Trace")
        add_symbol("PyArray_Clip")
        add_symbol("PyArray_Conjugate")
        add_symbol("PyArray_Std")
        add_symbol("PyArray_Round")
        add_symbol("PyArray_EquivTypenums")
        add_symbol("PyArray_ScalarKind")
        add_symbol("PyArray_CanCoerceScalar")
        add_symbol("PyArray_CanCastScalar")
        add_symbol("PyArray_PromoteTypes")
        add_symbol("PyArray_ResultType")
        add_symbol("PyArray_ConvertToCommonType")
        add_symbol("PyArray_IntTupleFromIntp")
        add_symbol("PyArray_ClipmodeConverter")
        add_symbol("PyArray_ConvertClipmodeSequence")
        add_symbol("PyArray_OutputConverter")
        add_symbol("PyArray_SearchsideConverter")
        add_symbol("PyArray_OrderConverter")
        add_symbol("PyArray_BoolConverter")
        add_symbol("PyArray_OptionalBoolConverter")
        add_symbol("PyArray_AxisConverter")
        add_symbol("PyArray_GetNDArrayCVersion")
        add_symbol("PyArray_ByteorderConverter")
        add_symbol("PyArray_SortkindConverter")
        add_symbol("PyArray_SelectkindConverter")
        add_symbol("PyArray_OverflowMultiplyList")
        add_symbol("PyArray_GetEndianness")
        add_symbol("PyArray_GetNDArrayCFeatureVersion")
        add_symbol("PyArray_CheckAxis")
        add_symbol("PyArray_DescrAlignConverter")
        add_symbol("PyArray_DescrAlignConverter2")
        add_symbol("PyArray_DescrConverter")
        add_symbol("PyArray_DescrConverter2")
        add_symbol("PyArray_Sum")
        add_symbol("PyArray_CumSum")
        add_symbol("PyArray_Prod")
        add_symbol("PyArray_CumProd")
        add_symbol("PyArray_Max")
        add_symbol("PyArray_Min")
        add_symbol("PyArray_Ptp")
        add_symbol("PyArray_Mean")
        add_symbol("PyArray_Any")
        add_symbol("PyArray_All")
        add_symbol("PyArray_ArgMax")
        add_symbol("PyArray_ArgMin")
        add_symbol("PyArray_Reshape")
        add_symbol("PyArray_Newshape")
        add_symbol("PyArray_SwapAxes")
        add_symbol("PyArray_CheckFromAny")
        add_symbol("PyArray_FromArray")
        add_symbol("PyArray_MultiplyList")
        add_symbol("PyArray_MultiplyIntList")
        add_symbol("PyArray_GetPtr")
        add_symbol("PyArray_ElementStrides")
        add_symbol("PyArray_ValidType")
        add_symbol("PyArray_Item_INCREF")
        add_symbol("PyArray_Item_XDECREF")
        add_symbol("PyArray_NewCopy")
        add_symbol("PyArray_INCREF")
        add_symbol("PyArray_XDECREF")
        add_symbol("PyArray_FromAny")
        add_symbol("PyArray_SimpleNew")
        add_symbol("PyArray_SimpleNewFromData")
        add_symbol("PyArray_NDIM")
        add_symbol("PyArray_DIMS")
        add_symbol("PyArray_STRIDES")
        add_symbol("PyArray_DATA")
        add_symbol("PyArray_DESCR")
        add_symbol("PyArray_DTYPE")
        add_symbol("PyArray_TYPE")
        add_symbol("PyDataType_TYPE")
        add_symbol("PyDataType_KIND")
        add_symbol("PyDataType_ELSIZE")
        add_symbol("PyDataType_ALIGNMENT")
        add_symbol("PyTypeNum_ISBOOL")
        add_symbol("PyTypeNum_ISUNSIGNED")
        add_symbol("PyTypeNum_ISSIGNED")
        add_symbol("PyTypeNum_ISINTEGER")
        add_symbol("PyTypeNum_ISFLOAT")
        add_symbol("PyTypeNum_ISNUMBER")
        add_symbol("PyTypeNum_ISSTRING")
        add_symbol("PyTypeNum_ISCOMPLEX")
        add_symbol("PyTypeNum_ISFLEXIBLE")
        add_symbol("PyTypeNum_ISOBJECT")
        add_symbol("PyDataType_ISBOOL")
        add_symbol("PyDataType_ISUNSIGNED")
        add_symbol("PyDataType_ISSIGNED")
        add_symbol("PyDataType_ISINTEGER")
        add_symbol("PyDataType_ISFLOAT")
        add_symbol("PyDataType_ISNUMBER")
        add_symbol("PyDataType_ISSTRING")
        add_symbol("PyDataType_ISCOMPLEX")
        add_symbol("PyDataType_ISFLEXIBLE")
        add_symbol("PyDataType_ISOBJECT")
        add_symbol("PyArray_GETITEM")
        add_symbol("PyArray_SETITEM")
        add_symbol("PyArray_SIZE")
        add_symbol("PyArray_ITEMSIZE")
        add_symbol("PyArray_NBYTES")
        add_symbol("PyArray_FILLWBYTE")
        add_symbol("PyArray_EquivByteorders")
        add_symbol("PyArray_SHAPE")
        add_symbol("PyArray_FLAGS")
        add_symbol("PyArray_CompareLists")
        add_symbol("PyArray_Empty")
        add_symbol("PyArray_Zeros")
        add_symbol("PyArray_EMPTY")
        add_symbol("PyArray_ZEROS")
        add_symbol("PyArray_EquivTypes")
        add_symbol("PyArray_EquivArrTypes")
        add_symbol("PyArray_NewFromDescr")
        add_symbol("PyArray_New")
        add_symbol("PyArray_MultiIterNew")
        add_symbol("PyArray_MultiIterFromObjects")
        add_symbol("PyArray_SimpleNewFromDescr")
        add_symbol("PyArray_BASE")
        add_symbol("PyArray_SetBaseObject")
        add_symbol("PyArray_SetUpdateIfCopyBase")
        add_symbol("PyArray_SetWritebackIfCopyBase")
        add_symbol("PyArray_ResolveWritebackIfCopy")
        add_symbol("PyArray_DiscardWritebackIfCopy")
        add_symbol("PyDataMem_NEW")
        add_symbol("PyDataMem_FREE")
        add_symbol("PyDataMem_RENEW")
        add_symbol("PyDataMem_NEW_ZEROED")
        add_symbol("PyDataMem_GetHandler")
        add_symbol("PyDataMem_UserNEW")
        add_symbol("PyDataMem_UserFREE")
        add_symbol("PyDataMem_UserRENEW")
        add_symbol("PyDataMem_UserNEW_ZEROED")
        add_symbol("PyArray_Return")
        add_symbol("PyArray_ENABLEFLAGS")
        add_symbol("PyArray_CLEARFLAGS")
        add_symbol("PyArray_UpdateFlags")
        add_symbol("PyArray_CopyInto")
        add_symbol("PyArray_CopyAnyInto")
        add_symbol("PyArray_ToScalar")
        add_symbol("PyArray_Copy")
        add_symbol("PyArray_EnsureArray")
        add_symbol("PyArray_EnsureAnyArray")
        add_symbol("PyArray_SAMESHAPE")
        add_symbol("PyArray_CHKFLAGS")
        add_symbol("PyArray_FROM_O")
        add_symbol("PyArray_FROM_OF")
        add_symbol("PyArray_FROM_OT")
        add_symbol("PyArray_FROM_OTF")
        add_symbol("PyArray_FROMANY")
        add_symbol("PyArray_ContiguousFromAny")
        add_symbol("PyArray_FromObject")
        add_symbol("PyArray_ContiguousFromObject")
        add_symbol("PyArray_CopyFromObject")
        add_symbol("PyArray_ISCONTIGUOUS")
        add_symbol("PyArray_IS_C_CONTIGUOUS")
        add_symbol("PyArray_ISALIGNED")
        add_symbol("PyArray_ISWRITEABLE")
        add_symbol("PyArray_ISCARRAY")
        add_symbol("PyArray_IS_F_CONTIGUOUS")
        add_symbol("PyArray_ISONESEGMENT")
        add_symbol("PyArray_ISFORTRAN")
        add_symbol("PyArray_FORTRAN_IF")
        add_symbol("PyArray_ISNBO")
        add_symbol("PyArray_IsNativeByteOrder")
        add_symbol("PyArray_ISNOTSWAPPED")
        add_symbol("PyArray_ISBYTESWAPPED")
        add_symbol("PyArray_FLAGSWAP")
        add_symbol("PyArray_ISCARRAY_RO")
        add_symbol("PyArray_ISFARRAY")
        add_symbol("PyArray_ISFARRAY_RO")
        add_symbol("PyArray_ISBEHAVED")
        add_symbol("PyArray_ISBEHAVED_RO")
        add_symbol("PyDataType_ISNOTSWAPPED")
        add_symbol("PyDataType_ISBYTESWAPPED")
        add_symbol("PyArray_ISVARIABLE")
        add_symbol("PyArray_SAFEALIGNEDCOPY")
        add_symbol("PyArray_ISBOOL")
        add_symbol("PyArray_ISUNSIGNED")
        add_symbol("PyArray_ISSIGNED")
        add_symbol("PyArray_ISINTEGER")
        add_symbol("PyArray_ISFLOAT")
        add_symbol("PyArray_ISNUMBER")
        add_symbol("PyArray_ISSTRING")
        add_symbol("PyArray_ISCOMPLEX")
        add_symbol("PyArray_ISFLEXIBLE")
        add_symbol("PyArray_ISOBJECT")
        add_symbol("PyArray_Check")
        add_symbol("PyArray_CheckExact")
        add_symbol("PyArray_DIM")
        add_symbol("PyArray_BYTES")
        add_symbol("PyArray_STRIDE")
        add_symbol("PyArray_GETPTR1")
        add_symbol("PyArray_GETPTR2")
        add_symbol("PyArray_GETPTR3")
        add_symbol("PyArray_GETPTR4")
        add_symbol("PyUFunc_API")
        add_symbol("PyUFunc_FromFuncAndData")
    i = 0
    while i < len(requested):
        if _native_known_capi_header(requested[i]) is None:
            return False
        if include_dir is not None:
            header = _native_known_capi_header(requested[i])
            if header is not None and not os.path.isfile(include_dir + "/" + header):
                return False
        if not _native_capi_implemented(requested[i]):
            return False
        i += 1
    if (
        expected_abi is not None
        and actual_abi is not None
        and expected_abi != actual_abi
    ):
        return False
    return True


def _run_native_package_inspect_from_pcc1(module_args) -> int:
    name = "package"
    path = None
    emit_json = False
    i = 0
    while i < len(module_args):
        arg = module_args[i]
        if arg == "--json":
            emit_json = True
        elif arg == "--path":
            if i + 1 >= len(module_args):
                _write_text("Error: --path requires a value", err=True)
                return 2
            path = module_args[i + 1]
            i += 1
        elif not arg.startswith("-"):
            name = arg
        i += 1
    report = _package_inspection_json(name, path)
    if emit_json:
        _write_text(report)
    else:
        _write_text(report)
    return 0


def _run_native_package_build_plan_from_pcc1(module_args) -> int:
    name = "package"
    path = None
    i = 0
    while i < len(module_args):
        arg = module_args[i]
        if arg == "--json":
            pass
        elif arg == "--path":
            if i + 1 >= len(module_args):
                _write_text("Error: --path requires a value", err=True)
                return 2
            path = module_args[i + 1]
            i += 1
        elif not arg.startswith("-"):
            name = arg
        i += 1
    _write_text(_native_build_plan_json(name, path))
    return 0


def _native_collect_suffix_files(root: str, suffixes, build_relevant: bool = False):
    files = []
    if root is None or not os.path.isdir(root):
        return files
    stack = [root]
    while len(stack) > 0:
        current = stack.pop()
        try:
            names = sorted(os.listdir(current))
        except Exception:
            names = []
        i = 0
        while i < len(names):
            name = names[i]
            path = current + "/" + name
            if os.path.isdir(path):
                skip = (
                    _should_skip_package_build_dir(name)
                    if build_relevant
                    else (name == "__pycache__" or name == ".git" or name == "build")
                )
                if not skip:
                    stack.append(path)
            elif os.path.isfile(path):
                lower = name.lower()
                j = 0
                while j < len(suffixes):
                    if lower.endswith(suffixes[j]):
                        files.append(path)
                        break
                    j += 1
            i += 1
    return files


def _native_split_ws(text: str):
    parts = []
    current = ""
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == " " or ch == "\n" or ch == "\t":
            if current != "":
                parts.append(current)
                current = ""
        else:
            current += ch
        i += 1
    if current != "":
        parts.append(current)
    return parts


def _native_compile_command_rows(root: str):
    rows = []
    path = os.path.join(root, "compile_commands.json")
    if not os.path.exists(path):
        return rows
    try:
        with open(path, "r") as fh:
            text = fh.read()
    except Exception:
        return rows
    pos = 0
    while True:
        key = _native_find_from(text, '"command"', pos)
        if key < 0:
            break
        colon = _native_find_from(text, ":", key)
        first = _native_find_from(text, '"', colon + 1)
        second = _native_find_from(text, '"', first + 1)
        if colon < 0 or first < 0 or second < 0:
            pos = key + 9
            continue
        command = text[first + 1 : second]
        tokens = _native_split_ws(command)
        output = None
        i = 0
        while i < len(tokens):
            if tokens[i] == "-o" and i + 1 < len(tokens):
                output = tokens[i + 1]
            i += 1
        rows.append([command, tokens, output])
        pos = second + 1
    return rows


def _native_source_language(path: str) -> str:
    lower = path.lower()
    if lower.endswith(".c"):
        return "c"
    if (
        lower.endswith(".cc")
        or lower.endswith(".cpp")
        or lower.endswith(".cxx")
        or lower.endswith(".c++")
    ):
        return "cxx"
    if (
        lower.endswith(".f")
        or lower.endswith(".for")
        or lower.endswith(".f77")
        or lower.endswith(".f90")
        or lower.endswith(".f95")
        or lower.endswith(".f03")
        or lower.endswith(".f08")
    ):
        return "fortran"
    return "unknown"


def _native_shell_quote(text: str) -> str:
    return "'" + text.replace("'", "'\"'\"'") + "'"


def _native_path_search_dirs():
    raw = os.environ.get("PATH") or ""
    parts = raw.split(_path_list_sep())
    out = []
    i = 0
    while i < len(parts):
        item = parts[i].strip()
        if item != "" and not _native_list_contains(out, item):
            out.append(item)
        i += 1
    return out


def _native_pcc_header_roots():
    candidates = []
    explicit = os.environ.get("PCC_REPO_ROOT")
    if explicit:
        candidates.append(os.path.abspath(explicit))
    candidates.append(os.path.abspath(os.getcwd()))
    try:
        candidates.append(
            os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        )
    except Exception:
        pass
    i = 0
    while i < len(candidates):
        root = candidates[i]
        capi = os.path.join(root, "utils", "fake_libc_include")
        runtime = os.path.join(root, "pcc", "py_runtime", "include")
        if os.path.isfile(os.path.join(capi, "Python.h")) and os.path.isdir(runtime):
            return [capi, runtime]
        i += 1
    return [None, None]


def _native_materialize_pcc_capi_include(root: str, execute: bool):
    roots = _native_pcc_header_roots()
    source = roots[0]
    runtime = roots[1]
    if source is None or runtime is None:
        return [None, None]
    dest = os.path.join(root, "build", "pcc-package", "pcc-capi-include")
    if execute:
        try:
            _bootstrap_subprocess_run(["mkdir", "-p", dest], check=True)
            i = 0
            while i < len(PCC_CAPI_HEADERS):
                header = PCC_CAPI_HEADERS[i]
                header_source = os.path.join(source, header)
                if not os.path.isfile(header_source):
                    return [None, None]
                _bootstrap_subprocess_run(["cp", header_source, dest], check=True)
                i += 1
        except Exception:
            return [None, None]
    return [dest, runtime]


def _native_generated_target_suffix_ok(path: str) -> bool:
    lower = path.lower()
    return (
        lower.endswith(".c")
        or lower.endswith(".cc")
        or lower.endswith(".cpp")
        or lower.endswith(".cxx")
        or lower.endswith(".c++")
        or lower.endswith(".h")
        or lower.endswith(".hh")
        or lower.endswith(".hpp")
        or lower.endswith(".inc")
        or lower.endswith(".py")
        or lower.endswith(".pxd")
        or lower.endswith(".pyx")
        or lower.endswith(".json")
        or lower.endswith(".txt")
    )


def _native_is_file_custom_target(target: str) -> bool:
    if target == "" or _native_find_from(target, "meson-internal__", 0) == 0:
        return False
    if (
        target == "all"
        or target == "test"
        or target == "install"
        or target == "benchmark"
        or target == "clean"
        or target == "uninstall"
    ):
        return False
    return _native_generated_target_suffix_ok(target)


def _native_meson_intro_path(root: str):
    candidate1 = root + "/meson-info/intro-targets.json"
    candidate2 = root + "/build/meson-info/intro-targets.json"
    candidate3 = root + "/build/pcc-package/meson-build/meson-info/intro-targets.json"
    if os.path.isfile(candidate1):
        return candidate1
    if os.path.isfile(candidate2):
        return candidate2
    if os.path.isfile(candidate3):
        return candidate3
    return None


def _native_meson_build_dir(root: str) -> str:
    path = _native_meson_intro_path(root)
    if path is None:
        return root + "/build/pcc-package/meson-build"
    marker = "/meson-info/intro-targets.json"
    pos = _native_find_from(path, marker, 0)
    if pos >= 0:
        return path[:pos]
    return root + "/build/pcc-package/meson-build"


def _native_ninja_custom_targets(root: str, search_paths):
    build_dir = _native_meson_build_dir(root)
    ninja = _native_find_tool_path(["ninja"], search_paths) or "ninja"
    output_path = "/tmp/pcc_ninja_targets_" + str(os.getpid())
    command = (
        _native_shell_quote(ninja)
        + " -C "
        + _native_shell_quote(build_dir)
        + " -t targets all > "
        + _native_shell_quote(output_path)
        + " 2>/dev/null"
    )
    try:
        _bootstrap_subprocess_run(["/bin/sh", "-c", command], check=True)
    except Exception:
        return []
    try:
        with open(output_path, "r") as fh:
            text = fh.read()
    except Exception:
        text = ""
    try:
        _bootstrap_subprocess_run(["rm", "-f", output_path], check=True)
    except Exception:
        pass
    targets = []
    pos = 0
    while pos < len(text):
        end = _native_find_from(text, "\n", pos)
        if end < 0:
            end = len(text)
        line = text[pos:end]
        colon = _native_find_from(line, ":", 0)
        if colon > 0:
            target = line[:colon].strip()
            kind = line[colon + 1 :].strip()
            if (
                _native_find_from(kind, "CUSTOM_COMMAND", 0) == 0
                and _native_is_file_custom_target(target)
                and not _native_list_contains(targets, target)
            ):
                targets.append(target)
        pos = end + 1
    return targets


def _native_meson_command_rows(root: str):
    rows = []
    path = _native_meson_intro_path(root)
    if path is None:
        return rows
    try:
        with open(path, "r") as fh:
            text = fh.read()
    except Exception:
        return rows
    pos = 0
    seen = []
    while True:
        first = _native_find_from(text, '"', pos)
        if first < 0:
            break
        second = _native_find_from(text, '"', first + 1)
        if second < 0:
            break
        value = text[first + 1 : second]
        language = _native_source_language(value)
        if language != "unknown":
            if value.startswith("/"):
                source = value
            else:
                source = root + "/" + value
            if not _native_list_contains(seen, source):
                seen.append(source)
                base = os.path.basename(source).split(".")[0]
                output = (
                    root
                    + "/build/pcc-package/meson-"
                    + base
                    + "-"
                    + str(len(rows))
                    + ".o"
                )
                rows.append([source, language, output])
        pos = second + 1
    return rows


def _native_generated_c_provenance_json(
    root: str, diagnostics, enforce_generated_c: bool
) -> str:
    pyx_files = _native_collect_suffix_files(root, [".pyx"], True)
    out = "["
    i = 0
    while i < len(pyx_files):
        pyx = pyx_files[i]
        generated = pyx[:-4] + ".c"
        exists = os.path.exists(generated)
        status = "missing"
        if exists:
            try:
                if os.path.getmtime(generated) >= os.path.getmtime(pyx):
                    status = "up_to_date"
                else:
                    status = "stale"
            except Exception:
                status = "unknown"
        if i > 0:
            out += ", "
        out += "{"
        out += '"exists": ' + ("true" if exists else "false")
        out += ', "generated_c": ' + _json_str(generated)
        out += ', "pyx": ' + _json_str(pyx)
        out += ', "status": ' + _json_str(status)
        out += "}"
        if enforce_generated_c:
            if status == "missing" and not _native_list_contains(
                diagnostics, "PCC-PKG-GENERATED-C-MISSING"
            ):
                diagnostics.append("PCC-PKG-GENERATED-C-MISSING")
            if status == "stale" and not _native_list_contains(
                diagnostics, "PCC-PKG-GENERATED-C-STALE"
            ):
                diagnostics.append("PCC-PKG-GENERATED-C-STALE")
        i += 1
    out += "]"
    return out


def _native_build_exec_json(
    name: str,
    explicit_path,
    search_paths,
    include_dirs,
    library_dirs,
    execute: bool,
    regenerate_cython: bool,
    run_f2py: bool,
    link_output,
    libraries,
    abi_mode: str,
    from_compile_commands: bool,
    from_meson_introspection: bool,
    configure_meson: bool,
    enforce_generated_c: bool,
    build_mode: str = "owned",
) -> str:
    pkg_name = (name or "").strip() or "package"
    root = _native_package_path(pkg_name, explicit_path)
    actions = "["
    diagnostics = []
    object_outputs = []
    vendor_bindings = "[]"
    linkage = "null"
    generated_c_provenance = "[]"
    ok = True
    effective_include_dirs = _copy_seq(include_dirs)

    def add_action(kind: str, source, output, command, status: str, returncode) -> None:
        nonlocal actions, ok
        if actions != "[":
            actions += ", "
        actions += "{"
        actions += '"command": ' + _json_str_list(command)
        actions += ', "kind": ' + _json_str(kind)
        actions += ', "output": ' + _json_str_or_null(output)
        actions += ', "returncode": ' + (
            "null" if returncode is None else str(returncode)
        )
        actions += ', "source": ' + _json_str_or_null(source)
        actions += ', "status": ' + _json_str(status)
        actions += "}"
        if status == "blocked" or status == "failed" or status == "timeout":
            ok = False

    if root is None or not os.path.isdir(root):
        diagnostics.append("PCC-PKG-BUILD-PATH-MISSING")
        ok = False
    else:
        if execute:
            try:
                _bootstrap_subprocess_run(
                    ["mkdir", "-p", root + "/build/pcc-package"], check=True
                )
            except Exception:
                diagnostics.append("PCC-PKG-BUILD-DIR-FAILED")
                ok = False

        pyx_files = _native_collect_suffix_files(root, [".pyx"], True)
        c_files = _native_collect_suffix_files(root, [".c"], True)
        fortran_files = _native_collect_suffix_files(
            root,
            [".f", ".for", ".f77", ".f90", ".f95", ".f03", ".f08"],
            True,
        )

        if (
            abi_mode == "pcc-native"
            and len(c_files) > 0
            and not from_compile_commands
            and not from_meson_introspection
        ):
            pcc_includes = _native_materialize_pcc_capi_include(root, execute)
            if pcc_includes[0] is None or pcc_includes[1] is None:
                diagnostics.append("PCC-PKG-CAPI-INCLUDE-MISSING")
                ok = False
            else:
                if not _native_list_contains(effective_include_dirs, pcc_includes[0]):
                    effective_include_dirs.append(pcc_includes[0])
                if not _native_list_contains(effective_include_dirs, pcc_includes[1]):
                    effective_include_dirs.append(pcc_includes[1])

        if (
            len(pyx_files) > 0
            and not regenerate_cython
            and execute
            and not from_compile_commands
            and not from_meson_introspection
        ):
            diagnostics.append("PCC-PKG-CYTHON-REGENERATION-REQUIRED")
        if regenerate_cython:
            cython = _native_find_tool_path(["cython", "cython3"], search_paths)
            i = 0
            while i < len(pyx_files):
                source = pyx_files[i]
                output = source[:-4] + ".c"
                command = [cython or "cython", source, "-o", output]
                if execute and cython is None:
                    add_action(
                        "cython_regenerate", source, output, command, "blocked", None
                    )
                    diagnostics.append("PCC-PKG-MISSING-CYTHON")
                elif execute:
                    try:
                        _bootstrap_subprocess_run(command, check=True)
                        add_action(
                            "cython_regenerate",
                            source,
                            output,
                            command,
                            "passed",
                            0,
                        )
                    except Exception:
                        add_action(
                            "cython_regenerate", source, output, command, "failed", 127
                        )
                else:
                    add_action(
                        "cython_regenerate", source, output, command, "planned", None
                    )
                i += 1

        generated_c_provenance = _native_generated_c_provenance_json(
            root,
            diagnostics,
            enforce_generated_c,
        )

        if run_f2py:
            f2py = _native_find_tool_path(["f2py", "f2py3"], search_paths)
            i = 0
            while i < len(fortran_files):
                source = fortran_files[i]
                command = [f2py or "f2py", "-c", source]
                if execute and f2py is None:
                    add_action("f2py_build", source, None, command, "blocked", None)
                    diagnostics.append("PCC-PKG-MISSING-F2PY")
                elif execute:
                    try:
                        _bootstrap_subprocess_run(command, check=True)
                        add_action(
                            "f2py_build",
                            source,
                            None,
                            command,
                            "passed",
                            0,
                        )
                    except Exception:
                        add_action("f2py_build", source, None, command, "failed", 127)
                else:
                    add_action("f2py_build", source, None, command, "planned", None)
                i += 1

        cc = _native_find_tool_path(["cc", "clang", "gcc"], search_paths)
        fortran = _native_find_tool_path(
            ["gfortran", "flang", "ifx", "ifort"], search_paths
        )
        if from_compile_commands:
            rows = _native_compile_command_rows(root)
            i = 0
            while i < len(rows):
                command = rows[i][1]
                if len(command) > 0 and _native_find_from(command[0], "/", 0) < 0:
                    found_tool = _native_find_tool_path([command[0]], search_paths)
                    if found_tool is not None:
                        command[0] = found_tool
                output = rows[i][2]
                if output is not None:
                    object_outputs.append(output)
                if execute:
                    try:
                        _bootstrap_subprocess_run(command, check=True)
                        add_action(
                            "compile_command", None, output, command, "passed", 0
                        )
                    except Exception:
                        add_action(
                            "compile_command", None, output, command, "failed", 127
                        )
                else:
                    add_action(
                        "compile_command", None, output, command, "planned", None
                    )
                i += 1
        elif from_meson_introspection:
            rows = _native_meson_command_rows(root)
            if len(rows) == 0 and configure_meson:
                meson = _native_find_tool_path(["meson"], search_paths)
                setup_dir = root + "/build/pcc-package/meson-build"
                command = [meson or "meson", "setup", setup_dir, root]
                if execute and meson is None:
                    add_action("meson_setup", None, setup_dir, command, "blocked", None)
                    if not _native_list_contains(diagnostics, "PCC-PKG-MISSING-MESON"):
                        diagnostics.append("PCC-PKG-MISSING-MESON")
                elif execute:
                    try:
                        _bootstrap_subprocess_run(command, check=True)
                        add_action("meson_setup", None, setup_dir, command, "passed", 0)
                        rows = _native_meson_command_rows(root)
                    except Exception:
                        add_action(
                            "meson_setup", None, setup_dir, command, "failed", 127
                        )
                else:
                    add_action("meson_setup", None, setup_dir, command, "planned", None)
            if len(rows) == 0:
                if not configure_meson or execute:
                    diagnostics.append("PCC-PKG-MESON-INTROSPECTION-MISSING")
                    ok = False
            generated_targets = []
            if execute and len(rows) > 0:
                generated_targets = _native_ninja_custom_targets(root, search_paths)
            if len(generated_targets) > 0:
                ninja = _native_find_tool_path(["ninja"], search_paths) or "ninja"
                build_dir = _native_meson_build_dir(root)
                command = [ninja, "-C", build_dir]
                g = 0
                while g < len(generated_targets):
                    command.append(generated_targets[g])
                    g += 1
                try:
                    _bootstrap_subprocess_run(command, check=True)
                    add_action(
                        "meson_generated_targets", None, None, command, "passed", 0
                    )
                except Exception:
                    add_action(
                        "meson_generated_targets", None, None, command, "failed", 127
                    )
            i = 0
            while i < len(rows):
                source = rows[i][0]
                language = rows[i][1]
                output = rows[i][2]
                object_outputs.append(output)
                if language == "fortran":
                    command = [fortran or "gfortran", "-c", source, "-o", output]
                    kind = "meson_compile_fortran"
                    tool_missing = fortran is None
                    missing_diag = "PCC-PKG-MISSING-FORTRAN"
                elif language == "cxx":
                    command = [cc or "cc", "-c", source, "-o", output]
                    kind = "meson_compile_cxx"
                    tool_missing = cc is None
                    missing_diag = "PCC-PKG-MISSING-C-COMPILER"
                else:
                    command = [cc or "cc", "-c", source, "-o", output]
                    kind = "meson_compile_c"
                    tool_missing = cc is None
                    missing_diag = "PCC-PKG-MISSING-C-COMPILER"
                if execute and tool_missing:
                    add_action(kind, source, output, command, "blocked", None)
                    if not _native_list_contains(diagnostics, missing_diag):
                        diagnostics.append(missing_diag)
                elif execute:
                    try:
                        _bootstrap_subprocess_run(command, check=True)
                        add_action(kind, source, output, command, "passed", 0)
                    except Exception:
                        add_action(kind, source, output, command, "failed", 127)
                else:
                    add_action(kind, source, output, command, "planned", None)
                i += 1
        else:
            i = 0
            while i < len(c_files):
                source = c_files[i]
                base = os.path.basename(source).split(".")[0]
                output = root + "/build/pcc-package/" + base + ".o"
                object_outputs.append(output)
                command = [cc or "cc", "-c"]
                if link_output is not None:
                    command.append("-fPIC")
                j = 0
                while j < len(effective_include_dirs):
                    command.append("-I" + effective_include_dirs[j])
                    j += 1
                command.append(source)
                command.append("-o")
                command.append(output)
                if execute and cc is None:
                    add_action("c_compile", source, output, command, "blocked", None)
                    diagnostics.append("PCC-PKG-MISSING-C-COMPILER")
                elif execute:
                    try:
                        _bootstrap_subprocess_run(command, check=True)
                        add_action(
                            "c_compile",
                            source,
                            output,
                            command,
                            "passed",
                            0,
                        )
                    except Exception:
                        add_action("c_compile", source, output, command, "failed", 127)
                else:
                    add_action("c_compile", source, output, command, "planned", None)
                i += 1

            i = 0
            while i < len(fortran_files):
                source = fortran_files[i]
                base = os.path.basename(source).split(".")[0]
                output = root + "/build/pcc-package/" + base + ".o"
                object_outputs.append(output)
                command = [fortran or "gfortran", "-c", source, "-o", output]
                if execute and fortran is None:
                    add_action(
                        "fortran_compile", source, output, command, "blocked", None
                    )
                    diagnostics.append("PCC-PKG-MISSING-FORTRAN")
                elif execute:
                    try:
                        _bootstrap_subprocess_run(command, check=True)
                        add_action(
                            "fortran_compile",
                            source,
                            output,
                            command,
                            "passed",
                            0,
                        )
                    except Exception:
                        add_action(
                            "fortran_compile", source, output, command, "failed", 127
                        )
                else:
                    add_action(
                        "fortran_compile", source, output, command, "planned", None
                    )
                i += 1

        vendor_bindings = _native_vendor_bindings_json(
            libraries, library_dirs, diagnostics
        )
        linkage = "null"
        if link_output is not None:
            if link_output.startswith("/"):
                link_path = link_output
            else:
                link_path = root + "/" + link_output
            command = [cc or "cc", "-shared"]
            if sys.platform == "darwin":
                command.append("-undefined")
                command.append("dynamic_lookup")
            i = 0
            while i < len(object_outputs):
                command.append(object_outputs[i])
                i += 1
            command.append("-o")
            command.append(link_path)
            i = 0
            while i < len(library_dirs):
                command.append("-L" + library_dirs[i])
                i += 1
            i = 0
            while i < len(libraries):
                binding = _native_find_library_binding_values(
                    libraries[i], library_dirs
                )
                if binding[0]:
                    command.append("-l" + binding[1])
                i += 1
            if execute and cc is None:
                add_action("native_link", None, link_path, command, "blocked", None)
            elif execute and not ok:
                add_action("native_link", None, link_path, command, "blocked", None)
            elif execute:
                try:
                    _bootstrap_subprocess_run(command, check=True)
                    add_action("native_link", None, link_path, command, "passed", 0)
                except Exception:
                    add_action("native_link", None, link_path, command, "failed", 127)
            else:
                add_action("native_link", None, link_path, command, "planned", None)
            artifacts = []
            if os.path.exists(link_path):
                artifacts.append(link_path)
            linkage = _native_linkage_json(artifacts, [], [" ".join(command)], abi_mode)
            if _native_find_from(linkage, '"ok": false', 0) >= 0:
                ok = False
                if not _native_list_contains(diagnostics, "PCC-PKG-003"):
                    diagnostics.append("PCC-PKG-003")
    if (
        _native_list_contains(diagnostics, "PCC-PKG-GENERATED-C-MISSING")
        or _native_list_contains(diagnostics, "PCC-PKG-GENERATED-C-STALE")
        or _native_list_contains(diagnostics, "PCC-PKG-MISSING-LIBRARY")
    ):
        ok = False
    if not ok and not _native_list_contains(diagnostics, "PCC-PKG-BUILD-ACTION-FAILED"):
        diagnostics.append("PCC-PKG-BUILD-ACTION-FAILED")
    actions += "]"
    out = "{"
    out += '"actions": ' + actions
    out += ', "build_backend": "pcc-native"'
    out += ', "build_mode_requested": ' + _json_str(build_mode)
    out += ', "build_ownership": "owned"'
    out += ', "build_plan": ' + _native_build_plan_json(pkg_name, root)
    out += ', "diagnostics": ' + _json_str_list(diagnostics)
    out += ', "execute": ' + ("true" if execute else "false")
    # Host contract (build_exec.py): the report echoes the CALLER's include
    # dirs; internally materialized pcc-capi include dirs stay internal to the
    # compile commands (they are visible there), so host and pcc1 reports stay
    # byte-comparable.
    out += ', "include_dirs": ' + _json_str_list(_copy_seq(include_dirs))
    out += ', "from_compile_commands": ' + (
        "true" if from_compile_commands else "false"
    )
    out += ', "from_meson_introspection": ' + (
        "true" if from_meson_introspection else "false"
    )
    out += ', "configure_meson": ' + ("true" if configure_meson else "false")
    out += ', "generated_c_provenance": ' + generated_c_provenance
    out += ', "host_assisted": false'
    out += ', "host_free_build_claim": ' + ("true" if ok else "false")
    out += ', "host_python": null'
    out += ', "linkage": ' + linkage
    out += ', "name": ' + _json_str(pkg_name)
    out += ', "ok": ' + ("true" if ok else "false")
    out += ', "path": ' + _json_str_or_null(root)
    out += ', "vendor_bindings": ' + vendor_bindings
    out += "}"
    return out


def _run_native_package_build_exec_from_pcc1(module_args) -> int:
    name = "package"
    path = None
    search_paths = []
    include_dirs = []
    library_dirs = []
    libraries = []
    link_output = None
    abi_mode = "pcc-native"
    execute = False
    regenerate_cython = False
    run_f2py = False
    from_compile_commands = False
    from_meson_introspection = False
    configure_meson = False
    enforce_generated_c = False
    i = 0
    while i < len(module_args):
        arg = module_args[i]
        if arg == "--json":
            pass
        elif arg == "--path":
            if i + 1 >= len(module_args):
                _write_text("Error: --path requires a value", err=True)
                return 2
            path = module_args[i + 1]
            i += 1
        elif arg == "--search-path":
            if i + 1 >= len(module_args):
                _write_text("Error: --search-path requires a value", err=True)
                return 2
            search_paths.append(module_args[i + 1])
            i += 1
        elif arg.startswith("--search-path="):
            search_paths.append(arg.split("=", 1)[1])
        elif arg == "--include-dir":
            if i + 1 >= len(module_args):
                _write_text("Error: --include-dir requires a value", err=True)
                return 2
            include_dirs.append(module_args[i + 1])
            i += 1
        elif arg.startswith("--include-dir="):
            include_dirs.append(arg.split("=", 1)[1])
        elif arg == "--library-dir":
            if i + 1 >= len(module_args):
                _write_text("Error: --library-dir requires a value", err=True)
                return 2
            library_dirs.append(module_args[i + 1])
            i += 1
        elif arg.startswith("--library-dir="):
            library_dirs.append(arg.split("=", 1)[1])
        elif arg == "--library":
            if i + 1 >= len(module_args):
                _write_text("Error: --library requires a value", err=True)
                return 2
            libraries.append(module_args[i + 1])
            i += 1
        elif arg.startswith("--library="):
            libraries.append(arg.split("=", 1)[1])
        elif arg == "--link-output":
            if i + 1 >= len(module_args):
                _write_text("Error: --link-output requires a value", err=True)
                return 2
            link_output = module_args[i + 1]
            i += 1
        elif arg.startswith("--link-output="):
            link_output = arg.split("=", 1)[1]
        elif arg == "--abi":
            if i + 1 >= len(module_args):
                _write_text("Error: --abi requires a value", err=True)
                return 2
            abi_mode = module_args[i + 1]
            i += 1
        elif arg.startswith("--abi="):
            abi_mode = arg.split("=", 1)[1]
        elif arg == "--execute":
            execute = True
        elif arg == "--regenerate-cython":
            regenerate_cython = True
        elif arg == "--run-f2py":
            run_f2py = True
        elif arg == "--from-compile-commands":
            from_compile_commands = True
        elif arg == "--from-meson-introspection":
            from_meson_introspection = True
        elif arg == "--configure-meson":
            configure_meson = True
        elif arg == "--enforce-generated-c":
            enforce_generated_c = True
        elif not arg.startswith("-"):
            name = arg
        i += 1
    report = _native_build_exec_json(
        name,
        path,
        search_paths,
        include_dirs,
        library_dirs,
        execute,
        regenerate_cython,
        run_f2py,
        link_output,
        libraries,
        abi_mode,
        from_compile_commands,
        from_meson_introspection,
        configure_meson,
        enforce_generated_c,
    )
    _write_text(report)
    return 2 if _native_find_from(report, '"ok": false', 0) >= 0 else 0


def _run_native_package_ext_abi_from_pcc1(module_args) -> int:
    symbols = []
    provider = "extension"
    expected_abi = None
    actual_abi = None
    abi_mode = "pcc-native"
    include_dir = None
    require_capsule = False
    require_buffer = False
    require_memoryview = False
    require_numpy_capi = False
    i = 0
    while i < len(module_args):
        arg = module_args[i]
        if arg == "--symbol":
            if i + 1 >= len(module_args):
                _write_text("Error: --symbol requires a value", err=True)
                return 2
            symbols.append(module_args[i + 1])
            i += 1
        elif arg.startswith("--symbol="):
            symbols.append(arg.split("=", 1)[1])
        elif arg == "--provider":
            if i + 1 >= len(module_args):
                _write_text("Error: --provider requires a value", err=True)
                return 2
            provider = module_args[i + 1]
            i += 1
        elif arg.startswith("--provider="):
            provider = arg.split("=", 1)[1]
        elif arg == "--expected-abi":
            if i + 1 >= len(module_args):
                _write_text("Error: --expected-abi requires a value", err=True)
                return 2
            expected_abi = int(module_args[i + 1])
            i += 1
        elif arg.startswith("--expected-abi="):
            expected_abi = int(arg.split("=", 1)[1])
        elif arg == "--actual-abi":
            if i + 1 >= len(module_args):
                _write_text("Error: --actual-abi requires a value", err=True)
                return 2
            actual_abi = int(module_args[i + 1])
            i += 1
        elif arg.startswith("--actual-abi="):
            actual_abi = int(arg.split("=", 1)[1])
        elif arg == "--abi":
            if i + 1 >= len(module_args):
                _write_text("Error: --abi requires a value", err=True)
                return 2
            abi_mode = module_args[i + 1]
            i += 1
        elif arg.startswith("--abi="):
            abi_mode = arg.split("=", 1)[1]
        elif arg == "--include-dir":
            if i + 1 >= len(module_args):
                _write_text("Error: --include-dir requires a value", err=True)
                return 2
            include_dir = module_args[i + 1]
            i += 1
        elif arg.startswith("--include-dir="):
            include_dir = arg.split("=", 1)[1]
        elif arg == "--require-capsule":
            require_capsule = True
        elif arg == "--require-buffer":
            require_buffer = True
        elif arg == "--require-memoryview":
            require_memoryview = True
        elif arg == "--require-numpy-capi":
            require_numpy_capi = True
        elif arg == "--json":
            pass
        i += 1
    _write_text(
        _native_extension_abi_json(
            symbols,
            provider,
            expected_abi,
            actual_abi,
            abi_mode,
            include_dir,
            require_capsule,
            require_buffer,
            require_memoryview,
            require_numpy_capi,
        )
    )
    if _native_extension_abi_ok(
        symbols,
        expected_abi,
        actual_abi,
        require_capsule,
        require_buffer,
        require_memoryview,
        require_numpy_capi,
        include_dir,
    ):
        return 0
    return 2


def _native_campaign_pattern_match(name: str, pattern: str) -> bool:
    if pattern == "test_*.py":
        return name.startswith("test_") and name.endswith(".py")
    star = _native_find_from(pattern, "*", 0)
    if star < 0:
        return name == pattern
    prefix = pattern[:star]
    suffix = pattern[star + 1 :]
    return name.startswith(prefix) and name.endswith(suffix)


def _native_campaign_filter_match(path: str, includes, excludes) -> bool:
    if len(includes) > 0:
        matched = False
        i = 0
        while i < len(includes):
            if _native_find_from(path, includes[i], 0) >= 0:
                matched = True
            i += 1
        if not matched:
            return False
    i = 0
    while i < len(excludes):
        if _native_find_from(path, excludes[i], 0) >= 0:
            return False
        i += 1
    return True


def _native_campaign_xfail_reason(path: str, xfails):
    i = 0
    while i < len(xfails):
        rule = xfails[i]
        eq = _native_find_from(rule, "=", 0)
        colon = _native_find_from(rule, ":", 0)
        split = eq
        if split < 0 or (colon >= 0 and colon < split):
            split = colon
        if split >= 0:
            token = rule[:split]
            reason = rule[split + 1 :]
        else:
            token = rule
            reason = "unspecified"
        if token != "" and _native_find_from(path, token, 0) >= 0:
            return reason or "unspecified"
        i += 1
    return None


def _native_campaign_profile_root(root: str, profile: str) -> str:
    data = campaign_profile(profile)
    if data is not None:
        nested = root
        i = 0
        parts = data["root_parts"]
        while i < len(parts):
            nested += "/" + parts[i]
            i += 1
        if os.path.isdir(nested):
            return nested
    return root


def _native_campaign_profile_task(path: str, profile: str) -> str:
    data = campaign_profile(profile)
    if data is None:
        return ""
    name = _native_basename(path)
    metadata = data["files"].get(name)
    return metadata[0] if metadata is not None else ""


def _native_campaign_profile_feature(path: str, profile: str) -> str:
    data = campaign_profile(profile)
    if data is None:
        return ""
    name = _native_basename(path)
    metadata = data["files"].get(name)
    return metadata[1] if metadata is not None else ""


def _native_campaign_profile_selected(path: str, profile: str) -> bool:
    if profile == "":
        return True
    return _native_campaign_profile_task(path, profile) != ""


def _native_campaign_json(
    root: str, pattern: str, area: str, includes, excludes, xfails, profile: str
) -> str:
    scan_root = _native_campaign_profile_root(root, profile)
    effective_area = area
    profile_data = campaign_profile(profile)
    if profile_data is not None and area == profile_data["default_area"]:
        effective_area = profile_data["area"]
    selected = []
    stack = [scan_root]
    while len(stack) > 0:
        current = stack.pop()
        try:
            names = sorted(os.listdir(current))
        except Exception:
            names = []
        i = 0
        while i < len(names):
            name = names[i]
            path = current + "/" + name
            if os.path.isdir(path):
                stack.append(path)
            elif os.path.isfile(path) and _native_campaign_pattern_match(name, pattern):
                if _native_campaign_profile_selected(
                    path, profile
                ) and _native_campaign_filter_match(path, includes, excludes):
                    selected.append(path)
            i += 1
    selected = sorted(selected)

    selected_count = 0
    xfail_count = 0
    xfail_reasons = []
    task_names = []
    records = "["
    i = 0
    while i < len(selected):
        reason = _native_campaign_xfail_reason(selected[i], xfails)
        status = "selected"
        task = _native_campaign_profile_task(selected[i], profile)
        feature = _native_campaign_profile_feature(selected[i], profile)
        if task != "" and not _native_list_contains(task_names, task):
            task_names.append(task)
        if reason is not None:
            status = "xfail"
            xfail_count += 1
            if not _native_list_contains(xfail_reasons, reason):
                xfail_reasons.append(reason)
        else:
            selected_count += 1
            reason = ""
        if records != "[":
            records += ", "
        records += "{"
        records += '"area": ' + _json_str(effective_area)
        if feature != "":
            records += ', "feature": ' + _json_str(feature)
        records += ', "path": ' + _json_str(selected[i])
        if profile != "":
            records += ', "profile": ' + _json_str(profile)
        records += ', "reason": ' + _json_str(reason)
        records += ', "status": ' + _json_str(status)
        if task != "":
            records += ', "task": ' + _json_str(task)
        records += "}"
        i += 1
    records += "]"

    xfail_taxonomy = "{"
    i = 0
    while i < len(xfail_reasons):
        if i > 0:
            xfail_taxonomy += ", "
        reason = xfail_reasons[i]
        count = 0
        j = 0
        while j < len(selected):
            if _native_campaign_xfail_reason(selected[j], xfails) == reason:
                count += 1
            j += 1
        xfail_taxonomy += _json_str(reason) + ": " + str(count)
        i += 1
    xfail_taxonomy += "}"

    task_counts = "{"
    task_names = sorted(task_names)
    i = 0
    while i < len(task_names):
        if i > 0:
            task_counts += ", "
        task = task_names[i]
        count = 0
        j = 0
        while j < len(selected):
            if _native_campaign_profile_task(selected[j], profile) == task:
                count += 1
            j += 1
        task_counts += _json_str(task) + ": " + str(count)
        i += 1
    task_counts += "}"

    dashboard = "{"
    dashboard += '"by_area": {'
    dashboard += _json_str(effective_area) + ": {"
    dashboard += '"fail": 0, "pass": 0, "selected": ' + str(selected_count)
    dashboard += ', "skip": 0, "xfail": ' + str(xfail_count)
    dashboard += "}}"
    dashboard += ', "by_status": {'
    dashboard += '"fail": 0, "pass": 0, "selected": ' + str(selected_count)
    dashboard += ', "skip": 0, "xfail": ' + str(xfail_count)
    dashboard += "}"
    dashboard += ', "total": ' + str(len(selected))
    dashboard += ', "xfail_taxonomy": ' + xfail_taxonomy
    dashboard += "}"

    out = "{"
    out += '"area": ' + _json_str(effective_area)
    out += ', "dashboard": ' + dashboard
    out += ', "exclude": ' + _json_str_list(excludes)
    out += ', "include": ' + _json_str_list(includes)
    out += ', "pattern": ' + _json_str(pattern)
    out += ', "profile": ' + _json_str(profile)
    if profile_data is not None:
        out += ', "profile_description": ' + _json_str(profile_data["description"])
        out += ', "selection_rule": ' + _json_str(profile_data["selection_rule"])
    else:
        out += ', "profile_description": ""'
        out += ', "selection_rule": ' + _json_str("pattern/include/exclude")
    out += ', "records": ' + records
    out += ', "root": ' + _json_str(root)
    out += ', "scan_root": ' + _json_str(scan_root)
    out += ', "selected": ' + _json_str_list(selected)
    out += ', "task_counts": ' + task_counts
    out += ', "xfail_rules": ' + _json_str_list(xfails)
    out += "}"
    return out


def _run_native_package_campaign_from_pcc1(module_args) -> int:
    root = None
    pattern = "test_*.py"
    area = "core"
    includes = []
    excludes = []
    xfails = []
    profile = ""
    out_path = None
    i = 0
    while i < len(module_args):
        arg = module_args[i]
        if arg == "--root":
            if i + 1 >= len(module_args):
                _write_text("Error: --root requires a value", err=True)
                return 2
            root = module_args[i + 1]
            i += 1
        elif arg.startswith("--root="):
            root = arg.split("=", 1)[1]
        elif arg == "--pattern":
            if i + 1 >= len(module_args):
                _write_text("Error: --pattern requires a value", err=True)
                return 2
            pattern = module_args[i + 1]
            i += 1
        elif arg.startswith("--pattern="):
            pattern = arg.split("=", 1)[1]
        elif arg == "--area":
            if i + 1 >= len(module_args):
                _write_text("Error: --area requires a value", err=True)
                return 2
            area = module_args[i + 1]
            i += 1
        elif arg.startswith("--area="):
            area = arg.split("=", 1)[1]
        elif arg == "--include":
            if i + 1 >= len(module_args):
                _write_text("Error: --include requires a value", err=True)
                return 2
            includes.append(module_args[i + 1])
            i += 1
        elif arg.startswith("--include="):
            includes.append(arg.split("=", 1)[1])
        elif arg == "--exclude":
            if i + 1 >= len(module_args):
                _write_text("Error: --exclude requires a value", err=True)
                return 2
            excludes.append(module_args[i + 1])
            i += 1
        elif arg.startswith("--exclude="):
            excludes.append(arg.split("=", 1)[1])
        elif arg == "--xfail":
            if i + 1 >= len(module_args):
                _write_text("Error: --xfail requires a value", err=True)
                return 2
            xfails.append(module_args[i + 1])
            i += 1
        elif arg.startswith("--xfail="):
            xfails.append(arg.split("=", 1)[1])
        elif arg == "--profile":
            if i + 1 >= len(module_args):
                _write_text("Error: --profile requires a value", err=True)
                return 2
            profile = module_args[i + 1]
            i += 1
        elif arg.startswith("--profile="):
            profile = arg.split("=", 1)[1]
        elif arg == "--out":
            if i + 1 >= len(module_args):
                _write_text("Error: --out requires a value", err=True)
                return 2
            out_path = module_args[i + 1]
            i += 1
        elif arg.startswith("--out="):
            out_path = arg.split("=", 1)[1]
        elif arg == "--json":
            pass
        i += 1
    if root is None:
        _write_text('{"error": "missing --root", "ok": false}')
        return 2
    if profile != "" and campaign_profile(profile) is None:
        _write_text('{"error": "unknown campaign profile", "ok": false}')
        return 2
    report = _native_campaign_json(
        root, pattern, area, includes, excludes, xfails, profile
    )
    if out_path is not None:
        try:
            with open(out_path, "w") as fh:
                fh.write(report)
        except Exception:
            _write_text('{"error": "failed to write campaign report", "ok": false}')
            return 2
    _write_text(report)
    return 0


def _native_find_tool_json(names, search_paths):
    found_name = names[0]
    found_path = None
    probe_ok = False
    probe_output = ""
    i = 0
    while i < len(names):
        j = 0
        while j < len(search_paths):
            candidate = search_paths[j] + "/" + names[i]
            if os.path.isfile(candidate):
                found_name = names[i]
                found_path = candidate
                probe = _native_probe_tool(candidate, names[i])
                probe_ok = probe[0]
                probe_output = probe[1]
                if probe_ok:
                    break
            j += 1
        if found_path is not None and probe_ok:
            break
        i += 1
    out = "{"
    out += '"found": ' + ("true" if found_path is not None and probe_ok else "false")
    out += ', "name": ' + _json_str(found_name)
    out += ', "path": ' + _json_str_or_null(found_path)
    out += ', "probe_ok": ' + ("true" if probe_ok else "false")
    out += ', "probe_output": ' + _json_str(probe_output)
    out += "}"
    return out


def _native_probe_tool(path: str, name: str):
    arg = "--version"
    if name == "cython" or name == "cython3":
        arg = "-V"
    output_path = "/tmp/pcc_tool_probe_" + str(os.getpid()) + "_" + name
    command = (
        "'"
        + path.replace("'", "'\"'\"'")
        + "' "
        + arg
        + " > '"
        + output_path
        + "' 2>&1"
    )
    ok = True
    try:
        _bootstrap_subprocess_run(["/bin/sh", "-c", command], check=True)
    except Exception as exc:
        ok = False
    try:
        with open(output_path, "r") as fh:
            output = fh.read()
    except Exception:
        output = ""
    try:
        _bootstrap_subprocess_run(["rm", "-f", output_path], check=True)
    except Exception:
        pass
    return [ok, output.strip()]


def _native_version_parts(text: str):
    start = -1
    i = 0
    while i < len(text):
        ch = text[i]
        if ch >= "0" and ch <= "9":
            start = i
            break
        i += 1
    if start < 0:
        return []
    token = ""
    i = start
    while i < len(text):
        ch = text[i]
        if (ch >= "0" and ch <= "9") or ch == ".":
            token += ch
        else:
            break
        i += 1
    raw_parts = token.split(".")
    parts = []
    i = 0
    while i < len(raw_parts):
        if raw_parts[i] != "":
            try:
                parts.append(int(raw_parts[i]))
            except Exception:
                parts.append(0)
        i += 1
    return parts


def _native_version_less(actual_text: str, minimum_text: str) -> bool:
    actual = _native_version_parts(actual_text)
    minimum = _native_version_parts(minimum_text)
    if len(actual) == 0 or len(minimum) == 0:
        return False
    length = len(actual)
    if len(minimum) > length:
        length = len(minimum)
    i = 0
    while i < length:
        a = actual[i] if i < len(actual) else 0
        b = minimum[i] if i < len(minimum) else 0
        if a < b:
            return True
        if a > b:
            return False
        i += 1
    return False


def _native_tool_found(names, search_paths) -> bool:
    i = 0
    while i < len(names):
        j = 0
        while j < len(search_paths):
            candidate = search_paths[j] + "/" + names[i]
            if os.path.isfile(candidate) and _native_probe_tool(candidate, names[i])[0]:
                return True
            j += 1
        i += 1
    return False


def _native_find_tool_path(names, search_paths):
    i = 0
    while i < len(names):
        j = 0
        while j < len(search_paths):
            candidate = search_paths[j] + "/" + names[i]
            if os.path.isfile(candidate):
                return candidate
            j += 1
        i += 1
    return None


def _native_find_library_json(names, library_dirs):
    found_name = names[0]
    found_path = None
    suffixes = [".so", ".dylib", ".a", ".dll"]
    i = 0
    while i < len(library_dirs):
        root = library_dirs[i]
        try:
            entries = sorted(os.listdir(root))
        except Exception:
            entries = []
        j = 0
        while j < len(entries):
            entry = entries[j]
            path = root + "/" + entry
            if os.path.isfile(path):
                lower = entry.lower()
                k = 0
                while k < len(names):
                    prefix1 = "lib" + names[k]
                    prefix2 = names[k]
                    m = 0
                    while m < len(suffixes):
                        if (
                            lower.startswith(prefix1) or lower.startswith(prefix2)
                        ) and lower.endswith(suffixes[m]):
                            found_name = names[k]
                            found_path = path
                        m += 1
                    k += 1
            if found_path is not None:
                break
            j += 1
        if found_path is not None:
            break
        i += 1
    out = "{"
    out += '"checked_dirs": ' + _json_str_list(library_dirs)
    out += ', "found": ' + ("true" if found_path is not None else "false")
    out += ', "name": ' + _json_str(found_name)
    out += ', "path": ' + _json_str_or_null(found_path)
    out += "}"
    return out


def _native_library_found(names, library_dirs) -> bool:
    suffixes = [".so", ".dylib", ".a", ".dll"]
    i = 0
    while i < len(library_dirs):
        root = library_dirs[i]
        try:
            entries = sorted(os.listdir(root))
        except Exception:
            entries = []
        j = 0
        while j < len(entries):
            lower = entries[j].lower()
            k = 0
            while k < len(names):
                m = 0
                while m < len(suffixes):
                    if (
                        lower.startswith("lib" + names[k]) or lower.startswith(names[k])
                    ) and lower.endswith(suffixes[m]):
                        return True
                    m += 1
                k += 1
            j += 1
        i += 1
    return False


def _native_find_library_binding_values(request: str, library_dirs):
    names = [request]
    if request == "blas":
        names = ["openblas", "blas"]
    suffixes = [".so", ".dylib", ".a", ".dll"]
    i = 0
    while i < len(library_dirs):
        root = library_dirs[i]
        try:
            entries = sorted(os.listdir(root))
        except Exception:
            entries = []
        j = 0
        while j < len(entries):
            entry = entries[j]
            lower = entry.lower()
            k = 0
            while k < len(names):
                name = names[k]
                m = 0
                while m < len(suffixes):
                    suffix = suffixes[m]
                    if (
                        lower.startswith("lib" + name) or lower.startswith(name)
                    ) and lower.endswith(suffix):
                        link_name = entry
                        if link_name.startswith("lib"):
                            link_name = link_name[3:]
                        if link_name.endswith(suffix):
                            link_name = link_name[: len(link_name) - len(suffix)]
                        return [True, link_name, root + "/" + entry]
                    m += 1
                k += 1
            j += 1
        i += 1
    return [False, request, None]


def _native_vendor_bindings_json(libraries, library_dirs, diagnostics) -> str:
    out = "["
    i = 0
    while i < len(libraries):
        binding = _native_find_library_binding_values(libraries[i], library_dirs)
        if i > 0:
            out += ", "
        out += "{"
        out += '"found": ' + ("true" if binding[0] else "false")
        out += ', "link_name": ' + _json_str(binding[1])
        out += ', "path": ' + _json_str_or_null(binding[2])
        out += ', "request": ' + _json_str(libraries[i])
        out += "}"
        if not binding[0] and not _native_list_contains(
            diagnostics, "PCC-PKG-MISSING-LIBRARY"
        ):
            diagnostics.append("PCC-PKG-MISSING-LIBRARY")
        i += 1
    out += "]"
    return out


def _native_toolchain_json(
    search_paths,
    library_dirs,
    require_fortran: bool,
    require_blas: bool,
    require_lapack: bool,
    require_cython: bool,
    require_f2py: bool,
    min_cython_version,
) -> str:
    diagnostics = []
    if require_fortran and not _native_tool_found(
        ["gfortran", "flang", "ifx", "ifort"], search_paths
    ):
        diagnostics.append("PCC-PKG-MISSING-FORTRAN")
    if require_cython and not _native_tool_found(["cython", "cython3"], search_paths):
        diagnostics.append("PCC-PKG-MISSING-CYTHON")
    cython_json = _native_find_tool_json(["cython", "cython3"], search_paths)
    if (
        require_cython
        and min_cython_version is not None
        and _native_find_from(cython_json, '"found": true', 0) >= 0
    ):
        marker = '"probe_output": "'
        pos = _native_find_from(cython_json, marker, 0)
        output = ""
        if pos >= 0:
            start = pos + len(marker)
            end = _native_find_from(cython_json, '"', start)
            if end > start:
                output = cython_json[start:end]
        if _native_version_less(output, min_cython_version):
            diagnostics.append("PCC-PKG-CYTHON-VERSION-TOO-OLD")
    if require_f2py and not _native_tool_found(["f2py", "f2py3"], search_paths):
        diagnostics.append("PCC-PKG-MISSING-F2PY")
    if require_blas and not _native_library_found(["openblas", "blas"], library_dirs):
        diagnostics.append("PCC-PKG-MISSING-BLAS")
    if require_lapack and not _native_library_found(["lapack"], library_dirs):
        diagnostics.append("PCC-PKG-MISSING-LAPACK")

    diag_json = "["
    i = 0
    while i < len(diagnostics):
        if i > 0:
            diag_json += ", "
        diag_json += "{"
        diag_json += '"code": ' + _json_str(diagnostics[i])
        diag_json += (
            ', "message": "required package build toolchain component is missing"'
        )
        diag_json += "}"
        i += 1
    diag_json += "]"

    out = "{"
    out += '"diagnostics": ' + diag_json
    out += ', "libraries": {'
    out += '"blas": ' + _native_find_library_json(["openblas", "blas"], library_dirs)
    out += ', "lapack": ' + _native_find_library_json(["lapack"], library_dirs)
    out += "}"
    out += ', "library_dirs": ' + _json_str_list(library_dirs)
    out += ', "ok": ' + ("true" if len(diagnostics) == 0 else "false")
    out += ', "requirements": {'
    out += '"blas": ' + ("true" if require_blas else "false")
    out += ', "cython": ' + ("true" if require_cython else "false")
    out += ', "f2py": ' + ("true" if require_f2py else "false")
    out += ', "fortran": ' + ("true" if require_fortran else "false")
    out += ', "lapack": ' + ("true" if require_lapack else "false")
    out += "}"
    out += ', "version_requirements": {'
    out += '"cython": ' + _json_str_or_null(min_cython_version)
    out += "}"
    out += ', "search_paths": ' + _json_str_list(search_paths)
    out += ', "tools": {'
    out += '"c_compiler": ' + _native_find_tool_json(
        ["cc", "clang", "gcc"], search_paths
    )
    out += ', "cxx_compiler": ' + _native_find_tool_json(
        ["c++", "clang++", "g++"], search_paths
    )
    out += ', "cython": ' + cython_json
    out += ', "f2py": ' + _native_find_tool_json(["f2py", "f2py3"], search_paths)
    out += ', "fortran_compiler": ' + _native_find_tool_json(
        ["gfortran", "flang", "ifx", "ifort"], search_paths
    )
    out += "}"
    out += "}"
    return out


def _run_native_package_toolchain_from_pcc1(module_args) -> int:
    search_paths = []
    library_dirs = []
    require_fortran = False
    require_blas = False
    require_lapack = False
    require_cython = False
    require_f2py = False
    min_cython_version = None
    i = 0
    while i < len(module_args):
        arg = module_args[i]
        if arg == "--search-path":
            if i + 1 >= len(module_args):
                _write_text("Error: --search-path requires a value", err=True)
                return 2
            search_paths.append(module_args[i + 1])
            i += 1
        elif arg.startswith("--search-path="):
            search_paths.append(arg.split("=", 1)[1])
        elif arg == "--library-dir":
            if i + 1 >= len(module_args):
                _write_text("Error: --library-dir requires a value", err=True)
                return 2
            library_dirs.append(module_args[i + 1])
            i += 1
        elif arg.startswith("--library-dir="):
            library_dirs.append(arg.split("=", 1)[1])
        elif arg == "--require-fortran":
            require_fortran = True
        elif arg == "--require-blas":
            require_blas = True
        elif arg == "--require-lapack":
            require_lapack = True
        elif arg == "--require-cython":
            require_cython = True
        elif arg == "--require-f2py":
            require_f2py = True
        elif arg == "--min-cython-version":
            if i + 1 >= len(module_args):
                _write_text("Error: --min-cython-version requires a value", err=True)
                return 2
            min_cython_version = module_args[i + 1]
            i += 1
        elif arg.startswith("--min-cython-version="):
            min_cython_version = arg.split("=", 1)[1]
        elif arg == "--json":
            pass
        i += 1
    report = _native_toolchain_json(
        search_paths,
        library_dirs,
        require_fortran,
        require_blas,
        require_lapack,
        require_cython,
        require_f2py,
        min_cython_version,
    )
    _write_text(report)
    if (
        (
            require_fortran
            and not _native_tool_found(
                ["gfortran", "flang", "ifx", "ifort"], search_paths
            )
        )
        or (
            require_cython
            and not _native_tool_found(["cython", "cython3"], search_paths)
        )
        or (require_f2py and not _native_tool_found(["f2py", "f2py3"], search_paths))
        or (
            require_blas
            and not _native_library_found(["openblas", "blas"], library_dirs)
        )
        or (require_lapack and not _native_library_found(["lapack"], library_dirs))
        or (
            require_cython
            and min_cython_version is not None
            and _native_find_from(report, "PCC-PKG-CYTHON-VERSION-TOO-OLD", 0) >= 0
        )
    ):
        return 2
    return 0


def _native_ascii_digit(ch: str) -> bool:
    return "0" <= ch and ch <= "9"


def _native_libpython_sep_char(ch: str) -> bool:
    # The host separator class [\s/:=,-] from linkage._LIBPYTHON_PATTERNS
    # (\v/\f omitted: platform tool output never contains them).
    return (
        ch == " "
        or ch == "\t"
        or ch == "\n"
        or ch == "\r"
        or ch == "/"
        or ch == ":"
        or ch == "="
        or ch == ","
        or ch == "-"
    )


def _native_version_digits_end(text: str, start: int) -> int:
    """Consume the host version-suffix shape ``\\d*(\\.\\d+)*`` from start."""
    end = start
    n = len(text)
    while end < n and _native_ascii_digit(text[end]):
        end += 1
    while end + 1 < n and text[end] == "." and _native_ascii_digit(text[end + 1]):
        end += 2
        while end < n and _native_ascii_digit(text[end]):
            end += 1
    return end


def _native_libpython_match_span(text: str):
    """First host-parity libpython edge as ``[start, end]``, or ``[-1, -1]``.

    Detection mirrors linkage._LIBPYTHON_PATTERNS exactly:
      1. ``libpython`` must be preceded by start-or-separator AND followed by
         a version digit. pcc's own runtime diagnostic literal
         ``[pcc-native/no-libpython]`` is embedded in every artifact that
         links libpy_runtime.a, so the previous bare-substring match flagged
         every pcc-native artifact as libpython-linked (false PCC-PKG-003).
      2. ``-lpython`` is a linker flag: start-or-whitespace before, version
         digits optional.
      3. ``Python.framework`` is case-sensitive with separator boundaries on
         both sides.
      4. ``python<digits>.dll`` for Windows probe lines.
    The edge slice starts at the marker itself (the host regex group may keep
    one leading separator character; the gates compare detection, not the
    edge spelling).
    """
    lower = text.lower()
    n = len(text)
    idx = _native_find_from(lower, "libpython", 0)
    while idx >= 0:
        if idx == 0 or _native_libpython_sep_char(text[idx - 1]):
            digits = idx + 9
            if digits < n and _native_ascii_digit(text[digits]):
                return [idx, _native_version_digits_end(text, digits)]
        idx = _native_find_from(lower, "libpython", idx + 1)
    idx = _native_find_from(lower, "-lpython", 0)
    while idx >= 0:
        prev_ok = idx == 0
        if idx > 0:
            ch = text[idx - 1]
            prev_ok = ch == " " or ch == "\t" or ch == "\n" or ch == "\r"
        if prev_ok:
            return [idx, _native_version_digits_end(text, idx + 8)]
        idx = _native_find_from(lower, "-lpython", idx + 1)
    pos = _native_find_from(text, "Python.framework", 0)
    while pos >= 0:
        lead_ok = pos == 0 or _native_libpython_sep_char(text[pos - 1])
        end = pos + 16
        tail_ok = end >= n
        if not tail_ok:
            ch = text[end]
            tail_ok = (
                ch == "/"
                or ch == " "
                or ch == "\t"
                or ch == "\n"
                or ch == "\r"
                or ch == ":"
                or ch == "="
                or ch == ","
                or ch == "-"
            )
        if lead_ok and tail_ok:
            return [pos, end]
        pos = _native_find_from(text, "Python.framework", pos + 1)
    idx = _native_find_from(lower, "python", 0)
    while idx >= 0:
        digits = idx + 6
        if digits < n and _native_ascii_digit(text[digits]):
            end = _native_version_digits_end(text, digits)
            if (
                end + 4 <= n
                and lower[end] == "."
                and lower[end + 1] == "d"
                and lower[end + 2] == "l"
                and lower[end + 3] == "l"
            ):
                return [idx, end + 4]
        idx = _native_find_from(lower, "python", idx + 1)
    return [-1, -1]


def _native_text_has_libpython(text: str) -> bool:
    span = _native_libpython_match_span(text)
    return span[0] >= 0


def _native_libpython_edge(text: str) -> str:
    span = _native_libpython_match_span(text)
    if span[0] >= 0:
        return text[span[0] : span[1]]
    return ""


def _native_libpython_grep_pattern() -> str:
    # Digit-aware prefilter so ``grep -m 1`` surfaces a real edge line rather
    # than pcc's own "[pcc-native/no-libpython]" diagnostic;
    # _native_libpython_match_span stays the authoritative check on the line.
    return (
        "libpython[0-9]|-lpython|Python[.]framework([/[:space:]:=,-]|$)"
        "|python[0-9][0-9.]*[.]dll"
    )


def _native_command_output_line(command: str, label: str) -> str:
    output_path = "/tmp/pcc_" + label + "_" + str(os.getpid())
    redirected = command + " > " + _native_shell_quote(output_path) + " 2>/dev/null"
    try:
        _bootstrap_subprocess_run(["/bin/sh", "-c", redirected], check=True)
    except Exception:
        pass
    try:
        with open(output_path, "r") as fh:
            text = fh.read()
    except Exception:
        text = ""
    try:
        _bootstrap_subprocess_run(["rm", "-f", output_path], check=True)
    except Exception:
        pass
    pos = _native_find_from(text, "\n", 0)
    if pos >= 0:
        text = text[:pos]
    return text.strip()


def _native_runtime_artifact_libpython_edge(path: str) -> str:
    # pcc1 string indexing is intentionally minimal and slow for very large
    # binary blobs. Probe native artifacts through platform tools and scan only
    # their small textual output instead of reading full .so/.dylib contents.
    quoted = _native_shell_quote(path)
    pattern = _native_shell_quote(_native_libpython_grep_pattern())
    commands = [
        "if command -v otool >/dev/null 2>&1; then otool -L "
        + quoted
        + " | grep -i -m 1 -E "
        + pattern
        + "; fi",
        "if command -v readelf >/dev/null 2>&1; then readelf -d "
        + quoted
        + " | grep -i -m 1 -E "
        + pattern
        + "; fi",
        "if command -v objdump >/dev/null 2>&1; then objdump -p "
        + quoted
        + " | grep -i -m 1 -E "
        + pattern
        + "; fi",
        "if command -v strings >/dev/null 2>&1; then strings -a "
        + quoted
        + " | grep -i -m 1 -E "
        + pattern
        + "; fi",
    ]
    i = 0
    while i < len(commands):
        text = _native_command_output_line(commands[i], "linkage_scan")
        if text != "" and _native_text_has_libpython(text):
            return _native_libpython_edge(text)
        i += 1
    return ""


def _native_is_archive_artifact_path(path: str) -> bool:
    lower = path.lower()
    return (
        lower.endswith(".whl")
        or lower.endswith(".zip")
        or lower.endswith(".tar.gz")
        or lower.endswith(".tar.bz2")
        or lower.endswith(".tar.xz")
        or lower.endswith(".tgz")
    )


def _native_archive_mentions_libpython(path: str) -> str:
    lower = path.lower()
    if not _native_is_archive_artifact_path(path):
        return ""
    extract_root = (
        "/tmp/pcc-linkage-scan-" + _native_basename(path) + "." + str(os.getpid())
    )
    try:
        _bootstrap_subprocess_run(["rm", "-rf", extract_root], check=True)
        _bootstrap_subprocess_run(["mkdir", "-p", extract_root], check=True)
        if lower.endswith(".whl") or lower.endswith(".zip"):
            _bootstrap_subprocess_run(
                ["env", "LC_ALL=C", "LANG=C", "unzip", "-q", path, "-d", extract_root],
                check=True,
            )
        else:
            _bootstrap_subprocess_run(
                ["env", "LC_ALL=C", "LANG=C", "tar", "-xf", path, "-C", extract_root],
                check=True,
            )
        edges = _native_linkage_edges_for_root(extract_root)
        _bootstrap_subprocess_run(["rm", "-rf", extract_root], check=True)
        if len(edges) > 0:
            return edges[0]
    except Exception:
        try:
            _bootstrap_subprocess_run(["rm", "-rf", extract_root], check=True)
        except Exception:
            pass
    return ""


def _native_artifact_mentions_libpython(path: str) -> str:
    if path.lower().endswith(".a"):
        return ""
    if _native_is_archive_artifact_path(path):
        return _native_archive_mentions_libpython(path)
    if _native_is_native_artifact(path):
        return _native_runtime_artifact_libpython_edge(path)
    try:
        with open(path, "r") as fh:
            text = fh.read()
    except Exception:
        text = ""
    if _native_text_has_libpython(text):
        return _native_libpython_edge(text)
    edge = _native_archive_mentions_libpython(path)
    if edge:
        return edge
    return ""


def _native_name_uses_cpython_extension_abi(path: str) -> bool:
    lower = _native_basename(path).lower()
    if _native_find_from(lower, ".cpython-", 0) >= 0:
        return True
    if _native_find_from(lower, "-cpython-", 0) >= 0:
        return True
    if _native_find_from(lower, "_cpython-", 0) >= 0:
        return True
    if _native_find_from(lower, ".abi3", 0) >= 0:
        return True
    if _native_find_from(lower, "-abi3", 0) >= 0:
        return True
    if _native_find_from(lower, "_abi3", 0) >= 0:
        return True
    first_cp = _native_find_from(lower, "-cp", 0)
    if first_cp >= 0 and _native_find_from(lower, "-cp", first_cp + 3) >= 0:
        return True
    return False


def _native_archive_uses_cpython_extension_abi(path: str) -> bool:
    lower = path.lower()
    if not _native_is_archive_artifact_path(path):
        return False
    extract_root = (
        "/tmp/pcc-linkage-abi-scan-" + _native_basename(path) + "." + str(os.getpid())
    )
    try:
        _bootstrap_subprocess_run(["rm", "-rf", extract_root], check=True)
        _bootstrap_subprocess_run(["mkdir", "-p", extract_root], check=True)
        if lower.endswith(".whl") or lower.endswith(".zip"):
            _bootstrap_subprocess_run(
                ["env", "LC_ALL=C", "LANG=C", "unzip", "-q", path, "-d", extract_root],
                check=True,
            )
        else:
            _bootstrap_subprocess_run(
                ["env", "LC_ALL=C", "LANG=C", "tar", "-xf", path, "-C", extract_root],
                check=True,
            )
        artifacts = _native_collect_artifacts(extract_root)
        i = 0
        while i < len(artifacts):
            if _native_name_uses_cpython_extension_abi(artifacts[i]):
                _bootstrap_subprocess_run(["rm", "-rf", extract_root], check=True)
                return True
            i += 1
        _bootstrap_subprocess_run(["rm", "-rf", extract_root], check=True)
    except Exception:
        try:
            _bootstrap_subprocess_run(["rm", "-rf", extract_root], check=True)
        except Exception:
            pass
    return False


def _native_artifact_uses_cpython_extension_abi(path: str) -> bool:
    if _native_is_archive_artifact_path(path):
        return _native_archive_uses_cpython_extension_abi(path)
    if _native_is_native_artifact(path):
        return _native_name_uses_cpython_extension_abi(path)
    return False


def _native_is_native_artifact(path: str) -> bool:
    lower = path.lower()
    return (
        lower.endswith(".so")
        or lower.endswith(".dylib")
        or lower.endswith(".pyd")
        or lower.endswith(".dll")
    )


def _native_collect_artifacts(root: str):
    artifacts = []
    if os.path.isfile(root):
        if _native_is_native_artifact(root):
            artifacts.append(root)
        return artifacts
    stack = [root]
    while len(stack) > 0:
        current = stack.pop()
        try:
            names = sorted(os.listdir(current))
        except Exception:
            names = []
        i = 0
        while i < len(names):
            path = current + "/" + names[i]
            if os.path.isdir(path):
                stack.append(path)
            elif os.path.isfile(path) and _native_is_native_artifact(path):
                artifacts.append(path)
            i += 1
    return artifacts


def _native_linkage_edges_for_root(root: str):
    edges = []
    artifacts = _native_collect_artifacts(root)
    i = 0
    while i < len(artifacts):
        edge = _native_artifact_mentions_libpython(artifacts[i])
        if edge and not _native_list_contains(edges, edge):
            edges.append(edge)
        i += 1
    return edges


def _native_cpython_extension_abi_paths_for_root(root: str):
    out = []
    artifacts = _native_collect_artifacts(root)
    i = 0
    while i < len(artifacts):
        path = artifacts[i]
        if _native_artifact_uses_cpython_extension_abi(
            path
        ) and not _native_list_contains(out, path):
            out.append(path)
        i += 1
    return out


def _native_linkage_diagnostics_json(edges, cpython_abi_paths=None) -> str:
    if cpython_abi_paths is None:
        cpython_abi_paths = []
    diagnostics = "["
    i = 0
    while i < len(edges):
        if i > 0:
            diagnostics += ", "
        diagnostics += "{"
        diagnostics += '"code": "PCC-PKG-003"'
        diagnostics += ', "edge": ' + _json_str(edges[i])
        diagnostics += (
            ', "message": "native artifact mentions libpython under pcc-native mode"'
        )
        diagnostics += "}"
        i += 1
    i = 0
    while i < len(cpython_abi_paths):
        if diagnostics != "[":
            diagnostics += ", "
        diagnostics += "{"
        diagnostics += '"code": "PCC-PKG-004"'
        diagnostics += ', "message": "native artifact name declares a CPython extension ABI under pcc-native mode"'
        diagnostics += ', "path": ' + _json_str(cpython_abi_paths[i])
        diagnostics += "}"
        i += 1
    diagnostics += "]"
    return diagnostics


def _native_capability_profile_json(
    abi_mode: str,
    has_artifact_scan: bool,
    links_libpython: bool,
    uses_cpython_extension_abi: bool,
) -> str:
    profile = capability_profile(
        abi_mode,
        has_artifact_scan,
        links_libpython,
        uses_cpython_extension_abi,
    )
    out = "{"
    out += '"execution_mode": ' + _json_str(profile["execution_mode"])
    out += ', "native_package_claim": ' + (
        "true" if profile["native_package_claim"] else "false"
    )
    out += ', "no_libpython_runtime": ' + (
        "true" if profile["no_libpython_runtime"] else "false"
    )
    out += "}"
    return out


def _native_linkage_json(artifacts, roots, commands, abi_mode: str) -> str:
    edges = []
    cpython_abi_paths = []
    scans = "["
    scan_count = 0

    def add_scan(kind: str, path, edge: str, uses_cpython_abi: bool) -> None:
        nonlocal scans, scan_count
        scan_count += 1
        # PKG-P0-ABI-MODE-LABELS: mirror linkage_report's per-scan labels so a
        # pcc1 no-libpython linkage result carries the same explicit
        # execution-mode labels as the in-process report. Generic mapping from
        # abi_mode only (no package-name special cases): libpython /
        # cpython-compat -> cpython-compat, otherwise pcc-native. A single scan
        # earns native_package_claim only under pcc-native with no libpython
        # edge and no CPython-ABI usage.
        scan_execution_mode = (
            "cpython-compat"
            if (abi_mode == "libpython" or abi_mode == "cpython-compat")
            else "pcc-native"
        )
        scan_native_package_claim = (
            abi_mode == "pcc-native" and not edge and not uses_cpython_abi
        )
        if scans != "[":
            scans += ", "
        scans += "{"
        scans += '"execution_mode": ' + _json_str(scan_execution_mode)
        scans += ', "kind": ' + _json_str(kind)
        scans += ', "link_libpython_edges": '
        scans += _json_str_list([edge] if edge else [])
        scans += ', "links_libpython": ' + ("true" if edge else "false")
        scans += ', "native_package_claim": ' + (
            "true" if scan_native_package_claim else "false"
        )
        scans += ', "path": ' + _json_str_or_null(path)
        scans += ', "uses_cpython_extension_abi": ' + (
            "true" if uses_cpython_abi else "false"
        )
        scans += "}"
        if edge and not _native_list_contains(edges, edge):
            edges.append(edge)
        if (
            uses_cpython_abi
            and path is not None
            and not _native_list_contains(cpython_abi_paths, path)
        ):
            cpython_abi_paths.append(path)

    i = 0
    while i < len(commands):
        command = commands[i]
        add_scan(
            "link_command",
            None,
            (
                _native_libpython_edge(command)
                if _native_text_has_libpython(command)
                else ""
            ),
            False,
        )
        i += 1
    i = 0
    while i < len(artifacts):
        add_scan(
            "artifact",
            artifacts[i],
            _native_artifact_mentions_libpython(artifacts[i]),
            _native_artifact_uses_cpython_extension_abi(artifacts[i]),
        )
        i += 1
    i = 0
    while i < len(roots):
        found = _native_collect_artifacts(roots[i])
        j = 0
        while j < len(found):
            add_scan(
                "artifact",
                found[j],
                _native_artifact_mentions_libpython(found[j]),
                _native_artifact_uses_cpython_extension_abi(found[j]),
            )
            j += 1
        i += 1
    scans += "]"

    diagnostics = _native_linkage_diagnostics_json(edges, cpython_abi_paths)

    links = len(edges) > 0
    uses_cpython_abi = len(cpython_abi_paths) > 0
    # PKG-P0-ABI-MODE-LABELS: additive top-level execution-mode labels that
    # mirror the in-process linkage_report. execution_mode is derived from
    # abi_mode alone; native_package_claim is True only when a pcc-native scan
    # produced no libpython edge and no CPython-ABI usage (a PCC-PKG-003/004
    # finding keeps it False). No existing key is renamed or removed.
    execution_mode = (
        "cpython-compat"
        if (abi_mode == "libpython" or abi_mode == "cpython-compat")
        else "pcc-native"
    )
    native_package_claim = (
        abi_mode == "pcc-native"
        and scan_count > 0
        and not links
        and not uses_cpython_abi
    )
    ok = ((not links) or abi_mode == "libpython") and (
        (not uses_cpython_abi)
        or abi_mode == "libpython"
        or abi_mode == "cpython-compat"
    )
    out = "{"
    out += '"abi_mode": ' + _json_str(abi_mode)
    out += ', "capability_profile": ' + _native_capability_profile_json(
        abi_mode, scan_count > 0, links, uses_cpython_abi
    )
    out += ', "cpython_extension_abi_paths": ' + _json_str_list(cpython_abi_paths)
    out += ', "diagnostics": ' + diagnostics
    out += ', "execution_mode": ' + _json_str(execution_mode)
    out += ', "link_libpython_edges": ' + _json_str_list(edges)
    out += ', "links_libpython": ' + ("true" if links else "false")
    out += ', "native_package_claim": ' + ("true" if native_package_claim else "false")
    out += ', "no_libpython_runtime": ' + (
        "true"
        if (not links and not uses_cpython_abi and abi_mode == "pcc-native")
        else "false"
    )
    out += ', "ok": ' + ("true" if ok else "false")
    out += ', "scans": ' + scans
    out += ', "uses_cpython_extension_abi": ' + (
        "true" if uses_cpython_abi else "false"
    )
    out += "}"
    return out


def _native_is_repo_artifact(path: str) -> bool:
    lower = path.lower()
    return (
        lower.endswith(".whl")
        or lower.endswith(".zip")
        or lower.endswith(".tar.gz")
        or lower.endswith(".tar.bz2")
        or lower.endswith(".tar.xz")
        or lower.endswith(".tgz")
    )


def _native_repo_artifact_kind(path: str) -> str:
    lower = path.lower()
    if lower.endswith(".whl"):
        return "wheel"
    if (
        lower.endswith(".zip")
        or lower.endswith(".tar.gz")
        or lower.endswith(".tar.bz2")
        or lower.endswith(".tar.xz")
        or lower.endswith(".tgz")
    ):
        return "sdist"
    return "artifact"


def _native_strip_repo_suffix(name: str) -> str:
    suffixes = [".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".zip", ".whl"]
    i = 0
    while i < len(suffixes):
        suffix = suffixes[i]
        if name.endswith(suffix):
            return name[: len(name) - len(suffix)]
        i += 1
    return name


def _native_abs_path(path: str) -> str:
    if path.startswith("/"):
        return path
    return os.getcwd() + "/" + path


def _native_basename(path: str) -> str:
    last = -1
    i = 0
    while i < len(path):
        if path[i] == "/":
            last = i
        i += 1
    return path[last + 1 :]


def _native_str_equal(left: str, right: str) -> bool:
    if len(left) != len(right):
        return False
    i = 0
    while i < len(left):
        if left[i] != right[i]:
            return False
        i += 1
    return True


def _native_wheel_tag_fields(path: str):
    return wheel_tag_fields(path)


def _native_wheel_tags_json(path) -> str:
    # Honest wheel-tag object for an install manifest. Derived from the
    # resolved artifact name via the existing wheel-tag splitter; a non-wheel
    # (package name / sdist / short filename) yields explicit ``null`` tags
    # rather than an invented tag triple.
    if path is None:
        return '{"abi_tag": null, "platform_tag": null, "python_tag": null}'
    fields = _native_wheel_tag_fields(path)
    python_tag = fields[1]
    abi_tag = fields[2]
    platform_tag = fields[3]
    out = "{"
    out += '"abi_tag": ' + _json_str_or_null(abi_tag if abi_tag != "" else None)
    out += ', "platform_tag": ' + _json_str_or_null(
        platform_tag if platform_tag != "" else None
    )
    out += ', "python_tag": ' + _json_str_or_null(
        python_tag if python_tag != "" else None
    )
    out += "}"
    return out


def _native_collect_repo_artifacts(root: str):
    artifacts = []
    if os.path.isfile(root):
        if _native_is_repo_artifact(root):
            artifacts.append(root)
        return artifacts
    if not os.path.isdir(root):
        return artifacts
    stack = [root]
    while len(stack) > 0:
        current = stack.pop()
        try:
            names = sorted(os.listdir(current))
        except Exception:
            names = []
        i = 0
        while i < len(names):
            name = names[i]
            path = current + "/" + name
            if os.path.isdir(path):
                if name != "__pycache__" and name != ".git":
                    stack.append(path)
            elif os.path.isfile(path) and _native_is_repo_artifact(path):
                artifacts.append(path)
            i += 1
    return artifacts


def _native_repo_compatibility(
    kind: str, python_tag: str, abi_tag: str, platform_tag: str
):
    if kind == "sdist":
        return [True, "source_artifact"]
    if kind != "wheel":
        return [False, "unsupported_artifact_kind"]
    if python_tag == "py3" and abi_tag == "none" and platform_tag == "any":
        return [True, "pure_python_wheel"]
    if (
        python_tag == "pcc3"
        and abi_tag == "pcc_native"
        and platform_tag == _native_current_platform_tag()
    ):
        return [True, "pcc_native_wheel"]
    return [False, "wheel_tag_not_pcc_native_compatible"]


def _native_wheel_repository_json(
    root: str, add_artifacts, abi_mode: str, write_manifest: bool
) -> str:
    repo_root = _native_abs_path(root)
    copied = []
    if len(add_artifacts) > 0:
        try:
            _bootstrap_subprocess_run(["mkdir", "-p", repo_root], check=True)
        except Exception:
            pass
        i = 0
        while i < len(add_artifacts):
            source = _native_abs_path(add_artifacts[i])
            dest = repo_root + "/" + _native_basename(source)
            if os.path.isfile(source):
                try:
                    if not _native_str_equal(source, dest):
                        _bootstrap_subprocess_run(["cp", source, dest], check=True)
                    copied.append(dest)
                except Exception:
                    pass
            i += 1
    artifacts = _native_collect_repo_artifacts(repo_root)
    rows = "["
    diagnostics = "["
    artifact_count = 0
    ok = True
    i = 0
    while i < len(artifacts):
        path = artifacts[i]
        kind = _native_repo_artifact_kind(path)
        fields = _native_wheel_tag_fields(path)
        name = fields[0]
        python_tag = fields[1]
        abi_tag = fields[2]
        platform_tag = fields[3]
        compat = _native_repo_compatibility(kind, python_tag, abi_tag, platform_tag)
        linkage = _native_linkage_json([path], [], [], abi_mode)
        if not compat[0] or _native_find_from(linkage, '"ok": false', 0) >= 0:
            ok = False
        if i > 0:
            rows += ", "
        rows += "{"
        rows += '"abi_tag": ' + _json_str_or_null(abi_tag if abi_tag != "" else None)
        rows += ', "compatibility_reason": ' + _json_str(compat[1])
        rows += ', "diagnostics": ['
        diag_written = False
        if not compat[0]:
            rows += "{"
            rows += '"code": "PCC-REPO-TAG-INCOMPATIBLE"'
            rows += ', "message": "artifact wheel tag is not compatible with pcc-native repository policy"'
            rows += ', "reason": ' + _json_str(compat[1])
            rows += "}"
            diag_written = True
            if diagnostics != "[":
                diagnostics += ", "
            diagnostics += "{"
            diagnostics += '"code": "PCC-REPO-TAG-INCOMPATIBLE"'
            diagnostics += ', "path": ' + _json_str(path)
            diagnostics += ', "reason": ' + _json_str(compat[1])
            diagnostics += "}"
        if _native_find_from(linkage, '"links_libpython": true', 0) >= 0:
            if diag_written:
                rows += ", "
            rows += "{"
            rows += '"code": "PCC-PKG-003"'
            rows += ', "message": "repository artifact mentions libpython under pcc-native mode"'
            rows += "}"
            if diagnostics != "[":
                diagnostics += ", "
            diagnostics += "{"
            diagnostics += '"code": "PCC-PKG-003"'
            diagnostics += ', "path": ' + _json_str(path)
            diagnostics += "}"
        rows += "]"
        rows += ', "linkage": ' + linkage
        rows += ', "links_libpython": ' + (
            "true"
            if _native_find_from(linkage, '"links_libpython": true', 0) >= 0
            else "false"
        )
        rows += ', "name": ' + _json_str(name)
        rows += ', "no_libpython_runtime": ' + (
            "true"
            if _native_find_from(linkage, '"no_libpython_runtime": true', 0) >= 0
            else "false"
        )
        rows += ', "path": ' + _json_str(path)
        rows += ', "pcc_native_compatible": ' + ("true" if compat[0] else "false")
        rows += ', "platform_tag": ' + _json_str_or_null(
            platform_tag if platform_tag != "" else None
        )
        rows += ', "python_tag": ' + _json_str_or_null(
            python_tag if python_tag != "" else None
        )
        rows += ', "source_kind": ' + _json_str(kind)
        rows += "}"
        artifact_count += 1
        i += 1
    rows += "]"
    diagnostics += "]"
    manifest_path = None
    if write_manifest:
        try:
            _bootstrap_subprocess_run(["mkdir", "-p", repo_root], check=True)
            manifest_path = repo_root + "/pcc-wheel-repository.json"
            out_preview = "{"
            out_preview += '"artifact_count": ' + str(artifact_count)
            out_preview += ', "artifacts": ' + rows
            out_preview += ', "current_platform_tag": ' + _json_str(
                _native_current_platform_tag()
            )
            out_preview += ', "diagnostics": ' + diagnostics
            out_preview += ', "manifest_path": ' + _json_str(manifest_path)
            out_preview += ', "ok": ' + ("true" if ok else "false")
            out_preview += ', "pcc_native_wheel_tag": ' + _json_str(
                _native_pcc_wheel_tag()
            )
            out_preview += ', "root": ' + _json_str(repo_root)
            out_preview += ', "schema": "pcc.wheel-repository.v1"'
            out_preview += "}"
            with open(manifest_path, "w") as fh:
                fh.write(out_preview)
        except Exception:
            manifest_path = None
    out = "{"
    out += '"artifact_count": ' + str(artifact_count)
    out += ', "artifacts": ' + rows
    out += ', "copied_artifacts": ' + _json_str_list(copied)
    out += ', "current_platform_tag": ' + _json_str(_native_current_platform_tag())
    out += ', "diagnostics": ' + diagnostics
    out += ', "manifest_path": ' + _json_str_or_null(manifest_path)
    out += ', "ok": ' + ("true" if ok else "false")
    out += ', "pcc_native_wheel_tag": ' + _json_str(_native_pcc_wheel_tag())
    out += ', "root": ' + _json_str(repo_root)
    out += ', "schema": "pcc.wheel-repository.v1"'
    out += "}"
    return out


def _run_native_package_wheel_repo_from_pcc1(module_args) -> int:
    root = None
    add_artifacts = []
    abi_mode = "pcc-native"
    write_manifest = False
    i = 0
    while i < len(module_args):
        arg = module_args[i]
        if arg == "--json":
            pass
        elif arg == "--root":
            if i + 1 >= len(module_args):
                _write_text("Error: --root requires a value", err=True)
                return 2
            root = module_args[i + 1]
            i += 1
        elif arg.startswith("--root="):
            root = arg.split("=", 1)[1]
        elif arg == "--add":
            if i + 1 >= len(module_args):
                _write_text("Error: --add requires a value", err=True)
                return 2
            add_artifacts.append(module_args[i + 1])
            i += 1
        elif arg.startswith("--add="):
            add_artifacts.append(arg.split("=", 1)[1])
        elif arg == "--abi":
            if i + 1 >= len(module_args):
                _write_text("Error: --abi requires a value", err=True)
                return 2
            abi_mode = module_args[i + 1]
            i += 1
        elif arg.startswith("--abi="):
            abi_mode = arg.split("=", 1)[1]
        elif arg == "--write-manifest":
            write_manifest = True
        i += 1
    if root is None:
        _write_text('{"error": "missing repository root", "ok": false}')
        return 2
    report = _native_wheel_repository_json(
        root, add_artifacts, abi_mode, write_manifest
    )
    _write_text(report)
    return 2 if _native_find_from(report, '"ok": false', 0) >= 0 else 0


def _run_native_package_linkage_from_pcc1(module_args) -> int:
    artifacts = []
    roots = []
    commands = []
    abi_mode = "pcc-native"
    i = 0
    while i < len(module_args):
        arg = module_args[i]
        if arg == "--artifact":
            if i + 1 >= len(module_args):
                _write_text("Error: --artifact requires a value", err=True)
                return 2
            artifacts.append(module_args[i + 1])
            i += 1
        elif arg.startswith("--artifact="):
            artifacts.append(arg.split("=", 1)[1])
        elif arg == "--root":
            if i + 1 >= len(module_args):
                _write_text("Error: --root requires a value", err=True)
                return 2
            roots.append(module_args[i + 1])
            i += 1
        elif arg.startswith("--root="):
            roots.append(arg.split("=", 1)[1])
        elif arg == "--command":
            if i + 1 >= len(module_args):
                _write_text("Error: --command requires a value", err=True)
                return 2
            commands.append(module_args[i + 1])
            i += 1
        elif arg.startswith("--command="):
            commands.append(arg.split("=", 1)[1])
        elif arg == "--abi":
            if i + 1 >= len(module_args):
                _write_text("Error: --abi requires a value", err=True)
                return 2
            abi_mode = module_args[i + 1]
            i += 1
        elif arg.startswith("--abi="):
            abi_mode = arg.split("=", 1)[1]
        elif arg == "--json":
            pass
        i += 1
    report = _native_linkage_json(artifacts, roots, commands, abi_mode)
    _write_text(report)
    if _native_find_from(report, '"ok": true', 0) >= 0:
        return 0
    return 2


def _run_native_pip_shim_from_pcc1(module_args) -> int:
    raw = _copy_seq(module_args)
    if len(raw) == 0:
        raw = ["install", "--dry-run"]
    if len(raw) > 0 and (raw[0] == "-h" or raw[0] == "--help"):
        _write_text(
            "usage: pcc -m pip [install] [packages...] [--dry-run] "
            "[--build=owned|host] [--force]"
        )
        return 0
    command = raw[0] if len(raw) > 0 else "install"
    dry_run = 0
    target_dir = ""
    cache_dir = ""
    abi = "pcc-native"
    report_path = ""
    find_links = []
    index_urls = []
    no_index = False
    acquire_mode = "auto"
    build_mode = "owned"
    force = False
    target_python = (
        os.environ.get("PCC_PACKAGE_TARGET_PYTHON") or DEFAULT_PYTHON_SEMANTIC_TARGET
    )
    packages = []
    i = 1
    while i < len(raw):
        arg = raw[i]
        if arg == "--dry-run":
            dry_run = 1
        elif arg == "--force":
            force = True
        elif arg == "--json":
            pass
        elif arg == "--report":
            dry_run = 1
            if i + 1 < len(raw) and not raw[i + 1].startswith("-"):
                report_path = raw[i + 1]
                i += 1
        elif arg.startswith("--report="):
            dry_run = 1
            report_path = arg.split("=", 1)[1]
        elif arg == "--target" or arg == "--target-dir":
            if i + 1 >= len(raw):
                _write_text(
                    '{"command": "install", "error": "--target requires a value", "ok": false}'
                )
                return 2
            target_dir = raw[i + 1]
            i += 1
        elif arg == "--cache-dir":
            if i + 1 >= len(raw):
                _write_text(
                    '{"command": "install", "error": "--cache-dir requires a value", "ok": false}'
                )
                return 2
            cache_dir = raw[i + 1]
            i += 1
        elif arg == "--find-links" or arg == "-f":
            if i + 1 >= len(raw):
                _write_text(
                    '{"command": "install", "error": "--find-links requires a value", "ok": false}'
                )
                return 2
            find_links.append(raw[i + 1])
            i += 1
        elif arg.startswith("--find-links="):
            find_links.append(arg.split("=", 1)[1])
        elif arg == "--no-index":
            no_index = True
        elif arg == "--acquire":
            if i + 1 >= len(raw):
                _write_text(
                    '{"command": "install", "error": "--acquire requires a value", "ok": false}'
                )
                return 2
            acquire_mode = raw[i + 1]
            i += 1
        elif arg.startswith("--acquire="):
            acquire_mode = arg.split("=", 1)[1]
        elif arg == "--build":
            if i + 1 >= len(raw):
                _write_text(
                    '{"command": "install", "error": "--build requires a value", "ok": false}'
                )
                return 2
            build_mode = raw[i + 1]
            i += 1
        elif arg.startswith("--build="):
            build_mode = arg.split("=", 1)[1]
        elif arg == "--python-version" or arg == "--target-python":
            if i + 1 >= len(raw):
                _write_text(
                    '{"command": "install", "error": "'
                    + arg
                    + ' requires a value", "ok": false}'
                )
                return 2
            target_python = raw[i + 1]
            i += 1
        elif arg.startswith("--python-version=") or arg.startswith("--target-python="):
            target_python = arg.split("=", 1)[1]
        elif arg == "--index-url" or arg == "-i":
            if i + 1 >= len(raw):
                _write_text(
                    '{"command": "install", "error": "--index-url requires a value", "ok": false}'
                )
                return 2
            index_urls.append(raw[i + 1])
            i += 1
        elif arg.startswith("--index-url="):
            index_urls.append(arg.split("=", 1)[1])
        elif arg == "--extra-index-url":
            if i + 1 >= len(raw):
                _write_text(
                    '{"command": "install", "error": "--extra-index-url requires a value", "ok": false}'
                )
                return 2
            index_urls.append(raw[i + 1])
            i += 1
        elif arg.startswith("--extra-index-url="):
            index_urls.append(arg.split("=", 1)[1])
        elif arg == "--abi":
            if i + 1 >= len(raw):
                _write_text(
                    '{"command": "install", "error": "--abi requires a value", "ok": false}'
                )
                return 2
            abi = raw[i + 1]
            i += 1
        elif arg.startswith("--abi="):
            abi = arg.split("=", 1)[1]
        elif not arg.startswith("-"):
            packages.append(arg)
        i += 1
    if command != "install":
        _write_text(
            '{"command": '
            + _json_str(command)
            + ', "error": "only install dry-run is supported", "ok": false}'
        )
        return 2
    if len(packages) == 0:
        _write_text(
            '{"command": "install", "error": '
            + _json_str("no packages requested")
            + ', "ok": false}'
        )
        return 2
    if not (
        acquire_mode == "auto"
        or acquire_mode == "host"
        or acquire_mode == "owned"
        or acquire_mode == "offline"
    ):
        _write_text(
            '{"command": "install", "diagnostic": '
            '"PCC-PKG-ACQUIRE-MODE-INVALID", "error": '
            '"PCC-PKG-ACQUIRE-MODE-INVALID", "ok": false}'
        )
        return 2
    if not (build_mode == "owned" or build_mode == "host"):
        _write_text(
            '{"command": "install", "diagnostic": '
            '"PCC-PKG-BUILD-MODE-INVALID", "error": '
            '"PCC-PKG-BUILD-MODE-INVALID", "ok": false}'
        )
        return 2
    if not _native_target_python_valid(target_python):
        _write_text(
            '{"command": "install", "diagnostic": '
            '"PCC-PKG-ACQUIRE-TARGET-PYTHON-INVALID", "error": '
            '"PCC-PKG-ACQUIRE-TARGET-PYTHON-INVALID", "ok": false}'
        )
        return 2
    if no_index:
        index_urls = []
        acquire_mode = "offline"
    requested_packages = _copy_seq(packages)
    effective_indexes = _copy_seq(index_urls)
    if not no_index and len(effective_indexes) == 0:
        effective_indexes.append("https://pypi.org/simple")
    actual_acquire_mode = "owned" if acquire_mode == "auto" else acquire_mode
    install_packages = []
    acquired_install_origins = []
    acquisition_jsons = []
    acquisition_failed = False
    acquisition_error = ""
    k = 0
    while k < len(packages):
        spec = packages[k]
        if dry_run != 0:
            install_packages.append(spec)
            k += 1
            continue
        local = _native_resolve_install_source_result(
            spec, cache_dir, find_links, abi, []
        )
        looks_local = False
        if _native_find_from(spec, "/", 0) >= 0 or spec.startswith("."):
            looks_local = True
        if (
            spec.endswith(".whl")
            or spec.endswith(".tar.gz")
            or spec.endswith(".tar.bz2")
            or spec.endswith(".tar.xz")
            or spec.endswith(".tgz")
            or spec.endswith(".zip")
        ):
            looks_local = True
        project_shadowed_by_online_acquire = (
            local[1] == "projects" and actual_acquire_mode != "offline"
        )
        if (
            local[0] is not None and not project_shadowed_by_online_acquire
        ) or looks_local:
            install_packages.append(spec)
            k += 1
            continue
        if actual_acquire_mode == "offline":
            acquired = [
                None,
                "",
                "",
                "",
                "PCC-PKG-ACQUIRE-OFFLINE",
            ]
        elif actual_acquire_mode == "owned":
            acquired = _native_acquire_owned_result(
                spec,
                cache_dir,
                effective_indexes,
                abi,
                target_python,
                acquire_mode == "auto",
            )
        else:
            acquired = _native_acquire_host_result(
                spec, cache_dir, effective_indexes, abi, target_python
            )
        acquisition_jsons.append(
            _native_acquisition_json(
                spec,
                acquire_mode,
                actual_acquire_mode,
                acquired,
                [] if no_index else effective_indexes,
                target_python,
            )
        )
        if acquired[0] is not None and acquired[4] == "":
            install_packages.append(acquired[0])
            acquired_install_origins.append([os.path.abspath(acquired[0]), "index-url"])
        else:
            acquisition_failed = True
            acquisition_error = acquired[4]
        k += 1
    packages = install_packages
    if len(find_links) > 0 or len(packages) != len(requested_packages):
        packages = _native_resolve_install_order(
            packages, cache_dir, find_links, abi, []
        )
    if dry_run == 0 and acquisition_failed:
        acquisitions = "[" + ", ".join(acquisition_jsons) + "]"
        out = "{"
        out += '"command": "install"'
        out += ', "abi": ' + _json_str(abi)
        out += ', "acquire_mode_requested": ' + _json_str(acquire_mode)
        out += ', "build_mode_requested": ' + _json_str(build_mode)
        out += ', "acquisitions": ' + acquisitions
        out += ', "dry_run": false'
        out += ', "error": ' + _json_str(acquisition_error)
        out += ', "find_links": ' + _json_str_list(find_links)
        out += ', "index_urls": ' + _json_str_list(
            [] if no_index else effective_indexes
        )
        out += ', "installs": []'
        out += ', "no_index": ' + ("true" if no_index else "false")
        out += ', "ok": false'
        out += ', "packages": ' + _json_str_list(requested_packages)
        out += ', "target_python": ' + _json_str(target_python)
        out += ', "report_path": ' + _json_str_or_null(
            None if report_path == "" else report_path
        )
        out += ', "resolver_diagnostics": []'
        out += "}"
        if report_path != "":
            try:
                with open(report_path, "w") as fh:
                    fh.write(out)
            except Exception:
                _write_text(
                    '{"command": "install", "error": "failed to write report", "ok": false}'
                )
                return 2
        _write_text(out)
        return 2
    if dry_run == 0:
        installs = "["
        all_ok = True
        k = 0
        while k < len(packages):
            if k > 0:
                installs += ", "
            resolved_from_override = None
            if os.path.exists(packages[k]):
                package_path = os.path.abspath(packages[k])
                origin_index = 0
                while origin_index < len(acquired_install_origins):
                    if acquired_install_origins[origin_index][0] == package_path:
                        resolved_from_override = acquired_install_origins[origin_index][
                            1
                        ]
                        break
                    origin_index += 1
            install_json = _native_install_manifest_json(
                packages[k],
                target_dir,
                cache_dir,
                find_links,
                abi,
                [],
                resolved_from_override,
                build_mode,
                force,
            )
            if _native_find_from(install_json, '"ok": false', 0) >= 0:
                all_ok = False
            installs += install_json
            k += 1
        installs += "]"
        out = "{"
        out += '"command": "install"'
        out += ', "abi": ' + _json_str(abi)
        out += ', "acquire_mode_requested": ' + _json_str(acquire_mode)
        out += ', "build_mode_requested": ' + _json_str(build_mode)
        out += ', "acquisitions": [' + ", ".join(acquisition_jsons) + "]"
        out += ', "dry_run": false'
        out += ', "find_links": ' + _json_str_list(find_links)
        out += ', "index_urls": ' + _json_str_list(
            [] if no_index else effective_indexes
        )
        out += ', "installs": ' + installs
        out += ', "no_index": ' + ("true" if no_index else "false")
        out += ', "ok": ' + ("true" if all_ok else "false")
        out += ', "packages": ' + _json_str_list(requested_packages)
        out += ', "target_python": ' + _json_str(target_python)
        out += ', "report_path": ' + _json_str_or_null(
            None if report_path == "" else report_path
        )
        out += ', "resolver_diagnostics": []'
        out += "}"
        if report_path != "":
            try:
                with open(report_path, "w") as fh:
                    fh.write(out)
            except Exception:
                _write_text(
                    '{"command": "install", "error": "failed to write report", "ok": false}'
                )
                return 2
        _write_text(out)
        return 0 if all_ok else 2
    inspections = "["
    j = 0
    while j < len(packages):
        if j > 0:
            inspections += ", "
        inspections += _package_inspection_json(packages[j], None)
        j += 1
    inspections += "]"
    out = "{"
    out += '"command": "install"'
    out += ', "abi": ' + _json_str(abi)
    out += ', "acquire_mode_requested": ' + _json_str(acquire_mode)
    out += ', "build_mode_requested": ' + _json_str(build_mode)
    out += ', "acquisitions": []'
    out += ', "dry_run": true'
    out += ', "find_links": ' + _json_str_list(find_links)
    out += ', "index_urls": ' + _json_str_list([] if no_index else effective_indexes)
    out += ', "inspections": ' + inspections
    out += ', "no_index": ' + ("true" if no_index else "false")
    out += ', "ok": true'
    out += ', "packages": ' + _json_str_list(requested_packages)
    out += ', "target_python": ' + _json_str(target_python)
    out += ', "report_path": ' + _json_str_or_null(
        None if report_path == "" else report_path
    )
    out += ', "resolver_diagnostics": []'
    out += "}"
    if report_path != "":
        try:
            with open(report_path, "w") as fh:
                fh.write(out)
        except Exception:
            _write_text(
                '{"command": "install", "error": "failed to write report", "ok": false}'
            )
            return 2
    _write_text(out)
    return 0


def _native_package_basename(spec: str) -> str:
    if os.path.isdir(spec):
        metadata = _native_local_project_metadata(spec)
        if metadata[0]:
            return metadata[0]
        return os.path.basename(os.path.normpath(spec)) or "package"
    return distribution_filename_fields(spec)[0]


def _native_local_project_metadata(root: str):
    path = os.path.join(root, "pyproject.toml")
    if not os.path.isfile(path):
        return ["", ""]
    with open(path, "r", encoding="utf-8") as stream:
        text = stream.read()
    return literal_project_metadata_fields(text)


def _native_source_build_policy(root: str) -> str:
    path = os.path.join(root, "pyproject.toml")
    if not os.path.isfile(path):
        return "unrecognized"
    if os.path.isfile(os.path.join(root, "hatch.toml")):
        return "requires_build_hook"
    if os.path.isfile(os.path.join(root, "hatch_build.py")):
        return "requires_build_hook"
    with open(path, "r", encoding="utf-8") as stream:
        text = stream.read()
    return source_build_policy(text)


def _native_normalized_package_name(name: str) -> str:
    return name.lower().replace("_", "-")


def _native_artifact_project_name(path: str) -> str:
    return _native_normalized_package_name(_native_package_basename(path))


def _native_artifact_version_text(path: str) -> str:
    if os.path.isdir(path):
        return _native_local_project_metadata(path)[1] or "0"
    return distribution_filename_fields(path)[1]


def _native_version_numbers(path: str):
    text = _native_artifact_version_text(path)
    nums = []
    current = -1
    i = 0
    while i < len(text):
        ch = text[i]
        if "0" <= ch <= "9":
            if current < 0:
                current = 0
            current = current * 10 + (ord(ch) - ord("0"))
        else:
            if current >= 0:
                nums.append(current)
                current = -1
        i += 1
    if current >= 0:
        nums.append(current)
    while len(nums) < 4:
        nums.append(0)
    return nums


def _native_artifact_better(candidate: str, best: str) -> bool:
    left = _native_version_numbers(candidate)
    right = _native_version_numbers(best)
    i = 0
    while i < 4:
        if left[i] > right[i]:
            return True
        if left[i] < right[i]:
            return False
        i += 1
    return candidate > best


def _native_artifact_rank(reason: str) -> int:
    if reason == "pcc_native_wheel":
        return 3
    if reason == "pure_python_wheel":
        return 2
    if reason == "source_artifact":
        return 1
    return 0


def _native_artifact_better_for_reason(
    candidate: str,
    candidate_reason: str,
    best: str,
    best_reason: str,
) -> bool:
    left = _native_version_numbers(candidate)
    right = _native_version_numbers(best)
    i = 0
    while i < 4:
        if left[i] > right[i]:
            return True
        if left[i] < right[i]:
            return False
        i += 1
    left_rank = _native_artifact_rank(candidate_reason)
    right_rank = _native_artifact_rank(best_reason)
    if left_rank > right_rank:
        return True
    if left_rank < right_rank:
        return False
    return candidate > best


def _native_artifact_compatibility_for_abi(path: str, abi: str):
    if abi != "pcc-native":
        return [True, "abi_mode_allows_artifact"]
    kind = _native_repo_artifact_kind(path)
    fields = _native_wheel_tag_fields(path)
    compat = _native_repo_compatibility(kind, fields[1], fields[2], fields[3])
    if not compat[0]:
        return compat
    edge = _native_artifact_mentions_libpython(path)
    if edge:
        return [False, "links_libpython"]
    if _native_artifact_uses_cpython_extension_abi(path):
        return [False, "cpython_extension_abi"]
    return compat


def _native_artifact_compatibility_for_name(name: str, abi: str):
    if abi != "pcc-native":
        return [True, "abi_mode_allows_artifact"]
    kind = _native_repo_artifact_kind(name)
    fields = _native_wheel_tag_fields(name)
    return _native_repo_compatibility(kind, fields[1], fields[2], fields[3])


def _native_json_string_after(text: str, marker: str, start: int):
    idx = _native_find_from(text, marker, start)
    if idx < 0:
        return None
    i = idx + len(marker)
    out = ""
    escaped = False
    while i < len(text):
        ch = text[i]
        if escaped:
            out += ch
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == '"':
            return out
        else:
            out += ch
        i += 1
    return None


def _native_json_bool_value(text: str, key: str):
    idx = _native_find_from(text, key, 0)
    if idx < 0:
        return None
    colon = _native_find_from(text, ":", idx + len(key))
    if colon < 0:
        return None
    i = colon + 1
    while i < len(text) and (text[i] == " " or text[i] == "\n" or text[i] == "\t"):
        i += 1
    if _native_find_from(text, "true", i) == i:
        return True
    if _native_find_from(text, "false", i) == i:
        return False
    return None


def _native_find_manifest_artifact(root: str, expected: str, abi: str):
    manifest_path = root + "/pcc-wheel-repository.json"
    if not os.path.isfile(manifest_path):
        return [None, ""]
    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except Exception:
        return [None, ""]
    best = None
    best_reason = ""
    pos = 0
    marker = '"path": "'
    while pos < len(text):
        path = _native_json_string_after(text, marker, pos)
        idx = _native_find_from(text, marker, pos)
        if path is None or idx < 0:
            break
        pos = idx + len(marker)
        if not path.startswith("/"):
            path = root + "/" + path
        if os.path.isfile(path):
            project = _native_artifact_project_name(path)
            if _native_normalized_package_name(project) == expected:
                start = idx - 1500
                if start < 0:
                    start = 0
                end = idx + 1500
                if end > len(text):
                    end = len(text)
                window = text[start:end]
                compatible = _native_json_bool_value(window, '"pcc_native_compatible"')
                links = _native_json_bool_value(window, '"links_libpython"')
                reason = _native_json_string_after(
                    window, '"compatibility_reason": "', 0
                )
                if reason is None:
                    reason = ""
                allowed = True
                if abi == "pcc-native":
                    if compatible is None or not compatible:
                        allowed = False
                    if links:
                        allowed = False
                if allowed:
                    compat = _native_artifact_compatibility_for_abi(path, abi)
                    if compat[0]:
                        reason = compat[1]
                        if best is None or _native_artifact_better_for_reason(
                            path,
                            reason,
                            best,
                            best_reason,
                        ):
                            best = os.path.abspath(path)
                            best_reason = reason
    return [best, best_reason]


def _native_find_links_artifact_result(spec: str, find_links, abi: str):
    expected = _native_normalized_package_name(spec)
    best = None
    best_reason = ""
    manifest_best = None
    manifest_best_reason = ""
    i = 0
    while i < len(find_links):
        root = find_links[i]
        candidates = []
        if os.path.isfile(root):
            candidates.append(root)
        elif os.path.isdir(root):
            manifest = _native_find_manifest_artifact(root, expected, abi)
            if manifest[0] is not None:
                if manifest_best is None or _native_artifact_better_for_reason(
                    manifest[0],
                    manifest[1],
                    manifest_best,
                    manifest_best_reason,
                ):
                    manifest_best = manifest[0]
                    manifest_best_reason = manifest[1]
            try:
                names = sorted(os.listdir(root))
            except Exception:
                names = []
            j = 0
            while j < len(names):
                candidates.append(root + "/" + names[j])
                j += 1
        j = 0
        while j < len(candidates):
            candidate = candidates[j]
            lower = candidate.lower()
            is_artifact = (
                lower.endswith(".whl")
                or lower.endswith(".tar.gz")
                or lower.endswith(".tar.bz2")
                or lower.endswith(".tar.xz")
                or lower.endswith(".tgz")
                or lower.endswith(".zip")
            )
            if is_artifact and os.path.isfile(candidate):
                project = _native_artifact_project_name(candidate)
                if _native_normalized_package_name(project) == expected:
                    compat = _native_artifact_compatibility_for_abi(candidate, abi)
                    if compat[0] and (
                        best is None
                        or _native_artifact_better_for_reason(
                            candidate,
                            compat[1],
                            best,
                            best_reason,
                        )
                    ):
                        best = os.path.abspath(candidate)
                        best_reason = compat[1]
            j += 1
        i += 1
    if manifest_best is not None:
        return [manifest_best, "wheel-repository"]
    if best is not None:
        return [best, "find-links"]
    return [None, None]


def _native_find_links_artifact(spec: str, find_links, abi: str = "pcc-native"):
    result = _native_find_links_artifact_result(spec, find_links, abi)
    return result[0]


def _native_url_basename(url: str) -> str:
    end = len(url)
    i = 0
    while i < len(url):
        if url[i] == "?" or url[i] == "#":
            end = i
            break
        i += 1
    last = -1
    i = 0
    while i < end:
        if url[i] == "/":
            last = i
        i += 1
    return url[last + 1 : end]


def _native_url_origin(url: str) -> str:
    scheme_end = _native_find_from(url, "://", 0)
    if scheme_end < 0:
        return ""
    i = scheme_end + 3
    while i < len(url):
        if url[i] == "/":
            return url[:i]
        i += 1
    return url


def _native_url_join(page_url: str, href: str) -> str:
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("/"):
        return _native_url_origin(page_url) + href
    end = len(page_url)
    i = 0
    while i < len(page_url):
        if page_url[i] == "?" or page_url[i] == "#":
            end = i
            break
        i += 1
    last = -1
    i = 0
    while i < end:
        if page_url[i] == "/":
            last = i
        i += 1
    if last >= 0:
        return page_url[: last + 1] + href
    return href


def _native_index_package_url(index_url: str, spec: str) -> str:
    base = index_url
    if not base.endswith("/"):
        base += "/"
    return base + _native_normalized_package_name(spec) + "/"


def _native_html_attr_value(tag_text: str, attr_name: str) -> str:
    lower = tag_text.lower()
    pos = 0
    while pos < len(tag_text):
        idx = _native_find_from(lower, attr_name.lower(), pos)
        if idx < 0:
            return ""
        before_ok = idx == 0 or not (
            ("a" <= lower[idx - 1] <= "z")
            or ("0" <= lower[idx - 1] <= "9")
            or lower[idx - 1] == "-"
            or lower[idx - 1] == "_"
        )
        i = idx + len(attr_name)
        while i < len(tag_text) and tag_text[i] in (" ", "\n", "\t"):
            i += 1
        if not before_ok or i >= len(tag_text) or tag_text[i] != "=":
            pos = idx + len(attr_name)
            continue
        i += 1
        while i < len(tag_text) and tag_text[i] in (" ", "\n", "\t"):
            i += 1
        if i >= len(tag_text) or not (tag_text[i] == '"' or tag_text[i] == "'"):
            return ""
        quote = tag_text[i]
        i += 1
        start = i
        while i < len(tag_text) and tag_text[i] != quote:
            i += 1
        return tag_text[start:i]
    return ""


def _native_html_unescape_attr(value: str) -> str:
    out = value.replace("&gt;", ">")
    out = out.replace("&lt;", "<")
    out = out.replace("&#62;", ">")
    out = out.replace("&#60;", "<")
    out = out.replace("&amp;", "&")
    return out


def _native_extract_href_links(text: str, page_url: str):
    links = []
    lower_text = text.lower()
    pos = 0
    while pos < len(text):
        anchor_idx = _native_find_from(lower_text, "<a", pos)
        if anchor_idx < 0:
            break
        end_idx = _native_find_from(text, ">", anchor_idx + 2)
        if end_idx < 0:
            break
        tag_text = text[anchor_idx : end_idx + 1]
        href = _native_html_attr_value(tag_text, "href")
        requires_python = _native_html_unescape_attr(
            _native_html_attr_value(tag_text, "data-requires-python")
        )
        url = _native_url_join(page_url, href)
        name = _native_url_basename(url)
        if href != "" and name != "":
            links.append([url, name, requires_python])
        pos = end_idx + 1
    return links


def _native_find_index_artifact_result(spec: str, cache_dir, index_urls, abi: str):
    expected = _native_normalized_package_name(spec)
    cache_root = cache_dir or _pcc_default_package_cache()
    scratch = cache_root + "/index-pages"
    downloads = cache_root + "/downloads"
    best_url = None
    best_name = ""
    best_reason = ""
    try:
        _bootstrap_subprocess_run(["mkdir", "-p", scratch], check=True)
        _bootstrap_subprocess_run(["mkdir", "-p", downloads], check=True)
    except Exception:
        return [None, None]
    i = 0
    while i < len(index_urls):
        page_url = _native_index_package_url(index_urls[i], spec)
        page_path = (
            scratch
            + "/"
            + _native_normalized_package_name(spec)
            + "."
            + str(i)
            + ".html"
        )
        if os._pcc_http_download_to_file(page_url, page_path) != 0:
            i += 1
            continue
        try:
            with open(page_path, "r", encoding="utf-8") as fh:
                page = fh.read()
        except Exception:
            i += 1
            continue
        links = _native_extract_href_links(page, page_url)
        j = 0
        while j < len(links):
            url = links[j][0]
            name = links[j][1]
            if _native_is_repo_artifact(name):
                project = _native_artifact_project_name(name)
                if _native_normalized_package_name(project) == expected:
                    compat = _native_artifact_compatibility_for_name(name, abi)
                    if compat[0] and (
                        best_url is None
                        or _native_artifact_better_for_reason(
                            name,
                            compat[1],
                            best_name,
                            best_reason,
                        )
                    ):
                        best_url = url
                        best_name = name
                        best_reason = compat[1]
            j += 1
        i += 1
    if best_url is None:
        return [None, None]
    dest = downloads + "/" + best_name
    if os._pcc_http_download_to_file(best_url, dest) != 0:
        return [None, None]
    return [os.path.abspath(dest), "index-url"]


def _native_requirement_parts(spec: str):
    split = _native_find_from(spec, "==", 0)
    if split >= 0:
        name = spec[:split]
        version = spec[split + 2 :]
        if version == "" or _native_find_from(version, "=", 0) >= 0:
            return [None, None]
    else:
        name = spec
        version = ""
    if name == "":
        return [None, None]
    i = 0
    while i < len(name):
        ch = name[i]
        allowed = (
            ("a" <= ch <= "z")
            or ("A" <= ch <= "Z")
            or ("0" <= ch <= "9")
            or ch == "_"
            or ch == "-"
            or ch == "."
        )
        if not allowed:
            return [None, None]
        i += 1
    return [name, version]


def _native_target_python_valid(value: str) -> bool:
    parts = value.split(".")
    if not (len(parts) == 2 or len(parts) == 3):
        return False
    i = 0
    while i < len(parts):
        if parts[i] == "":
            return False
        j = 0
        while j < len(parts[i]):
            if not ("0" <= parts[i][j] <= "9"):
                return False
            j += 1
        i += 1
    return True


def _native_numeric_version(value: str):
    parts = value.split(".")
    out = []
    i = 0
    while i < len(parts):
        if parts[i] == "":
            return []
        number = 0
        j = 0
        while j < len(parts[i]):
            ch = parts[i][j]
            if not ("0" <= ch <= "9"):
                return []
            number = number * 10 + (ord(ch) - ord("0"))
            j += 1
        out.append(number)
        i += 1
    return out


def _native_compare_numeric_versions(left, right) -> int:
    width = len(left)
    if len(right) > width:
        width = len(right)
    i = 0
    while i < width:
        lhs = left[i] if i < len(left) else 0
        rhs = right[i] if i < len(right) else 0
        if lhs < rhs:
            return -1
        if lhs > rhs:
            return 1
        i += 1
    return 0


def _native_requires_python_allows(specifier: str, target_python: str) -> bool:
    if specifier.strip() == "":
        return True
    target = _native_numeric_version(target_python)
    if len(target) == 0:
        return False
    clauses = specifier.split(",")
    i = 0
    while i < len(clauses):
        clause = clauses[i].strip()
        op = ""
        for candidate in ("===", "~=", "==", "!=", "<=", ">=", "<", ">"):
            if clause.startswith(candidate):
                op = candidate
                break
        if op == "" or op == "===":
            return False
        raw_bound = clause[len(op) :].strip()
        if raw_bound.endswith(".*") and (op == "==" or op == "!="):
            prefix = _native_numeric_version(raw_bound[:-2])
            if len(prefix) == 0:
                return False
            same = True
            j = 0
            while j < len(prefix):
                actual = target[j] if j < len(target) else 0
                if actual != prefix[j]:
                    same = False
                j += 1
            if (op == "==" and not same) or (op == "!=" and same):
                return False
            i += 1
            continue
        bound = _native_numeric_version(raw_bound)
        if len(bound) == 0:
            return False
        comparison = _native_compare_numeric_versions(target, bound)
        if op == "==" and comparison != 0:
            return False
        if op == "!=" and comparison == 0:
            return False
        if op == "<=" and comparison > 0:
            return False
        if op == ">=" and comparison < 0:
            return False
        if op == "<" and comparison >= 0:
            return False
        if op == ">" and comparison <= 0:
            return False
        if op == "~=":
            if comparison < 0:
                return False
            prefix_len = len(bound) - 1
            if prefix_len < 1:
                prefix_len = 1
            j = 0
            while j < prefix_len:
                actual = target[j] if j < len(target) else 0
                if actual != bound[j]:
                    return False
                j += 1
        i += 1
    return True


def _native_url_without_fragment(url: str) -> str:
    marker = _native_find_from(url, "#", 0)
    if marker < 0:
        return url
    return url[:marker]


def _native_sha256_fragment(url: str) -> str:
    marker = _native_find_from(url, "#sha256=", 0)
    if marker < 0:
        return ""
    value = url[marker + 8 :]
    amp = _native_find_from(value, "&", 0)
    if amp >= 0:
        value = value[:amp]
    if len(value) != 64:
        return ""
    i = 0
    out = ""
    while i < len(value):
        ch = value[i].lower()
        if not (("0" <= ch <= "9") or ("a" <= ch <= "f")):
            return ""
        out += ch
        i += 1
    return out


def _native_publish_acquired_artifact(
    temp_path: str,
    filename: str,
    cache_root: str,
    expected_sha256: str,
):
    actual = os._pcc_sha256_file_hex(temp_path)
    if actual == "":
        return [None, "", False]
    verified = expected_sha256 != "" and actual == expected_sha256
    if expected_sha256 != "" and not verified:
        try:
            _bootstrap_subprocess_run(["rm", "-f", temp_path], check=True)
        except Exception:
            pass
        return [None, actual, False]
    dest_dir = os.path.join(cache_root, "acquired", "sha256", actual)
    dest = os.path.join(dest_dir, filename)
    try:
        _bootstrap_subprocess_run(["mkdir", "-p", dest_dir], check=True)
        if os.path.isfile(dest):
            if os._pcc_sha256_file_hex(dest) != actual:
                return [None, actual, verified]
            _bootstrap_subprocess_run(["rm", "-f", temp_path], check=True)
        else:
            _bootstrap_subprocess_run(["mv", temp_path, dest], check=True)
    except Exception:
        return [None, actual, verified]
    return [os.path.abspath(dest), actual, verified]


def _native_owned_shape_diagnostic(path: str, cache_root: str) -> str:
    deps = _native_artifact_requires_dist(path, cache_root)
    if len(deps) > 0:
        return "PCC-PKG-ACQUIRE-DEPENDENCY-RESOLUTION-UNSUPPORTED"
    if _native_is_source_archive(path):
        prepared = _native_prepare_install_source_tree(path, cache_root, "owned-shape")
        root = prepared[0]
        pyproject = os.path.join(root, "pyproject.toml")
        if os.path.isfile(pyproject):
            try:
                with open(pyproject, "r", encoding="utf-8") as fh:
                    text = fh.read()
            except Exception:
                text = ""
            _native_cleanup_prepared_source(prepared[1])
            if (
                _native_find_from(text, "[build-system]", 0) >= 0
                and _native_find_from(text, "requires", 0) >= 0
            ):
                return "PCC-PKG-ACQUIRE-BUILD-ISOLATION-UNSUPPORTED"
        else:
            _native_cleanup_prepared_source(prepared[1])
    return ""


def _native_acquire_owned_result(
    spec: str,
    cache_dir,
    index_urls,
    abi: str,
    target_python: str,
    allow_pcc_native_build: bool = False,
):
    requirement = _native_requirement_parts(spec)
    if requirement[0] is None:
        return [
            None,
            "",
            "",
            "",
            "PCC-PKG-ACQUIRE-REQUIREMENT-UNSUPPORTED",
        ]
    name = requirement[0]
    exact_version = requirement[1]
    cache_root = cache_dir or _pcc_default_package_cache()
    scratch = os.path.join(cache_root, ".owned-acquire-" + str(os.getpid()))
    try:
        _bootstrap_subprocess_run(["mkdir", "-p", scratch], check=True)
    except Exception:
        return [None, "", "", "", "PCC-PKG-ACQUIRE-CACHE-FAILED"]
    best_url = None
    best_name = ""
    best_reason = ""
    best_hash = ""
    i = 0
    while i < len(index_urls):
        page_url = _native_index_package_url(index_urls[i], name)
        page_path = os.path.join(scratch, "index-" + str(i) + ".html")
        if os._pcc_http_download_to_file(page_url, page_path) == 0:
            try:
                with open(page_path, "r", encoding="utf-8") as fh:
                    page = fh.read()
            except Exception:
                page = ""
            links = _native_extract_href_links(page, page_url)
            j = 0
            while j < len(links):
                published_url = links[j][0]
                requires_python = links[j][2]
                if not _native_requires_python_allows(requires_python, target_python):
                    j += 1
                    continue
                clean_url = _native_url_without_fragment(published_url)
                filename = _native_url_basename(clean_url)
                expected_hash = _native_sha256_fragment(published_url)
                if expected_hash != "" and _native_is_repo_artifact(filename):
                    project = _native_artifact_project_name(filename)
                    version_ok = exact_version == "" or (
                        _native_artifact_version_text(filename) == exact_version
                    )
                    if (
                        _native_normalized_package_name(project)
                        == _native_normalized_package_name(name)
                        and version_ok
                    ):
                        compat = _native_artifact_compatibility_for_name(filename, abi)
                        if compat[0] and (
                            best_url is None
                            or _native_artifact_better_for_reason(
                                filename,
                                compat[1],
                                best_name,
                                best_reason,
                            )
                        ):
                            best_url = clean_url
                            best_name = filename
                            best_reason = compat[1]
                            best_hash = expected_hash
                j += 1
        i += 1
    if best_url is None:
        return [None, "", "", "", "PCC-PKG-ACQUIRE-HASH-REQUIRED"]
    temp_path = os.path.join(scratch, best_name + ".part")
    if os._pcc_http_download_to_file(best_url, temp_path) != 0:
        return [None, best_url, "", "", "PCC-PKG-ACQUIRE-DOWNLOAD-FAILED"]
    published = _native_publish_acquired_artifact(
        temp_path, best_name, cache_root, best_hash
    )
    if published[0] is None:
        return [None, best_url, published[1], "", "PCC-PKG-ACQUIRE-HASH-MISMATCH"]
    diagnostic = _native_owned_shape_diagnostic(published[0], cache_root)
    build_isolation = ""
    if (
        allow_pcc_native_build
        and diagnostic == "PCC-PKG-ACQUIRE-BUILD-ISOLATION-UNSUPPORTED"
    ):
        diagnostic = ""
        build_isolation = "delegated-to-pcc-native-builder"
    if diagnostic != "":
        return [
            published[0],
            best_url,
            published[1],
            _native_artifact_version_text(best_name),
            diagnostic,
        ]
    return [
        published[0],
        best_url,
        published[1],
        _native_artifact_version_text(best_name),
        "",
        build_isolation,
    ]


def _native_acquire_host_result(
    spec: str, cache_dir, index_urls, abi: str, target_python: str
):
    requirement = _native_requirement_parts(spec)
    if requirement[0] is None:
        return [
            None,
            "",
            "",
            "",
            "PCC-PKG-ACQUIRE-REQUIREMENT-UNSUPPORTED",
        ]
    name = requirement[0]
    exact_version = requirement[1]
    cache_root = cache_dir or _pcc_default_package_cache()
    stage = os.path.join(cache_root, ".host-acquire-" + str(os.getpid()))
    configured_host_python = os.environ.get("PCC_HOST_PYTHON")
    host_pythons = []
    if configured_host_python:
        host_pythons.append(configured_host_python)
    else:
        path_value = os.environ.get("PATH") or ""
        path_parts = path_value.split(":")
        i = 0
        while i < len(path_parts):
            if path_parts[i] != "":
                for basename in ("python3", "python"):
                    candidate = os.path.join(path_parts[i], basename)
                    if os.path.isfile(candidate) and candidate not in host_pythons:
                        host_pythons.append(candidate)
            i += 1
        if len(host_pythons) == 0:
            host_pythons.append("python3")
    try:
        _bootstrap_subprocess_run(["rm", "-rf", stage], check=True)
        _bootstrap_subprocess_run(["mkdir", "-p", stage], check=True)
    except Exception:
        return [None, "", "", "", "PCC-PKG-ACQUIRE-CACHE-FAILED"]
    selected = None
    selected_host_python = ""
    i = 0
    while i < len(host_pythons):
        base = [
            host_pythons[i],
            "-m",
            "pip",
            "download",
            "--disable-pip-version-check",
            "--no-deps",
            "--python-version",
            target_python,
            "--dest",
            stage,
        ]
        if len(index_urls) > 0:
            base += ["--index-url", index_urls[0]]
            j = 1
            while j < len(index_urls):
                base += ["--extra-index-url", index_urls[j]]
                j += 1
        command = (
            base + ["--no-binary=:all:", spec] if abi == "pcc-native" else base + [spec]
        )
        try:
            names = os.listdir(stage)
        except Exception:
            names = []
        j = 0
        while j < len(names):
            try:
                _bootstrap_subprocess_run(
                    ["rm", "-f", os.path.join(stage, names[j])], check=True
                )
            except Exception:
                pass
            j += 1
        try:
            _bootstrap_subprocess_run(command, check=True)
        except Exception:
            i += 1
            continue
        candidate = _native_find_links_artifact_result(name, [stage], abi)
        if candidate[0] is not None and (
            exact_version == ""
            or _native_artifact_version_text(candidate[0]) == exact_version
        ):
            selected = candidate[0]
            selected_host_python = host_pythons[i]
            break
        i += 1
    if selected is None:
        return [None, "", "", "", "PCC-PKG-ACQUIRE-HOST-FAILED"]
    filename = os.path.basename(selected)
    published = _native_publish_acquired_artifact(selected, filename, cache_root, "")
    if published[0] is None:
        return [None, "", published[1], "", "PCC-PKG-ACQUIRE-CACHE-FAILED"]
    return [
        published[0],
        "",
        published[1],
        _native_artifact_version_text(filename),
        "",
        selected_host_python,
    ]


def _native_acquisition_json(
    spec: str,
    requested_mode: str,
    actual_mode: str,
    result,
    index_urls,
    target_python: str,
) -> str:
    ok = result[0] is not None and result[4] == ""
    out = "{"
    out += '"acquire_mode": ' + _json_str(actual_mode)
    out += ', "acquire_mode_requested": ' + _json_str(requested_mode)
    out += ', "artifact_origin": ' + _json_str(
        "host-pip" if actual_mode == "host" else "simple-repository"
    )
    out += ', "artifact_path": ' + _json_str_or_null(result[0])
    out += ', "artifact_url": ' + _json_str_or_null(
        None if result[1] == "" else result[1]
    )
    build_isolation = "not_attempted"
    if actual_mode == "owned" and len(result) > 5 and result[5] != "":
        build_isolation = result[5]
    out += ', "build_isolation": ' + _json_str(build_isolation)
    out += ', "dependency_resolution": "not_attempted"'
    out += ', "diagnostic": ' + _json_str_or_null(
        None if result[4] == "" else result[4]
    )
    out += ', "hash_verified": ' + (
        "true" if actual_mode == "owned" and result[0] is not None else "false"
    )
    out += ', "host_assisted": ' + ("true" if actual_mode == "host" else "false")
    out += ', "host_python": ' + _json_str_or_null(
        result[5] if actual_mode == "host" and len(result) > 5 else None
    )
    out += ', "immutable_cache": true'
    out += ', "index_urls": ' + _json_str_list(index_urls)
    out += ', "ok": ' + ("true" if ok else "false")
    out += ', "requested_spec": ' + _json_str(spec)
    out += ', "resolved_version": ' + _json_str_or_null(
        None if result[3] == "" else result[3]
    )
    out += ', "sha256": ' + _json_str_or_null(None if result[2] == "" else result[2])
    out += ', "target_python": ' + _json_str(target_python)
    out += ', "transport_provider": ' + _json_str(
        "host-python-pip" if actual_mode == "host" else "pcc-runtime-libcurl"
    )
    out += "}"
    return out


def _native_find_links_package_names(find_links):
    packages = []
    i = 0
    while i < len(find_links):
        root = find_links[i]
        candidates = []
        if os.path.isfile(root):
            candidates.append(root)
        elif os.path.isdir(root):
            try:
                names = sorted(os.listdir(root))
            except Exception:
                names = []
            j = 0
            while j < len(names):
                candidates.append(root + "/" + names[j])
                j += 1
        j = 0
        while j < len(candidates):
            candidate = candidates[j]
            lower = candidate.lower()
            is_artifact = (
                lower.endswith(".whl")
                or lower.endswith(".tar.gz")
                or lower.endswith(".tar.bz2")
                or lower.endswith(".tar.xz")
                or lower.endswith(".tgz")
                or lower.endswith(".zip")
            )
            if is_artifact and os.path.isfile(candidate):
                project = _native_artifact_project_name(candidate)
                if project not in packages:
                    packages.append(project)
            j += 1
        i += 1
    return packages


def _native_parse_requires_dist_name(line: str):
    prefix = "Requires-Dist:"
    if not line.startswith(prefix):
        return None
    rest = line[len(prefix) :].strip()
    out = ""
    i = 0
    while i < len(rest):
        ch = rest[i]
        ok = (
            ("a" <= ch <= "z")
            or ("A" <= ch <= "Z")
            or ("0" <= ch <= "9")
            or ch == "_"
            or ch == "-"
            or ch == "."
        )
        if not ok:
            break
        out += ch
        i += 1
    return out or None


def _native_requires_from_metadata_file(path: str):
    deps = []
    try:
        with open(path, "r") as fh:
            text = fh.read()
    except Exception:
        return deps
    for line in text.split("\n"):
        dep = _native_parse_requires_dist_name(line)
        if dep is not None:
            deps.append(dep)
    return deps


def _native_requires_from_tree(root: str):
    deps = []
    for path in package_metadata_paths(root):
        file_deps = _native_requires_from_metadata_file(path)
        for dependency in file_deps:
            if dependency not in deps:
                deps.append(dependency)
    return deps


def _native_artifact_requires_dist(source, scratch_root: str):
    if source is None:
        return []
    if os.path.isdir(source):
        return _native_requires_from_tree(source)
    lower = source.lower()
    is_zip = lower.endswith(".whl") or lower.endswith(".zip")
    is_tar = (
        lower.endswith(".tar.gz")
        or lower.endswith(".tar.bz2")
        or lower.endswith(".tar.xz")
        or lower.endswith(".tgz")
    )
    if not (is_zip or is_tar):
        return []
    extract_root = scratch_root + "/" + _native_package_basename(source) + ".deps"
    try:
        _bootstrap_subprocess_run(["rm", "-rf", extract_root], check=True)
        _bootstrap_subprocess_run(["mkdir", "-p", extract_root], check=True)
        if is_zip:
            _bootstrap_subprocess_run(
                [
                    "env",
                    "LC_ALL=C",
                    "LANG=C",
                    "unzip",
                    "-q",
                    source,
                    "-d",
                    extract_root,
                ],
                check=True,
            )
        else:
            _bootstrap_subprocess_run(
                ["env", "LC_ALL=C", "LANG=C", "tar", "-xf", source, "-C", extract_root],
                check=True,
            )
        deps = _native_requires_from_tree(extract_root)
        _bootstrap_subprocess_run(["rm", "-rf", extract_root], check=True)
        return deps
    except Exception:
        try:
            _bootstrap_subprocess_run(["rm", "-rf", extract_root], check=True)
        except Exception:
            pass
        return []


def _native_resolve_install_order(
    packages,
    cache_dir,
    find_links,
    abi: str = "pcc-native",
    index_urls=None,
):
    if index_urls is None:
        index_urls = []
    ordered = []
    visiting = []
    done = []
    scratch = cache_dir or _pcc_default_package_cache()

    def visit(pkg: str) -> None:
        if _native_package_list_contains(done, pkg) != 0:
            return
        if _native_package_list_contains(visiting, pkg) != 0:
            return
        visiting.append(pkg)
        source = _native_resolve_install_source(
            pkg, cache_dir, find_links, abi, index_urls
        )
        if source is not None:
            deps = _native_artifact_requires_dist(source, scratch)
            i = 0
            while i < len(deps):
                dep = deps[i]
                if (
                    _native_resolve_install_source(
                        dep, cache_dir, find_links, abi, index_urls
                    )
                    is not None
                ):
                    visit(dep)
                i += 1
        visiting.pop()
        if _native_package_list_contains(done, pkg) == 0:
            done.append(pkg)
            ordered.append(pkg)

    i = 0
    if len(find_links) > 0:
        wheelhouse_packages = _native_find_links_package_names(find_links)
        while i < len(wheelhouse_packages):
            if _native_package_list_contains(packages, wheelhouse_packages[i]) == 0:
                visit(wheelhouse_packages[i])
            i += 1
        i = 0
    while i < len(packages):
        visit(packages[i])
        i += 1
    return ordered


def _native_resolve_install_source_result(
    spec: str,
    cache_dir,
    find_links=None,
    abi: str = "pcc-native",
    index_urls=None,
):
    if os.path.exists(spec):
        return [os.path.abspath(spec), "direct"]
    if find_links is None:
        find_links = []
    if index_urls is None:
        index_urls = []
    linked = _native_find_links_artifact_result(spec, find_links, abi)
    if linked[0] is not None:
        return linked
    if cache_dir is not None and cache_dir != "":
        direct = os.path.join(cache_dir, spec)
        if os.path.exists(direct):
            return [os.path.abspath(direct), "cache"]
    path = _native_package_path(spec, None)
    if path is not None:
        return [path, "projects"]
    if len(index_urls) > 0:
        indexed = _native_find_index_artifact_result(spec, cache_dir, index_urls, abi)
        if indexed[0] is not None:
            return indexed
    return [None, None]


def _native_resolve_install_source(
    spec: str,
    cache_dir,
    find_links=None,
    abi: str = "pcc-native",
    index_urls=None,
):
    result = _native_resolve_install_source_result(
        spec, cache_dir, find_links, abi, index_urls
    )
    return result[0]


def _native_package_list_contains(items, value) -> int:
    key = _native_normalized_package_name(value)
    i = 0
    while i < len(items):
        if _native_normalized_package_name(items[i]) == key:
            return 1
        i += 1
    return 0


def _native_cython_build_requirement(root: str) -> str:
    path = os.path.join(root, "pyproject.toml")
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "r") as fh:
            text = fh.read()
    except Exception:
        return ""
    lower = text.lower()
    pos = _native_find_from(lower, "cython", 0)
    if pos < 0:
        return ""
    ge = _native_find_from(lower, ">=", pos)
    if ge < 0:
        return "Cython"
    i = ge + 2
    while i < len(text) and (text[i] == " " or text[i] == "\t"):
        i += 1
    version = ""
    while i < len(text):
        ch = text[i]
        if ("0" <= ch <= "9") or ch == ".":
            version += ch
            i += 1
        else:
            break
    if version == "":
        return "Cython"
    return "Cython>=" + version


def _native_prepare_build_tools(root: str) -> str:
    requirement = _native_cython_build_requirement(root)
    if requirement == "":
        return ""
    tool_dir = "/tmp/pcc_build_tools_" + str(os.getpid())
    try:
        _bootstrap_subprocess_run(["rm", "-rf", tool_dir], check=True)
        _bootstrap_subprocess_run(["mkdir", "-p", tool_dir], check=True)
        script = "#!/bin/sh\n"
        script += "exec uv run --with " + _native_shell_quote(requirement)
        script += ' cython "$@"\n'
        for name in ["cython", "cython3"]:
            path = tool_dir + "/" + name
            with open(path, "w") as fh:
                fh.write(script)
            _bootstrap_subprocess_run(["chmod", "+x", path], check=True)
    except Exception:
        return ""
    return tool_dir


def _native_path_prefix(tool_dir: str) -> str:
    if tool_dir == "":
        return ""
    return "PATH=" + _native_shell_quote(tool_dir) + ":$PATH "


def _native_shell_command_succeeds(command: str) -> bool:
    try:
        _bootstrap_subprocess_run(["/bin/sh", "-c", command], check=True)
        return True
    except Exception:
        return False


def _native_meson_setup_shell_command(source: str, build_dir: str, tool_dir: str):
    prefix = _native_path_prefix(tool_dir)
    if _native_shell_command_succeeds(prefix + "command -v meson >/dev/null 2>&1"):
        return (
            prefix
            + "meson setup "
            + _native_shell_quote(build_dir)
            + " "
            + _native_shell_quote(source)
        )
    vendored = os.path.join(source, "vendored-meson", "meson", "meson.py")
    if os.path.isfile(vendored):
        python = os.environ.get("PCC_BUILD_PYTHON") or "python3"
        return (
            prefix
            + _native_shell_quote(python)
            + " "
            + _native_shell_quote(vendored)
            + " setup "
            + _native_shell_quote(build_dir)
            + " "
            + _native_shell_quote(source)
        )
    return None


def _native_build_report_action_json(
    kind: str, command: str, status: str, returncode, output_tail: str
) -> str:
    out = "{"
    out += '"command": ' + _json_str(command)
    out += ', "kind": ' + _json_str(kind)
    out += ', "returncode": ' + ("null" if returncode is None else str(returncode))
    out += ', "status": ' + _json_str(status)
    out += ', "output_tail": ' + _json_str(output_tail)
    out += "}"
    return out


def _native_redirected_shell_command(command: str, label: str) -> list:
    log_path = "/tmp/pcc_build_" + str(os.getpid()) + "_" + label + ".log"
    redirected = command + " > " + _native_shell_quote(log_path) + " 2>&1"
    return [redirected, log_path]


def _native_build_log_tail(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except Exception:
        return ""
    if len(text) > 4000:
        return text[len(text) - 4000 :]
    return text


def _native_ensure_meson_build_outputs_json(
    source, build_mode: str = "owned"
) -> str:
    if not (build_mode == "owned" or build_mode == "host"):
        return (
            '{"actions": [], "build_mode_requested": '
            + _json_str(build_mode)
            + ', "build_ownership": "unresolved", '
            '"diagnostics": ["PCC-PKG-BUILD-MODE-INVALID"], '
            '"host_assisted": false, "host_free_build_claim": false, '
            '"host_python": null, "ok": false, '
            '"reason": "build_mode_invalid", "skipped": false}'
        )
    if source is None or not os.path.isdir(source):
        return (
            '{"actions": [], "build_mode_requested": '
            + _json_str(build_mode)
            + ', "build_ownership": "not-required", '
            '"host_assisted": false, "host_free_build_claim": true, '
            '"host_python": null, "ok": true, '
            '"reason": "not_source_tree", "skipped": true}'
        )
    if not os.path.isfile(os.path.join(source, "meson.build")):
        return (
            '{"actions": [], "build_mode_requested": '
            + _json_str(build_mode)
            + ', "build_ownership": "not-required", '
            '"host_assisted": false, "host_free_build_claim": true, '
            '"host_python": null, "ok": true, '
            '"reason": "no_meson_build", "skipped": true}'
        )
    if build_mode == "owned":
        return _native_owned_build_tool_required_json(build_mode)

    build_dir = os.path.join(source, "build", "pcc-package", "meson-build")
    tool_dir = _native_prepare_build_tools(source)
    actions = "["
    ok = True
    skipped = False
    reason = None

    def add_action(
        kind: str, command: str, status: str, returncode, output_tail: str
    ) -> None:
        nonlocal actions, ok
        if actions != "[":
            actions += ", "
        actions += _native_build_report_action_json(
            kind, command, status, returncode, output_tail
        )
        if status != "passed":
            ok = False

    try:
        if not os.path.isfile(os.path.join(build_dir, "build.ninja")):
            setup_command = _native_meson_setup_shell_command(
                source, build_dir, tool_dir
            )
            if setup_command is None:
                ok = False
                skipped = False
                reason = "meson_not_available"
            else:
                redirected = _native_redirected_shell_command(
                    setup_command, "meson_setup"
                )
                try:
                    _bootstrap_subprocess_run(["mkdir", "-p", build_dir], check=True)
                    _bootstrap_subprocess_run(
                        ["/bin/sh", "-c", redirected[0]], check=True
                    )
                    add_action(
                        "meson_setup",
                        setup_command,
                        "passed",
                        0,
                        _native_build_log_tail(redirected[1]),
                    )
                except Exception:
                    add_action(
                        "meson_setup",
                        setup_command,
                        "failed",
                        1,
                        _native_build_log_tail(redirected[1]),
                    )
                try:
                    _bootstrap_subprocess_run(["rm", "-f", redirected[1]], check=True)
                except Exception:
                    pass
        if ok and not skipped:
            jobs_text = os.environ.get("PCC_PACKAGE_BUILD_JOBS") or "2"
            try:
                jobs = int(jobs_text)
            except Exception:
                jobs = 2
            if jobs < 1:
                jobs = 1
            ninja_command = (
                _native_path_prefix(tool_dir)
                + "ninja -C "
                + _native_shell_quote(build_dir)
                + " -j "
                + str(jobs)
            )
            redirected = _native_redirected_shell_command(ninja_command, "meson_build")
            try:
                _bootstrap_subprocess_run(["/bin/sh", "-c", redirected[0]], check=True)
                add_action(
                    "meson_build",
                    ninja_command,
                    "passed",
                    0,
                    _native_build_log_tail(redirected[1]),
                )
            except Exception:
                add_action(
                    "meson_build",
                    ninja_command,
                    "failed",
                    1,
                    _native_build_log_tail(redirected[1]),
                )
            try:
                _bootstrap_subprocess_run(["rm", "-f", redirected[1]], check=True)
            except Exception:
                pass
    finally:
        if tool_dir != "":
            try:
                _bootstrap_subprocess_run(["rm", "-rf", tool_dir], check=True)
            except Exception:
                pass

    actions += "]"
    out = "{"
    out += '"actions": ' + actions
    out += ', "build_backend": "meson"'
    out += ', "build_mode_requested": ' + _json_str(build_mode)
    out += ', "build_ownership": "host"'
    out += ', "host_assisted": true'
    out += ', "host_free_build_claim": false'
    out += ', "host_python": ' + _json_str(
        os.environ.get("PCC_BUILD_PYTHON")
        or os.environ.get("PCC_HOST_PYTHON")
        or "python3"
    )
    out += ', "ok": ' + ("true" if ok else "false")
    out += ', "reason": ' + _json_str_or_null(reason)
    out += ', "skipped": ' + ("true" if skipped else "false")
    out += "}"
    return out


def _native_name_endswith_any(name: str, suffixes) -> bool:
    i = 0
    while i < len(suffixes):
        if name.endswith(suffixes[i]):
            return True
        i += 1
    return False


def _native_skip_importable_dir_name(name: str) -> bool:
    if name.startswith(".") or name == "__pycache__":
        return True
    return _native_name_endswith_any(name, [".dist-info", ".egg-info"])


def _native_has_direct_importable_payload(path: str) -> bool:
    if not os.path.isdir(path):
        return False
    try:
        names = sorted(os.listdir(path))
    except Exception:
        names = []
    i = 0
    while i < len(names):
        child_name = names[i]
        if (
            not child_name.startswith(".")
            and child_name != "__pycache__"
            and child_name != "setup.py"
        ):
            child = os.path.join(path, child_name)
            lower = child_name.lower()
            if os.path.isfile(child) and (
                lower.endswith(".py")
                or lower.endswith(".pyi")
                or lower.endswith(".so")
                or lower.endswith(".pyd")
                or lower.endswith(".dll")
                or lower.endswith(".dylib")
            ):
                return True
        i += 1
    return False


def _native_install_importable_payload(source, target: str, name: str) -> str:
    install_root = os.path.abspath(os.path.join(target, name))
    if source is not None and not os.path.isdir(source):
        lower = source.lower()
        is_zip = lower.endswith(".whl") or lower.endswith(".zip")
        is_tar = (
            lower.endswith(".tar.gz")
            or lower.endswith(".tar.bz2")
            or lower.endswith(".tar.xz")
            or lower.endswith(".tgz")
        )
        if is_zip or is_tar:
            extract_root = os.path.abspath(
                os.path.join(target, "." + name + ".extract")
            )
            _bootstrap_subprocess_run(["rm", "-rf", extract_root], check=True)
            _bootstrap_subprocess_run(["mkdir", "-p", extract_root], check=True)
            if is_zip:
                _bootstrap_subprocess_run(
                    [
                        "env",
                        "LC_ALL=C",
                        "LANG=C",
                        "unzip",
                        "-q",
                        source,
                        "-d",
                        extract_root,
                    ],
                    check=True,
                )
            else:
                _bootstrap_subprocess_run(
                    [
                        "env",
                        "LC_ALL=C",
                        "LANG=C",
                        "tar",
                        "-xf",
                        source,
                        "-C",
                        extract_root,
                    ],
                    check=True,
                )
            install_root = _native_install_importable_payload(
                extract_root, target, name
            )
            _bootstrap_subprocess_run(["rm", "-rf", extract_root], check=True)
            return install_root
    if source is None or not os.path.isdir(source):
        _bootstrap_subprocess_run(["mkdir", "-p", install_root], check=True)
        return install_root

    _bootstrap_subprocess_run(["mkdir", "-p", target], check=True)
    copied = False
    first_install_root = ""
    preferred_install_root = ""

    def remember_install_root(dest: str) -> None:
        nonlocal first_install_root
        # ``installed_path`` is also the manifest directory.  A source project
        # may ship top-level helper modules (for example a docs ``conf.py``)
        # alongside its real package directory; never select a copied file as
        # that directory.
        if first_install_root == "" and os.path.isdir(dest):
            first_install_root = os.path.abspath(dest)

    def copy_payload(path: str) -> None:
        dest = os.path.join(target, os.path.basename(path))
        if os.path.abspath(dest) == os.path.abspath(path):
            # Payload already lives at the destination (e.g. re-recording a
            # cache-resolved source into the same cache root). The ``rm -rf``
            # below would delete the source itself before ``cp`` runs.
            remember_install_root(dest)
            return
        _bootstrap_subprocess_run(["rm", "-rf", dest], check=True)
        _bootstrap_subprocess_run(["cp", "-R", path, target], check=True)
        remember_install_root(dest)

    def overlay_payload(path: str) -> None:
        dest = os.path.join(target, os.path.basename(path))
        if os.path.abspath(dest) == os.path.abspath(path):
            remember_install_root(dest)
            return
        _bootstrap_subprocess_run(["mkdir", "-p", dest], check=True)
        _bootstrap_subprocess_run(["cp", "-R", path + "/.", dest], check=True)
        remember_install_root(dest)

    if os.path.isfile(os.path.join(source, "__init__.py")) or (
        _native_has_direct_importable_payload(source)
        and not _native_has_source_project_marker(source)
    ):
        copy_payload(source)
        copied = True
    else:
        bases = [source, os.path.join(source, "src")]
        visible_dirs = []
        try:
            top_names = sorted(os.listdir(source))
        except Exception:
            top_names = []
        j = 0
        while j < len(top_names):
            top_name = top_names[j]
            top_child = os.path.join(source, top_name)
            if os.path.isdir(top_child) and not _native_skip_importable_dir_name(
                top_name
            ):
                visible_dirs.append(top_child)
                if _native_normalized_package_name(
                    top_name
                ) == _native_normalized_package_name(name) and (
                    os.path.isfile(os.path.join(top_child, "__init__.py"))
                    or _native_has_direct_importable_payload(top_child)
                ):
                    preferred_install_root = top_child
            j += 1
        if preferred_install_root != "":
            copy_payload(preferred_install_root)
            copied = True
        elif len(visible_dirs) == 1:
            bases.append(visible_dirs[0])
            bases.append(visible_dirs[0] + "/src")
        for base in ([] if preferred_install_root != "" else bases):
            if not os.path.isdir(base):
                continue
            try:
                names = sorted(os.listdir(base))
            except Exception:
                names = []
            for child_name in names:
                if _native_skip_importable_dir_name(child_name):
                    continue
                child = os.path.join(base, child_name)
                if os.path.isdir(child) and (
                    os.path.isfile(os.path.join(child, "__init__.py"))
                    or _native_has_direct_importable_payload(child)
                ):
                    copy_payload(child)
                    copied = True
                elif (
                    os.path.isfile(child)
                    and child.endswith(".py")
                    and child_name != "setup.py"
                ):
                    dest = os.path.join(target, child_name)
                    _bootstrap_subprocess_run(["rm", "-f", dest], check=True)
                    _bootstrap_subprocess_run(["cp", child, dest], check=True)
                    remember_install_root(dest)
                    copied = True
    build_root = os.path.join(source, "build", "pcc-package", "meson-build")
    if os.path.isdir(build_root):
        try:
            build_names = sorted(os.listdir(build_root))
        except Exception:
            build_names = []
        b = 0
        preferred_build_root = ""
        while b < len(build_names):
            build_name = build_names[b]
            build_child = os.path.join(build_root, build_name)
            if os.path.isdir(build_child) and _native_normalized_package_name(
                build_name
            ) == _native_normalized_package_name(name):
                preferred_build_root = build_child
                break
            b += 1
        b = 0
        while b < len(build_names):
            build_name = build_names[b]
            build_child = os.path.join(build_root, build_name)
            if (
                os.path.isdir(build_child)
                and (
                    preferred_build_root == ""
                    or os.path.abspath(build_child)
                    == os.path.abspath(preferred_build_root)
                )
                and not _native_skip_importable_dir_name(build_name)
                and (
                    os.path.isfile(os.path.join(build_child, "__init__.py"))
                    or _native_has_direct_importable_payload(build_child)
                )
            ):
                overlay_payload(build_child)
                copied = True
            b += 1
    if not copied:
        _bootstrap_subprocess_run(["mkdir", "-p", install_root], check=True)
    elif os.path.isdir(os.path.join(target, name.replace("-", "_"))):
        install_root = os.path.abspath(os.path.join(target, name.replace("-", "_")))
    elif first_install_root != "":
        install_root = first_install_root
    elif not os.path.exists(install_root):
        _bootstrap_subprocess_run(["mkdir", "-p", install_root], check=True)
    return install_root


def _native_is_source_archive(path) -> bool:
    if path is None or os.path.isdir(path):
        return False
    lower = path.lower()
    return (
        lower.endswith(".tar.gz")
        or lower.endswith(".tar.bz2")
        or lower.endswith(".tar.xz")
        or lower.endswith(".tgz")
        or lower.endswith(".zip")
    )


def _native_has_source_project_marker(path: str) -> bool:
    return os.path.isfile(os.path.join(path, "pyproject.toml")) or os.path.isfile(
        os.path.join(path, "setup.py")
    )


def _native_select_extracted_source_root(extract_root: str) -> str:
    if _native_has_source_project_marker(extract_root):
        return extract_root
    try:
        names = sorted(os.listdir(extract_root))
    except Exception:
        names = []
    candidates = []
    i = 0
    while i < len(names):
        path = os.path.join(extract_root, names[i])
        if os.path.isdir(path) and _native_has_source_project_marker(path):
            candidates.append(path)
        i += 1
    if len(candidates) == 1:
        return candidates[0]
    return extract_root


def _native_prepare_install_source_tree(source, cache_root: str, name: str):
    if source is None or os.path.isdir(source) or not _native_is_source_archive(source):
        return [source, None]
    staging = os.path.join(
        os.path.abspath(cache_root),
        ".pcc-source-build-" + _sanitize_tag(name) + "-" + str(os.getpid()),
    )
    try:
        _bootstrap_subprocess_run(["rm", "-rf", staging], check=True)
        _bootstrap_subprocess_run(["mkdir", "-p", staging], check=True)
        if source.lower().endswith(".zip"):
            _bootstrap_subprocess_run(
                ["unzip", "-q", source, "-d", staging], check=True
            )
        else:
            _bootstrap_subprocess_run(["tar", "-xf", source, "-C", staging], check=True)
    except Exception:
        return [source, None]
    return [_native_select_extracted_source_root(staging), staging]


def _native_cleanup_prepared_source(staging) -> None:
    if staging is None or staging == "":
        return
    try:
        _bootstrap_subprocess_run(["rm", "-rf", staging], check=True)
    except Exception:
        pass


def _native_single_package_extension_target(root: str):
    c_files = _native_collect_suffix_files(root, [".c"], True)
    candidates = []
    root_prefix = os.path.abspath(root)
    if not root_prefix.endswith("/"):
        root_prefix += "/"
    i = 0
    while i < len(c_files):
        source = os.path.abspath(c_files[i])
        parent = os.path.dirname(source)
        while parent.startswith(root_prefix):
            if os.path.isfile(os.path.join(parent, "__init__.py")):
                relative_parent = parent[len(root_prefix) :]
                stem = os.path.basename(source)
                dot = stem.rfind(".")
                if dot > 0:
                    stem = stem[:dot]
                output = stem + _native_pcc_extension_suffix()
                if relative_parent != "":
                    output = relative_parent + "/" + output
                candidates.append([source, output])
                break
            next_parent = os.path.dirname(parent)
            if next_parent == parent:
                break
            parent = next_parent
        i += 1
    if len(candidates) == 1 and len(c_files) == 1:
        return candidates[0]
    return None


def _native_json_field_is(text: str, key: str, literal: str) -> bool:
    marker = '"' + key + '"'
    pos = _native_find_from(text, marker, 0)
    if pos < 0:
        return False
    colon = _native_find_from(text, ":", pos + len(marker))
    if colon < 0:
        return False
    i = colon + 1
    while i < len(text) and (
        text[i] == " " or text[i] == "\t" or text[i] == "\r" or text[i] == "\n"
    ):
        i += 1
    return text[i : i + len(literal)] == literal


def _native_json_string_field(text: str, key: str):
    """Read one JSON string field without importing the host json module."""
    marker = '"' + key + '"'
    pos = _native_find_from(text, marker, 0)
    if pos < 0:
        return None
    colon = _native_find_from(text, ":", pos + len(marker))
    if colon < 0:
        return None
    i = colon + 1
    while i < len(text) and (
        text[i] == " " or text[i] == "\t" or text[i] == "\r" or text[i] == "\n"
    ):
        i += 1
    if i >= len(text) or text[i] != '"':
        return None
    return _native_json_string_after(text, '"', i)


def _native_satisfied_manifest_response(text: str, manifest_path: str) -> str:
    """Return an existing install receipt with a no-op action label."""
    end = len(text)
    while end > 0 and (
        text[end - 1] == " "
        or text[end - 1] == "\t"
        or text[end - 1] == "\r"
        or text[end - 1] == "\n"
    ):
        end -= 1
    if end == 0 or text[end - 1] != "}":
        return ""
    body = text[: end - 1]
    installed = '"install_action": "installed"'
    pos = _native_find_from(body, installed, 0)
    if pos >= 0:
        body = (
            body[:pos]
            + '"install_action": "already-satisfied"'
            + body[pos + len(installed) :]
        )
    else:
        body += ', "install_action": "already-satisfied"'
    body += ', "manifest_path": ' + _json_str(manifest_path)
    return body + "}"


def _native_already_satisfied_manifest_json(
    site: str, name: str, digest: str, abi: str
):
    """Find a live pcc1 install matching one immutable artifact receipt."""
    if digest == "" or not os.path.isdir(site):
        return None
    try:
        children = sorted(os.listdir(site))
    except Exception:
        return None
    i = 0
    while i < len(children):
        manifest_path = os.path.join(site, children[i], "pcc-package.json")
        if os.path.isfile(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8") as fh:
                    text = fh.read()
            except Exception:
                text = ""
            installed_path = _native_json_string_field(text, "installed_path")
            if (
                _native_json_field_is(text, "name", _json_str(name))
                and _native_json_field_is(
                    text, "artifact_sha256", _json_str(digest)
                )
                and _native_json_field_is(text, "abi_mode", _json_str(abi))
                and _native_json_field_is(text, "install_success", "true")
                and installed_path is not None
                and os.path.isdir(installed_path)
            ):
                response = _native_satisfied_manifest_response(text, manifest_path)
                if response != "":
                    return response
        i += 1
    return None


def _native_owned_build_tool_required_json(build_mode: str) -> str:
    return (
        '{"actions": [], "build_backend": "meson", '
        '"build_mode_requested": '
        + _json_str(build_mode)
        + ', "build_ownership": "owned-unavailable", '
        '"diagnostics": ["PCC-PKG-OWNED-BUILD-TOOL-REQUIRED"], '
        '"host_assisted": false, "host_free_build_claim": false, '
        '"host_python": null, "ok": false, '
        '"reason": "owned_build_tool_required", "skipped": false}'
    )


def _native_owned_meson_entry(source: str):
    """Return the source entry for the Meson program owned by this build.

    A source distribution may carry its own pinned Meson checkout.  That is
    the only implicit source accepted here: an ambient ``meson`` executable
    may be a Python console script and therefore cannot prove an owned build.
    Tests and controlled packagers may point at an equivalent source entry
    explicitly, but the value must still be a real Python source file.
    """
    explicit = str(os.environ.get("PCC_PACKAGE_OWNED_MESON_SOURCE") or "").strip()
    if explicit != "":
        explicit = os.path.abspath(explicit)
        if os.path.isfile(explicit) and explicit.endswith(".py"):
            return explicit
        return None
    vendored = os.path.join(source, "vendored-meson", "meson", "meson.py")
    if os.path.isfile(vendored):
        return vendored
    return None


def _native_owned_build_exec_entry():
    """Locate the pcc-Python target replay entry without importing it."""
    explicit = str(
        os.environ.get("PCC_PACKAGE_OWNED_BUILD_EXEC_SOURCE") or ""
    ).strip()
    candidates = []
    if explicit != "":
        candidates.append(os.path.abspath(explicit))
    repo_root = str(os.environ.get("PCC_REPO_ROOT") or "").strip()
    if repo_root != "":
        candidates.append(
            os.path.join(os.path.abspath(repo_root), "pcc", "package", "build_exec.py")
        )
    candidates.append(
        os.path.join(os.path.abspath(os.getcwd()), "pcc", "package", "build_exec.py")
    )
    try:
        candidates.append(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "package", "build_exec.py")
        )
    except Exception:
        pass
    i = 0
    while i < len(candidates):
        if os.path.isfile(candidates[i]):
            return candidates[i]
        i += 1
    return None


def _native_owned_meson_receipt_path(build_dir: str) -> str:
    return os.path.join(build_dir, ".pcc-owned-meson-receipt")


def _native_owned_file_sha256(path: str) -> str:
    """Return one compiler-owned file digest or fail closed.

    The native ``os`` port owns this primitive, so producing an owned-build
    receipt never imports host ``hashlib``.  Validate the result here because
    an absent or malformed digest must not degrade into an existence marker.
    """
    try:
        value = str(os._pcc_sha256_file_hex(path) or "").lower()
    except Exception:
        return ""
    if len(value) != 64:
        return ""
    i = 0
    while i < len(value):
        ch = value[i]
        if not ("0" <= ch <= "9" or "a" <= ch <= "f"):
            return ""
        i += 1
    return value


def _native_owned_meson_receipt_text(
    compiler: str, meson_source: str, meson_tool: str
) -> str:
    """Bind a configured graph to its compiler, tool, and source closure."""
    compiler_sha = _native_owned_file_sha256(compiler)
    tool_sha = _native_owned_file_sha256(meson_tool)
    source_root = os.path.abspath(os.path.dirname(meson_source))
    sources = _iter_py_sources_under(source_root)
    if compiler_sha == "" or tool_sha == "" or len(sources) == 0:
        return ""
    entry = os.path.abspath(meson_source)
    found_entry = False
    out = "pcc-owned-meson-receipt-v2\n"
    out += "compiler-sha256=" + compiler_sha + "\n"
    out += "tool-sha256=" + tool_sha + "\n"
    out += "source-count=" + str(len(sources)) + "\n"
    i = 0
    while i < len(sources):
        path = os.path.abspath(sources[i])
        if path == entry:
            found_entry = True
        digest = _native_owned_file_sha256(path)
        if digest == "":
            return ""
        rel = os.path.relpath(path, source_root)
        out += "source=" + str(len(rel)) + ":" + rel + ":" + digest + "\n"
        i += 1
    if not found_entry:
        return ""
    return out


def _native_owned_meson_receipt_matches(path: str, expected: str) -> bool:
    if expected == "" or not os.path.isfile(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as fh:
            actual = fh.read()
    except Exception:
        return False
    return actual == expected


def _native_meson_graph_host_python_command(build_dir: str):
    """Name the first generated command that would reintroduce host Python.

    The compiled Meson process is owned, but generated custom commands are a
    second execution boundary.  Do not call Ninja and then infer ownership
    from its success: reject a graph containing a Python/Cython/uv command
    before it executes.  This deliberately scans only Ninja ``command =``
    records, so source paths containing the word ``python`` do not create a
    false ownership claim or a false rejection.
    """
    ninja_path = os.path.join(build_dir, "build.ninja")
    try:
        with open(ninja_path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except Exception:
        return None
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith("command ="):
            command = stripped[len("command =") :].strip()
            # Inspect command tokens, not arbitrary substrings: a compiler
            # argument such as ``-I.../python/include`` is not a host process.
            token_text = command
            for separator in ("&&", "||", ";", "|", "(", ")"):
                token_text = token_text.replace(separator, " ")
            tokens = token_text.split()
            j = 0
            while j < len(tokens):
                token = tokens[j].strip("'\"` ,")
                base = os.path.basename(token).lower()
                is_python = base == "python" or base.startswith("python3")
                is_pypy = base == "pypy" or base.startswith("pypy3")
                is_cython = base == "cython" or base.startswith("cython3")
                is_env_python = base in ("$python", "${python}", "%python%")
                is_uv_python = base in ("uv", "uvx") and (
                    base == "uvx"
                    or (j + 1 < len(tokens) and tokens[j + 1].lower() == "run")
                )
                if (
                    is_python
                    or is_pypy
                    or is_cython
                    or is_env_python
                    or is_uv_python
                ):
                    return command
                j += 1
        i += 1
    return None


def _native_owned_meson_build_json(name: str, source: str) -> str:
    """Compile and execute Meson through the current native pcc stage.

    ``sys.executable`` is not sufficient evidence here: in a host invocation
    it is CPython, while in pcc1 it is the native compiler.  Require the
    bootstrap executable discriminator, compile the source-carried Meson
    entry into its own native artifact, and only then configure/build.  Every
    successful report therefore has no host-interpreter process in this
    orchestration path.
    """
    compiler = _native_bootstrap_executable()
    meson_source = _native_owned_meson_entry(source)
    build_exec_source = _native_owned_build_exec_entry()
    build_root = os.path.join(source, "build", "pcc-package")
    build_dir = os.path.join(build_root, "meson-build")
    tool_dir = os.path.join(build_root, "owned-tools")
    meson_tool = os.path.join(tool_dir, "meson")
    build_exec_tool = os.path.join(tool_dir, "pcc-package-build-exec")
    build_report_path = os.path.join(
        build_root, "eager-meson-build-report.json"
    )
    receipt_path = _native_owned_meson_receipt_path(build_dir)
    expected_receipt = ""
    actions = "["
    diagnostics = []
    ok = True
    reason = None

    def add_action(kind: str, command, status: str, returncode) -> None:
        nonlocal actions, ok
        if actions != "[":
            actions += ", "
        command_text = ""
        j = 0
        while j < len(command):
            if command_text != "":
                command_text += " "
            command_text += _native_shell_quote(str(command[j]))
            j += 1
        actions += _native_build_report_action_json(
            kind,
            command_text,
            status,
            returncode,
            "",
        )
        if status != "passed":
            ok = False

    if compiler == "":
        diagnostics.append("PCC-PKG-OWNED-BUILD-COMPILER-REQUIRED")
        ok = False
        reason = "owned_build_compiler_required"
    elif meson_source is None:
        diagnostics.append("PCC-PKG-OWNED-MESON-SOURCE-REQUIRED")
        ok = False
        reason = "owned_meson_source_required"
    elif build_exec_source is None:
        diagnostics.append("PCC-PKG-OWNED-BUILD-EXEC-SOURCE-REQUIRED")
        ok = False
        reason = "owned_build_exec_source_required"

    compile_command = []
    if ok:
        compile_command = [
            compiler,
            "--backend=self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            meson_source,
            "-o",
            meson_tool,
        ]
        try:
            _bootstrap_subprocess_run(["mkdir", "-p", tool_dir], check=True)
            _bootstrap_subprocess_run(compile_command, check=True)
            if not os.path.isfile(meson_tool) or not os.access(meson_tool, os.X_OK):
                raise RuntimeError(
                    "owned Meson compile returned without an executable artifact"
                )
            add_action("owned_meson_compile", compile_command, "passed", 0)
        except Exception:
            add_action("owned_meson_compile", compile_command, "failed", 1)
            diagnostics.append("PCC-PKG-OWNED-MESON-COMPILE-FAILED")
            reason = "owned_meson_compile_failed"

    if ok:
        expected_receipt = _native_owned_meson_receipt_text(
            compiler, meson_source, meson_tool
        )
        if expected_receipt == "":
            diagnostics.append("PCC-PKG-OWNED-BUILD-RECEIPT-FAILED")
            ok = False
            reason = "owned_build_receipt_failed"

    if ok and os.path.isfile(os.path.join(build_dir, "build.ninja")):
        if not _native_owned_meson_receipt_matches(
            receipt_path, expected_receipt
        ):
            diagnostics.append("PCC-PKG-BUILD-PROVENANCE-UNVERIFIED")
            ok = False
            reason = "existing_meson_build_provenance_unverified"

    if ok and not os.path.isfile(os.path.join(build_dir, "build.ninja")):
        setup_command = [meson_tool, "setup", build_dir, source]
        try:
            _bootstrap_subprocess_run(["mkdir", "-p", build_dir], check=True)
            _bootstrap_subprocess_run(setup_command, check=True)
            if not os.path.isfile(os.path.join(build_dir, "build.ninja")):
                raise RuntimeError(
                    "owned Meson setup returned without build.ninja"
                )
            final_receipt = _native_owned_meson_receipt_text(
                compiler, meson_source, meson_tool
            )
            if final_receipt == "" or final_receipt != expected_receipt:
                raise RuntimeError(
                    "owned Meson inputs changed while configuring the graph"
                )
            with open(receipt_path, "w", encoding="utf-8") as fh:
                fh.write(final_receipt)
            add_action("owned_meson_setup", setup_command, "passed", 0)
        except Exception:
            add_action("owned_meson_setup", setup_command, "failed", 1)
            diagnostics.append("PCC-PKG-OWNED-MESON-SETUP-FAILED")
            reason = "owned_meson_setup_failed"

    if ok:
        host_command = _native_meson_graph_host_python_command(build_dir)
        if host_command is not None:
            diagnostics.append("PCC-PKG-OWNED-BUILD-GRAPH-HOST-PYTHON")
            ok = False
            reason = "owned_build_graph_uses_host_python"

    if ok:
        build_exec_compile_command = [
            compiler,
            "--backend=self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            build_exec_source,
            "-o",
            build_exec_tool,
        ]
        try:
            _bootstrap_subprocess_run(build_exec_compile_command, check=True)
            if not os.path.isfile(build_exec_tool) or not os.access(
                build_exec_tool, os.X_OK
            ):
                raise RuntimeError(
                    "owned target replay compile returned without an executable"
                )
            add_action(
                "owned_build_exec_compile",
                build_exec_compile_command,
                "passed",
                0,
            )
        except Exception:
            add_action(
                "owned_build_exec_compile",
                build_exec_compile_command,
                "failed",
                1,
            )
            diagnostics.append("PCC-PKG-OWNED-BUILD-EXEC-COMPILE-FAILED")
            reason = "owned_build_exec_compile_failed"

    if ok:
        jobs_text = str(os.environ.get("PCC_PACKAGE_BUILD_JOBS") or "2")
        try:
            jobs = int(jobs_text)
        except Exception:
            jobs = 2
        if jobs < 1:
            jobs = 1
        timeout_text = str(os.environ.get("PCC_PACKAGE_BUILD_TIMEOUT") or "300")
        build_command = [
            build_exec_tool,
            name,
            "--path",
            source,
            "--execute",
            "--eager-meson-extensions",
            "--jobs",
            str(jobs),
            "--timeout",
            timeout_text,
            "--build-mode",
            "owned",
            "--report",
            build_report_path,
            "--json",
        ]
        build_process_ok = False
        try:
            with open(build_report_path, "w", encoding="utf-8") as fh:
                fh.write("")
            _bootstrap_subprocess_run(build_command, check=True)
            build_process_ok = True
        except Exception:
            pass
        try:
            with open(build_report_path, "r", encoding="utf-8") as fh:
                build_report = fh.read()
        except Exception:
            build_report = ""
        report_ok = (
            build_process_ok
            and _native_json_field_is(build_report, "ok", "true")
            and _native_json_field_is(
                build_report, "build_ownership", '"owned"'
            )
            and _native_json_field_is(build_report, "host_assisted", "false")
            and _native_json_field_is(build_report, "host_python", "null")
            and _native_json_field_is(
                build_report, "host_free_build_claim", "true"
            )
        )
        if report_ok:
            add_action("owned_meson_target_replay", build_command, "passed", 0)
        else:
            add_action("owned_meson_target_replay", build_command, "failed", 1)
            diagnostics.append("PCC-PKG-OWNED-MESON-BUILD-FAILED")
            reason = "owned_meson_build_failed"

    actions += "]"
    out = "{"
    out += '"actions": ' + actions
    out += ', "build_backend": "pcc-native-meson"'
    out += ', "build_compiler": ' + _json_str_or_null(
        compiler if compiler != "" else None
    )
    out += ', "build_mode_requested": "owned"'
    out += ', "build_ownership": "owned"'
    out += ', "diagnostics": ' + _json_str_list(diagnostics)
    out += ', "host_assisted": false'
    out += ', "host_free_build_claim": ' + ("true" if ok else "false")
    out += ', "host_python": null'
    out += ', "meson_source": ' + _json_str_or_null(meson_source)
    out += ', "name": ' + _json_str(name)
    out += ', "ok": ' + ("true" if ok else "false")
    out += ', "reason": ' + _json_str_or_null(reason)
    out += ', "skipped": false'
    out += "}"
    return out


def _native_unsupported_source_build_json(build_mode: str, reason: str, diagnostic: str) -> str:
    return (
        '{"actions": [], "build_mode_requested": ' + _json_str(build_mode)
        + ', "build_ownership": "unresolved", "diagnostics": ['
        + _json_str(diagnostic) + '], "host_assisted": false, '
        '"host_free_build_claim": false, "host_python": null, "ok": false, '
        '"reason": ' + _json_str(reason) + ', "skipped": false}'
    )


def _native_build_install_source_json(
    name: str, source, abi: str, build_mode: str = "owned"
) -> str:
    if not (build_mode == "owned" or build_mode == "host"):
        return (
            '{"actions": [], "build_mode_requested": '
            + _json_str(build_mode)
            + ', "build_ownership": "unresolved", '
            '"diagnostics": ["PCC-PKG-BUILD-MODE-INVALID"], '
            '"host_assisted": false, "host_free_build_claim": false, '
            '"host_python": null, "ok": false, '
            '"reason": "build_mode_invalid", "skipped": false}'
        )
    if source is None or not os.path.isdir(source):
        return (
            '{"actions": [], "build_mode_requested": '
            + _json_str(build_mode)
            + ', "build_ownership": "not-required", '
            '"host_assisted": false, "host_free_build_claim": true, '
            '"host_python": null, "ok": true, '
            '"reason": "not_source_tree", "skipped": true}'
        )
    has_meson_build = os.path.isfile(os.path.join(source, "meson.build"))
    source_policy = "unrecognized"
    if not has_meson_build:
        try:
            source_policy = _native_source_build_policy(source)
        except ValueError:
            return _native_unsupported_source_build_json(
                build_mode, "unsupported_build_metadata", "PCC-PKG-PROJECT-METADATA-UNSUPPORTED"
            )
        if source_policy == "requires_build_hook":
            return _native_unsupported_source_build_json(
                build_mode, "declared_build_hook_requires_owner", "PCC-PKG-BUILD-HOOK-UNSUPPORTED"
            )
    if build_mode == "owned" and has_meson_build:
        # A source-tree report is not an ownership authority.  Re-enter the
        # compiler/tool/source-bound receipt path; it may reuse its verified
        # configured graph, but it never accepts a package-authored JSON claim
        # as proof that a host interpreter was absent.
        return _native_owned_meson_build_json(name, source)
    if abi == "pcc-native" and _native_has_installable_meson_payload(name, source):
        if build_mode == "owned":
            return (
                '{"actions": [], "build_backend": "existing", '
                '"build_mode_requested": "owned", '
                '"build_ownership": "prebuilt-unverified", '
                '"diagnostics": ["PCC-PKG-BUILD-PROVENANCE-UNVERIFIED"], '
                '"host_assisted": null, "host_free_build_claim": false, '
                '"host_python": null, "ok": false, '
                '"reason": "existing_build_provenance_unverified", '
                '"skipped": false}'
            )
        return (
            '{"actions": [], "build_backend": "existing", '
            '"build_mode_requested": '
            + _json_str(build_mode)
            + ', "build_ownership": "prebuilt-unverified"'
            + ', "host_assisted": null'
            + ', "host_free_build_claim": false'
            + ', "host_python": null, "ok": true, '
            '"reason": "existing_build_outputs", "skipped": true}'
        )
    if has_meson_build and abi == "pcc-native":
        return _native_host_eager_meson_build_json(name, source)
    if has_meson_build:
        return _native_ensure_meson_build_outputs_json(source, build_mode)
    if source_policy == "declarative_python_source":
        return (
            '{"actions": [], "build_backend": "hatchling.build", '
            '"build_mode_requested": ' + _json_str(build_mode)
            + ', "build_ownership": "not-required", '
            '"host_assisted": false, "host_free_build_claim": true, '
            '"host_python": null, "ok": true, '
            '"reason": "declarative_python_source", "skipped": true}'
        )
    c_files = _native_collect_suffix_files(source, [".c"], True)
    if len(c_files) == 0:
        return (
            '{"actions": [], "build_mode_requested": '
            + _json_str(build_mode)
            + ', "build_ownership": "not-required", '
            '"host_assisted": false, "host_free_build_claim": true, '
            '"host_python": null, "ok": true, '
            '"reason": "no_native_sources", "skipped": true}'
        )
    target = _native_single_package_extension_target(source)
    if target is None:
        return (
            '{"actions": [], "build_backend": "pcc-native", '
            '"build_mode_requested": '
            + _json_str(build_mode)
            + ', "build_ownership": "owned", "diagnostics": '
            '["PCC-PKG-EXTENSION-TARGET-AMBIGUOUS"], '
            '"host_assisted": false, "host_free_build_claim": false, '
            '"host_python": null, "ok": false, '
            '"reason": "extension_target_ambiguous", "skipped": false}'
        )
    return _native_build_exec_json(
        name,
        source,
        _native_path_search_dirs(),
        [],
        [],
        True,
        False,
        False,
        target[1],
        [],
        abi,
        False,
        False,
        False,
        False,
        build_mode,
    )


def _native_has_installable_meson_payload(name: str, source: str) -> bool:
    """Return whether a matching importable Meson payload already exists."""
    # A cached source tree is itself the already-built payload: it carries the
    # package __init__.py plus the built native artifacts, but no meson build
    # directory. Only looking under build/pcc-package/meson-build made a cache
    # hit re-enter the configure path on a tree that has no build system, so
    # the same spec installed twice reported success then failure.
    if (
        os.path.isfile(os.path.join(source, "__init__.py"))
        and len(_native_collect_artifacts(source)) > 0
    ):
        return True
    build_root = os.path.join(source, "build", "pcc-package", "meson-build")
    if not os.path.isdir(build_root):
        return False
    try:
        names = sorted(os.listdir(build_root))
    except Exception:
        names = []
    normalized_name = _native_normalized_package_name(name)
    i = 0
    while i < len(names):
        child_name = names[i]
        child = os.path.join(build_root, child_name)
        if (
            os.path.isdir(child)
            and _native_normalized_package_name(child_name) == normalized_name
            and (
                os.path.isfile(os.path.join(child, "__init__.py"))
                or _native_has_direct_importable_payload(child)
            )
        ):
            return True
        i += 1
    return False


def _native_host_eager_meson_build_json(name: str, source: str) -> str:
    """Delegate generic Meson graph replay while keeping pcc1 as install owner."""
    host_python = os.environ.get("PCC_HOST_PYTHON") or "python3"
    build_root = os.path.join(source, "build", "pcc-package")
    report_path = os.path.join(build_root, "eager-meson-build-report.json")
    jobs_text = os.environ.get("PCC_PACKAGE_BUILD_JOBS") or "2"
    timeout_text = os.environ.get("PCC_PACKAGE_BUILD_TIMEOUT") or "180"
    command = [
        host_python,
        "-m",
        "pcc.package.build_exec",
        name,
        "--path",
        source,
        "--execute",
        "--eager-meson-extensions",
        "--jobs",
        jobs_text,
        "--timeout",
        timeout_text,
        "--report",
        report_path,
        "--json",
    ]

    def read_labeled_report():
        try:
            with open(report_path, "r", encoding="utf-8") as fh:
                report = fh.read()
        except Exception:
            return None
        if not _native_json_field_is(report, "build_ownership", '"host"'):
            return None
        if not _native_json_field_is(report, "host_assisted", "true"):
            return None
        if _native_json_field_is(report, "host_python", "null"):
            return None
        if _native_find_from(report, '"host_python"', 0) < 0:
            return None
        return report
    shell_command = ""
    i = 0
    while i < len(command):
        if shell_command != "":
            shell_command += " "
        shell_command += _native_shell_quote(command[i])
        i += 1
    redirected = _native_redirected_shell_command(
        shell_command, "pcc_native_meson_build"
    )
    try:
        _bootstrap_subprocess_run(["mkdir", "-p", build_root], check=True)
        _bootstrap_subprocess_run(["/bin/sh", "-c", redirected[0]], check=True)
    except Exception:
        labeled = read_labeled_report()
        if labeled is not None:
            return labeled
        return (
            '{"actions": [], "build_backend": "host", '
            '"build_mode_requested": "host", "build_ownership": "host", '
            '"diagnostics": [{"code": '
            '"PCC-PKG-HOST-BUILD-BACKEND-FAILED", "message": '
            + _json_str(_native_build_log_tail(redirected[1]))
            + '}], "host_assisted": true, "host_free_build_claim": false, '
            '"host_python": '
            + _json_str(host_python)
            + ', "ok": false, '
            '"reason": "host_build_backend_failed", "skipped": false}'
        )
    finally:
        try:
            _bootstrap_subprocess_run(["rm", "-f", redirected[1]], check=True)
        except Exception:
            pass
    labeled = read_labeled_report()
    if labeled is not None:
        return labeled
    return (
        '{"actions": [], "build_backend": "host", '
        '"build_mode_requested": "host", "build_ownership": "host", '
        '"diagnostics": [{"code": '
        '"PCC-PKG-HOST-BUILD-PROVENANCE-MISSING", "message": '
        '"host build backend did not write complete ownership provenance"}], '
        '"host_assisted": true, "host_free_build_claim": false, '
        '"host_python": '
        + _json_str(host_python)
        + ', "ok": false, '
        '"reason": "host_build_provenance_missing", "skipped": false}'
    )


def _native_install_manifest_json(
    spec: str,
    target_dir,
    cache_dir,
    find_links,
    abi: str,
    index_urls=None,
    resolved_from_override=None,
    build_mode: str = "owned",
    force: bool = False,
) -> str:
    if index_urls is None:
        index_urls = []
    resolved = _native_resolve_install_source_result(
        spec, cache_dir, find_links, abi, index_urls
    )
    source = resolved[0]
    resolved_from = resolved[1]
    if resolved_from_override is not None:
        resolved_from = resolved_from_override
    if source is None and not os.path.exists(spec):
        # Nothing resolved locally (not a path, not in find-links/cache).
        # Mirror the host installer's not-found failure instead of
        # fabricating an empty phantom install with ok:true — creating a
        # bare site directory for an unresolved name was a fake success.
        # Acquisition is an explicit outer phase. Reaching this local
        # installer unresolved must stay a real failure, never a phantom site.
        return (
            '{"error": '
            + _json_str("package artifact not found locally or in pcc cache")
            + ', "ok": false, "spec": '
            + _json_str(spec)
            + "}"
        )
    if os.path.exists(spec):
        name = _native_package_basename(source if source is not None else spec)
    else:
        name = _native_normalized_package_name(spec)
    target = target_dir or _pcc_default_package_site()
    cache_root = cache_dir or _pcc_default_package_cache()
    cache_record = os.path.abspath(os.path.join(cache_root, name))
    artifact_sha256 = ""
    if source is not None and os.path.isfile(source):
        artifact_sha256 = _native_owned_file_sha256(source)
    if not force:
        satisfied = _native_already_satisfied_manifest_json(
            os.path.abspath(target), name, artifact_sha256, abi
        )
        if satisfied is not None:
            return satisfied
    prepared_source = [source, None]
    try:
        prepared_source = _native_prepare_install_source_tree(source, cache_root, name)
        install_source = prepared_source[0]
        build_report = _native_build_install_source_json(
            name, install_source, abi, build_mode
        )
        build_ok = _native_find_from(build_report, '"ok": false', 0) < 0
        if not build_ok:
            # Do not publish an unbuilt source tree after the owned build-tool
            # boundary rejects it (or after an explicit host build fails).
            # Returning the labeled build report is enough for both CLI
            # surfaces to fail; site/cache must remain untouched.
            _native_cleanup_prepared_source(prepared_source[1])
            out = "{"
            out += '"abi_mode": ' + _json_str(abi)
            out += ', "build_mode_requested": ' + _json_str(build_mode)
            out += ', "build_report": ' + build_report
            out += ', "diagnostics": []'
            out += ', "import_attempted": false'
            out += ', "import_success": null'
            out += ', "install_native_package_claim": false'
            out += ', "install_success": false'
            out += ', "linkage_native_package_claim": false'
            out += ', "name": ' + _json_str(name)
            out += ', "native_package_claim": false'
            out += ', "ok": false'
            out += ', "resolved_from": ' + _json_str_or_null(resolved_from)
            out += ', "source_path": ' + _json_str_or_null(source)
            out += ', "spec": ' + _json_str(spec)
            out += "}"
            return out
        install_root = _native_install_importable_payload(
            install_source, os.path.abspath(target), name
        )
        link_edges = _native_linkage_edges_for_root(install_root)
        links_libpython = len(link_edges) > 0
        cpython_abi_paths = _native_cpython_extension_abi_paths_for_root(install_root)
        uses_cpython_abi = len(cpython_abi_paths) > 0
        # Package repositories contain archives; installed package roots contain
        # native libraries.  Use the linkage artifact scanner here so a built
        # ``.so``/``.dylib``/``.dll`` can earn the linkage-only native claim.
        native_artifact_count = len(_native_collect_artifacts(install_root))
        linkage_native_package_claim = (
            abi == "pcc-native"
            and native_artifact_count > 0
            and not links_libpython
            and not uses_cpython_abi
        )
        install_ok = (
            ((not links_libpython) or abi == "libpython")
            and (
                (not uses_cpython_abi) or abi == "libpython" or abi == "cpython-compat"
            )
            and build_ok
        )
        if install_ok:
            # Re-record the payload into the cache only when the resolved
            # source lives OUTSIDE the cache root. A cache-resolved source
            # (spec name install, e.g. ``install demo_pkg`` after a direct
            # install cached ``cache_root/demo_pkg``) must not be re-copied:
            # ``copy_payload`` computes its destination from the payload
            # BASENAME while ``cache_record`` uses the PEP-503 normalized
            # name (``demo_pkg`` -> ``demo-pkg``), so the old
            # ``source != cache_record`` guard let the copy run with
            # dest == source and the ``rm -rf dest`` step deleted the
            # cache entry before ``cp`` could read it.
            source_in_cache_root = False
            if install_source is not None:
                source_parent = os.path.dirname(os.path.abspath(install_source))
                if source_parent == os.path.abspath(cache_root):
                    source_in_cache_root = True
            if (
                install_source is not None
                and not source_in_cache_root
                and os.path.abspath(install_source) != cache_record
            ):
                _native_install_importable_payload(
                    install_source, os.path.abspath(cache_root), name
                )
            _bootstrap_subprocess_run(["mkdir", "-p", cache_record], check=True)
        manifest_path = os.path.join(install_root, "pcc-package.json")
        manifest = "{"
        manifest += '"abi_mode": ' + _json_str(abi)
        manifest += ', "artifact_sha256": ' + _json_str_or_null(
            None if artifact_sha256 == "" else artifact_sha256
        )
        manifest += ', "build_mode_requested": ' + _json_str(build_mode)
        manifest += ', "capability_profile": ' + _native_capability_profile_json(
            abi, native_artifact_count > 0, links_libpython, uses_cpython_abi
        )
        manifest += ', "build_report": ' + build_report
        manifest += ', "cache_record": ' + _json_str(cache_record)
        manifest += ', "cpython_extension_abi_paths": ' + _json_str_list(
            cpython_abi_paths
        )
        manifest += ', "diagnostics": ' + _native_linkage_diagnostics_json(
            link_edges, cpython_abi_paths
        )
        manifest += ', "import_attempted": false'
        manifest += ', "import_success": null'
        manifest += ', "install_native_package_claim": false'
        manifest += ', "install_success": ' + ("true" if install_ok else "false")
        manifest += ', "install_action": "installed"'
        manifest += ', "installed_path": ' + _json_str(install_root)
        manifest += ', "installed_payload": ' + _json_str(install_root)
        manifest += ', "installed_payloads": [' + _json_str(install_root) + "]"
        manifest += ', "index_urls": ' + _json_str_list(index_urls)
        manifest += ', "link_libpython_edges": ' + _json_str_list(link_edges)
        manifest += ', "linkage_native_package_claim": ' + (
            "true" if linkage_native_package_claim else "false"
        )
        manifest += ', "links_libpython": ' + ("true" if links_libpython else "false")
        manifest += ', "metadata": ' + _native_package_metadata_json(name, source)
        manifest += ', "name": ' + _json_str(name)
        manifest += ', "native_package_claim": false'
        manifest += ', "no_libpython_runtime": ' + (
            "true"
            if (abi == "pcc-native" and not links_libpython and not uses_cpython_abi)
            else "false"
        )
        manifest += ', "ok": ' + ("true" if install_ok else "false")
        manifest += ', "pcc_native_extension_suffix": ' + _json_str(
            _native_pcc_extension_suffix()
        )
        manifest += ', "pcc_native_wheel_tag": ' + _json_str(_native_pcc_wheel_tag())
        manifest += ', "manifest_schema": ' + _json_str(PACKAGE_MANIFEST_SCHEMA)
        manifest += ', "schema_version": ' + str(PACKAGE_MANIFEST_SCHEMA_VERSION)
        manifest += ', "source_path": ' + _json_str_or_null(source)
        manifest += ', "resolved_from": ' + _json_str_or_null(resolved_from)
        manifest += ', "spec": ' + _json_str(spec)
        manifest += ', "uses_cpython_extension_abi": ' + (
            "true" if uses_cpython_abi else "false"
        )
        manifest += ', "wheel_tags": ' + _native_wheel_tags_json(
            source if source is not None else spec
        )
        manifest += "}"
        with open(manifest_path, "w", encoding="utf-8") as fh:
            fh.write(manifest)
        if install_ok:
            with open(
                os.path.join(cache_record, "pcc-package.json"), "w", encoding="utf-8"
            ) as fh:
                fh.write(manifest)
        _native_cleanup_prepared_source(prepared_source[1])
    except Exception:
        _native_cleanup_prepared_source(prepared_source[1])
        return (
            '{"error": "pcc1 package install failed"'
            + ', "import_attempted": false'
            + ', "import_success": null'
            + ', "install_native_package_claim": false'
            + ', "install_success": false'
            + ', "linkage_native_package_claim": false'
            + ', "name": '
            + _json_str(name)
            + ', "native_package_claim": false'
            + ', "ok": false'
            + ', "spec": '
            + _json_str(spec)
            + ', "wheel_tags": '
            + _native_wheel_tags_json(source if source is not None else spec)
            + "}"
        )
    out = "{"
    out += '"abi_mode": ' + _json_str(abi)
    out += ', "artifact_sha256": ' + _json_str_or_null(
        None if artifact_sha256 == "" else artifact_sha256
    )
    out += ', "build_mode_requested": ' + _json_str(build_mode)
    out += ', "capability_profile": ' + _native_capability_profile_json(
        abi, native_artifact_count > 0, links_libpython, uses_cpython_abi
    )
    out += ', "build_report": ' + build_report
    out += ', "cache_record": ' + _json_str(cache_record)
    cpython_abi_paths = _native_cpython_extension_abi_paths_for_root(install_root)
    uses_cpython_abi = len(cpython_abi_paths) > 0
    out += ', "cpython_extension_abi_paths": ' + _json_str_list(cpython_abi_paths)
    out += ', "diagnostics": ' + _native_linkage_diagnostics_json(
        link_edges, cpython_abi_paths
    )
    out += ', "import_attempted": false'
    out += ', "import_success": null'
    out += ', "install_native_package_claim": false'
    out += ', "install_success": ' + ("true" if install_ok else "false")
    out += ', "install_action": "installed"'
    out += ', "installed_path": ' + _json_str(install_root)
    out += ', "installed_payload": ' + _json_str(install_root)
    out += ', "installed_payloads": [' + _json_str(install_root) + "]"
    out += ', "index_urls": ' + _json_str_list(index_urls)
    out += ', "link_libpython_edges": ' + _json_str_list(link_edges)
    out += ', "linkage_native_package_claim": ' + (
        "true" if linkage_native_package_claim else "false"
    )
    out += ', "links_libpython": ' + ("true" if links_libpython else "false")
    out += ', "manifest_path": ' + _json_str(
        os.path.join(install_root, "pcc-package.json")
    )
    out += ', "name": ' + _json_str(name)
    out += ', "native_package_claim": false'
    out += ', "no_libpython_runtime": ' + (
        "true"
        if (abi == "pcc-native" and not links_libpython and not uses_cpython_abi)
        else "false"
    )
    out += ', "ok": ' + ("true" if install_ok else "false")
    out += ', "manifest_schema": ' + _json_str(PACKAGE_MANIFEST_SCHEMA)
    out += ', "schema_version": ' + str(PACKAGE_MANIFEST_SCHEMA_VERSION)
    out += ', "pcc_native_extension_suffix": ' + _json_str(
        _native_pcc_extension_suffix()
    )
    out += ', "resolved_from": ' + _json_str_or_null(resolved_from)
    out += ', "source_path": ' + _json_str_or_null(source)
    out += ', "spec": ' + _json_str(spec)
    out += ', "uses_cpython_extension_abi": ' + (
        "true" if uses_cpython_abi else "false"
    )
    out += ', "wheel_tags": ' + _native_wheel_tags_json(
        source if source is not None else spec
    )
    out += "}"
    return out


def _run_native_package_install_from_pcc1(module_args) -> int:
    spec = ""
    target_dir = ""
    cache_dir = ""
    find_links = []
    index_urls = []
    no_index = False
    abi = "pcc-native"
    build_mode = "owned"
    force = False
    i = 0
    while i < len(module_args):
        arg = module_args[i]
        if arg == "--target" or arg == "--target-dir":
            if i + 1 >= len(module_args):
                _write_text("Error: --target requires a value", err=True)
                return 2
            target_dir = module_args[i + 1]
            i += 1
        elif arg == "--cache-dir":
            if i + 1 >= len(module_args):
                _write_text("Error: --cache-dir requires a value", err=True)
                return 2
            cache_dir = module_args[i + 1]
            i += 1
        elif arg == "--find-links" or arg == "-f":
            if i + 1 >= len(module_args):
                _write_text("Error: --find-links requires a value", err=True)
                return 2
            find_links.append(module_args[i + 1])
            i += 1
        elif arg.startswith("--find-links="):
            find_links.append(arg.split("=", 1)[1])
        elif arg == "--no-index":
            no_index = True
        elif arg == "--index-url" or arg == "-i":
            if i + 1 >= len(module_args):
                _write_text("Error: --index-url requires a value", err=True)
                return 2
            index_urls.append(module_args[i + 1])
            i += 1
        elif arg.startswith("--index-url="):
            index_urls.append(arg.split("=", 1)[1])
        elif arg == "--extra-index-url":
            if i + 1 >= len(module_args):
                _write_text("Error: --extra-index-url requires a value", err=True)
                return 2
            index_urls.append(module_args[i + 1])
            i += 1
        elif arg.startswith("--extra-index-url="):
            index_urls.append(arg.split("=", 1)[1])
        elif arg == "--abi":
            if i + 1 >= len(module_args):
                _write_text("Error: --abi requires a value", err=True)
                return 2
            abi = module_args[i + 1]
            i += 1
        elif arg.startswith("--abi="):
            abi = arg.split("=", 1)[1]
        elif arg == "--build":
            if i + 1 >= len(module_args):
                _write_text("Error: --build requires a value", err=True)
                return 2
            build_mode = module_args[i + 1]
            i += 1
        elif arg.startswith("--build="):
            build_mode = arg.split("=", 1)[1]
        elif arg == "--force":
            force = True
        elif arg != "--json" and not arg.startswith("-"):
            spec = arg
        i += 1
    if spec == "":
        _write_text('{"error": "missing package spec", "ok": false}')
        return 2
    if not (build_mode == "owned" or build_mode == "host"):
        _write_text(
            '{"diagnostic": "PCC-PKG-BUILD-MODE-INVALID", '
            '"error": "PCC-PKG-BUILD-MODE-INVALID", "ok": false}'
        )
        return 2
    if no_index:
        index_urls = []
    result = _native_install_manifest_json(
        spec,
        target_dir,
        cache_dir,
        find_links,
        abi,
        index_urls,
        None,
        build_mode,
        force,
    )
    _write_text(result)
    return 2 if _native_find_from(result, '"ok": false', 0) >= 0 else 0


def _split_module_name(module_name: str):
    parts = []
    current = ""
    i = 0
    while i < len(module_name):
        ch = module_name[i]
        if ch == ".":
            if current == "":
                return []
            parts.append(current)
            current = ""
        else:
            current += ch
        i += 1
    if current == "":
        return []
    parts.append(current)
    return parts


def _module_search_roots():
    roots = []
    cwd = os.getcwd()
    roots.append(cwd)
    env_path = os.environ.get("PYTHONPATH") or ""
    item = ""
    i = 0
    while i <= len(env_path):
        ch = env_path[i] if i < len(env_path) else os.pathsep
        if ch == os.pathsep:
            if item == "":
                item = cwd
            roots.append(item)
            item = ""
        else:
            item += ch
        i += 1
    i = 0
    while i < len(sys.path):
        root = sys.path[i]
        if root is None or root == "":
            root = cwd
        roots.append(str(root))
        i += 1
    return roots


def _join_module_parts(root, parts):
    path = root
    i = 0
    while i < len(parts):
        path = os.path.join(path, parts[i])
        i += 1
    return path


def _find_module_entry_source(module_name: str):
    parts = _split_module_name(module_name)
    if len(parts) == 0:
        return (None, "invalid module name: " + module_name)
    roots = _module_search_roots()
    saw_package = False
    i = 0
    while i < len(roots):
        root = roots[i]
        base = _join_module_parts(root, parts)
        package_main = os.path.join(base, "__main__.py")
        if os.path.isfile(package_main):
            return (package_main, None)
        package_init = os.path.join(base, "__init__.py")
        if os.path.isfile(package_init):
            saw_package = True
        module_file = base + ".py"
        if os.path.isfile(module_file):
            return (module_file, None)
        i += 1
    if saw_package:
        return (None, "package has no __main__.py: " + module_name)
    return (None, "module not found: " + module_name)


def _run_compiled_python_module_from_pcc1(module_name: str, module_args) -> int:
    src, err = _find_module_entry_source(module_name)
    if src is None:
        _write_text("Error: " + str(err), err=True)
        return 1
    forwarded = [src]
    i = 0
    while i < len(module_args):
        forwarded.append(module_args[i])
        i += 1
    # Reuse the script compiler/runner so module execution gets the same
    # content-addressed run cache, exact exit-status handling, and artifact
    # publication contract.  The validated startup envelope preserves the
    # module-visible argv[0] while the physical cache path remains executable.
    return _bootstrap_cli_main_impl(
        forwarded,
        _logical_argv0=src,
        _execution_mode="module",
    )


def _run_python_module_from_pcc1(argv) -> int:
    if len(argv) < 2:
        _write_text("Error: -m requires a module name", err=True)
        return 2
    module_name = argv[1]
    module_args = _copy_seq(argv[2:])
    unsupported = _unsupported_tooling_module(module_name)
    if unsupported is not None:
        _write_text(
            "Error: " + unsupported[0] + ": " + unsupported[1]
            + "; pcc1 did not invoke CPython",
            err=True,
        )
        return 2
    if module_name in ("pip", "pip3"):
        return _run_native_pip_shim_from_pcc1(module_args)
    if module_name == "pcc.package.pip_shim":
        return _run_native_pip_shim_from_pcc1(module_args)
    if module_name == "pcc.package.inspect":
        return _run_native_package_inspect_from_pcc1(module_args)
    if module_name == "pcc.package.campaign":
        return _run_native_package_campaign_from_pcc1(module_args)
    if module_name == "pcc.package.array_core":
        return _run_native_package_array_core_from_pcc1(module_args)
    if module_name == "pcc.package.toolchain":
        return _run_native_package_toolchain_from_pcc1(module_args)
    if module_name == "pcc.package.wheel_repo":
        return _run_native_package_wheel_repo_from_pcc1(module_args)
    if module_name == "pcc.package.linkage":
        return _run_native_package_linkage_from_pcc1(module_args)
    if module_name == "pcc.package.extension_abi":
        return _run_native_package_ext_abi_from_pcc1(module_args)
    if module_name == "pcc.package.build_plan":
        return _run_native_package_build_plan_from_pcc1(module_args)
    if module_name == "pcc.package.build_exec":
        return _run_native_package_build_exec_from_pcc1(module_args)
    if module_name == "pcc.package.install":
        return _run_native_package_install_from_pcc1(module_args)
    if module_name == "pcc.package":
        if len(module_args) > 0 and module_args[0] == "campaign":
            return _run_native_package_campaign_from_pcc1(module_args[1:])
        if len(module_args) > 0 and module_args[0] == "array-core":
            return _run_native_package_array_core_from_pcc1(module_args[1:])
        if len(module_args) > 0 and module_args[0] == "toolchain":
            return _run_native_package_toolchain_from_pcc1(module_args[1:])
        if len(module_args) > 0 and module_args[0] == "wheel-repo":
            return _run_native_package_wheel_repo_from_pcc1(module_args[1:])
        if len(module_args) > 0 and module_args[0] == "linkage":
            return _run_native_package_linkage_from_pcc1(module_args[1:])
        if len(module_args) > 0 and module_args[0] == "ext-abi":
            return _run_native_package_ext_abi_from_pcc1(module_args[1:])
        if len(module_args) > 0 and module_args[0] == "build-plan":
            return _run_native_package_build_plan_from_pcc1(module_args[1:])
        if len(module_args) > 0 and module_args[0] == "build-exec":
            return _run_native_package_build_exec_from_pcc1(module_args[1:])
        if len(module_args) > 0 and module_args[0] == "install":
            return _run_native_package_install_from_pcc1(module_args[1:])
        if len(module_args) > 0 and module_args[0] == "inspect":
            return _run_native_package_inspect_from_pcc1(module_args[1:])
        return _run_native_package_inspect_from_pcc1(module_args)
    return _run_compiled_python_module_from_pcc1(module_name, module_args)


def _run_python_module_from_pcc1_with_mode(argv, mode) -> int:
    """Run a module request through its explicitly selected execution owner.

    `auto`/`on` delegate to a generic CPython module subprocess. The pcc1
    process remains no-libpython and the manifest does not claim otherwise.
    `off` (including plain `-m`) keeps the native module runner unchanged.
    """
    if mode == "auto" or mode == "on":
        _write_text(
            "PCC1_COMPAT_RUNNER_MANIFEST: " + compat_runner_manifest_json(mode),
            err=True,
        )
        compat_python = os.environ.get("PCC_COMPAT_PYTHON")
        if not compat_python:
            compat_python = os.environ.get("PCC_HOST_PYTHON") or "python3"
        if compat_python == sys.executable:
            _write_text(
                "Error: compatibility Python points at this pcc1 binary; "
                "refusing recursive module delegation",
                err=True,
            )
            return 2
        command = [compat_python]
        i = 0
        while i < len(argv):
            command.append(argv[i])
            i += 1
        try:
            _bootstrap_subprocess_run(command, check=True)
        except Exception:
            _write_text("Error: pcc1 CPython compatibility runner failed", err=True)
            return 1
        return 0
    return _run_python_module_from_pcc1(argv)


def _is_host_cli_c_indicator(arg) -> bool:
    if arg is None:
        return False
    if arg.endswith(".c"):
        return True
    if arg in (
        "--sources-from-make",
        "--separate-tus",
        "--system-link",
        "--emit-obj",
        "--emit-asm",
        "--link-arg",
        "--cpp-arg",
        "--include-dir",
        "--jobs",
    ):
        return True
    return (
        arg.startswith("--sources-from-make=")
        or arg.startswith("--link-arg=")
        or arg.startswith("--cpp-arg=")
        or arg.startswith("--include-dir=")
        or arg.startswith("-I")
        or arg.startswith("-D")
        or arg.startswith("-U")
    )


def _should_delegate_to_host_cli(argv) -> bool:
    # Historical name retained for internal callers; dispatch is in-process.
    path = cli_input_path(argv)
    return bool(path) and not path.endswith(".py")


def _run_c_cli(argv) -> int:
    """Hand a C/project input, or a Python input needing the full option set,
    to the complete pcc CLI.

    This runs the host driver in a child process rather than importing
    ``pcc.cli_core`` here, and that is not a stylistic choice.  All three
    entrypoints -- the ``pcc`` console script, ``python -m pcc`` and ``pcc1``
    -- reach this module, and ``pcc/__main__.py`` is also the module the
    bootstrap compiles into pcc1.  An import of ``pcc.cli_core`` therefore
    lands in the pcc1 source closure two different ways:

    * written statically, it drags the C frontend, packaging and llvm_capi in,
      and eight of those modules do not compile under the self backend today,
      so stage1 fails outright;
    * resolved through ``importlib`` to hide it from the closure walker, it
      costs 84 CPython fallback calls in this module, measured by
      ``test_cli_bootstrap_package_schema_static_imports_stay_native``, and
      this module's contract is zero.

    Both were tried in this order.  The child process keeps the closure clean
    and this module at zero fallbacks; the delegated process runs the same
    ``cli_core`` in-process, so the user-visible semantics are the full CLI's
    either way.  In-process dispatch from the compiled stage stays the target
    and needs the C frontend closure to compile first.
    """
    host = os.environ.get("PCC_HOST_PCC")
    if host:
        if host == sys.executable:
            _write_text(
                "Error: PCC_HOST_PCC points at this bootstrap binary; "
                "refusing recursive C delegation",
                err=True,
            )
            return 2
        cmd = [host]
    else:
        host_python = os.environ.get("PCC_HOST_PYTHON") or "python3"
        cmd = [host_python, "-m", "pcc.pcc"]

    backend = os.environ.get("PCC_BACKEND", "") or DEFAULT_PUBLIC_BACKEND
    cmd.append("--backend")
    cmd.append(backend)
    i = 0
    while i < len(argv):
        cmd.append(argv[i])
        i += 1

    try:
        _bootstrap_subprocess_run(cmd, check=True)
    except Exception:
        _write_text(
            "Error: PCC-CPY-UNSUPPORTED-L3-TOOLING-C-DRIVER: the C compilation "
            "driver is not owned by this stage and delegating to the host pcc "
            "failed; set PCC_HOST_PCC to a host pcc entrypoint",
            err=True,
        )
        return 1
    return 0


def _requires_full_compile_cli(argv) -> bool:
    """Keep existing full-CLI compiler controls effective for Python too."""
    takes_value = (
        "--target", "--gpu-backend", "--link-arg", "--pass", "--disable-pass",
        "--jobs", "--cache-dir", "--emit-obj", "--emit-asm",
    )
    for arg in argv:
        if arg == "--":
            break
        if arg in takes_value or arg in ("--no-cache", "--llvmdump", "-O0", "-O1", "-O2", "-O3"):
            return True
        for option in takes_value:
            if arg.startswith(option + "="):
                return True
    return False


def _run_host_pcc_from_pcc1(argv) -> int:
    """Compatibility name; no host process or external compiler is launched here."""
    return _run_c_cli(argv)


def _option_value(arg):
    idx = arg.find("=")
    if idx >= 0:
        return arg[idx + 1 :]
    return ""


class ObservabilityOptions:
    def __init__(
        self,
        diagnostic_format="text",
        profile_json=None,
        explain_fallback=False,
        phase="compile",
        entry="pcc",
    ) -> None:
        fmt = (diagnostic_format or "text").strip().lower()
        if fmt not in _VALID_DIAGNOSTIC_FORMATS:
            raise ValueError(
                "invalid diagnostic format "
                f"{diagnostic_format!r}; expected text, json, or sarif"
            )
        self.diagnostic_format = fmt
        self.profile_json = profile_json
        self.explain_fallback = bool(explain_fallback)
        self.phase = phase
        self.entry = entry


class ObservedCompileError(RuntimeError):
    def __init__(self, formatted: str) -> None:
        super().__init__(formatted)
        self.formatted = formatted


def parse_observability_cli_option(arg: str, argv: list[str], index: int):
    if arg.startswith("--diagnostic-format="):
        return ("diagnostic_format", arg.split("=", 1)[1], index + 1)
    if arg == "--diagnostic-format":
        if index + 1 >= len(argv):
            raise ValueError("--diagnostic-format requires a value")
        return ("diagnostic_format", argv[index + 1], index + 2)
    if arg.startswith("--profile-json="):
        return ("profile_json", arg.split("=", 1)[1], index + 1)
    if arg == "--profile-json":
        if index + 1 >= len(argv):
            raise ValueError("--profile-json requires a value")
        return ("profile_json", argv[index + 1], index + 2)
    if arg == "--explain-fallback":
        return ("explain_fallback", "1", index + 1)
    return None


def _observability_json_str(value) -> str:
    text = str(value)
    out = '"'
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            out += "\\\\"
        elif ch == '"':
            out += '\\"'
        elif ch == "\n":
            out += "\\n"
        elif ch == "\r":
            out += "\\r"
        elif ch == "\t":
            out += "\\t"
        else:
            out += ch
        i += 1
    out += '"'
    return out


def _json_float(value) -> str:
    try:
        return str(value)
    except Exception:
        return "0"


def _json_ns_from_ms(value) -> str:
    try:
        ms = int(value)
    except Exception:
        ms = 0
    return str(ms * 1000000)


def _json_int(value) -> str:
    try:
        return str(int(value))
    except Exception:
        return "0"


def _json_seconds_from_ms(value) -> str:
    try:
        ms = int(value)
    except Exception:
        ms = 0
    sign = ""
    if ms < 0:
        sign = "-"
        ms = -ms
    whole = ms // 1000
    frac = ms % 1000
    frac_s = str(frac)
    while len(frac_s) < 3:
        frac_s = "0" + frac_s
    return sign + str(whole) + "." + frac_s


def _format_compile_error(exc, *, options, metadata):
    diagnostic_span = None
    original_exception_type = ""
    if os.environ.get("PCC_DEBUG_CODEGEN_PHASES"):
        try:
            diagnostic_span = exc.diagnostic_span
            original_exception_type = exc.original_exception_type
        except Exception:
            diagnostic_span = None
            original_exception_type = ""
    if str(os.environ.get("PCC_DEBUG_BOOTSTRAP_TRACE", "") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        tb_lines = []
        tb = getattr(exc, "__traceback__", None)
        i = 0
        while tb is not None and i < 12:
            frame = getattr(tb, "tb_frame", None)
            if frame is not None:
                code = getattr(frame, "f_code", None)
                if code is not None:
                    tb_lines.append(
                        f"tb[{i}]: "
                        + str(code.co_filename)
                        + ":"
                        + str(tb.tb_lineno)
                        + " "
                        + str(code.co_name)
                    )
                else:
                    tb_lines.append("tb[" + str(i) + "]: <frame-missing>")
            tb = getattr(tb, "tb_next", None)
            i += 1
        _write_text("debug: compile exception type=" + str(type(exc).__name__))
        _write_text("debug: compile exception args=" + repr(getattr(exc, "args", None)))
        _write_text("debug: compile exception repr=" + repr(exc))
        _write_text(
            "debug: compile traceback="
            + ("; ".join(tb_lines) if tb_lines else "<none>")
        )
        cause = getattr(exc, "__cause__", None)
        if cause is not None:
            _write_text("debug: cause=" + repr(cause))
        cause = getattr(exc, "__context__", None)
        if cause is not None:
            _write_text("debug: context=" + repr(cause))
        _write_text(
            "debug: metadata_keys="
            + (
                ",".join(sorted(metadata.keys()))
                if isinstance(metadata, dict)
                else "<non-dict>"
            )
        )
        try:
            _write_text("debug: compile exception message=" + str(exc))
        except Exception:
            _write_text("debug: compile exception message unavailable")
    message = str(exc)
    if not message:
        # Under pcc1 a PyPipelineError has been observed with str()=""
        # AND args=None AND no __cause__ chain (2026-08-27, three stage2
        # failures) — the args machinery and the from-chain both lost the
        # text.  PyPipelineError therefore also stores its message in a
        # plain instance attribute at construction; that storage path is
        # independent of the args tuple and survives when args does not.
        fallback_text = ""
        try:
            fallback_text = str(getattr(exc, "pcc_message", "") or "")
        except Exception:
            fallback_text = ""
        if fallback_text:
            message = fallback_text
    if not message:
        # Every re-raise in the pipeline uses `raise PyPipelineError(...) from
        # exc`, so the original failure is on __cause__.  Reporting only the
        # outer wrapper printed "compile failed" with no text at all, and the
        # real error then surfaced much later as an unrelated linker complaint.
        cause = getattr(exc, "__cause__", None)
        if cause is None:
            cause = getattr(exc, "__context__", None)
        depth = 0
        while cause is not None and depth < 6:
            cause_text = ""
            try:
                cause_text = str(cause)
            except Exception:
                cause_text = ""
            cause_name = ""
            try:
                cause_name = str(type(cause).__name__)
            except Exception:
                cause_name = "Exception"
            if cause_text:
                message = cause_name + ": " + cause_text
                break
            if not message:
                message = cause_name
            nxt = getattr(cause, "__cause__", None)
            if nxt is None:
                nxt = getattr(cause, "__context__", None)
            cause = nxt
            depth = depth + 1
    if not message:
        message = "compile failed"
    # Keep this bootstrap formatter deliberately static. The stage1 binary
    # runs this path inside the pcc-py runtime after arbitrary compiler
    # exceptions; probing type(exc).__name__ or repr(metadata) can itself
    # require dynamic attribute/dict formatting and mask the original error.
    note = "exception_type=" + (
        str(original_exception_type) if original_exception_type else "Exception"
    )
    mode_backend = metadata.get("backend", "") if metadata else ""
    mode_libpython = metadata.get("python_libpython", "") if metadata else ""
    mode_scaffold = metadata.get("ir_scaffold", "") if metadata else ""
    if mode_backend or mode_libpython or mode_scaffold:
        note += (
            "; backend="
            + str(mode_backend)
            + "; python_libpython="
            + str(mode_libpython)
            + "; ir_scaffold="
            + str(mode_scaffold)
        )
    if options.explain_fallback:
        note += (
            "; fallback_explain=libpython fallback is controlled by "
            "--python-libpython/PCC_PYTHON_LIBPYTHON"
        )
    span_json = ""
    location = ""
    if diagnostic_span is not None:
        span_file = str(diagnostic_span.file)
        span_line = int(diagnostic_span.line)
        span_col = int(diagnostic_span.col)
        span_end_line = int(diagnostic_span.end_line)
        span_end_col = int(diagnostic_span.end_col)
        location = span_file + ":" + str(span_line) + ":" + str(span_col) + ": "
        span_json = (
            ",\n"
            '      "span": {\n'
            '        "file": ' + _observability_json_str(span_file) + ",\n"
            '        "line": ' + str(span_line) + ",\n"
            '        "col": ' + str(span_col) + ",\n"
            '        "end_line": ' + str(span_end_line) + ",\n"
            '        "end_col": ' + str(span_end_col) + "\n"
            "      }"
        )
    if options.diagnostic_format == "json" or options.diagnostic_format == "sarif":
        metadata_json = "{}"
        if mode_backend or mode_libpython or mode_scaffold:
            metadata_json = (
                '{"backend": '
                + _observability_json_str(mode_backend)
                + ', "python_libpython": '
                + _observability_json_str(mode_libpython)
                + ', "ir_scaffold": '
                + _observability_json_str(mode_scaffold)
                + "}"
            )
        return (
            "{\n"
            '  "schema": "pcc.diagnostics.v1",\n'
            '  "diagnostics": [\n'
            "    {\n"
            '      "code": "PCC-PY-COMPILE-001",\n'
            '      "severity": "error",\n'
            '      "phase": ' + _observability_json_str(options.phase) + ",\n"
            '      "message": ' + _observability_json_str(message) + ",\n"
            '      "notes": [' + _observability_json_str(note) + "],\n"
            '      "metadata": ' + metadata_json + span_json + "\n"
            "    }\n"
            "  ],\n"
            '  "has_errors": true\n'
            "}"
        )
    return (
        location
        + "error: PCC-PY-COMPILE-001: ["
        + options.phase
        + "] "
        + message
        + "\n  note: "
        + note
    )


def _write_profile_json(path: str, options, metadata) -> None:
    entry = options.entry
    phase = options.phase
    emit_llvm = metadata.get("emit_llvm", False) if metadata else False
    output_path = metadata.get("output_path", None) if metadata else None
    backend = metadata.get("backend", None) if metadata else None
    python_libpython = metadata.get("python_libpython", None) if metadata else None
    ir_scaffold = metadata.get("ir_scaffold", None) if metadata else None
    counters = metadata.get("counters", {}) if metadata else {}
    phase_totals = metadata.get("phase_totals_ms", {}) if metadata else {}
    events = metadata.get("events", []) if metadata else []
    total_ms = 0
    if phase_totals:
        total_ms = phase_totals.get(
            "compile_python_total",
            phase_totals.get("compile_python_multi_total", 0),
        )
    with open(path, "w", encoding="utf-8") as f:
        f.write("{\n")
        f.write('  "schema": "pcc.profile.v1",\n')
        f.write('  "total_ns": ' + _json_ns_from_ms(total_ms) + ",\n")
        f.write('  "total_ms": ' + _json_int(total_ms) + ",\n")
        f.write('  "total_s": ' + _json_seconds_from_ms(total_ms) + ",\n")
        f.write('  "metadata": {\n')
        f.write('    "entry": ' + _observability_json_str(entry) + ",\n")
        f.write('    "phase": ' + _observability_json_str(phase) + ",\n")
        f.write('    "time_unit": "seconds",\n')
        f.write('    "emit_llvm": ' + ("true" if emit_llvm else "false"))
        if backend is not None:
            f.write(",\n")
            f.write('    "backend": ' + _observability_json_str(backend))
        if python_libpython is not None:
            f.write(",\n")
            f.write(
                '    "python_libpython": ' + _observability_json_str(python_libpython)
            )
        if ir_scaffold is not None:
            f.write(",\n")
            f.write('    "ir_scaffold": ' + _observability_json_str(ir_scaffold))
        if output_path is not None:
            f.write(",\n")
            f.write('    "output_path": ' + _observability_json_str(output_path) + "\n")
        else:
            f.write("\n")
        f.write("  },\n")
        f.write('  "counters": {')
        if counters:
            f.write("\n")
            first_counter = True
            for key in counters:
                if not first_counter:
                    f.write(",\n")
                first_counter = False
                f.write(
                    "    "
                    + _observability_json_str(key)
                    + ": "
                    + _json_float(counters.get(key, 0))
                )
            f.write("\n  ")
        f.write("}")
        f.write(",\n")
        f.write('  "phase_totals_ms": {')
        if phase_totals:
            f.write("\n")
            first_total = True
            for key in phase_totals:
                if not first_total:
                    f.write(",\n")
                first_total = False
                f.write(
                    "    "
                    + _observability_json_str(key)
                    + ": "
                    + _json_int(phase_totals.get(key, 0))
                )
            f.write("\n  ")
        f.write("}")
        f.write(",\n")
        f.write('  "phase_totals_s": {')
        if phase_totals:
            f.write("\n")
            first_total = True
            for key in phase_totals:
                if not first_total:
                    f.write(",\n")
                first_total = False
                f.write(
                    "    "
                    + _observability_json_str(key)
                    + ": "
                    + _json_seconds_from_ms(phase_totals.get(key, 0))
                )
            f.write("\n  ")
        f.write("}")
        f.write(",\n")
        f.write('  "events": [')
        if events:
            f.write("\n")
            i = 0
            while i < len(events):
                event = events[i]
                if i > 0:
                    f.write(",\n")
                name = ""
                ms = 0
                detail = None
                if event:
                    name = event.get("name", "")
                    ms = event.get("ms", 0)
                    detail = event.get("detail", None)
                f.write("    {\n")
                f.write('      "name": ' + _observability_json_str(name) + ",\n")
                f.write('      "ms": ' + _json_int(ms) + ",\n")
                f.write('      "s": ' + _json_seconds_from_ms(ms))
                if detail is not None:
                    f.write(",\n")
                    f.write('      "detail": ' + _observability_json_str(detail) + "\n")
                else:
                    f.write("\n")
                f.write("    }")
                i += 1
            f.write("\n  ")
        f.write("]")
        f.write(",\n")
        f.write('  "top_events": [')
        if events:
            f.write("\n")
            i = 0
            while i < len(events):
                event = events[i]
                if i > 0:
                    f.write(",\n")
                name = ""
                ms = 0
                detail = None
                if event:
                    name = event.get("name", "")
                    ms = event.get("ms", 0)
                    detail = event.get("detail", None)
                f.write("    {\n")
                f.write('      "name": ' + _observability_json_str(name) + ",\n")
                f.write('      "ms": ' + _json_int(ms) + ",\n")
                f.write('      "s": ' + _json_seconds_from_ms(ms))
                if detail is not None:
                    f.write(",\n")
                    f.write('      "detail": ' + _observability_json_str(detail) + "\n")
                else:
                    f.write("\n")
                f.write("    }")
                i += 1
            f.write("\n  ")
        f.write("]")
        f.write("\n")
        f.write("}\n")


def _observed_compile_python(
    compile_input,
    compile_output,
    *,
    options,
    metadata,
    verbose,
    emit_llvm_only,
    libpython_mode,
    ir_scaffold_mode,
    backend,
    python_library,
):
    """Bootstrap-specialized observed compile path.

    This intentionally calls ``_compile_python`` directly instead of
    passing it as a first-class callable through ``observed_compile``.
    The self-host path does not yet have a native ``callable(*args,
    **kwargs)`` ABI, and the bootstrap CLI has a fixed compile shape.
    """
    if str(os.environ.get("PCC_DEBUG_NO_CATCH", "")).strip():
        # Let the exception escape.  Under pcc1 a CAUGHT exception carries
        # neither __cause__ nor __traceback__ (both verified: MISSING and
        # <unavailable>), so every diagnostic built from a caught exception is
        # empty by construction -- which is why seven rounds of message fixes
        # recovered nothing.  An UNCAUGHT exception, by contrast, gets a full
        # traceback printed by the runtime itself.  So the only way to see the
        # stack is to not catch it.
        _compile_python(
            compile_input,
            compile_output,
            verbose=verbose,
            emit_llvm_only=emit_llvm_only,
            libpython_mode=libpython_mode,
            ir_scaffold_mode=ir_scaffold_mode,
            backend=backend,
            python_library=python_library,
            profile=metadata,
        )
        return None
    try:
        _compile_python(
            compile_input,
            compile_output,
            verbose=verbose,
            emit_llvm_only=emit_llvm_only,
            libpython_mode=libpython_mode,
            ir_scaffold_mode=ir_scaffold_mode,
            backend=backend,
            python_library=python_library,
            profile=metadata,
        )
        return None
    except Exception as exc:
        if str(
            os.environ.get("PCC_DEBUG_BOOTSTRAP_TRACE", "") or ""
        ).strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        ):
            tb_lines = []
            tb = getattr(exc, "__traceback__", None)
            i = 0
            while tb is not None and i < 12:
                frame = getattr(tb, "tb_frame", None)
                if frame is not None:
                    code = getattr(frame, "f_code", None)
                    if code is not None:
                        tb_lines.append(
                            "tb["
                            + str(i)
                            + "]: "
                            + str(code.co_filename)
                            + ":"
                            + str(tb.tb_lineno)
                            + " "
                            + str(code.co_name)
                        )
                    else:
                        tb_lines.append("tb[" + str(i) + "]: <frame-missing>")
                tb = getattr(tb, "tb_next", None)
                i = i + 1
            _write_text(
                "debug: raw_exception_traceback="
                + ("; ".join(tb_lines) if tb_lines else "<unavailable>")
            )
            _write_text("debug: raw_exception_type=" + str(type(exc).__name__))
            # `.message` has not existed on exceptions since Python 2, so this
            # printed an empty string for every failure and hid the actual text.
            # str(exc) inline: `.message` has not existed on exceptions since
            # Python 2, so the old form printed an empty string for every
            # failure.  Do not call a helper from another module here -- an
            # undefined name in the *error reporting* path raises inside the
            # handler and hides the very failure being reported, which is what
            # happened when this line called safe_exception_text without an
            # import.
            reported_message = ""
            try:
                reported_message = str(exc)
            except Exception:
                reported_message = "<unprintable>"
            _write_text("debug: raw_exception_message=" + reported_message)
            try:
                _write_text(
                    "debug: raw_exception_hint=" + repr(getattr(exc, "hint", None))
                )
            except Exception:
                pass
            _write_text("debug: raw_exception_args=" + repr(getattr(exc, "args", None)))
        try:
            formatted = _format_compile_error(
                exc,
                options=options,
                metadata=metadata or {},
            )
        except Exception:
            formatted = (
                "error: PCC-PY-COMPILE-001: ["
                + options.phase
                + "] compile failed\n"
                + "  note: diagnostic formatter failed"
            )
        try:
            _write_text(formatted, err=True)
        except Exception:
            sys.stderr.write("error: PCC-PY-COMPILE-001: compile failed\n")
        return "error"
    finally:
        if options.profile_json:
            _write_profile_json(options.profile_json, options, metadata or {})


def _parse_python_libpython(value):
    lowered = (value or "").strip().lower()
    if lowered not in PYTHON_LIBPYTHON_CHOICES:
        raise ValueError(
            "invalid --python-libpython " f"{value!r}; expected auto, on, or off"
        )
    return lowered


def _parse_ir_scaffold(value):
    lowered = (value or "").strip().lower()
    if lowered not in IR_SCAFFOLD_CHOICES:
        raise ValueError(
            "invalid --ir-scaffold " f"{value!r}; expected off, on, or auto"
        )
    return lowered


def _resolve_pcc1_python_modes(python_libpython, ir_scaffold, backend):
    libpython_value = python_libpython
    if libpython_value is None:
        libpython_value = os.environ.get("PCC_PYTHON_LIBPYTHON", "")
    if not str(libpython_value or "").strip():
        libpython_value = "off"
    resolved_libpython = _parse_python_libpython(libpython_value)

    scaffold_value = ir_scaffold
    if scaffold_value is None:
        scaffold_value = os.environ.get("PCC_IR_SCAFFOLD", "")
    if not str(scaffold_value or "").strip():
        scaffold_value = "on"
    resolved_scaffold = _parse_ir_scaffold(scaffold_value)
    if resolved_scaffold == "auto":
        resolved_scaffold = "on"

    backend_value = backend
    if backend_value is None:
        backend_value = os.environ.get("PCC_BACKEND", "")
    resolved_backend = str(backend_value or "").strip().lower()
    if not resolved_backend:
        resolved_backend = DEFAULT_PUBLIC_BACKEND
    if resolved_backend not in BACKEND_CHOICES:
        raise ValueError(
            "invalid --backend "
            + repr(backend_value)
            + "; expected llvm, llvm_capi, or self"
        )
    return resolved_libpython, resolved_scaffold, resolved_backend


def _bootstrap_compile_metadata(
    *, emit_llvm, output_path, backend, python_libpython, ir_scaffold
):
    return {
        "emit_llvm": bool(emit_llvm),
        "output_path": output_path,
        "backend": backend,
        "python_libpython": python_libpython,
        "ir_scaffold": ir_scaffold,
    }


def _should_consume_emit_llvm_value(argv, index, path):
    next_index = index + 1
    if next_index >= len(argv):
        return False
    candidate = argv[next_index]
    if candidate == "--" or candidate.startswith("-"):
        return False
    if candidate.endswith(".ll") or candidate.endswith(".bc"):
        return True
    if path is not None:
        return True
    if next_index + 1 < len(argv):
        return True
    return False


def parse_bootstrap_cli_args(argv=None):
    if argv is None:
        argv = _normalized_sys_argv()
    else:
        argv = _copy_seq(argv)

    normalized_argv = []
    i = 0
    while i < len(argv):
        normalized_argv.append((argv[i] or "") + "")
        i += 1
    argv = normalized_argv

    if len(argv) == 1:
        arg0 = argv[0]
        if arg0 == "-h" or arg0 == "--help":
            return None, 0, None

    path = None
    output_path = None
    emit_llvm = None
    verbose = False
    python_libpython = None
    python_library = False
    ir_scaffold = None
    backend = None
    diagnostic_format = "text"
    profile_json = None
    explain_fallback = False
    program_args = []

    i = 0
    while i < len(argv):
        arg = argv[i]
        if path is not None and arg == "--":
            i += 1
            while i < len(argv):
                program_args.append(argv[i])
                i += 1
            break
        if arg in ("-h", "--help"):
            return None, 0, None
        if arg in ("-v", "--verbose"):
            verbose = True
            i += 1
            continue
        if arg.startswith("--diagnostic-format="):
            diagnostic_format = _option_value(arg) or "text"
            i += 1
            continue
        if arg == "--diagnostic-format":
            if i + 1 >= len(argv):
                return None, 2, "--diagnostic-format requires a value"
            diagnostic_format = argv[i + 1] or "text"
            i += 2
            continue
        if arg.startswith("--profile-json="):
            profile_json = _option_value(arg)
            i += 1
            continue
        if arg == "--profile-json":
            if i + 1 >= len(argv):
                return None, 2, "--profile-json requires a value"
            profile_json = argv[i + 1]
            i += 2
            continue
        if arg == "--explain-fallback":
            explain_fallback = True
            i += 1
            continue
        if arg == "--python-library":
            python_library = True
            i += 1
            continue
        if arg == "-g" or arg == "--debug":
            # Read by ``debug_info_lowering``; pcc1 is where the
            # instrument-and-rebuild loop hurts most, so the bootstrap CLI
            # accepts the flag the host CLI does.
            os.environ["PCC_PY_DEBUG_INFO"] = "1"
            i += 1
            continue
        if arg.startswith("--pass="):
            i += 1
            continue
        if arg == "--pass":
            if i + 1 >= len(argv):
                return None, 2, "--pass requires a value"
            i += 2
            continue
        if arg.startswith("--disable-pass="):
            i += 1
            continue
        if arg == "--disable-pass":
            if i + 1 >= len(argv):
                return None, 2, "--disable-pass requires a value"
            i += 2
            continue
        if arg.startswith("--backend="):
            backend = _option_value(arg)
            i += 1
            continue
        if arg == "--backend":
            if i + 1 >= len(argv):
                return None, 2, "--backend requires a value"
            backend = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--python-libpython="):
            try:
                python_libpython = _parse_python_libpython(_option_value(arg))
            except ValueError as exc:
                return None, 2, str(exc)
            i += 1
            continue
        if arg == "--python-libpython":
            if i + 1 >= len(argv):
                return None, 2, "--python-libpython requires a value"
            try:
                python_libpython = _parse_python_libpython(argv[i + 1])
            except ValueError as exc:
                return None, 2, str(exc)
            i += 2
            continue
        if arg.startswith("--ir-scaffold="):
            try:
                ir_scaffold = _parse_ir_scaffold(_option_value(arg))
            except ValueError as exc:
                return None, 2, str(exc)
            i += 1
            continue
        if arg == "--ir-scaffold":
            if i + 1 >= len(argv):
                return None, 2, "--ir-scaffold requires a value"
            try:
                ir_scaffold = _parse_ir_scaffold(argv[i + 1])
            except ValueError as exc:
                return None, 2, str(exc)
            i += 2
            continue
        if arg.startswith("--emit-llvm="):
            emit_llvm = _option_value(arg) or _DEFAULT_EMIT_LL
            i += 1
            continue
        if arg == "--emit-llvm":
            if _should_consume_emit_llvm_value(argv, i, path):
                emit_llvm = argv[i + 1]
                i += 2
            else:
                emit_llvm = _DEFAULT_EMIT_LL
                i += 1
            continue
        if arg.startswith("-o") and arg != "-o":
            output_path = arg[2:]
            i += 1
            continue
        if arg == "-o":
            if i + 1 >= len(argv):
                return None, 2, "-o requires a value"
            output_path = argv[i + 1]
            i += 2
            continue
        if arg == "--":
            if i + 1 >= len(argv):
                return None, 2, "-- requires a Python input path"
            path = argv[i + 1]
            i += 2
            while i < len(argv):
                program_args.append(argv[i])
                i += 1
            break
        if arg.startswith("-"):
            if path is None:
                return None, 2, f"unknown option: {arg}"
            while i < len(argv):
                program_args.append(argv[i])
                i += 1
            break
        if path is None:
            path = arg
        else:
            while i < len(argv):
                program_args.append(argv[i])
                i += 1
            break
        i += 1

    import os as _os

    if _os.environ.get("PCC_DEBUG_BOOTSTRAP"):
        _write_text("[bootstrap] argv=" + repr(_normalized_sys_argv()) + "\n")

    if path is None:
        return None, 2, "missing required PATH"

    return (
        (
            path,
            output_path,
            emit_llvm,
            verbose,
            python_libpython,
            python_library,
            ir_scaffold,
            backend,
            program_args,
            diagnostic_format,
            profile_json,
            explain_fallback,
        ),
        0,
        None,
    )


def _bootstrap_cli_main_impl(
    argv=None, _logical_argv0=None, _execution_mode="script"
) -> int:
    if argv is None:
        raw_argv = _normalized_sys_argv()
    else:
        raw_argv = _copy_seq(argv)
    if len(raw_argv) == 1 and raw_argv[0] == "--tooling-capabilities":
        _write_text(python_tooling_capabilities_json())
        return 0
    # Core tool modules use the ordinary native module compiler/runner. Keep
    # their implementation outside the bootstrap closure and never route C
    # header arguments through the legacy host-C delegation branch below.
    if len(raw_argv) > 0 and raw_argv[0] == "inspect":
        return _run_compiled_python_module_from_pcc1(
            "pcc.artifact_inspect", raw_argv[1:]
        )
    if len(raw_argv) > 0 and raw_argv[0] == "bindgen":
        return _run_compiled_python_module_from_pcc1(
            "pcc.bindgen", raw_argv[1:]
        )
    if len(raw_argv) == 0:
        _write_text(
            "Error: PCC-CPY-UNSUPPORTED-L3-TOOLING-INTERACTIVE-REPL: "
            "interactive REPL execution is not yet owned by pcc1; "
            "use `pcc1 -` for stdin source",
            err=True,
        )
        return 2
    if raw_argv[0] == "-c":
        if len(raw_argv) < 2:
            _write_text("Error: -c requires a command", err=True)
            return 2
        applied_defaults = apply_locked_environment_resource_defaults()
        restarted = _restart_with_locked_environment_defaults(
            raw_argv,
            applied_defaults,
        )
        if restarted is not None:
            return restarted
        return _run_inline_python_request("command", raw_argv[1], raw_argv[2:])
    if raw_argv[0] == "-":
        applied_defaults = apply_locked_environment_resource_defaults()
        restarted = _restart_with_locked_environment_defaults(
            raw_argv,
            applied_defaults,
        )
        if restarted is not None:
            return restarted
        return _run_inline_python_request(
            "stdin", _read_python_stdin_source(), raw_argv[1:]
        )
    if len(raw_argv) > 0 and raw_argv[0] == "env":
        if len(raw_argv) not in (2, 3) or raw_argv[1] != "info":
            _write_text("Error: expected `pcc1 env info [--json]`", err=True)
            return 2
        if len(raw_argv) == 3 and raw_argv[2] != "--json":
            _write_text("Error: unknown env info option: " + str(raw_argv[2]), err=True)
            return 2
        if len(raw_argv) == 3:
            _write_text(environment_info_json(), nl=False)
        else:
            _write_text(environment_info_text(), nl=False)
        return 0
    if len(raw_argv) > 0 and raw_argv[0] == "--pcc-python-multi-codegen-worker":
        if len(raw_argv) != 2:
            _write_text(
                "Error: --pcc-python-multi-codegen-worker requires a manifest path",
                err=True,
            )
            return 2
        return _run_python_multi_codegen_worker(raw_argv[1])
    if len(raw_argv) > 0 and raw_argv[0] == "--pcc-self-backend-emit-worker":
        if len(raw_argv) not in (3, 5):
            _write_text(
                "Error: --pcc-self-backend-emit-worker requires IR/result paths and optional object/compiler paths",
                err=True,
            )
            return 2
        if len(raw_argv) == 5:
            return _run_self_backend_emit_worker(
                raw_argv[1], raw_argv[2], raw_argv[3], raw_argv[4]
            )
        return _run_self_backend_emit_worker(raw_argv[1], raw_argv[2])
    if (
        len(raw_argv) > 0
        and raw_argv[0] == "--pcc-self-backend-indexed-emit-worker"
    ):
        if len(raw_argv) != 4:
            _write_text(
                "Error: --pcc-self-backend-indexed-emit-worker requires sidecar, output and ASM/PCO kind",
                err=True,
            )
            return 2
        return _run_self_backend_indexed_emit_worker(
            raw_argv[1], raw_argv[2], raw_argv[3]
        )
    if len(raw_argv) > 0 and raw_argv[0] == "--pcc-self-backend-emit-batch-worker":
        if len(raw_argv) != 2:
            _write_text(
                "Error: --pcc-self-backend-emit-batch-worker requires a manifest path",
                err=True,
            )
            return 2
        return _run_self_backend_emit_batch_worker(raw_argv[1])
    if len(raw_argv) > 0 and raw_argv[0] == "--pcc-self-backend-split-worker":
        if len(raw_argv) != 6:
            _write_text(
                "Error: --pcc-self-backend-split-worker requires IR/result/output-prefix/export-prefix/shard-bytes arguments",
                err=True,
            )
            return 2
        return _run_self_backend_split_worker(
            raw_argv[1],
            raw_argv[2],
            raw_argv[3],
            raw_argv[4],
            raw_argv[5],
        )
    if _is_pytest_request(raw_argv):
        return _run_pytest_from_pcc1(raw_argv)
    module_is_request, module_mode, module_argv = _module_request_libpython_mode(
        raw_argv
    )
    if module_is_request:
        applied_defaults = apply_locked_environment_resource_defaults()
        restarted = _restart_with_locked_environment_defaults(
            raw_argv,
            applied_defaults,
        )
        if restarted is not None:
            return restarted
        return _run_python_module_from_pcc1_with_mode(module_argv, module_mode)
    if _should_delegate_to_host_cli(raw_argv) or _requires_full_compile_cli(raw_argv):
        return _run_c_cli(raw_argv)

    parsed, status, err = parse_bootstrap_cli_args(argv)
    if parsed is None:
        if err is None:
            _write_text(_HELP_TEXT, nl=False)
        else:
            _write_text("Error: " + str(err), err=True)
            _write_text(_HELP_TEXT, err=True, nl=False)
        return status

    (
        path,
        output_path,
        emit_llvm,
        verbose,
        python_libpython,
        python_library,
        ir_scaffold,
        backend,
        program_args,
        diagnostic_format,
        profile_json,
        explain_fallback,
    ) = parsed
    path = (path or "") + ""
    output_path = None if output_path is None else (output_path or "") + ""
    emit_llvm = None if emit_llvm is None else (emit_llvm or "") + ""
    verbose = True if verbose else False
    python_libpython = (
        None if python_libpython is None else (python_libpython or "") + ""
    )
    python_library = True if python_library else False
    ir_scaffold = None if ir_scaffold is None else (ir_scaffold or "") + ""
    backend = None if backend is None else (backend or "") + ""
    logical_argv0 = path if _logical_argv0 is None else str(_logical_argv0)

    try:
        python_libpython, ir_scaffold, backend = _resolve_pcc1_python_modes(
            python_libpython,
            ir_scaffold,
            backend,
        )
    except ValueError as exc:
        _write_text("Error: " + str(exc), err=True)
        return 2

    try:
        observability = ObservabilityOptions(
            diagnostic_format=diagnostic_format,
            profile_json=profile_json,
            explain_fallback=explain_fallback,
            phase="python-frontend",
            entry="cli_bootstrap",
        )
    except ValueError as exc:
        _write_text("Error: " + str(exc), err=True)
        return 2

    if not path.endswith(".py"):
        _write_text(
            "Error: bootstrap entry only supports Python inputs; "
            "use the full `pcc` CLI for C inputs",
            err=True,
        )
        return 1

    if not os.path.exists(path):
        _write_text("Error: input file not found: " + path, err=True)
        return 1

    applied_defaults = apply_locked_environment_resource_defaults()
    restarted = _restart_with_locked_environment_defaults(
        raw_argv,
        applied_defaults,
    )
    if restarted is not None:
        return restarted
    _seed_package_site_for_python_entry(path)

    if output_path is None and emit_llvm is None:
        cache_path = _python_run_cache_path(
            path,
            python_libpython=python_libpython,
            python_library=python_library,
            ir_scaffold=ir_scaffold,
            backend=backend,
        )
        if cache_path is not None and os.path.isfile(cache_path):
            try:
                _bootstrap_subprocess_run(
                    _python_program_command(
                        cache_path, _execution_mode, logical_argv0, program_args
                    ),
                    check=True,
                )
                return 0
            except subprocess.CalledProcessError as exc:
                return exc.returncode
            except Exception:
                _write_text("Error: pcc1 cached program run failed", err=True)
                return 1
        if cache_path is not None:
            try:
                _bootstrap_subprocess_run(
                    ["mkdir", "-p", os.path.dirname(cache_path)], check=True
                )
            except Exception:
                cache_path = None
        if cache_path is not None:
            exe_path = cache_path + ".tmp." + str(os.getpid())
            formatted_error = _observed_compile_python(
                path,
                exe_path,
                options=observability,
                metadata=_bootstrap_compile_metadata(
                    emit_llvm=False,
                    output_path=exe_path,
                    backend=backend,
                    python_libpython=python_libpython,
                    ir_scaffold=ir_scaffold,
                ),
                verbose=verbose,
                emit_llvm_only=False,
                libpython_mode=python_libpython,
                ir_scaffold_mode=ir_scaffold,
                backend=backend,
                python_library=python_library,
            )
            if formatted_error is not None:
                return 1
            try:
                _bootstrap_subprocess_run(
                    ["mv", "-f", exe_path, cache_path], check=True
                )
            except Exception:
                cache_path = exe_path
            try:
                _bootstrap_subprocess_run(
                    _python_program_command(
                        cache_path, _execution_mode, logical_argv0, program_args
                    ),
                    check=True,
                )
                return 0
            except subprocess.CalledProcessError as exc:
                return exc.returncode
            except Exception:
                _write_text("Error: pcc1 compiled program run failed", err=True)
                return 1

        td = _make_bootstrap_run_tempdir("pcc1_py_run_")
        try:
            exe_path = os.path.join(td, os.path.basename(path)[:-3] or "pcc1_run")
            formatted_error = _observed_compile_python(
                path,
                exe_path,
                options=observability,
                metadata=_bootstrap_compile_metadata(
                    emit_llvm=False,
                    output_path=exe_path,
                    backend=backend,
                    python_libpython=python_libpython,
                    ir_scaffold=ir_scaffold,
                ),
                verbose=verbose,
                emit_llvm_only=False,
                libpython_mode=python_libpython,
                ir_scaffold_mode=ir_scaffold,
                backend=backend,
                python_library=python_library,
            )
            if formatted_error is not None:
                return 1
            try:
                _bootstrap_subprocess_run(
                    _python_program_command(
                        exe_path, _execution_mode, logical_argv0, program_args
                    ),
                    check=True,
                )
                return 0
            except subprocess.CalledProcessError as exc:
                return exc.returncode
            except Exception:
                _write_text("Error: pcc1 compiled program run failed", err=True)
                return 1
        finally:
            _remove_bootstrap_run_tempdir(td)

    if emit_llvm is not None:
        if emit_llvm == _DEFAULT_EMIT_LL:
            ll_out = output_path if output_path else path[:-3] + ".ll"
        else:
            ll_out = output_path if output_path else emit_llvm
        formatted_error = _observed_compile_python(
            path,
            ll_out,
            options=observability,
            metadata=_bootstrap_compile_metadata(
                emit_llvm=True,
                output_path=ll_out,
                backend=backend,
                python_libpython=python_libpython,
                ir_scaffold=ir_scaffold,
            ),
            verbose=verbose,
            emit_llvm_only=True,
            libpython_mode=python_libpython,
            ir_scaffold_mode=ir_scaffold,
            backend=backend,
            python_library=python_library,
        )
        if formatted_error is not None:
            return 1
    else:
        formatted_error = _observed_compile_python(
            path,
            output_path,
            options=observability,
            metadata=_bootstrap_compile_metadata(
                emit_llvm=False,
                output_path=output_path,
                backend=backend,
                python_libpython=python_libpython,
                ir_scaffold=ir_scaffold,
            ),
            verbose=verbose,
            emit_llvm_only=False,
            libpython_mode=python_libpython,
            ir_scaffold_mode=ir_scaffold,
            backend=backend,
            python_library=python_library,
        )
        if formatted_error is not None:
            return 1
    return 0


def bootstrap_cli_main(argv=None) -> int:
    """Run one bootstrap request without leaking temporary build budgets."""
    frontend_jobs = str(os.environ.get("PCC_PY_FRONTEND_JOBS") or "")
    self_backend_jobs = str(os.environ.get("PCC_SELF_BACKEND_JOBS") or "")
    try:
        return _bootstrap_cli_main_impl(argv)
    finally:
        # Native pcc1 restarts after the defaults are published, so the child
        # still receives them in its initial environment.  Restoring here only
        # affects a long-lived embedding process (including a pytest worker).
        os.environ["PCC_PY_FRONTEND_JOBS"] = frontend_jobs
        os.environ["PCC_SELF_BACKEND_JOBS"] = self_backend_jobs


def bootstrap_cli_sys_argv_exit() -> None:
    code = bootstrap_cli_main()
    if code != 0:
        from sys import exit as _exit

        _exit(code)
